"""LinkedIn UAE pass: an overlay that runs inside run_linkedin_local.py's own browser, before the India pass.

Inert unless logs/LINKEDIN_UAE_ARMED exists and is valid (JSON, written by the user's go-ahead, never by the bot):
    {"mode": "inventory" | "live", "until": "YYYY-MM-DD", "quota": 12, "max_visits": 45}
  inventory  walks UAE Easy Apply jobs, answers every question from the closed-world facts file, and DISCARDS at the
             Submit step. Nothing is applied. Proposed (unconfirmed) facts may be used and are flagged.
  live       the same, but a job is submitted only when every question was answered from known/decided facts, up to
             the daily quota. A proposed fact makes the question unanswerable.
A question no fact answers exactly (portal_kit/facts.py) blocks the job: it is discarded, never guessed, and the
question is written verbatim to logs/linkedin_uae_unanswered.jsonl. Every job the pass looks at is one row of
logs/linkedin_uae_jobs.jsonl; every pass is one row of logs/linkedin_region_cycles.jsonl (the quota counts from it).

Region (U1, 2026-10-14): the pass's searches carry the location in the URL (runAiBot.SEARCH_URL_EXTRA) besides the filter
dialog's location box, and the URL LinkedIn shows for each term's results is logged and kept on the cycle row. A term whose
first results page shows at least WRONG_REGION_CARDS cards and none in the UAE stops at once with an outcome 'wrong_region'
row in the job log; WRONG_REGION_TERMS such terms end the pass and write logs/LINKEDIN_UAE_WRONG_REGION_<date>.

Everything patched on the bot module `R` is restored in a finally block, so the India pass that follows runs the
unchanged bot. Facts live outside the repo (C:\\Users\\Admin\\.secrets\\gulf_facts.json).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from selenium.common.exceptions import (ElementClickInterceptedException, NoSuchElementException, StaleElementReferenceException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.select import Select

REPO = Path(__file__).resolve().parent
LOGS = REPO / "logs"
ARM_FILE = LOGS / "LINKEDIN_UAE_ARMED"
JOBS_LOG = LOGS / "linkedin_uae_jobs.jsonl"
LEDGER = LOGS / "linkedin_uae_unanswered.jsonl"
CYCLES_LOG = LOGS / "linkedin_region_cycles.jsonl"
AI_LOG = LOGS / "ai_answers.jsonl"
# written when live submissions could not be verified on LinkedIn twice in one pass; the live pass stays off until a human has
# looked (delete the file to resume): a Submit click that LinkedIn does not show as applied is either a broken check or lost work
VERIFY_STOP = LOGS / "LINKEDIN_UAE_VERIFY_STOPPED"
SHOTS = LOGS / "uae-evidence"
FACTS_PKG = Path(r"C:\Users\Admin\GitHub\job-tracker\portal_kit")
# U1: appended to the pass's search URLs (runAiBot.SEARCH_URL_EXTRA, '' again for India). The filter dialog still types cfg.SEARCH_LOCATION
# too, as before; on its own it left 92% of the pass's skips Indian cards.
UAE_SEARCH_URL_EXTRA = "&location=United%20Arab%20Emirates"
# The wrong-region stop (U1). A term whose first results page shows at least WRONG_REGION_CARDS cards with a readable location, none of them
# in the UAE, did not get the UAE (LinkedIn fell back to another region): it stops at once and gets a wrong_region row. WRONG_REGION_TERMS
# such terms in one pass end the pass and write the day's marker, logs/LINKEDIN_UAE_WRONG_REGION_<date>. Nothing here ever deletes it.
WRONG_REGION_CARDS = 10
WRONG_REGION_TERMS = 3
WRONG_REGION_MARKER = "LINKEDIN_UAE_WRONG_REGION_"

_R = None
_note = print


def _jsonl(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        _note(f"uae: could not write {path.name}: {e!r}")


def _read_jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _armed() -> dict | None:
    try:
        arm = json.loads(ARM_FILE.read_text(encoding="utf-8-sig"))     # a BOM from a Windows editor must not silently disarm
    except FileNotFoundError:
        return None
    except Exception as e:
        _note(f"uae: arming file unreadable ({e!r}); India only")
        return None
    if arm.get("mode") not in ("inventory", "live"):
        return None
    try:
        until = date.fromisoformat(str(arm.get("until"))[:10])
    except Exception:
        _note("uae: arming file has no valid 'until' date; India only")
        return None
    if until < date.today():
        _note(f"uae: arming expired on {until}; India only")
        return None
    return arm


_CODE_BASE = ""


def _code_base() -> str:
    """Hash of the code this PROCESS loaded (the overlay, the bot, the runner, the bot's search settings), taken once in install(), before any later
    edit changes the files on disk: a hash taken from disk at every pass would stamp old code with a new revision."""
    h = hashlib.sha1()
    for p in (Path(__file__), REPO / "runAiBot.py", REPO / "run_linkedin_local.py", REPO / "config" / "search.py"):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def _code_rev() -> str:
    """A short hash that joins the facts revision: the code the process loaded (_code_base) plus config/region_uae.py read NOW, because that file
    (the title and location gates, the search terms, the bad words) is re-executed at every pass, so an edit takes effect without a restart and
    must re-open what the old gate skipped. A skip decided under older logic (the first real pass skipped 48 jobs as 'not in the UAE' because the
    bot's card parsing was broken) is looked at again after any such change."""
    h = hashlib.sha1((_CODE_BASE or _code_base()).encode())
    try:
        h.update((REPO / "config" / "region_uae.py").read_bytes())
    except OSError:
        h.update(b"?")
    return h.hexdigest()[:6]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _applied_today() -> int:
    """Live Submit clicks today, counted from the per-job rows (written the moment a job is submitted, so a crash cannot lose them).
    The quota is a ceiling on submissions: every click is charged, verified or not, so a check that fails (or LinkedIn being slow to show
    a real application) can never let the pass apply beyond the cap. Whether a submission COUNTS as an application (towards earning the
    dashboard back) is a separate question, answered only by `verified` rows."""
    today = date.today().isoformat()
    return len({str(r.get("job_id")) for r in _read_jsonl(JOBS_LOG)
                if r.get("outcome") == "applied" and r.get("mode") == "live" and str(r.get("ts", ""))[:10] == today})


def _unverified_today() -> int:
    """Live submissions today that LinkedIn did not show as applied and that no later check verified. Read from the log (not from pass
    state), so the breaker spans passes and restarts."""
    today = date.today().isoformat()
    rows = _read_jsonl(JOBS_LOG)
    failed = {str(r.get("job_id")) for r in rows
              if r.get("outcome") == "verify_failed" and r.get("mode") == "live" and str(r.get("applied_ts", ""))[:10] == today}
    verified = {str(r.get("job_id")) for r in rows if r.get("outcome") == "verified" and r.get("mode") == "live"}
    return len(failed - verified)


# ------------------------------------------------------------------ per-pass state
class Pass:
    def __init__(self, mode: str, arm: dict, cfg, facts, F):
        self.mode, self.arm, self.cfg, self.facts, self.F = mode, arm, cfg, facts, F
        self.job: dict | None = None
        self.visits = 0
        self.started = time.time()
        self.counts: Counter = Counter()
        self.quota_left = max(0, int(arm.get("quota", cfg.DAILY_QUOTA)) - (_applied_today() if mode == "live" else 0))
        # both modes are capped: a pass that grinds through blocked jobs must not delay the India pass for hours or trip the
        # supervisor's 180-minute zero-yield kill (which writes NEEDS_HUMAN and stops the bot for the day)
        self.max_visits = int(arm.get("max_visits", 45 if mode == "inventory" else 60))
        self.max_minutes = int(arm.get("max_minutes", 45 if mode == "inventory" else 60))
        self.handled = self._handled_ids()
        self.seen: set = set()                      # job ids already opened in this pass (a job can come up under several search terms)
        self.unverified = 0                         # live submissions this pass that LinkedIn did not show as applied
        self.ai = None                              # the AI answerer module, loaded by _uae_pass only when the arming file asks for it
        self.ai_memo: dict = {}                     # (job, question, kind, options) -> what the AI answerer said: the bot re-asks a page that does not advance
        self.ai_dead = False                        # the answerer's key or configuration was rejected in this pass
        self.loc_skips = 0                          # cards rejected by the location on the card itself
        self.cards_seen = 0                         # every card the pass looked at, including the ones it skipped without a row
        self.uae_cards = 0                          # cards whose own location was in the UAE
        self.placeholders = 0                       # cards still empty after scrolling to them
        self.term: dict | None = None               # the search term being worked: LinkedIn's URL for it, its first page's cards (U1)
        self.terms: list = []                       # the finished terms' records, for the cycle row
        self.wrong_terms: list = []                 # terms whose first results page showed no UAE card (each has a wrong_region row)
        self.stop: str | None = None                # set when the wrong-region stop ends the whole pass (over_budget reports it)
        self.paused_switch = 0                      # R.switch_number, held at 0 while a wrong-region term winds down; the next term gets it back
        # roles (company + title) already applied to live: LinkedIn reposts the same role under new job ids
        self.applied_keys = {_role_key(r.get("company"), r.get("title")) for r in _read_jsonl(JOBS_LOG)
                             if r.get("outcome") == "applied" and r.get("mode") == "live"}

    def _handled_ids(self) -> set:
        """Jobs not worth opening again: applied, or skipped under the same facts file and resolver. A job inventoried as answerable
        is handled for another inventory pass, but a live pass is exactly where it should be applied to."""
        done = set()
        for r in _read_jsonl(JOBS_LOG):
            o, same = r.get("outcome"), r.get("facts_rev") == self.facts.rev
            if o == "applied" or (same and o in ("skipped_unanswerable", "skipped_job", "held_ai")) or (same and o == "would_apply" and self.mode == "inventory"):
                done.add(str(r.get("job_id")))
        return done

    def ctx(self) -> dict:
        j = self.job or {}
        return {"region": "UAE", "channel": "linkedin", "title": j.get("title"), "company": j.get("company"), "location": j.get("location")}

    def over_budget(self) -> str | None:
        if self.stop:
            return self.stop                        # the wrong-region stop: no further terms or cards in this pass
        if self.mode == "live" and self.quota_left <= 0:
            return "daily quota"                    # the cap on submissions is a budget too: the remaining terms and cards need not even be loaded
        if self.visits >= self.max_visits:
            return f"visit budget of {self.max_visits}"
        if time.time() - self.started > self.max_minutes * 60:
            return f"time budget of {self.max_minutes} minutes"
        return None

    def begin(self, job_id, title, company, location, style, count: bool = True):
        self.finish("no_outcome", "the pass moved on without a result for this job")
        if count:
            self.visits += 1
        self.seen.add(str(job_id))
        self.job = {"ts": datetime.now().isoformat(timespec="seconds"), "mode": self.mode, "facts_rev": self.facts.rev, "job_id": str(job_id),
                    "title": title, "company": company, "location": location, "style": style, "qa": [], "blocked": None,
                    "uses_proposed": False, "done": False}
        why = self.over_budget()
        if why and _R.switch_number:
            _R.switch_number = 0                    # no more jobs: the loops in apply_to_jobs end on their own
            _note(f"uae: {why} reached")

    def finish(self, outcome: str, reason: str = ""):
        """Write the one result row for the open job (a job is written once)."""
        j = self.job
        if not j or j["done"]:
            return
        j["done"] = True
        j["really_applied"] = outcome == "applied"
        self.counts[outcome] += 1
        _jsonl(JOBS_LOG, {k: v for k, v in j.items() if k not in ("done", "really_applied", "awaiting_done", "first_page_seen")} | {"outcome": outcome, "reason": reason[:300]})
        if outcome in ("applied", "skipped_unanswerable", "skipped_job", "held_ai") or (outcome == "would_apply" and self.mode == "inventory"):
            self.handled.add(j["job_id"])
        if outcome == "applied":
            self.applied_keys.add(_role_key(j.get("company"), j.get("title")))
            self.quota_left -= 1
            if self.mode == "live" and self.quota_left <= 0:
                _R.switch_number = 0
                _note("uae: daily quota reached")

    def block(self, q_label: str, kind: str, exc) -> None:
        self.job["blocked"] = f"{q_label[:100]}: {exc.reason}"
        # a Selenium hiccup or an unselectable resume is not a question without a fact: it is retried later, never listed as one
        self.job["block_transient"] = exc.intent in ("fill", "resume")
        _jsonl(LEDGER, self.facts.ledger_row(q_label, kind, exc) | {"job_id": self.job["job_id"], "title": self.job["title"],
                                                                    "company": self.job["company"], "mode": self.mode,
                                                                    "ts": datetime.now().isoformat(timespec="seconds")})

    def screenshot(self, tag: str) -> None:
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            shots = sorted(SHOTS.glob("*.png"))
            if len(shots) >= 400:                   # keep the newest 300: evidence must not silently stop after a few passes
                for old in shots[:100]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            _R.driver.save_screenshot(str(SHOTS / f"{datetime.now():%Y%m%d_%H%M%S}-{tag}-{(self.job or {}).get('job_id', '')}.png"))
        except Exception:
            pass


S: Pass | None = None


# ------------------------------------------------------------------ verification: LinkedIn itself must show the job as applied
def _applied_marker(job_id: str) -> str:
    """Why LinkedIn now shows this job as applied, or "" when it does not (yet). Two signals the bot's own already-applied checks rely on
    (runAiBot.py): the job card's footer state, and the 'See application' link the detail pane shows once a job is applied."""
    d = _R.driver
    try:
        for el in d.find_elements(By.XPATH, f"//*[@data-occludable-job-id='{job_id}']//*[contains(@class,'job-card-container__footer-job-state')]"):
            if (el.text or "").strip() == "Applied":
                return "the job card's footer says Applied"
    except Exception:
        pass
    try:
        m = re.search(r"currentJobId=(\d+)", d.current_url or "")
        # the pane counts only when the URL names THIS job: a pane left on an older, applied neighbour must not verify this one
        if m and m.group(1) == job_id and d.find_elements(By.CLASS_NAME, "jobs-s-apply__application-link"):
            return "the job pane shows the 'See application' link"
    except Exception:
        pass
    return ""


def _dump_dom(job_id: str) -> None:
    """A check that found no Applied marker: keep the real DOM around the apply area and the job's card, so the selectors can be corrected from
    evidence instead of guessed. Only reads; never raises."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        parts = []
        for sel in (".jobs-s-apply", ".jobs-apply-button--top-card", ".job-details-jobs-unified-top-card__container--two-pane",
                    ".jobs-unified-top-card"):
            for el in _R.driver.find_elements(By.CSS_SELECTOR, sel)[:1]:
                parts.append(f"<!-- {sel} -->\n" + (el.get_attribute("outerHTML") or "")[:12000])
        for el in _R.driver.find_elements(By.XPATH, f"//*[@data-occludable-job-id='{job_id}']")[:1]:
            parts.append("<!-- the job's card -->\n" + (el.get_attribute("outerHTML") or "")[:8000])
        parts.append(f"<!-- url: {_R.driver.current_url} -->")
        (SHOTS / f"{datetime.now():%Y%m%d_%H%M%S}-not-verified-{job_id}.html").write_text("\n\n".join(parts), encoding="utf-8")
    except Exception:
        pass


def _render_card(card) -> None:
    """LinkedIn renders a result card only while it is near the viewport (the others are empty placeholders), and the bot scrolls each card into view as
    it works through the list. An early reject that never touches the next card leaves the cards after it empty: each then cost the runner's 8-20 s
    pacing wait and a failed lookup (the first live pass spent its first minutes that way). So every card is scrolled to, and given up to ~2 s to show
    its content, before anything is decided from it. Never raises."""
    try:
        _R.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
    except Exception:
        return
    for _ in range(10):
        try:
            if card.find_elements(By.TAG_NAME, "a"):
                return
        except Exception:
            return
        time.sleep(0.2)


def _role_key(company, title) -> str:
    """A role as company + title, case and punctuation folded: LinkedIn reposts the same role under new job ids (Halian and Jobgether did on day one)."""
    return re.sub(r"[^a-z0-9]+", " ", f"{company or ''} {title or ''}".lower()).strip()


def _card_fields(card) -> tuple:
    """(title, company, "Location (Style)") read from a job card's own elements the way the bot's get_job_main_details does since the card
    layout changed: the company alone in the subtitle and the location in a separate caption (the older "Company · Location" subtitle is read
    too). An empty string for whatever the card does not show. Never raises."""
    def text(cls: str) -> str:
        try:
            return card.find_element(By.CLASS_NAME, cls).text.strip()
        except Exception:
            return ""
    try:
        title = card.find_element(By.TAG_NAME, "a").text.split("\n")[0].strip()
    except Exception:
        title = ""
    sub = text("artdeco-entity-lockup__subtitle")
    if " · " in sub:
        company, _, loc = sub.partition(" · ")
    else:
        company, loc = sub, text("artdeco-entity-lockup__caption")
    return title, company.split("\n")[0].strip(), loc.strip()


def _in_uae(location: str) -> bool:
    """Whether a location names a place in the UAE (config UAE_LOCATION_WORDS, a substring test on the lower-cased text). The one rule for the
    card's own location, the bot's parse of it and the wrong-region count."""
    low = (location or "").lower()
    return any(w in low for w in S.cfg.UAE_LOCATION_WORDS)


def _title_problem(title: str) -> str:
    """Why this job title is not one the pass opens ("" when it is): it must name a data-engineering role (config TITLE_ANY) and none of the words
    that rule a job out (TITLE_NONE). A config without TITLE_ANY applies no title gate."""
    any_words = tuple(getattr(S.cfg, "TITLE_ANY", ()) or ())
    if not any_words or not title:
        return ""
    # NFKC folds the mathematical-bold letters some recruiters use into plain ones; hyphens, slashes, dots and non-breaking spaces become spaces
    t = re.sub(r"[\s\-_/. ]+", " ", unicodedata.normalize("NFKC", title).lower()).strip()

    def has(words) -> bool:
        return bool(words) and bool(re.search(r"\b(" + "|".join(re.escape(w) for w in words) + r")s?\b", t))

    if has(tuple(getattr(S.cfg, "TITLE_NONE", ()) or ())):
        return f"title says this is not the user's kind of role: {title}"
    if has(any_words) or any(re.search(p, t) for p in (getattr(S.cfg, "TITLE_PATTERNS", ()) or ())):
        return ""
    if has(tuple(getattr(S.cfg, "TITLE_TECH", ()) or ())) and has(tuple(getattr(S.cfg, "TITLE_ROLE", ()) or ())):
        return ""
    return f"title is not a data-engineering role: {title}"


def _dump_card(card, job_id: str) -> None:
    """The real HTML of a card the pass rejected by location (the first two of a pass). Only reads; never raises."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{datetime.now():%Y%m%d_%H%M%S}-card-{job_id}.html").write_text((card.get_attribute("outerHTML") or "")[:6000], encoding="utf-8")
    except Exception:
        pass


_calib = {"ok": 0, "bad": 0}


def _calibrate_card(card, job_id: str) -> None:
    """Read-only check of the verification's card lookup on the real page: where the bot's own lookup (on the card element) says Applied, the
    lookup used for verification (by job id, from the page root) must say it too. Logged for the first few such cards of a run."""
    if _calib["ok"] + _calib["bad"] >= 4:
        return
    try:
        if not any((e.text or "").strip() == "Applied" for e in card.find_elements(By.CLASS_NAME, "job-card-container__footer-job-state")):
            return
        mine = bool(_applied_marker(job_id))
        _calib["ok" if mine else "bad"] += 1
        _note(f"uae: calibration: card {job_id} says Applied; the verification lookup {'agrees' if mine else 'DISAGREES (selector needs a fix)'}")
    except Exception:
        pass


def _post_submit_evidence() -> None:
    """What LinkedIn shows right after the Submit click (its confirmation wording was never observed before the first live run): kept on the
    applied row and as a screenshot. Only reads; never raises."""
    try:
        text = ""
        for _ in range(12):                          # up to ~6 s: LinkedIn shows a loading dialog first (the first real submit's fixed 2 s missed the real one)
            time.sleep(0.5)
            modals = _R.driver.find_elements(By.CSS_SELECTOR, ".jobs-easy-apply-modal, .artdeco-modal")
            text = " ".join((modals[0].text or "").split())[:300] if modals else ""
            if "application sent" in text.lower():
                break
        S.job["post_submit"] = text or "(no modal on screen)"
        S.job["post_submit_sent"] = "application sent" in text.lower()      # LinkedIn's own confirmation: a second signal for the verification
        S.screenshot("post-submit")
    except Exception:
        pass


def _verify_after_submit(job: dict, applied_ts: str) -> None:
    """Right after a live Submit: look for LinkedIn's own Applied state (up to 15 s: the card and pane update a moment after the modal closes)
    and write a `verified` or `verify_failed` row joined to the applied row by job id. Only `verified` rows count towards earning the
    dashboard back; the quota is charged either way (it caps submissions), and two unverified submissions in a day switch the live pass
    off until a human has looked. Never raises."""
    how, err = "", ""
    try:
        for _ in range(5):
            how = _applied_marker(job["job_id"])
            if not how and job.get("post_submit_sent"):
                how = "LinkedIn's own dialog said the application was sent"
            if how:
                break
            time.sleep(3.0)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:100]}"
    base = {"ts": datetime.now().isoformat(timespec="seconds"), "mode": "live", "facts_rev": S.facts.rev, "job_id": job["job_id"],
            "title": job["title"], "company": job["company"], "applied_ts": applied_ts}
    if how:
        S.counts["verified"] += 1
        _jsonl(JOBS_LOG, base | {"outcome": "verified", "how": how})
        _note(f"uae: verified on LinkedIn: {job['title']} | {job['company']} ({how})")
        return
    S.counts["verify_failed"] += 1
    S.unverified += 1
    S.screenshot("not-verified")
    _dump_dom(job["job_id"])
    _jsonl(JOBS_LOG, base | {"outcome": "verify_failed", "reason": err or "no Applied marker on the job card or pane within 15 s"})
    today_n = _unverified_today()                   # counted from the log: it spans passes and restarts, not just this pass
    _note(f"uae: NOT verified on LinkedIn: {job['title']} | {job['company']} ({today_n} today)")
    if today_n >= 2:
        _R.switch_number = 0
        try:
            VERIFY_STOP.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} two live submissions today were not shown as applied by LinkedIn; "
                                   "the live UAE pass stays off until you have looked (delete this file to resume).\n", encoding="utf-8")
        except OSError:
            pass
        _note("uae: two submissions today could not be verified; live pass stopped and switched off (see LINKEDIN_UAE_VERIFY_STOPPED)")


