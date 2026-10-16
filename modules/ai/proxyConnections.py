"""AI provider "proxy": Gemini through the local Antigravity proxy (2026-10-17, plan section E).

The proxy listens on 127.0.0.1:8080 and speaks the Anthropic Messages API (POST /v1/messages); it is the route the Naukri bot already uses for its
answers. The model answers ONLY from the candidate's private knowledge file and resume lines (modules/knowledge_answers.py); a question those do not
answer comes back as None, and the bot then does what it did before this provider existed (types its years-of-experience figure, and lists the
question as randomly answered). Nothing here raises into the bot: every failure returns None.

Safety: a question about contact details, IDs, a password, age or date of birth, gender, religion, caste, marital status, disability, veteran status,
consent or other self-identification never reaches the model; a run that meets 3 failed questions in a row stops asking (the proxy is down, hung or
out of quota) and checks again every 5 minutes with one tiny real request; a run never spends more than RUN_BUDGET_S seconds waiting for the
model; a question waits at most about 30 s; every question the model was asked is written to logs/ai_answers_india.jsonl with its answer, so the
answers can be audited.
"""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime

from modules.knowledge_answers import grounding_text

PROXY_HOST, PROXY_PORT = "127.0.0.1", 8080
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}/v1/messages"
MODELS = ("gemini-2.5-flash", "gemini-3.8-flash-tiered")   # the fast one first; the proxy refused gemini-3.7-flash-tiered ("Invalid model") on 2026-10-17
CALL_TIMEOUT_S = 15
QUESTION_BUDGET_S = 15            # no new model attempt once a question has used this long: one question waits 30 s at the very most
PROBE_TIMEOUT_S = 15              # the tiny request that decides whether the model may be asked again (one 8 s probe failed and then answered in 1.2 s)
MAX_FAILS_IN_A_ROW = 3
REPROBE_SECONDS = 300
RUN_BUDGET_S = 1800               # seconds a bot run may spend waiting for the model in total (healthy calls take 1-5 s); past it the model is not asked again in that run
TEXT_MAX_CHARS = 120
TEXTAREA_MAX_CHARS = 350
JOB_CONTEXT_CHARS = 1500
AUDIT_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "ai_answers_india.jsonl")

SYSTEM = ("You fill in a real job application on a real candidate's behalf; what you write is sent to an employer. Use ONLY the candidate "
          "information you are given. Never invent an employer, project, tool, certificate, number or date. If the information does not answer "
          "the question, reply with exactly the single word UNKNOWN. Never give a phone number, e-mail address, ID number, password, date of "
          "birth or gender.")

_NEVER_ASK = re.compile(r"\b(phone|mobile|whatsapp|contact (number|no)|e-?mail|aadhaa?r|passport|pan card|pan number|date of birth|birth ?date|dob|"
                        r"age|gender|sex|password|otp|social security|bank account|card number|cvv|consent|sms|religio\w*|caste|ethnic\w*|race|"
                        r"marital|pronouns?|sexual orientation|veteran|disabilit\w*|self[- ]identif\w*)\b", re.I)
# a count of years, months or things: typed as a whole number; a score (CGPA, percentage, a rating out of 10): typed as given, up to 2 decimals
_COUNT = re.compile(r"\b(how many|number of|years?|months?)\b", re.I)
_SCORE = re.compile(r"\b(c?gpa|percentage|percent|rate yourself|rate your|out of|scale|score|rating)\b|%|/\s?10\b", re.I)
_UNKNOWN = re.compile(r"^\W*(unknown|insufficient[_ ]data|n/?a|not (stated|available|mentioned|provided))\b", re.I)


def _audit(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **row}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _listening() -> bool:
    try:
        socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=1.5).close()
        return True
    except OSError:
        return False


def proxy_create_client() -> dict:
    """The 'client': a small state dict. Never raises, and never blocks the bot's start; availability is checked here once and again later."""
    up = _listening()
    return {"fails": 0, "off": not up, "probe_at": time.time() + REPROBE_SECONDS, "asked": 0}


