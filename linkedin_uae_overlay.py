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

Everything patched on the bot module `R` is restored in a finally block, so the India pass that follows runs the
unchanged bot. Facts live outside the repo (C:\\Users\\Admin\\.secrets\\gulf_facts.json).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

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
SHOTS = LOGS / "uae-evidence"
FACTS_PKG = Path(r"C:\Users\Admin\GitHub\job-tracker\portal_kit")

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


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _applied_today() -> int:
    """Live applications today, counted from the per-job rows (written the moment a job is submitted, so a crash cannot lose them)."""
    today = date.today().isoformat()
    return sum(1 for r in _read_jsonl(JOBS_LOG)
               if r.get("outcome") == "applied" and r.get("mode") == "live" and str(r.get("ts", ""))[:10] == today)


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

    def _handled_ids(self) -> set:
        """Jobs not worth opening again: applied, or skipped under the same facts file and resolver. A job inventoried as answerable
        is handled for another inventory pass, but a live pass is exactly where it should be applied to."""
        done = set()
        for r in _read_jsonl(JOBS_LOG):
            o, same = r.get("outcome"), r.get("facts_rev") == self.facts.rev
            if o == "applied" or (same and o in ("skipped_unanswerable", "skipped_job")) or (same and o == "would_apply" and self.mode == "inventory"):
                done.add(str(r.get("job_id")))
        return done

    def ctx(self) -> dict:
        j = self.job or {}
        return {"region": "UAE", "title": j.get("title"), "company": j.get("company"), "location": j.get("location")}

    def over_budget(self) -> str | None:
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
        if outcome in ("applied", "skipped_unanswerable", "skipped_job") or (outcome == "would_apply" and self.mode == "inventory"):
            self.handled.add(j["job_id"])
        if outcome == "applied":
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
                        prefilled = bool(q["prev"]) and str(q["prev"]).strip().lower() not in ("select an option", "false")
                        if q["required"] or prefilled:
                            # an optional question nobody can answer stays blank, but not when LinkedIn already filled it in from an
                            # older application: that value would be submitted unseen
                            S.block(q["label"], q["kind"], e)
                            raise
                        break
                    shown = _fill(q, ans, F)
                    S.job["qa"].append({"q": q["label"][:120], "kind": q["kind"], "intent": ans.intent, "facts": list(ans.facts),
                                        "answer": ("<keep>" if ans.keep else "<salary>" if ans.intent.startswith("salary") else shown[:80]),
                                        "proposed": ans.proposed})
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