def _record_late_application(job_id: str, how: str, why: str) -> None:
    """The bot's flow ended as "failed" or with the date still Pending, but LinkedIn itself already shows the job as applied: a Submit click
    that was dispatched went through. Record the truth (an applied row, which charges the quota, and a verified row joined to it) instead of
    a failure. The bot's own history CSV gets no row from this path. Never raises."""
    try:
        job = S.job
        S.finish("applied", f"{why}; LinkedIn shows the job as applied")
        _jsonl(JOBS_LOG, {"ts": datetime.now().isoformat(timespec="seconds"), "mode": "live", "facts_rev": S.facts.rev, "job_id": job_id,
                          "title": job["title"], "company": job["company"], "applied_ts": job["ts"], "outcome": "verified",
                          "how": how + " (found after the bot's flow had failed)"})
        S.counts["verified"] += 1
        _note(f"uae: recorded as applied and verified: {job['title']} | {job['company']} ({why})")
    except Exception as e:
        _note(f"uae: could not record a late application: {e!r}")


# ------------------------------------------------------------------ reading and filling the Easy Apply questions
def _hidden_or_text(el) -> str:
    try:
        hidden = el.find_elements(By.CLASS_NAME, "visually-hidden")
        if hidden and hidden[0].text.strip():
            return hidden[0].text.strip()
    except Exception:
        pass
    return (el.text or "").strip()