def _available(client: dict) -> bool:
    """True when the model may be asked. After failures the client is off; every REPROBE_SECONDS one tiny real request decides (a proxy that
    listens but hangs fails it, which a port check would not). A run that has spent RUN_BUDGET_S waiting for the model is off for good."""
    if client.get("spent", 0.0) >= RUN_BUDGET_S:
        if not client.get("budget_logged"):
            client["budget_logged"] = True
            _audit({"type": "budget", "answer": None, "why": f"this run has spent {client.get('spent', 0.0):.0f} s waiting for the model: not asked again in this run"})
        client["off"], client["probe_at"] = True, float("inf")
        return False
    if not client.get("off"):
        return True
    if time.time() >= client.get("probe_at", 0):
        client["probe_at"] = time.time() + REPROBE_SECONDS
        t0 = time.time()
        err = ""
        try:
            ok = bool(_call("Reply with the single word OK.", MODELS[0], 8, timeout=PROBE_TIMEOUT_S))
        except Exception as e:
            ok, err = False, f"{type(e).__name__} {str(e)[:60]}"
        client["spent"] = client.get("spent", 0.0) + (time.time() - t0)
        _audit({"type": "probe", "answer": None, "why": "proxy answers again" if ok else f"proxy still not answering ({err or 'empty reply'}); next check in {REPROBE_SECONDS} s"})
        if ok:
            client.update(off=False, fails=0)
            return True
    return False