# ------------------------------------------------------------------ patches on the bot module, all undone in a finally
def _patches(R) -> dict:
    orig = {n: getattr(R, n) for n in ("answer_questions", "wait_span_click", "get_job_main_details", "submitted_jobs", "failed_job",
                                       "get_applied_job_ids", "external_apply", "print_lg", "discard_job", "upload_resume")}

    def print_lg(*msgs, **kw):
        # apply_to_jobs logs 'Successfully saved "<job>"' after every job it did not skip, including an inventory job it discarded.
        # run_linkedin_supervised.py counts those lines as applications for its yield watchdog, so say what really happened.
        if msgs and isinstance(msgs[0], str) and msgs[0].startswith("Successfully saved") and not (S.job or {}).get("really_applied"):
            msgs = ("UAE pass: nothing was submitted for" + msgs[0][len("Successfully saved"):],) + msgs[1:]
        return orig["print_lg"](*msgs, **kw)

    def details(job, blacklisted_companies, rejected_jobs):
        # Decide from the card's own id BEFORE the runner's per-job pacing wait (8-20 s) and card click: once the job pool is
        # saturated every card is already handled, and the pass must neither burn that time nor outlive its budget.
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
        if card_id and (card_id in S.handled or card_id in S.seen):
            return (card_id, "Unknown", "Unknown", "", "", True)
        res = orig["get_job_main_details"](job, blacklisted_companies, rejected_jobs)
        job_id, title, company, location, style, skip = res
        if skip:
            return res
        if str(job_id) in S.handled or str(job_id) in S.seen:
            R.print_lg(f'UAE pass: "{title} | {company}" was already handled. Skipping.')
            return (job_id, title, company, location, style, True)
        low = (location or "").lower()
        if not any(w in low for w in S.cfg.UAE_LOCATION_WORDS):
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
        if text == "Done" and click and S.mode == "inventory" and (S.job or {}).get("awaiting_done"):
            S.job["awaiting_done"] = False          # the modal is already discarded; do not wait for a button that is not there
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
            if S.mode == "inventory" or S.job.get("uses_proposed"):
                # would_apply only when a Submit button is really there: the bot's finally asks for Submit after a stuck loop, a
                # timeout or an unread page too, and those jobs are not "answered completely"
                if not orig["wait_span_click"](driver, "Submit application", 2, False, True, False):
                    S.screenshot("no-submit")
                    S.finish("failed", "the flow ended before a Submit step (stuck, timed out or an unread page)")
                    return False
                S.screenshot("would-apply")
                S.finish("would_apply", "every question was answered from the facts" +
                         ("; some answers use PROPOSED facts" if S.job["uses_proposed"] else ""))
                R.print_lg("UAE pass: inventory, so the application is discarded instead of submitted.")
                try:
                    R.discard_job()
                except Exception:
                    pass
                S.job["awaiting_done"] = True       # the bot takes True as "submitted" and looks for Done; see above
                return True
        return orig["wait_span_click"](driver, text, timeout, click, scroll, scrollTop)

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
            S.finish("failed", "Submit did not complete (date applied still Pending)")
            return
        S.finish("applied", "")
        return orig["submitted_jobs"](job_id, title, company, work_location, work_style, description, experience_required, skills, hr_name,
                                      hr_link, resume, reposted, date_listed, date_applied, job_link, application_link, questions_list,
                                      connect_request)

    def failed(job_id, job_link, resume, date_listed, error, exception, application_link, screenshot_name):
        # not passed on to the bot's failed-jobs CSV and evidence dumps: that list is the India run's, and a UAE skip is not a failure
        if S.job is not None and S.job["job_id"] == str(job_id):
            blocked = S.job.get("blocked")
            if blocked:
                S.screenshot("blocked")
            outcome = (("failed" if S.job.get("block_transient") else "skipped_unanswerable") if blocked
                       else "skipped_job" if "Skipped" in str(application_link) else "failed")
            S.finish(outcome, blocked or f"{error}: {str(exception)[:140]}")

    def external(pagination_element, job_id, job_link, resume, date_listed, application_link, screenshot_name):
        if S.job is not None:
            S.finish("no_easy_apply", "no Easy Apply button (external apply is not used in the UAE pass)")
        return True, "External (skipped in the UAE pass)", R.tabs_count

    R.answer_questions, R.wait_span_click, R.get_job_main_details = uae_answer_questions, submit_click, details
    R.submitted_jobs, R.failed_job, R.get_applied_job_ids, R.external_apply = submitted, failed, get_applied, external
    R.discard_job, R.upload_resume, R.print_lg = discard_job, upload_resume, print_lg
    return orig


class _Terms(list):
    """The search terms, which stop being handed out once the pass is over its budget (each remaining term would otherwise still load
    a search page and its filters before finding nothing to do)."""

    def __iter__(self):
        for term in list.__iter__(self):
            if S is not None and S.over_budget():
                _note(f"uae: {S.over_budget()} reached; the remaining search terms are skipped")
                return
            yield term


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
    S = Pass(mode, arm, cfg, facts, F)
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
        R.bad_words = list(saved_attrs["bad_words"]) + list(cfg.BAD_WORDS_EXTRA)
        terms = list(cfg.SEARCH_TERMS)
        k = (date.today().toordinal() * 3) % len(terms)           # rotate the start so every term gets its turn over the days
        terms = _Terms(terms[k:] + terms[:k])
        saved_funcs = _patches(R)
        _note(f"uae: {mode} pass starts ({len(terms)} terms, facts {facts.rev}, quota left {S.quota_left if mode == 'live' else 'n/a'})")
        orig_apply(terms)
    finally:
        try:
            S.finish("no_outcome", "the pass ended while this job was open")
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
        if S.visits or sum(S.counts.values()):      # a pass that stopped at LinkedIn's limit banner did nothing: it must not start the min-gap clock
            _jsonl(CYCLES_LOG, {"start": start.isoformat(timespec="seconds"), "end": datetime.now().isoformat(timespec="seconds"), "region": "UAE",
                                "mode": mode, "applied": S.counts["applied"], "would_apply": S.counts["would_apply"],
                                "skipped_unanswerable": S.counts["skipped_unanswerable"], "skipped_job": S.counts["skipped_job"],
                                "no_easy_apply": S.counts["no_easy_apply"], "failed": S.counts["failed"], "visits": S.visits,
                                "facts_rev": facts.rev})
        _write_shortlist()
        _note(f"uae: {mode} pass ended: {dict(S.counts)} over {S.visits} jobs")


def install(R, note) -> None:
    """Called by run_linkedin_local.py just before R.main(). Cheap and inert when the arming file is absent."""
    global _R, _note
    _R, _note = R, note
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