def _required(*els) -> bool:
    for e in els:
        try:
            if e.get_attribute("required") is not None or (e.get_attribute("aria-required") or "").lower() == "true":
                return True
        except Exception:
            pass
    return False


def _read_question(el) -> dict | None:
    sel_els = el.find_elements(By.XPATH, ".//select")
    if sel_els:
        label = "Unknown"
        try:
            lab = el.find_element(By.TAG_NAME, "label")
            spans = lab.find_elements(By.TAG_NAME, "span")
            label = (spans[0].text if spans and spans[0].text.strip() else lab.text).strip() or "Unknown"
        except Exception:
            pass
        s = Select(sel_els[0])
        # the phone-country-code list has about 250 entries and is never needed (the contact fields keep what LinkedIn pre-filled);
        # reading every option text costs seconds per job, so its options are read only if the field turns out to be empty
        lazy = "country code" in label.lower()
        return {"kind": "select", "label": label, "options": [] if lazy else [o.text for o in s.options],
                "prev": s.first_selected_option.text, "numeric": False, "required": _required(sel_els[0]), "w": s, "lazy_options": lazy}
    fs = el.find_elements(By.XPATH, './/fieldset[@data-test-form-builder-radio-button-form-component="true"]')
    if fs:
        radio = fs[0]
        title = radio.find_elements(By.XPATH, ".//span[@data-test-form-builder-radio-button-form-component__title]")
        label = _hidden_or_text(title[0]) if title else "Unknown"
        inputs = radio.find_elements(By.TAG_NAME, "input")
        options, prev = [], None
        for inp in inputs:
            labs = radio.find_elements(By.XPATH, f'.//label[@for="{inp.get_attribute("id")}"]')
            options.append((labs[0].text if labs else "Unknown").strip())
            if inp.is_selected():
                prev = options[-1]
        return {"kind": "radio", "label": label or "Unknown", "options": options, "prev": prev, "numeric": False,
                "required": _required(*inputs) or True, "w": (radio, inputs)}
    texts = el.find_elements(By.XPATH, ".//input[@type='text']")
    if texts:
        t = texts[0]
        labs = el.find_elements(By.XPATH, ".//label[@for]")
        label = _hidden_or_text(labs[0]) if labs else "Unknown"
        iid = (t.get_attribute("id") or "").lower()
        numeric = "numeric" in iid or (t.get_attribute("inputmode") or "") in ("numeric", "decimal")
        return {"kind": "text", "label": label or "Unknown", "options": [], "prev": t.get_attribute("value"), "numeric": numeric,
                "required": _required(t), "w": t}
    areas = el.find_elements(By.XPATH, ".//textarea")
    if areas:
        labs = el.find_elements(By.XPATH, ".//label[@for]")
        return {"kind": "textarea", "label": (labs[0].text.strip() if labs else "Unknown") or "Unknown", "options": [],
                "prev": areas[0].get_attribute("value"), "numeric": False, "required": _required(areas[0]), "w": areas[0]}
    boxes = el.find_elements(By.XPATH, ".//input[@type='checkbox']")
    if len(boxes) > 1:
        return None                 # "select all that apply": not read here, so the unrecognised-widget block applies
    if boxes:
        spans = el.find_elements(By.XPATH, ".//span[@class='visually-hidden']")
        labs = el.find_elements(By.XPATH, ".//label[@for]")
        label = (spans[0].text.strip() if spans and spans[0].text.strip() else (labs[0].text.strip() if labs else "Unknown")) or "Unknown"
        return {"kind": "checkbox", "label": label, "options": [], "prev": boxes[0].is_selected(), "numeric": False,
                "required": _required(boxes[0]), "w": boxes[0]}
    return None