def _call(prompt: str, model: str, max_tokens: int, timeout: float = CALL_TIMEOUT_S) -> str:
    body = json.dumps({"model": model, "max_tokens": max_tokens, "system": SYSTEM,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(PROXY_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-api-key": "dummy", "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _ask(prompt: str, client: dict):
    """(reply, model, error): the first model that answers wins; error names the last failure when none did. The seconds spent are added to
    client["spent"] (RUN_BUDGET_S)."""
    err = ""
    started = time.time()
    try:
        for model in MODELS:
            if time.time() - started > QUESTION_BUDGET_S:
                err = err or "question budget spent"
                break
            try:
                raw = _call(prompt, model, 256 if model == MODELS[0] else 1024)
                if raw:
                    return raw, model, ""
                err = f"{model}: empty reply"
            except urllib.error.HTTPError as e:
                err = f"{model}: HTTP {e.code}"
            except Exception as e:
                err = f"{model}: {type(e).__name__} {str(e)[:60]}"
        return "", "", err
    finally:
        client["spent"] = client.get("spent", 0.0) + (time.time() - started)


def _failed(client: dict) -> None:
    client["fails"] = client.get("fails", 0) + 1
    if client["fails"] >= MAX_FAILS_IN_A_ROW:
        client["off"], client["probe_at"] = True, time.time() + REPROBE_SECONDS


def _clean(answer: str, question: str, question_type: str):
    """The text to type, or None when the model had no answer (UNKNOWN) or the answer does not fit the question."""
    a = (answer or "").strip().strip("\"'`").strip()
    if not a or _UNKNOWN.match(a):
        return None
    if question_type != "textarea":
        a = a.splitlines()[0].strip()
    else:
        a = " ".join(a.split())
    is_score, is_count = bool(_SCORE.search(question)), bool(_COUNT.search(question))
    if (is_score or is_count) and question_type != "textarea":      # a paragraph answer is words; only a single-line field is a number
        m = re.match(r"^\D{0,12}(\d+(?:\.\d+)?)", a)
        if not m:
            return None                      # the field wants a number and the model gave words: not an answer
        value = float(m.group(1))
        if is_score:
            return f"{value:.2f}".rstrip("0").rstrip(".")      # 8.34 stays 8.34
        return str(int(round(value)))
    limit = TEXTAREA_MAX_CHARS if question_type == "textarea" else TEXT_MAX_CHARS
    if len(a) > limit:
        a = a[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return a or None


def proxy_answer_question(client, question: str, options=None, question_type: str = "text", job_description: str = None,
                          about_company: str = None, user_information_all: str = None):
    """Same call shape as the other providers' answer functions. Returns the text to type, or None."""
    try:
        if not client or not question or _NEVER_ASK.search(question) or not _available(client):
            return None
        ground = grounding_text()
        if not ground:
            return None                      # nothing to ground an answer on: do not ask the model
        kind = "paragraph field" if question_type == "textarea" else "single-line text field"
        parts = [ground, f'Question: "{question.strip()}"', f"Answer type: {kind}.",
                 "Rules: for years, duration, a count or any number reply with a whole number only (no unit, no words); for a Yes/No question reply "
                 "Yes or No only; for a short description reply with one sentence in the first person under 120 characters; for a paragraph reply "
                 "in the first person under 350 characters with no greeting, sign-off or markdown. Reply with the answer only, no preamble. If the "
                 "candidate information above does not answer it, reply UNKNOWN."]
        if question_type == "textarea" and job_description and job_description != "Unknown":
            parts.insert(1, "The job (for context only):\n" + str(job_description)[:JOB_CONTEXT_CHARS])
        prompt = "\n\n".join(parts)
        client["asked"] = client.get("asked", 0) + 1
        raw, used, err = _ask(prompt, client)
        if not raw:
            _failed(client)
            _audit({"question": question[:300], "type": question_type, "answer": None, "why": f"no model answered ({err})"})
            return None
        client["fails"] = 0
        answer = _clean(raw, question, question_type)
        _audit({"question": question[:300], "type": question_type, "model": used, "raw": raw[:300], "answer": answer})
        return answer
    except Exception:
        return None


_PLACEHOLDER_OPTION = re.compile(r"^\s*(select an option|please select|choose( an option)?|--+.*|)\s*$", re.I)


def _match_option(raw: str, options: list):
    """The element of `options` the model named, or None. The reply must be one option's text (case and surrounding quotes aside); a reply that
    merely contains the text of exactly one option also counts. Anything else, UNKNOWN included, is None."""
    lines = (raw or "").strip().strip("\"'`").strip().splitlines()
    r = lines[0].strip() if lines else ""
    if not r or _UNKNOWN.match(r):
        return None
    low = r.casefold().rstrip(".")
    for o in options:
        if o.strip().casefold() == low:
            return o
    hits = [o for o in options if o.strip() and re.search(r"(?<!\w)" + re.escape(o.strip().casefold()) + r"(?!\w)", low)]
    return hits[0] if len(hits) == 1 else None


def proxy_choose_option(client, question: str, options: list, job_description: str = None):
    """For a select or radio question the bot cannot map: the ONE option (an element of `options`) that is true for the candidate according to the
    knowledge file and resume, or None (the bot then does what it did before: a random option for a select, the first for a radio)."""
    try:
        if not client or not question or _NEVER_ASK.search(question) or not _available(client):
            return None
        opts = [str(o).strip() for o in (options or []) if str(o).strip() and not _PLACEHOLDER_OPTION.match(str(o))]
        if len(opts) < 2 or len(opts) > 40:
            return None                      # nothing to choose between, or a list (phone country codes) that is not a question about him
        memo = client.setdefault("memo", {})
        key = (question.strip().casefold(), tuple(o.casefold() for o in opts))
        if key in memo:
            return memo[key]
        ground = grounding_text()
        if not ground:
            return None
        parts = [ground, f'Question: "{question.strip()}"', "Options:\n" + "\n".join(f"- {o}" for o in opts),
                 "Reply with the exact text of the ONE option that is true for the candidate, nothing else. Choose an option only if the candidate "
                 "information above settles it; a number of years falls in the range that contains the candidate's years. If it does not settle "
                 "it, reply UNKNOWN."]
        if job_description and job_description != "Unknown":
            parts.insert(1, "The job (for context only):\n" + str(job_description)[:JOB_CONTEXT_CHARS])
        prompt = "\n\n".join(parts)
        client["asked"] = client.get("asked", 0) + 1
        raw, used, err = _ask(prompt, client)
        if not raw:
            _failed(client)
            _audit({"question": question[:300], "type": "option", "options": opts[:12], "answer": None, "why": f"no model answered ({err})"})
            return None
        client["fails"] = 0
        pick = _match_option(raw, opts)
        memo[key] = pick
        _audit({"question": question[:300], "type": "option", "options": opts[:12], "model": used, "raw": raw[:200], "answer": pick})
        return pick
    except Exception:
        return None


def proxy_extract_skills(client, description) -> str:
    """The skills column of the history file. No call: it would only spend quota; the column keeps the value it has without an AI."""
    return "Needs an AI"