def _keep_fallback(ans, q) -> str:
    """Contact fields keep what LinkedIn pre-filled; an empty one gets the same value the hardened runner already uses."""
    R, n = _R, q["label"].lower()
    if ans.intent == "contact.phone":
        return str(getattr(R, "phone_number", ""))
    if ans.intent == "contact.email":
        return str(getattr(R, "username", ""))
    if ans.intent == "contact.phone_country_code":
        return "India (+91)"
    if ans.intent == "contact.linkedin":
        return str(getattr(R, "linkedIn", ""))
    if ans.intent == "contact.name":
        if "first" in n:
            return str(getattr(R, "first_name", ""))
        if "last" in n:
            return str(getattr(R, "last_name", ""))
        if "middle" in n:
            return str(getattr(R, "middle_name", ""))
        return str(getattr(R, "full_name", ""))
    return ""


def _fail(q, why: str, F):
    exc = F.Unanswerable(why, intent="fill")
    S.block(q["label"], q["kind"], exc)
    raise exc


def _fill(q: dict, ans, F) -> str:
    """_fill_inner, with any Selenium error (a select option that is not there, a click that fails) turned into a recorded block
    instead of an anonymous exception that would never reach the unanswered-questions list."""
    try:
        return _fill_inner(q, ans, F)
    except (F.Unanswerable, StaleElementReferenceException):
        raise
    except Exception as e:
        _fail(q, f"could not fill: {type(e).__name__}: {str(e)[:80]}", F)


def _fill_inner(q: dict, ans, F) -> str:
    """Do what the resolver decided, then read the field back. Anything that does not read back as intended blocks the job."""
    R = _R
    kind, w = q["kind"], q["w"]
    if kind == "select":
        want = ans.value
        if ans.keep:
            if q["prev"] and q["prev"].strip().lower() != "select an option":
                return q["prev"]
            fb = _keep_fallback(ans, q)
            if q.get("lazy_options"):
                q["options"] = [o.text for o in w.options]
            want = next((o for o in q["options"] if fb and (fb.lower() in o.lower() or o.strip() == fb)), "")
            if not want:
                _fail(q, f"no option to pre-fill '{q['label'][:40]}'", F)
        if q["prev"] != want:
            w.select_by_visible_text(want)
        if w.first_selected_option.text != want:
            _fail(q, f"select did not take '{want[:30]}'", F)
        return want
    if kind == "radio":
        radio, inputs = w
        if ans.keep:
            if q["prev"]:
                return q["prev"]
            _fail(q, "a contact radio with nothing pre-filled", F)
        idx = [i for i, o in enumerate(q["options"]) if o.strip() == ans.value.strip()]
        if len(idx) != 1:
            _fail(q, f"radio has no single option '{ans.value[:30]}'", F)
        if q["prev"] != ans.value:
            lab = radio.find_elements(By.XPATH, f'.//label[@for="{inputs[idx[0]].get_attribute("id")}"]')
            R.actions.move_to_element(lab[0] if lab else inputs[idx[0]]).click().perform()
        if not inputs[idx[0]].is_selected():
            _fail(q, f"radio did not take '{ans.value[:30]}'", F)
        return ans.value
    if kind == "text":
        value = ans.value
        if ans.keep:
            if (q["prev"] or "").strip():
                return q["prev"]
            value = _keep_fallback(ans, q)
            if not value:
                _fail(q, f"no value to pre-fill '{q['label'][:40]}'", F)
        if (q["prev"] or "").strip() != value:
            w.clear()
            w.send_keys(value)
            if ans.typeahead:
                time.sleep(2)
                R.actions.send_keys(Keys.ARROW_DOWN)
                R.actions.send_keys(Keys.ENTER).perform()
        got = (w.get_attribute("value") or "").strip()
        if not got or (not ans.typeahead and got != value):
            _fail(q, f"text did not take the answer ({len(got)} chars typed back)", F)
        return value
    if kind == "checkbox":
        if ans.check and not w.is_selected():
            R.actions.move_to_element(w).click().perform()
        if bool(ans.check) != w.is_selected():
            _fail(q, "checkbox did not reach the intended state", F)
        return "checked" if ans.check else "unchecked"
    _fail(q, f"cannot fill a {kind} question", F)


AI_KINDS = ("select", "radio", "text")                  # what _fill_inner can fill: there is no textarea branch, so no AI call for one
AI_TRANSIENT_STAGES = ("transport", "breaker", "material", "auth")
# written when the answerer's key or configuration is rejected (HTTP 400/401/403/404): the model is not called again, by this or any later pass,
# until a human has fixed the key and deleted the file (an outage that fixes itself is "transport" and is simply retried)
AI_OFF = LOGS / "AI_ANSWERER_OFF"


def _ai_rescue(q: dict, exc):
    """(answer, exception). Only a REQUIRED question that no intent maps at all can reach the AI answerer (which re-checks that itself);
    every other refusal (unconfirmed fact, visa, ID, relocation, compound, skill not on the resume...) stays a refusal, and so does every
    decline or failure of the answerer. A used AI answer is logged in full to ai_answers.jsonl and marks the job."""
    if S.ai is None or not q["required"] or q["kind"] not in AI_KINDS or exc.reason != "no intent maps this question":
        return None, exc
    if S.ai_dead:           # the key or configuration was rejected earlier in this pass: no more calls, and the job is retried, not skipped for good
        return None, S.F.Unanswerable(f"{exc.reason}; the AI answerer is switched off (its key or configuration was rejected)"[:220],
                                      intent="fill", facts=exc.facts)
    key = (S.job["job_id"], q["label"], q["kind"], tuple(q["options"]))
    if key in S.ai_memo:                                    # the bot re-asks a page that did not advance: one model call per question
        ans, why_not = S.ai_memo[key]
        if ans is not None:
            S.job["ai_answered"] = True
        return (ans, exc) if ans is not None else (None, why_not)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "mode": S.mode, "job_id": S.job["job_id"], "title": S.job["title"],
           "company": S.job["company"], "question": q["label"], "kind": q["kind"], "options": q["options"][:12], "facts_rev": S.facts.rev}
    try:
        ans = S.ai.ai_answer(S.facts, q["label"], q["kind"], q["options"], numeric=q["numeric"], required=q["required"], ctx=S.ctx())
    except Exception as x:
        audit = getattr(x, "audit", None) or {}
        why = getattr(x, "reason", None) or f"{type(x).__name__}: {str(x)[:80]}"
        # an outage (transport, circuit breaker) or a missing resume file is not a property of the question: the job ends "failed" and is
        # looked at again, instead of being skipped for good under this revision
        transient = not hasattr(x, "reason") or audit.get("stage") in AI_TRANSIENT_STAGES
        _jsonl(AI_LOG, row | {"answer": None, "declined": why, "stage": audit.get("stage"), "audit": audit})
        if audit.get("stage") == "auth":
            S.ai_dead = True
            try:
                AI_OFF.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} the AI answerer's key or configuration was rejected ({why[:120]}); "
                                  "AI answers stay off until you have fixed the key and deleted this file.\n", encoding="utf-8")
            except OSError:
                pass
            _note(f"uae: the AI answerer was rejected ({why[:80]}); AI answers are off until logs\\AI_ANSWERER_OFF is deleted")
        refusal = S.F.Unanswerable(f"{exc.reason}; the AI answerer declined: {why}"[:220], intent="fill" if transient else exc.intent, facts=exc.facts)
        S.ai_memo[key] = (None, refusal)
        return None, refusal
    S.job["ai_answered"] = True
    _jsonl(AI_LOG, row | {"answer": ans.value, "audit": getattr(ans, "audit", {})})
    S.ai_memo[key] = (ans, None)
    return ans, exc


def _load_ai(F, facts, arm: dict):
    """The AI answerer (job-tracker/portal_kit/ai_answer.py), loaded by path like the facts module. None when the file is missing or does not
    load: the pass then simply has no AI. The answerer's fingerprint and its live/inventory setting join the facts revision, so jobs skipped
    under another answerer (or none), or held back until the AI answers were approved, are looked at again."""
    if AI_OFF.exists():
        _note("uae: AI answers are off: logs\\AI_ANSWERER_OFF exists (its key or configuration was rejected); delete it after fixing the key")
        facts.rev = f"{facts.rev}-ai-off"           # jobs skipped meanwhile are looked at again once the AI is back (a different revision)
        return None
    try:
        mod = _load(FACTS_PKG / "ai_answer.py", "uae_ai_answer")
        facts.rev = f"{facts.rev}-ai{mod.answerer_fingerprint()}{'L' if arm.get('ai_live') is True else 'I'}"
        return mod
    except Exception as e:
        _note(f"uae: AI answerer not loaded ({type(e).__name__}: {str(e)[:100]}); no AI this pass")
        return None


def uae_answer_questions(modal, questions_list, work_location, job_description=None):
    """Replacement for R.answer_questions while the UAE pass runs: every answer from the facts, or the job is blocked.

    Any failure here blocks the job. That matters: the bot's own `finally` after a failed question page still tries
    "Review" and "Submit application", and an unexpected error must never end in a Submit."""
    R, F = _R, S.F
    R.useNewResume = True                       # the resume step is re-checked on every page, as in the hardened runner
    try:
        for el in modal.find_elements(By.XPATH, ".//div[@data-test-form-element]"):
            for attempt in (1, 2):
                try:
                    q = _read_question(el)
                    if q is None:
                        # a container with a widget this pass cannot read (date picker, combobox, grouped checkboxes) may carry a
                        # value LinkedIn pre-filled from an earlier application: block rather than submit it unseen
                        widgets = el.find_elements(By.XPATH, ".//input[not(@type='hidden')] | .//select | .//textarea | "
                                                             ".//*[@role='combobox'] | .//*[@role='listbox']")
                        if widgets:
                            exc = F.Unanswerable("unrecognised form widget", intent="widget")
                            S.block((el.text or "")[:100].replace("\n", " "), "unknown", exc)
                            raise exc
                        break
                    try:
                        ans = S.facts.resolve(q["label"], q["kind"], q["options"], numeric=q["numeric"], required=q["required"], ctx=S.ctx())
                    except F.Unanswerable as e:
                        ans, e = _ai_rescue(q, e)
                        if ans is None:
                            prefilled = bool(q["prev"]) and str(q["prev"]).strip().lower() not in ("select an option", "false")
                            if q["required"] or prefilled:
                                # an optional question nobody can answer stays blank, but not when LinkedIn already filled it in from an
                                # older application: that value would be submitted unseen
                                S.block(q["label"], q["kind"], e)
                                raise e
                            break
                    shown = _fill(q, ans, F)
                    S.job["qa"].append({"q": q["label"][:120], "kind": q["kind"], "intent": ans.intent, "facts": list(ans.facts),
                                        "answer": ("<keep>" if ans.keep else "<salary>" if ans.intent.startswith("salary") else shown[:80]),
                                        "proposed": ans.proposed, "ai": bool(getattr(ans, "audit", None))})
                    if ans.proposed:
                        S.job["uses_proposed"] = True
                    questions_list.add((q["label"], shown, q["kind"], q["prev"]))
                    break
                except StaleElementReferenceException:
                    if attempt == 2:
                        raise NoSuchElementException("question element went stale twice")
                    time.sleep(1.0)
        try:
            R.try_xp(R.driver, "//button[contains(@aria-label, 'This is today')]")
        except Exception:
            pass
    except Exception as e:
        if S.job is not None and not S.job.get("blocked"):
            S.job["blocked"] = f"answering failed unexpectedly: {type(e).__name__}: {str(e)[:120]}"
            S.job["block_transient"] = True
        raise
    return questions_list


# ------------------------------------------------------------------ search terms: LinkedIn's own URL and the wrong-region stop (U1)
# per-click tracking parameters, dropped from a logged jobs URL (compared in lower case; any key starting with "trk" too). Every other
# parameter stays: U2 pins the search's own ones from these URLs (keywords, location, geoId, f_*, sortBy).
_URL_DROP = frozenset(("currentjobid", "trackingid", "refid", "lipi", "ebp", "midtoken", "midsig", "otptoken"))


def _current_url() -> str:
    """The browser's URL, kept only for a LinkedIn jobs page (where a search's results are), without its per-click tracking parameters
    (_URL_DROP and trk*). Any other page, such as a security wall or the sign-in page, is named by its host and first path segment only:
    those URLs carry tokens and do not belong in a log. Never raises."""
    try:
        url = str(_R.driver.current_url or "")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (host == "linkedin.com" or host.endswith(".linkedin.com")) and parts.path.lower().startswith("/jobs"):
            kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k.lower() not in _URL_DROP and not k.lower().startswith("trk")]
            # quote, not urlencode's default quote_plus: a space stays %20 and a comma %2C, as LinkedIn writes them
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, quote_via=quote), parts.fragment))
        return f"(not a LinkedIn jobs page: {host or '?'}/{parts.path.strip('/').split('/')[0][:24]})"
    except Exception as e:
        return f"(unreadable: {type(e).__name__})"


def _open_term(term) -> None:
    """A search term starts (_Terms calls this just before apply_to_jobs loads the term's search page). A term stopped for its region held
    R.switch_number at 0 only to end itself, so the new term gets the number back. Nothing else can have zeroed it in between: after that stop
    the bot only breaks out of its card loop and its page loop, and a stop that ends the whole pass is in over_budget, which _Terms checks first."""
    if S.paused_switch and not _R.switch_number:
        _R.switch_number = S.paused_switch
    S.paused_switch = 0
    S.term = {"term": str(term), "url": None, "page": 0, "page1_cards": 0, "page1_uae": 0, "judged": False, "stop": None}


def _term_url() -> None:
    """Keep and log the URL LinkedIn shows for the term's results, once per term. Read at the term's first card, which comes after the filter
    dialog, the bot's wait for the result list and its 3 s buffer: the URL of the results the pass really works through."""
    t = S.term
    if t is not None and t["url"] is None:
        t["url"] = _current_url()
        _note(f"uae: search '{t['term']}': LinkedIn's URL for the results is {t['url']}")


def _term_counting() -> bool:
    """True while the open term is still on its first results page and has not been judged."""
    t = S.term
    return t is not None and t["page"] == 1 and not t["judged"]


def _term_card(location: str) -> None:
    """Count one first-page card that shows a location, for the wrong-region test. Cards handled in an earlier pass count too: the test is about
    what LinkedIn returned, and a page whose UAE cards were all handled before is not a wrong region."""
    if not location or not _term_counting():
        return
    S.term["page1_cards"] += 1
    if _in_uae(location):
        S.term["page1_uae"] += 1


def _judge_term(t: dict) -> bool:
    """The wrong-region test, once per term, when its first results page is over. True, with a wrong_region row in the job log, when the page
    showed at least WRONG_REGION_CARDS cards with a location and none in the UAE. The WRONG_REGION_TERMS-th such term ends the pass."""
    t["judged"] = True
    if t["page1_cards"] < WRONG_REGION_CARDS or t["page1_uae"]:
        return False
    t["stop"] = "wrong_region"
    S.counts["wrong_region"] += 1
    S.wrong_terms.append({"term": t["term"], "url": t["url"], "cards": t["page1_cards"]})
    n = len(S.wrong_terms)
    # a term row, not a job row: no job_id; ts, mode and outcome are there because every reader of the job log keys on them
    _jsonl(JOBS_LOG, {"ts": datetime.now().isoformat(timespec="seconds"), "mode": S.mode, "facts_rev": S.facts.rev, "outcome": "wrong_region",
                      "term": t["term"], "url": t["url"], "cards": t["page1_cards"], "uae_cards": 0,
                      "reason": f"the first results page showed {t['page1_cards']} cards with a location and none in the UAE"})
    _note(f"uae: search '{t['term']}' stopped: its first results page showed {t['page1_cards']} cards and none in the UAE "
          f"(wrong-region terms in this pass: {n}; {WRONG_REGION_TERMS} end it)")
    if n >= WRONG_REGION_TERMS:
        S.stop = f"the wrong-region stop ({n} search terms showed no UAE card on their first results page)"
        _R.switch_number = 0
        _wrong_region_marker()
        _note(f"uae: {S.stop}; the pass ends (logs\\{WRONG_REGION_MARKER}{date.today().isoformat()})")
    return True


def _wrong_region_marker() -> None:
    """logs/LINKEDIN_UAE_WRONG_REGION_<date>, with the terms and the URLs LinkedIn showed for them. One a day: an existing marker is kept, so
    'since' stays the day's first stop. Never raises."""
    path = LOGS / f"{WRONG_REGION_MARKER}{date.today().isoformat()}"
    try:
        if path.exists():
            return
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"since": datetime.now().isoformat(timespec="seconds"), "mode": S.mode, "facts_rev": S.facts.rev,
                                   "what": f"{len(S.wrong_terms)} search terms in one UAE pass showed at least {WRONG_REGION_CARDS} cards and "
                                           "none in the UAE on their first results page, so the pass was ended",
                                   "terms": S.wrong_terms}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        _note(f"uae: {path.name} not written: {e!r}")


def _close_term(judge: bool) -> None:
    """The open term is over; its record goes to the cycle row. Its first page is judged here when the term ended while still on it (one page
    of results, or the list ran out) and the pass was not stopping: R.switch_number 0 means a budget, the quota or a verification stop cut the
    page short. judge=False (the finally block, after an early return or an exception) is a page cut short too."""
    t = S.term
    if t is None:
        return
    S.term = None
    if t["url"] is None:                        # no result card was reached: the URL the term's search ended on
        t["url"] = _current_url()
        _note(f"uae: search '{t['term']}': no result card reached; LinkedIn's URL is {t['url']}")
    if judge and not t["judged"] and _R.switch_number:
        _judge_term(t)
    S.terms.append({"term": t["term"], "url": t["url"], "pages": t["page"], "page1_cards": t["page1_cards"],
                    "page1_uae": t["page1_uae"], "stop": t["stop"]})


# ------------------------------------------------------------------ patches on the bot module, all undone in a finally
def _patches(R) -> dict:
    # get_page_info is run_linkedin_local.py's L2 wrapper (its wall check, then the bot's own): page_info below calls it, so the check stays
    orig = {n: getattr(R, n) for n in ("answer_questions", "wait_span_click", "get_job_main_details", "submitted_jobs", "failed_job",
                                       "get_applied_job_ids", "external_apply", "print_lg", "discard_job", "upload_resume", "get_page_info")}

    def print_lg(*msgs, **kw):
        # apply_to_jobs logs 'Successfully saved "<job>"' after every job it did not skip, including an inventory job it discarded.
        # run_linkedin_supervised.py counts those lines as applications for its yield watchdog, so say what really happened.
        if msgs and isinstance(msgs[0], str) and msgs[0].startswith("Successfully saved") and not (S.job or {}).get("really_applied"):
            msgs = ("UAE pass: nothing was submitted for" + msgs[0][len("Successfully saved"):],) + msgs[1:]
        # page_info hands back no pagination element to stop a wrong-region term (U1); the bot then blames the page for it
        if (msgs and isinstance(msgs[0], str) and msgs[0].startswith("Couldn't find pagination element")
                and (S.term or {}).get("stop") == "wrong_region"):
            msgs = (f"UAE pass: search '{S.term['term']}' stopped after its first results page (no UAE card on it).",) + msgs[1:]
        return orig["print_lg"](*msgs, **kw)

    def details(job, blacklisted_companies, rejected_jobs):
        # Decide from the card's own id BEFORE the runner's per-job pacing wait (8-20 s) and card click: once the job pool is
        # saturated every card is already handled, and the pass must neither burn that time nor outlive its budget.
        _term_url()                                  # the term's first card: keep the URL LinkedIn shows for these results (U1)
        try:
            card_id = str(job.get_dom_attribute("data-occludable-job-id") or "")
        except Exception:
            card_id = ""
        why = S.over_budget()
        if why:
            if R.switch_number:
                R.switch_number = 0
                _note(f"uae: {why} reached")
            return (card_id or "unknown", "Unknown", "Unknown", "", "", True)
        _render_card(job)                            # before any decision: the list only renders what is near the viewport
        S.cards_seen += 1
        if card_id and (card_id in S.handled or card_id in S.seen):
            if _term_counting():
                _term_card(_card_fields(job)[2])     # still part of what the first page shows (the wrong-region test)
            return (card_id, "Unknown", "Unknown", "", "", True)
        # The card shows its own location: a job outside the UAE is rejected here, before the bot clicks it and waits its per-job pacing
        # (the first real pass spent 24 minutes of its 45 on 48 cards it then rejected one by one).
        title0, company0, loc0 = _card_fields(job)
        if card_id and not (title0 or company0 or loc0):
            time.sleep(0.3)                          # a card that went stale between the scroll and the read looks just like a placeholder: once more
            title0, company0, loc0 = _card_fields(job)
        if card_id and not (title0 or company0 or loc0):
            # still an empty placeholder after scrolling to it: nothing to decide on, and the bot's own path would only wait its pacing and fail
            S.placeholders += 1
            return (card_id, "Unknown", "Unknown", "", "", True)
        _term_card(loc0)
        if card_id and loc0 and not _in_uae(loc0):
            S.begin(card_id, title0, company0, loc0, "", count=False)
            S.finish("skipped_job", f"location is not in the UAE: {loc0}")
            S.loc_skips += 1
            if S.loc_skips <= 2:
                _dump_card(job, card_id)             # evidence of what a rejected card looks like, in case this gate is ever wrong
            return (card_id, title0 or "Unknown", company0 or "Unknown", loc0, "", True)
        if card_id and loc0:
            S.uae_cards += 1                         # a card whose own location is in the UAE (the "layout changed" warning needs at least one)
        why_title = _title_problem(title0)
        if card_id and why_title:                    # decided from the card too: no click, no per-job wait
            S.begin(card_id, title0, company0, loc0, "", count=False)
            S.finish("skipped_job", why_title)
            return (card_id, title0, company0 or "Unknown", loc0, "", True)
        if card_id and title0 and company0 and _role_key(company0, title0) in S.applied_keys:
            # the same role at the same company under another job id (reposts): applying twice looks careless
            S.begin(card_id, title0, company0, loc0, "", count=False)
            S.finish("skipped_job", "the same role at the same company was already applied to under another job id")
            return (card_id, title0, company0, loc0, "", True)
        res = orig["get_job_main_details"](job, blacklisted_companies, rejected_jobs)
        job_id, title, company, location, style, skip = res
        if not loc0:
            _term_card(location)                     # the card showed no location of its own: the bot's parse is what it shows
        if skip:
            _calibrate_card(job, str(job_id))
            return res
        if str(job_id) in S.handled or str(job_id) in S.seen:
            R.print_lg(f'UAE pass: "{title} | {company}" was already handled. Skipping.')
            return (job_id, title, company, location, style, True)
        why_title = _title_problem(title)            # the bot's own parse, when the card's elements gave nothing to decide on
        if why_title:
            S.begin(job_id, title, company, location, style, count=False)
            S.finish("skipped_job", why_title)
            return (job_id, title, company, location, style, True)
        if title and company and _role_key(company, title) in S.applied_keys:
            S.begin(job_id, title, company, location, style, count=False)
            S.finish("skipped_job", "the same role at the same company was already applied to under another job id")
            return (job_id, title, company, location, style, True)
        if not location and loc0:                    # the card went stale during the bot's own parse: the early read of its location stands
            location = re.sub(r"\s*\((on-site|hybrid|remote)\)\s*$", "", loc0, flags=re.I)
        if not _in_uae(location):
            R.print_lg(f'UAE pass: "{title} | {company}" is in "{location}", not the UAE. Skipping.')
            S.begin(job_id, title, company, location, style, count=False)
            S.finish("skipped_job", f"location is not in the UAE: {location}")
            return (job_id, title, company, location, style, True)
        S.begin(job_id, title, company, location, style)
        return res

    def get_applied():
        return set(orig["get_applied_job_ids"]()) | set(S.handled)

    def modal_open() -> bool:
        try:
            return bool(R.driver.find_elements(By.CSS_SELECTOR, ".jobs-easy-apply-modal, .artdeco-modal"))
        except Exception:
            return False

    def discard_job():
        # The runner's hardened discard also clicks the first "Dismiss"/"Close" button on the page once the modal is gone, which is a
        # job card's "Dismiss <title> job" (it hides a job from the results list). The UAE pass discards on every inventoried job,
        # so it uses only LinkedIn's own two steps: Escape opens "Save this application?", its Discard button closes the modal.
        for _ in range(3):
            if not modal_open():
                return
            try:
                R.actions.send_keys(Keys.ESCAPE).perform()
                time.sleep(1.0)
                if not modal_open():                # Escape closed it outright: no prompt, so no Discard button to wait for
                    return
                orig["wait_span_click"](R.driver, "Discard", 3)
            except Exception:
                pass
        if modal_open():                            # last resort: the runner's own discard, which also reloads the job page
            try:
                orig["discard_job"]()
            except Exception:
                pass

    def submit_click(driver, text, timeout=5.0, click=True, scroll=True, scrollTop=False):
        if (text == "Next" and click and S.job is not None and not S.job.get("done") and not S.job.get("first_page_seen")
                and getattr(driver, "tag_name", None)):
            # the bot clicks Next on the first page (contact info) before it ever calls the answerer; vet that page too,
            # so nothing LinkedIn pre-filled on it goes through unseen. `driver` is the modal element in this call.
            S.job["first_page_seen"] = True
            uae_answer_questions(driver, set(), "", None)
        if text == "Done" and click and (S.job or {}).get("awaiting_done"):
            S.job["awaiting_done"] = False          # the modal is already discarded (inventory, or a held job); do not wait for a button that is not there
            return True
        if text == "Submit application" and click:
            if S.job is None or S.job.get("done"):
                R.print_lg("UAE pass: Submit refused, no open job to attribute it to.")
                return False
            if not S.job.get("blocked"):
                try:
                    m = re.search(r"currentJobId=(\d+)", R.driver.current_url or "")
                    if m and m.group(1) != S.job["job_id"]:
                        S.job["blocked"] = f"the open page is job {m.group(1)}, not {S.job['job_id']}"
                        S.job["block_transient"] = True         # a pane that lags behind the click, not a property of the job
                except Exception:
                    pass
            blocked = S.job.get("blocked")
            if blocked:
                # the bot's own except path discards the modal after this refusal
                R.print_lg(f"UAE pass: Submit refused, job is blocked ({blocked[:90]}).")
                return False
            # AI-written selections are never submitted until the user has read the first ones and set "ai_live" in the arming file
            hold_ai = S.mode == "live" and bool(S.job.get("ai_answered")) and S.arm.get("ai_live") is not True
            if S.mode == "inventory" or S.job.get("uses_proposed") or hold_ai:
                # would_apply only when a Submit button is really there: the bot's finally asks for Submit after a stuck loop, a
                # timeout or an unread page too, and those jobs are not "answered completely"
                if not orig["wait_span_click"](driver, "Submit application", 2, False, True, False):
                    S.screenshot("no-submit")
                    S.finish("failed", "the flow ended before a Submit step (stuck, timed out or an unread page)")
                    return False
                S.screenshot("would-apply")
                if hold_ai:
                    S.finish("held_ai", "answered, but some answers were chosen by the AI answerer and are held until you approve them (ai_live)")
                else:
                    S.finish("would_apply", "every question was answered from the facts" +
                             ("; some answers use PROPOSED facts" if S.job["uses_proposed"] else "") +
                             ("; some answers were chosen by the AI answerer" if S.job.get("ai_answered") else ""))
                R.print_lg("UAE pass: inventory, so the application is discarded instead of submitted.")
                try:
                    R.discard_job()
                except Exception:
                    pass
                S.job["awaiting_done"] = True       # the bot takes True as "submitted" and looks for Done; see above
                return True
        res = orig["wait_span_click"](driver, text, timeout, click, scroll, scrollTop)
        if res and click and text == "Submit application" and S.job is not None and not S.job.get("done"):
            _post_submit_evidence()                 # a real live Submit: keep what LinkedIn shows next
        return res

    def upload_resume(modal, resume):
        ok, label = orig["upload_resume"](modal, resume)
        if str(label).startswith("Previous resume ("):
            # the runner could not find the current resume among LinkedIn's stored ones and fell back to whatever was pre-selected
            if S.job is not None:
                S.job["resume_unmatched"] = True
                if S.mode == "live":
                    exc = S.F.Unanswerable("the current resume could not be selected; LinkedIn's pre-selected one would be sent", intent="resume")
                    S.block("Resume step", "resume", exc)
                    raise exc
        return ok, label

    def submitted(job_id, title, company, work_location, work_style, description, experience_required, skills, hr_name, hr_link,
                  resume, reposted, date_listed, date_applied, job_link, application_link, questions_list, connect_request):
        if S.mode == "inventory" or (S.job is not None and S.job.get("done")):
            return                              # nothing was submitted (or the job was already closed out); no history row
        if date_applied == "Pending":           # live, but the Submit click never completed: the bot would still write a history row
            late = _applied_marker(str(job_id))     # unless LinkedIn already shows the job as applied: then the click did go through
            if late:
                _record_late_application(str(job_id), late, "date applied still Pending")
            else:
                S.finish("failed", "Submit did not complete (date applied still Pending)")
            return
        S.finish("applied", "")
        job_row = S.job
        res = orig["submitted_jobs"](job_id, title, company, work_location, work_style, description, experience_required, skills, hr_name,
                                     hr_link, resume, reposted, date_listed, date_applied, job_link, application_link, questions_list,
                                     connect_request)
        # the history row is written first; then LinkedIn's own Applied state decides whether this counts (see _verify_after_submit)
        _verify_after_submit(job_row, date_applied.isoformat(timespec="seconds") if hasattr(date_applied, "isoformat") else str(date_applied))
        return res

    def failed(job_id, job_link, resume, date_listed, error, exception, application_link, screenshot_name):
        # not passed on to the bot's failed-jobs CSV and evidence dumps: that list is the India run's, and a UAE skip is not a failure
        if S.job is not None and S.job["job_id"] == str(job_id):
            blocked = S.job.get("blocked")
            if blocked:
                S.screenshot("blocked")
            outcome = (("failed" if S.job.get("block_transient") else "skipped_unanswerable") if blocked
                       else "skipped_job" if "Skipped" in str(application_link) else "failed")
            if outcome == "failed" and not blocked and S.mode == "live" and not S.job.get("done"):
                # the bot's flow raised, but a Submit click that was already dispatched may have gone through (a renderer timeout, a stale
                # element after the modal closed): when LinkedIn itself already shows this job as applied, record what happened
                late = _applied_marker(str(job_id))
                if late:
                    _record_late_application(str(job_id), late, f"the bot's flow raised after Submit ({error})")
                    return
            S.finish(outcome, blocked or f"{error}: {str(exception)[:140]}")

    def external(pagination_element, job_id, job_link, resume, date_listed, application_link, screenshot_name):
        if S.job is not None:
            S.finish("no_easy_apply", "no Easy Apply button (external apply is not used in the UAE pass)")
        return True, "External (skipped in the UAE pass)", R.tabs_count

    def page_info(*args, **kwargs):
        # apply_to_jobs calls this once per results page, before reading its cards. The runner's wall check (L2) runs first, as before.
        res = orig["get_page_info"](*args, **kwargs)
        t = S.term
        if t is None:
            return res
        t["page"] += 1
        if t["page"] == 2 and not t["judged"] and _judge_term(t):
            # The first page showed no UAE card: stop the term at once. With switch_number 0 the bot leaves its card loop before this page's
            # first card, and with no pagination element it ends the term instead of clicking on to another page. _open_term gives the next
            # term its switch_number back (unless the stop ended the whole pass, which already set it to 0).
            if not S.stop:
                S.paused_switch = R.switch_number
                R.switch_number = 0
            return None, None
        return res

    R.answer_questions, R.wait_span_click, R.get_job_main_details = uae_answer_questions, submit_click, details
    R.submitted_jobs, R.failed_job, R.get_applied_job_ids, R.external_apply = submitted, failed, get_applied, external
    R.discard_job, R.upload_resume, R.print_lg, R.get_page_info = discard_job, upload_resume, print_lg, page_info
    return orig


class _Terms(list):
    """The search terms, which stop being handed out once the pass is over its budget (each remaining term would otherwise still load
    a search page and its filters before finding nothing to do). apply_to_jobs asks for the next term when the previous one is over, so
    this is also where each term's record (U1) is closed and the next one opened."""

    def __iter__(self):
        for term in list.__iter__(self):
            if S is not None:
                _close_term(judge=True)
                if S.over_budget():
                    _note(f"uae: {S.over_budget()} reached; the remaining search terms are skipped")
                    return
                _open_term(term)
            yield term
        if S is not None:
            _close_term(judge=True)


def _write_shortlist() -> None:
    """Jobs the pass skipped because a question had no fact, as the board's shortlist page reads them (apply by hand if one looks good)."""
    try:
        rows = _read_jsonl(JOBS_LOG)
        applied = {str(r["job_id"]) for r in rows if r.get("outcome") == "applied"}
        latest = {}
        for r in rows:
            if r.get("outcome") == "skipped_unanswerable" and str(r["job_id"]) not in applied:
                latest[str(r["job_id"])] = r                    # the last row for a job wins
        items = [{"title": r["title"], "company": r["company"], "location": r["location"], "posted": str(r["ts"])[:10],
                  "url": f"https://www.linkedin.com/jobs/view/{jid}/", "reason": f"A question has no fact: {r.get('reason', '')}"[:220]}
                 for jid, r in latest.items()]
        items.sort(key=lambda i: i["posted"], reverse=True)
        (LOGS / "linkedin_uae_shortlist.json").write_text(
            json.dumps({"updated": datetime.now().isoformat(timespec="minutes"), "items": items[:300]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        _note(f"uae: shortlist not written: {e!r}")


def _uae_pass(R, arm: dict, orig_apply) -> None:
    global S
    cfg = _load(REPO / "config" / "region_uae.py", "region_uae")
    F = _load(FACTS_PKG / "facts.py", "uae_facts")
    mode = arm["mode"]
    facts = F.Facts(live=(mode == "live"))
    facts.rev = f"{facts.rev}-c{_code_rev()}"       # facts + resolver + THIS pass's and the bot's code (see _code_rev)
    if arm.get("ai"):
        ai_mod = _load_ai(F, facts, arm)                # may change facts.rev, which the Pass below reads
    else:
        ai_mod = None
    S = Pass(mode, arm, cfg, facts, F)
    S.ai = ai_mod
    if mode == "live" and VERIFY_STOP.exists():
        _note("uae: live pass is off: LINKEDIN_UAE_VERIFY_STOPPED exists (submissions were not verified); India only")
        return
    if mode == "live" and S.quota_left <= 0:
        _note(f"uae: today's quota of {arm.get('quota', cfg.DAILY_QUOTA)} is used; India only")
        return
    gap_h = float(arm.get("min_gap_hours", 3))          # the supervisor restarts the bot often; one UAE pass per few hours is plenty
    # a changed facts file or resolver (a different revision) lifts the gap: the user will want the rerun at once
    ran = [c for c in _read_jsonl(CYCLES_LOG) if c.get("region") == "UAE" and c.get("mode") == mode and c.get("facts_rev") == facts.rev]
    if ran and gap_h > 0:
        try:
            ago = (datetime.now() - datetime.fromisoformat(str(ran[-1]["start"])[:19])).total_seconds() / 3600
        except Exception:
            ago = 99.0
        if ago < gap_h:
            _note(f"uae: a {mode} pass ran {ago:.1f} h ago (minimum gap {gap_h:g} h); India only")
            return
    start = datetime.now()
    keys = ("search_location", "experience_level", "job_type", "date_posted", "sort_by", "switch_number", "bad_words")
    saved_attrs = {k: getattr(R, k) for k in keys}
    saved_counts = {k: getattr(R, k) for k in ("easy_applied_count", "failed_count", "skip_count", "external_jobs_count")}
    saved_funcs = None
    try:
        R.search_location, R.experience_level, R.job_type = cfg.SEARCH_LOCATION, list(cfg.EXPERIENCE_LEVEL), list(cfg.JOB_TYPE)
        R.date_posted, R.sort_by, R.switch_number = cfg.DATE_POSTED, cfg.SORT_BY, int(cfg.SWITCH_NUMBER)
        R.SEARCH_URL_EXTRA = UAE_SEARCH_URL_EXTRA       # U1: the location in the search URL itself; '' again in the finally below
        R.bad_words = list(saved_attrs["bad_words"]) + list(cfg.BAD_WORDS_EXTRA)
        terms = list(cfg.SEARCH_TERMS)
        k = (date.today().toordinal() * 3) % len(terms)           # rotate the start so every term gets its turn over the days
        terms = _Terms(terms[k:] + terms[:k])
        saved_funcs = _patches(R)
        _note(f"uae: {mode} pass starts ({len(terms)} terms, facts {facts.rev}, quota left {S.quota_left if mode == 'live' else 'n/a'})")
        orig_apply(terms)
    finally:
        R.SEARCH_URL_EXTRA = ""                         # first, so nothing below can stop the India pass searching exactly as before
        try:
            S.finish("no_outcome", "the pass ended while this job was open")
        except Exception:
            pass
        try:
            _close_term(judge=False)                # a term still open after an early return or an exception: recorded, not judged
        except Exception:
            pass
        if saved_funcs:
            for n, fn in saved_funcs.items():
                setattr(R, n, fn)
        for n, v in saved_attrs.items():
            setattr(R, n, v)
        for n, v in saved_counts.items():
            setattr(R, n, v)
        if mode == "live":
            R.easy_applied_count += S.counts["applied"]            # R counts a failed Submit as applied; only real ones count here
        # a pass that looked at no card at all (it stopped at LinkedIn's limit banner) did nothing: it must not start the min-gap clock; one that only met
        # handled or rejected cards did run, and without a cycle row it would run again every cycle, burning minutes the India pass needs
        if S.visits or sum(S.counts.values()) or S.cards_seen:
            # U1 fields: uae_cards / loc_skips are the cards not handled before whose own location was / was not in the UAE (the pass's
            # existing counters); wrong_region counts the terms whose first page showed no UAE card; terms holds, per search term, the URL
            # LinkedIn showed for its results, how many result pages it went through, its first page's cards with a location and how many
            # of those were in the UAE (handled cards included), and stop: "wrong_region" for such a term, else None
            _jsonl(CYCLES_LOG, {"start": start.isoformat(timespec="seconds"), "end": datetime.now().isoformat(timespec="seconds"), "region": "UAE",
                                "cards": S.cards_seen, "placeholders": S.placeholders, "uae_cards": S.uae_cards, "loc_skips": S.loc_skips,
                                "mode": mode, "applied": S.counts["applied"], "verified": S.counts["verified"],
                                "verify_failed": S.counts["verify_failed"], "would_apply": S.counts["would_apply"],
                                "held_ai": S.counts["held_ai"],
                                "skipped_unanswerable": S.counts["skipped_unanswerable"], "skipped_job": S.counts["skipped_job"],
                                "no_easy_apply": S.counts["no_easy_apply"], "failed": S.counts["failed"], "visits": S.visits,
                                "wrong_region": S.counts["wrong_region"], "facts_rev": facts.rev, "terms": S.terms})
        _write_shortlist()
        if S.visits == 0 and S.cards_seen >= 10 and S.uae_cards == 0:
            # exactly what a changed card layout looks like (the first real pass: 48 cards, every one "not in the UAE", none opened). A normal day with
            # an exhausted UAE pool still shows UAE cards (handled or filtered), only padding from India shows none at all.
            _note(f"uae: WARNING: {S.cards_seen} cards were looked at and none had a UAE location; if that is not the search's real result, the "
                  "card layout has changed again (HTML of the first rejected cards is in logs\\uae-evidence)")
        _note(f"uae: {mode} pass ended: {dict(S.counts)} over {S.visits} jobs ({S.cards_seen} cards looked at, {S.placeholders} empty placeholders, "
              f"{S.uae_cards} new cards in the UAE, {S.loc_skips} rejected by location, {len(S.terms)} search terms)")


def install(R, note) -> None:
    """Called by run_linkedin_local.py just before R.main(). Cheap and inert when the arming file is absent."""
    global _R, _note, _CODE_BASE
    _R, _note = R, note
    _CODE_BASE = _code_base()                       # the code this process runs, fixed now (see _code_rev)
    orig_apply = R.apply_to_jobs

    def regional_apply(search_terms):
        arm = _armed()
        if arm:
            try:
                _uae_pass(R, arm, orig_apply)
            except SystemExit:
                raise
            except Exception as e:
                note(f"uae: pass failed ({type(e).__name__}: {str(e)[:160]}); the India pass continues")
            if getattr(R, "dailyEasyApplyLimitReached", False):
                return
        return orig_apply(search_terms)

    R.apply_to_jobs = regional_apply
    note(f"uae overlay installed (armed now: {bool(_armed())}).")
