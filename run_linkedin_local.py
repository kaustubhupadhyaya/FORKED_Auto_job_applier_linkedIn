"""Untracked hardened runner for the LinkedIn bot. Leaves tracked files untouched.

What it does beyond `python runAiBot.py`:
1. Credentials from C:\\Users\\Admin\\GitHub\\creds.env (falls back to config/secrets.py).
2. Monkeypatches login_LN (LinkedIn removed id="username"/id="password"; React
   inputs need native value setter + off-screen scroll).
3. UNATTENDED hardening (all untracked, reversible by deleting this file):
   - resilient print_lg: console UTF-8 safe, file-append retry, fallback file,
     NEVER pyautogui.alert (the "Failed Logging" modal blocked the whole bot).
   - global pyautogui.alert/confirm neutralised to log-and-continue.
   - manual_login_retry: timed wait (180s) instead of infinite modal loop.
   - per-job session watchdog in get_job_main_details: re-login on logout.
   - LinkedIn security pages (checkpoint challenge, captcha, add-phone, account
     restriction), at sign-in or mid-run: evidence + logs/LINKEDIN_WALL_<date>,
     then exit 48 (L2). Sign-in pages are not walls: a lapsed session signs in
     from the vault, at most once per process; otherwise exit 45.
   - safe answers: explicit email/phone/country-code/city mappings; random
     select fallback (select_by_index) converted to discard instead of guess.
   - wait_span_click retry for Submit/Review/Done/Next/Discard.
   - close_tabs forced True (tab leak), pauses forced False, resume validated.
"""
import os
import sys
import time
from pprint import pprint
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)

# 2026-09-30: removed a PATH prepend of C:\WebDrivers and an old Temp scratchpad\bin that held
# chromedriver 152. Selenium Manager uses a driver found on PATH even when its version does not
# match, so after Chrome auto-updated to 154 every launch failed ("only supports Chrome version
# 152") and the supervisor looped all night. With nothing prepended, Selenium Manager fetches
# the chromedriver that matches the installed Chrome.

# Console must never raise on ₹/emoji (that used to masquerade as "log.txt occupied").
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- Neutralise blocking desktop modals BEFORE importing the bot ---------------
import pyautogui  # noqa: E402

_UNATTENDED_LOG = os.path.join(REPO, "logs", "unattended-hardening.log")


def _note(msg):
    try:
        os.makedirs(os.path.dirname(_UNATTENDED_LOG), exist_ok=True)
        with open(_UNATTENDED_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass
    try:
        print(f"[unattended] {msg}", flush=True)
    except Exception:
        pass


def _no_alert(*args, **kwargs):
    text = kwargs.get("text", args[0] if len(args) > 0 else "")
    title = kwargs.get("title", args[1] if len(args) > 1 else "")
    _note(f"alert suppressed title={title!r} text={str(text)[:160]!r}")
    return kwargs.get("button", "OK")


def _no_confirm(*args, **kwargs):
    text = kwargs.get("text", args[0] if len(args) > 0 else "")
    title = kwargs.get("title", args[1] if len(args) > 1 else "")
    buttons = kwargs.get("buttons")
    if buttons is None and len(args) >= 3:
        buttons = args[2]
    _note(f"confirm suppressed title={title!r} text={str(text)[:160]!r} buttons={buttons!r}")
    if isinstance(buttons, (list, tuple)) and buttons:
        return buttons[0]
    return "OK"


pyautogui.alert = _no_alert
pyautogui.confirm = _no_confirm

# --- Renderer-stability flags BEFORE the bot builds its Chrome options ---------
# open_chrome.py constructs Options() internally on import; patch the Chrome
# class up-front so every launch gets low-memory flags (prevents the
# "Timed out receiving message from renderer" death seen 2026-09-08).
try:
    from selenium import webdriver as _wd

    _OrigChrome = _wd.Chrome

    # 2026-09-30: with the external monitor off, maximize_window() left a 905x700 window on the
    # 1366x768 laptop screen (viewport 889x549). LinkedIn's job list pane shrank to 289x447, cards
    # just below its edge sat outside the scroll box, and every job-card click raised
    # ElementClickInterceptedException (0 applications). A fixed large window does not depend on
    # which monitor is active; Chrome allows a window larger than the screen.
    _WINDOW = (0, 0, 1600, 1000)

    class _StableChrome(_OrigChrome):
        def __init__(self, *args, **kwargs):
            try:
                _opts = kwargs.get("options")
                if _opts is not None:
                    for _flag in ("--disable-dev-shm-usage", "--disable-gpu",
                                  f"--window-position={_WINDOW[0]},{_WINDOW[1]}",
                                  f"--window-size={_WINDOW[2]},{_WINDOW[3]}"):
                        try:
                            _opts.add_argument(_flag)
                        except Exception:
                            pass
            except Exception:
                pass
            super().__init__(*args, **kwargs)

        def maximize_window(self):
            try:
                self.set_window_rect(x=_WINDOW[0], y=_WINDOW[1], width=_WINDOW[2], height=_WINDOW[3])
            except Exception:
                super().maximize_window()

    _wd.Chrome = _StableChrome
except Exception as _e:
    try:
        print(f"[run_linkedin_local] chrome-flag patch skipped: {_e!r}", flush=True)
    except Exception:
        pass

# --- Title-gate rules (user decision 2026-10-18), loaded BEFORE the bot launches Chrome ---------------------------------------------
# The India pass skips job cards whose TITLE the user's shared fit rules rule out (job-tracker/portal_kit/fit.py + ~/.secrets/fit_rules.json,
# the one definition every applier shares; 64% of the 158 applications of 10-13..10-18 failed them on the title alone: off-role titles such as
# intern and trainee, titles outside his roles, titles built on a stack he does not use). The gate is mandatory. If the live rules cannot be
# loaded, the last copy that did load (logs/fit_gate_lastgood/) is used and logs/FIT_RULES_STALE says so; if that fails too the run stops with
# exit 49 and logs/FIT_RULES_UNAVAILABLE, instead of applying without the gate.
_FIT_SRC = r"C:\Users\Admin\GitHub\job-tracker\portal_kit\fit.py"
_FIT_RULES_SRC = os.path.join(os.path.expanduser("~"), ".secrets", "fit_rules.json")
_FIT_LASTGOOD = os.path.join(REPO, "logs", "fit_gate_lastgood")
_FIT_EXIT = 49
_FIT = {"mod": None, "rules": None, "stale": False}


def _fit_load(py_path, rules_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("jt_fit_gate", py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rules = mod.load_rules(rules_path)
    mod.title_reasons("Data Engineer", "linkedin", rules)   # one real call: a module or rules this gate cannot use stop the run here, not at the first card
    return mod, rules


def _fit_marker(name, text=None):
    p = os.path.join(REPO, "logs", name)
    try:
        if text is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except Exception:
        pass


def _fit_gate_startup():
    import shutil
    try:
        mod, rules = _fit_load(_FIT_SRC, _FIT_RULES_SRC)
    except Exception as e:
        live_err = f"{type(e).__name__}: {str(e)[:160]}"
        try:
            mod, rules = _fit_load(os.path.join(_FIT_LASTGOOD, "fit.py"), os.path.join(_FIT_LASTGOOD, "fit_rules.json"))
        except Exception as e2:
            _fit_marker("FIT_RULES_UNAVAILABLE", f"live: {live_err}; last good copy: {type(e2).__name__}: {str(e2)[:120]}")
            _note(f"FIT RULES UNAVAILABLE (live: {live_err}; last good copy: {type(e2).__name__}: {str(e2)[:120]}). "
                  f"The bot does not apply without its title gate: exit {_FIT_EXIT}.")
            sys.exit(_FIT_EXIT)
        _FIT["stale"] = True
        _fit_marker("FIT_RULES_STALE", f"the live fit rules could not be loaded ({live_err}); the last copy that loaded (rules {rules.version}) is in use")
        _note(f"FIT RULES: the live copy could not be loaded ({live_err}); using the last copy that loaded (rules {rules.version}). Fix the live copy.")
    else:
        try:
            os.makedirs(_FIT_LASTGOOD, exist_ok=True)
            shutil.copy2(_FIT_SRC, os.path.join(_FIT_LASTGOOD, "fit.py"))
            shutil.copy2(_FIT_RULES_SRC, os.path.join(_FIT_LASTGOOD, "fit_rules.json"))
        except Exception as ce:
            _note(f"fit gate: could not keep a last-good copy ({type(ce).__name__})")
        _fit_marker("FIT_RULES_STALE")
        _fit_marker("FIT_RULES_UNAVAILABLE")
    _FIT["mod"], _FIT["rules"] = mod, rules
    _note(f"title gate loaded: fit rules {rules.version}{' (STALE last-good copy)' if _FIT['stale'] else ''}; India pass only")


_fit_gate_startup()

# --- Import the bot (launches Chrome on import via modules.open_chrome) --------
# 2026-09-30: open_chrome.py calls exit() (code 0) when Chrome/driver cannot start, which made a
# dead launch look like a clean run. Turn that into exit 43 so the supervisor stops and flags it.
try:
    import runAiBot as R  # noqa: E402
except SystemExit as _launch_exit:
    if _launch_exit.code in (None, 0):
        _note("browser/driver launch failed while importing runAiBot (exit 0) -> exit 43")
        sys.exit(43)
    raise
import modules.helpers as _helpers  # noqa: E402
import modules.clickers_and_finders as _clickers  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

try:
    from selenium.webdriver.support.select import Select as _Select
except Exception:
    _Select = None

# --- Credentials ------------------------------------------------------------------
# R.username/R.password are already vault-sourced (secure-vault label "linkedin-1")
# via config/secrets.py at import time above. No separate creds.env path anymore —
# that was a second, undocumented plaintext-credential fallback and is retired.
_src = "vault (linkedin-1, via config/secrets.py)"
print(f"[run_linkedin_local] LinkedIn user = {R.username!r} (from {_src})", flush=True)

# --- 1. Resilient logger (replaces "Failed Logging" modal) ----------------------
try:
    _LOG_PATH = _helpers.get_log_path()
except Exception:
    _LOG_PATH = os.path.join(REPO, "logs", "log.txt")


def _fallback_path():
    return os.path.join(REPO, "logs", f"log.fallback-{datetime.now():%Y%m%d}.txt")


def _resilient_print_lg(*msgs, end="\n", pretty=False, flush=False, from_critical=False):
    for message in msgs:
        text = message if isinstance(message, str) else str(message)
        try:
            if pretty:
                try:
                    pprint(message)
                except Exception:
                    try:
                        sys.stdout.buffer.write((text + end).encode("utf-8", errors="replace"))
                        sys.stdout.buffer.flush()
                    except Exception:
                        pass
            else:
                try:
                    print(text, end=end, flush=True)
                except Exception:
                    try:
                        sys.stdout.buffer.write((text + end).encode("utf-8", errors="replace"))
                        sys.stdout.buffer.flush()
                    except Exception:
                        pass
        except Exception:
            pass
        for _attempt in range(3):
            try:
                _dir = os.path.dirname(_LOG_PATH)
                if _dir:
                    os.makedirs(_dir, exist_ok=True)
                with open(_LOG_PATH, "a", encoding="utf-8") as _fh:
                    _fh.write(text + end)
                break
            except Exception:
                time.sleep(0.2)
                if _attempt == 2:
                    try:
                        with open(_fallback_path(), "a", encoding="utf-8") as _fh:
                            _fh.write(f"{datetime.now():%H:%M:%S} {text}{end}")
                    except Exception:
                        pass


def _resilient_critical(possible_reason, stack_trace):
    # Single attempt, never recursive, never modal.
    try:
        _resilient_print_lg(f"CRITICAL: {possible_reason} | {stack_trace!r}")
    except Exception:
        pass


_helpers.print_lg = _resilient_print_lg
_helpers.critical_error_log = _resilient_critical
R.print_lg = _resilient_print_lg
R.critical_error_log = _resilient_critical
for _mod in (_clickers,):
    try:
        _mod.print_lg = _resilient_print_lg
    except Exception:
        pass
try:
    import modules.open_chrome as _open_chrome
    _open_chrome.print_lg = _resilient_print_lg
    _open_chrome.critical_error_log = _resilient_critical
except Exception:
    pass
# AI connectors keep their own showAiErrorAlerts flag; silence modal path.
for _modname in ("modules.ai.openaiConnections", "modules.ai.deepseekConnections",
                 "modules.ai.geminiConnections"):
    try:
        _m = __import__(_modname, fromlist=["*"])
        if hasattr(_m, "showAiErrorAlerts"):
            _m.showAiErrorAlerts = False
    except Exception:
        pass

# --- 1b. Verification-wall stop (L2, 2026-10-13; review changes 2026-10-14) ---------
# A LinkedIn security page (a checkpoint challenge, captcha, add-phone or account-restriction page, or a shown captcha or
# HUMAN challenge frame), at sign-in or mid-run, ends the run at once: a screenshot and the URL path go to logs/wall-evidence,
# the day's marker logs/LINKEDIN_WALL_<date> is written, and the process exits 48. run_linkedin_supervised.py then writes
# NEEDS_HUMAN and does not restart; linkedin_keepalive.ps1 raises the toast and holds after a second wall day in a row. Before
# this, a wall got a 10-minute wait and the run carried on. A wall is never waited out and never continued past (the user's
# rule: never evade bot detection; stop and report). LinkedIn's sign-in pages (/login, /checkpoint/lg/..., /uas/login,
# password reset, /authwall) are not walls: a lapsed session signs in again from the vault (the user's decision, Q3), at most
# once per process (section 3: exit 45 otherwise).
from urllib.parse import quote as _quote, urlsplit as _urlsplit  # noqa: E402

_WALL_EXIT = 48
_WALL_DIR = os.path.join(REPO, "logs", "wall-evidence")
# where: "login" until the first job search starts, then "midrun". stopped: the wall's kind once a wall has stopped the run.
_WALL_STATE = {"where": "login", "stopped": None}
# URL path signals. Only the path is read, never the query string, and only under LinkedIn's security roots (or a path ending
# in /login-restriction): a job or company slug such as /jobs/view/challenge-lead-at-x-123/ or /company/2captcha/ never counts.
_WALL_PATH_ROOTS = ("/checkpoint/", "/check/", "/uas/")
_WALL_PATH_WORDS = (("captcha", "captcha"), ("challenge", "challenge"), ("add-phone", "add-phone"), ("restrict", "restricted"))
# The page's own wording (title and visible text), first match wins. It is read only off the bot's working pages
# (_WALL_WORK_PATHS, which include the Easy Apply modal) and only when LinkedIn's signed-in navigation bar is absent: job ads
# mention "security checks" (eleven of them in logs/log.txt) and feed posts are other members' words. A wall on the working
# pages arrives as a redirect (the URL signals) or a shown frame, and both are checked everywhere.
_WALL_TEXT = {
    "we've restricted your account": "restricted",
    "we've temporarily restricted your account": "restricted",
    "your account has been restricted": "restricted",
    "your account has been temporarily restricted": "restricted",
    "your account is temporarily restricted": "restricted",
    "verify your identity": "verify-identity",
    "let's do a quick security check": "security-check",
    "quick security check": "security-check",
    "security verification": "security-check",
}
_WALL_WORK_PATHS = ("/feed", "/jobs", "/my-items")
# Challenge frames (reCAPTCHA, hCaptcha, Arkose/FunCaptcha, HUMAN/PerimeterX) count only when really shown: overlapping the
# viewport, larger than 50x50, and no ancestor hidden or transparent. Every normal job page carries a hidden reCAPTCHA
# Enterprise frame (size=invisible, inside a display:none badge) and HUMAN / fingerprint sensor frames that are 0x0 or
# visibility:hidden and parked off-screen (seen in the logs/job-evidence page captures).
_WALL_JS = r"""
const phrases = arguments[0];
// Shown: overlaps the viewport on all four edges, larger than 50x50, and neither the frame nor any ancestor is
// display:none, visibility hidden/collapse or (nearly) transparent.
const shown = e => {
  const r = e.getBoundingClientRect();
  if (!(r.width > 50 && r.height > 50 && r.left < innerWidth && r.top < innerHeight && r.right > 0 && r.bottom > 0)) return false;
  for (let n = e; n; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || parseFloat(s.opacity) < 0.1) return false;
  }
  return true;
};
let frame = null;
for (const f of document.querySelectorAll('iframe')) {
  const a = [f.getAttribute('src'), f.getAttribute('title'), f.id, f.getAttribute('name')].join(' ').toLowerCase();
  if (!/captcha|arkose|protechts|perimeterx|humanthirdparty|security verification/.test(a) || a.includes('size=invisible')
      || !shown(f)) continue;
  frame = /protechts|perimeterx|humanthirdparty/.test(a) ? 'human' : a.includes('hcaptcha') ? 'hcaptcha'
        : /arkose|funcaptcha/.test(a) ? 'arkose' : a.includes('recaptcha') ? 'recaptcha'
        : a.includes('security verification') ? 'human' : 'captcha';
  break;
}
let phrase = null;
if (phrases.length && !document.querySelector('#global-nav, header.global-nav')) {
  // U+2018 / U+2019 typographic apostrophes become ' (LinkedIn writes "Let's" with U+2019)
  const text = (document.title + ' | ' + (document.body ? document.body.innerText : ''))
                 .replace(/[‘’]/g, "'").replace(/\s+/g, ' ').toLowerCase();
  phrase = phrases.find(p => text.includes(p)) || null;
}
return [frame, phrase];
"""


def _linkedin_path(url):
    """The lower-case path of a LinkedIn URL; None for any other URL (about:blank, a Chrome error page, an employer's site in
    an external-apply tab): walls are looked for on LinkedIn's own pages only."""
    parts = _urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return (parts.path or "/").lower()


def _wall_path_kind(path):
    """"url:<kind>" when a LinkedIn path is a security page (/checkpoint/challenge/..., /uas/consumer-email-challenge,
    /check/add-phone, .../login-restriction), else None. Sign-in pages under the same roots carry none of the words."""
    if path.startswith(_WALL_PATH_ROOTS) or path.rstrip("/").endswith("/login-restriction"):
        for needle, kind in _WALL_PATH_WORDS:
            if needle in path:
                return "url:" + kind
    return None


def _wall_kind(driver):
    """Short kind ("url:challenge", "captcha:arkose", "captcha:human", "text:restricted", ...) when the current page is a
    LinkedIn security wall, else None. Read-only: the URL path, shown challenge frames and the page's own wording. Never
    clicks, never waits."""
    try:
        path = _linkedin_path(driver.current_url)
    except Exception:
        return None
    if path is None:
        return None
    kind = _wall_path_kind(path)
    if kind:
        return kind
    phrases = [] if path.startswith(_WALL_WORK_PATHS) else list(_WALL_TEXT)
    try:
        frame, phrase = driver.execute_script(_WALL_JS, phrases) or (None, None)
    except Exception:
        return None
    if frame:
        return "captcha:" + str(frame)
    if phrase:
        return "text:" + _WALL_TEXT.get(phrase, "wall")
    return None


def _wall_url_path(driver):
    """The page's URL path for the evidence: no query string; e-mail addresses and the account's username masked."""
    import re
    try:
        path = _urlsplit(driver.current_url or "").path or "/"
    except Exception:
        return "?"
    path = re.sub(r"[^/]*(?:@|%40)[^/]*", "<email>", path)
    user = str(getattr(R, "username", "") or "").strip()
    if user:
        for form in {user, _quote(user, safe="")}:
            path = re.sub(re.escape(form), "<user>", path, flags=re.IGNORECASE)
    return path


def _wall_stop(driver, kind):
    """Record the wall (screenshot and URL path, the day's marker) and end the process with exit 48. Never returns."""
    import json
    where = _WALL_STATE["where"]
    now = datetime.now()
    base = os.path.join(_WALL_DIR, f"wall-{now:%Y%m%d_%H%M%S}-{where}")
    record = {"since": now.isoformat(timespec="seconds"), "kind": kind, "where": where, "url_path": _wall_url_path(driver),
              "evidence": base + ".png"}
    try:
        os.makedirs(_WALL_DIR, exist_ok=True)
        try:
            driver.save_screenshot(base + ".png")
        except Exception as e:
            _note(f"wall screenshot failed: {e!r}")
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except Exception as e:
        _note(f"wall evidence not saved: {e!r}")
    marker = os.path.join(REPO, "logs", f"LINKEDIN_WALL_{now:%Y-%m-%d}")
    try:
        if not os.path.exists(marker):      # one marker a day: "since" stays the day's first wall
            with open(marker + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            os.replace(marker + ".tmp", marker)
    except Exception as e:
        _note(f"wall marker not written: {e!r}")
    _WALL_STATE["stopped"] = kind
    msg = (f"WALL STOP: LinkedIn verification wall ({kind}) {'at sign-in' if where == 'login' else 'mid-run'}, "
           f"path {record['url_path']}. Not waiting, not continuing: exit {_WALL_EXIT}. Evidence: {base}.png")
    try:
        R.print_lg(msg)
    except Exception:
        pass
    _note(msg)
    raise SystemExit(_WALL_EXIT)


def _check_wall():
    """L2 check point: stop the run (exit 48) when the current page is a verification wall; return only when it is not.
    Called only where nothing between it and runAiBot.main() catches SystemExit (main itself lets it through)."""
    if _WALL_STATE["stopped"]:
        raise SystemExit(_WALL_EXIT)    # an inner handler swallowed the first stop: stop again (no second marker)
    try:
        d = R.driver
    except Exception:
        return
    kind = _wall_kind(d)
    if kind:
        _wall_stop(d, kind)


# Check points without a per-job hook. main() calls run() right after its sign-in step: a wall there can pass for a signed-in
# page, because is_logged_in_LN assumes "logged in" when it finds no Sign-in link. apply_to_jobs() calls apply_filters()
# right after loading each search page, and get_page_info() once per results page (inside a try that lets SystemExit
# through), which also covers the cards the UAE overlay rejects without opening them. The other check points:
# _patched_login_LN, the per-job watchdog and the official-count tab (and _timed_login_retry, which only runAiBot's own
# login_LN calls, and that is replaced below).
_orig_run = R.run


def _wall_checked_run(total_runs):
    _check_wall()
    _WALL_STATE["where"] = "midrun"
    return _orig_run(total_runs)


R.run = _wall_checked_run

_orig_apply_filters = R.apply_filters


def _wall_checked_apply_filters(*args, **kwargs):
    _check_wall()
    return _orig_apply_filters(*args, **kwargs)


R.apply_filters = _wall_checked_apply_filters

_orig_get_page_info = R.get_page_info


def _wall_checked_get_page_info(*args, **kwargs):
    _check_wall()
    return _orig_get_page_info(*args, **kwargs)


R.get_page_info = _wall_checked_get_page_info

# --- 2. Non-blocking login retry -------------------------------------------------
def _timed_login_retry(is_logged_in, limit=2, timeout_s=180):
    # L2: every poll checks for a verification wall first, so this wait never waits one out (exit 48 instead).
    waited = 0
    _check_wall()
    while not is_logged_in() and waited < timeout_s:
        if waited == 0:
            _resilient_print_lg("Not logged in yet — waiting up to "
                                f"{timeout_s}s (no popup in unattended mode).")
        time.sleep(5)
        waited += 5
        _check_wall()
    try:
        return bool(is_logged_in())
    except Exception:
        return False


_helpers.manual_login_retry = _timed_login_retry
R.manual_login_retry = _timed_login_retry

# --- 3. Login patch (React inputs) ----------------------------------------------
def _real_input(driver, css):
    best = None
    for e in driver.find_elements(By.CSS_SELECTOR, css):
        try:
            r = driver.execute_script(
                "const b=arguments[0].getBoundingClientRect();return [b.width,b.height];", e)
            if r and r[0] > 1 and r[1] > 1 and e.is_enabled():
                return e
            if best is None:
                best = e
        except Exception:
            pass
    return best


_REACT_SET = (
    "var el=arguments[0], v=arguments[1];"
    "var proto=Object.getPrototypeOf(el);"
    "var setter=Object.getOwnPropertyDescriptor(proto,'value').set;"
    "setter.call(el,'');el.dispatchEvent(new Event('input',{bubbles:true}));"
    "setter.call(el,v);"
    "el.dispatchEvent(new Event('input',{bubbles:true}));"
    "el.dispatchEvent(new Event('change',{bubbles:true}));"
)


def _fill(driver, el, value):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.4)
    try:
        el.click()
        el.send_keys(Keys.CONTROL + "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(value)
    except Exception:
        pass
    if (el.get_attribute("value") or "") != value:
        try:
            driver.execute_script(_REACT_SET, el, value)
        except Exception:
            driver.execute_script("arguments[0].value=arguments[1];", el, value)
    time.sleep(0.2)


# S10 (2026-10-14): at most ONE credential (vault) sign-in per process. A lapsed session signs in again once and the run
# continues; needing a second sign-in, or a sign-in that leaves the session signed out, ends the run with exit 45 (signed
# out / sign-in failed). That is not a wall: no marker, no hold. run_linkedin_supervised.py writes NEEDS_HUMAN and does not
# restart today.
_SIGNIN_EXIT = 45
_SIGNIN = {"used": 0}


def _signin_stop(why):
    msg = f"SIGN-IN STOP: {why}. Exit {_SIGNIN_EXIT}: YOUR TURN, sign in in the linkedin-1 profile."
    try:
        R.print_lg(msg)
    except Exception:
        pass
    _note(msg)
    raise SystemExit(_SIGNIN_EXIT)


def _patched_login_LN():
    d = R.driver
    d.get("https://www.linkedin.com/login")
    _check_wall()       # L2: a wall instead of the sign-in page ends the run before anything is typed
    if R.is_logged_in_LN():
        return R.print_lg("Already logged in via persistent session, skipping credential login!")
    if _SIGNIN["used"]:
        _signin_stop("the session lapsed again after this run's one vault sign-in")
    _SIGNIN["used"] += 1
    wait = WebDriverWait(d, 25)
    try:
        wait.until(lambda drv: _real_input(
            drv, "input[autocomplete='username'], input#username, input[name='session_key']"))
        email_el = _real_input(
            d, "input[autocomplete='username'], input#username, input[name='session_key']")
        pass_el = _real_input(
            d, "input[autocomplete='current-password'], input#password, input[name='session_password']")
        if not email_el or not pass_el:
            raise RuntimeError("login fields not found with current selectors")
        _fill(d, email_el, R.username)
        _fill(d, pass_el, R.password)
        btn = None
        for b in d.find_elements(By.CSS_SELECTOR, "button[type='submit']"):
            try:
                label = (b.text or b.get_attribute("aria-label") or "").lower()
                if b.is_displayed() and "sign in" in label:
                    btn = b
                    break
            except Exception:
                pass
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", btn or pass_el)
        time.sleep(0.3)
        if btn:
            btn.click()
        else:
            pass_el.send_keys(Keys.RETURN)
    except Exception as e:
        R.print_lg("Patched auto-login could not fill the form:", e)

    for _ in range(40):
        time.sleep(1)
        # L2: a checkpoint / challenge / captcha / add-phone page after sign-in ends the run (exit 48). It used to get a
        # 10-minute wait for a manual solve, after which the run carried on.
        _check_wall()
        try:
            url = d.current_url
        except Exception:
            url = ""
        if "/feed" in url:
            return R.print_lg("Login successful!")
    if R.is_logged_in_LN():
        return R.print_lg("Login successful!")
    # S10: still signed out after the 40 s loop. It used to wait 3 minutes for a manual sign-in and then carry on.
    _signin_stop("still signed out 40 s after the vault sign-in")


R.login_LN = _patched_login_LN

# --- 4b. Hardened discard: one stuck modal must never kill the run ---------------
try:
    _orig_discard_job = R.discard_job
except Exception:
    _orig_discard_job = None


def _hardened_discard_job():
    d = R.driver
    try:
        if _orig_discard_job is not None:
            try:
                _orig_discard_job()
            except Exception:
                pass
        # ESC pair (closes most Easy Apply modals to the Discard prompt)
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(d).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.5)
        except Exception:
            pass
        # Dismiss/X buttons (aria-label varies; span text is not always "Discard")
        for _xp in ("//button[contains(@aria-label,'Dismiss')]",
                    "//button[contains(@aria-label,'Close')]",
                    ".//span[normalize-space(.)='Discard']"):
            try:
                if _xp.startswith("//"):
                    _el = d.find_element(By.XPATH, _xp)
                else:
                    _el = d.find_element(By.XPATH, _xp)
                try:
                    _el.click()
                except Exception:
                    d.execute_script("arguments[0].click();", _el)
                time.sleep(0.5)
                break
            except Exception:
                continue
        # NOTE (2026-09-14 diag): DO NOT document.querySelectorAll(...).remove()
        # the modal/overlay nodes here. Ripping LinkedIn's SPA overlay singletons
        # out of the DOM corrupts its modal manager, so subsequent Easy Apply
        # clicks render a button-click with no modal -> consecutive modal-missing
        # cascade to GATE_STOPPED. ESC + Dismiss above is sufficient; if an
        # overlay is still stuck, reload the current job URL to reset SPA state.
        try:
            _leftover = d.execute_script(
                "return document.querySelectorAll('.jobs-easy-apply-modal, "
                ".artdeco-modal-overlay').length;")
            if _leftover:
                _note(f"discard: {_leftover} overlay node(s) still present; "
                      "reloading job page to reset SPA modal state.")
                try:
                    d.refresh()
                    time.sleep(3)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        _note(f"hardened discard raised (ignored): {e!r}")


R.discard_job = _hardened_discard_job

# --- 12. Official-count reconciliation (periodic, no AI) --------------------------
# Tally our tracker against the count LinkedIn itself shows. Best-effort scrape:
# each method used is recorded in the Excel Reconciliation sheet, so failed
# parses are visible and fixable without blocking anything.
import re as _re  # noqa: E402
import subprocess as _subprocess  # noqa: E402

_TRACKER_PATH = r"C:\Users\Admin\GitHub\job-tracker\track_job_applications.py"
_REC_STATE = {"last": 0.0, "first_done": False}
_REC_EVERY = 45 * 60
_REC_URL = "https://www.linkedin.com/jobs-tracker/"
_REC_EVIDENCE_DIR = os.path.join(REPO, "logs", "reconcile")


def _report_official(source, count, method):
    try:
        _subprocess.run(
            [sys.executable, _TRACKER_PATH, "official", "--source", source,
             "--count", "unknown" if count is None else str(count),
             "--method", (method or "")[:200]],
            timeout=90, stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL)
    except Exception as e:
        _note(f"tracker official report failed: {e!r}")


def _tracker_linkedin_count():
    """Mirror the tracker's own linkedin count (xlsx Applications + pending).

    The tracker derives Tracker Count server-side in cmd_official; this
    helper replicates that read so the suspect-0 flag below is accurate.
    Returns int, or None when unreadable (caller must NOT flag then).
    """
    try:
        import json as _json
        pend = 0
        try:
            _pp = r"C:\Users\Admin\GitHub\job-tracker\pending_rows.jsonl"
            if os.path.exists(_pp):
                with open(_pp, encoding="utf-8") as _fh:
                    for _line in _fh:
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _r = _json.loads(_line)
                        except Exception:
                            continue
                        if str(_r.get("source", "")).lower() == "linkedin":
                            pend += 1
        except Exception:
            pass
        _xp = r"C:/Users/Admin/GitHub/job_applications_tracker.xlsx"
        try:
            with open(r"C:\Users\Admin\GitHub\job-tracker\tracker_config.json",
                      encoding="utf-8") as _fh:
                _xp = (_json.load(_fh) or {}).get("xlsx_path", _xp)
        except Exception:
            pass
        _xlsx_n = None
        try:
            from openpyxl import load_workbook as _load_wb
            _wb = _load_wb(_xp, read_only=True, data_only=True)
            try:
                if "Applications" in _wb.sheetnames:
                    _ws = _wb["Applications"]
                    _rows = list(_ws.iter_rows(values_only=True))
                    if _rows and len(_rows) > 1:
                        _hdr = [str(_h) for _h in _rows[0]]
                        try:
                            _si = _hdr.index("Source")
                        except ValueError:
                            _si = 2
                        _xlsx_n = sum(
                            1 for _r in _rows[1:]
                            if _r and len(_r) > _si
                            and str(_r[_si] or "").lower() == "linkedin")
                    else:
                        _xlsx_n = 0
            finally:
                try:
                    _wb.close()
                except Exception:
                    pass
        except Exception as e:
            _note(f"tracker linkedin count: xlsx read skipped ({e!r})")
            _xlsx_n = None
        if _xlsx_n is None:
            # xlsx missing/locked: fall back to the LinkedIn CSV row count
            # (same source + track_from filter the tracker itself uses).
            try:
                import csv as _csv
                _cp = (r"C:\Users\Admin\GitHub\FORKED_Auto_job_applier_linkedIn"
                       r"\all excels\all_applied_applications_history.csv")
                _tf = "2026-09-10"
                try:
                    with open(r"C:\Users\Admin\GitHub\job-tracker"
                              r"\tracker_config.json", encoding="utf-8") as _fh:
                        _cfg = _json.load(_fh) or {}
                    _cp = ((_cfg.get("sources") or {}).get("linkedin")
                           or {}).get("path", _cp)
                    _tf = _cfg.get("track_from", _tf)
                except Exception:
                    pass
                _n = 0
                with open(_cp, encoding="utf-8", errors="replace",
                          newline="") as _fh:
                    for _r in _csv.DictReader(_fh):
                        _when = (_r.get("Date Applied") or "").strip()
                        if len(_when) < 10 or not _when[:4].isdigit():
                            continue
                        if _when[:10] < _tf:
                            continue
                        _n += 1
                return _n + pend
            except Exception as e:
                _note(f"tracker linkedin count: csv fallback skipped ({e!r})")
                return None
        return (_xlsx_n or 0) + pend
    except Exception as e:
        _note(f"tracker linkedin count failed: {e!r}")
        return None


# Specific formats first; the generic count-before-label comes last. ALL
# matches are collected and the max is taken (the old code took the FIRST
# match, which caught zero-state strings instead of the real total).
_REC_PATTERNS = (
    (r"applied\s*\(\s*(\d[\d,]{0,6})\s*\)", "Applied (N)"),
    # Label-then-count without parens, e.g. tab text "Applied 1,543"
    # (seen in logs\reconcile\ evidence 2026-09-11). Max-wins across all
    # patterns keeps the page total ahead of small stage/day counts.
    (r"(?:applied|applications?)\s*[:\-]?\s*(\d[\d,]{0,6})",
     "Applied N (label-first)"),
    (r"(\d[\d,]{0,6})\s*\+\s*(?:jobs?\s+)?(?:applied|applications?)",
     "N+ applied/applications"),
    (r"(?:of|of\s+about)\s+(\d[\d,]{0,6})\s+(?:job\s+)?applications?",
     "of N applications"),
    (r"(\d[\d,]{0,6})\s+(?:jobs?\s+)?(?:applied|applications?)",
     "N applied/applications"),
)

# Tab / stage / list-count elements that carry the real total on the
# React /jobs-tracker/ page. Parsed BEFORE the whole-body fallback.
_REC_TARGET_CSS = (
    "[aria-label*='pplied']",
    "[aria-label*='pplication']",
    "button.jobs-tracking__tab",
    ".jobs-tracking__tab",
    ".jobs-tracking-tab",
    "li.jobs-tracking__job-card",
    ".job-card-container",
    "li[data-job-id]",
    ".jobs-tracking-list__item",
)


def _rec_collect(text, tag_prefix):
    out = []
    for _pat, _tag in _REC_PATTERNS:
        try:
            for _m in _re.finditer(_pat, text or "", _re.IGNORECASE):
                try:
                    out.append((int(_m.group(1).replace(",", "")),
                                f"{tag_prefix}{_tag}"))
                except Exception:
                    continue
        except Exception:
            continue
    return out


def _parse_linkedin_official(d, tracker_count=None):
    targeted = []
    for _css in _REC_TARGET_CSS:
        try:
            _els = d.find_elements(By.CSS_SELECTOR, _css)
        except Exception:
            continue
        for _el in _els or []:
            try:
                _t = (_el.text or "").strip()
            except Exception:
                continue
            if not _t:
                continue
            targeted.extend(_rec_collect(_t, f"targeted element ({_css}): "))
    if targeted:
        _best = max(targeted, key=lambda _c: _c[0])
        _count, _method = _best
    else:
        try:
            _body = d.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            _body = ""
        _cands = _rec_collect(_body, "text regex on /jobs-tracker/: ")
        if not _cands:
            return None, "not parsed"
        _best = max(_cands, key=lambda _c: _c[0])
        _count, _method = _best
    if _count == 0 and tracker_count is not None and tracker_count > 0:
        _method = (f"suspect-0 (parsed 0 but tracker has {tracker_count}) | "
                   f"{_method}")
    return _count, _method


def _rec_ready(drv):
    """Data-bearing signal for the explicit wait on /jobs-tracker/."""
    try:
        _t = drv.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return False
    if len(_t) < 50:
        return False
    return bool(_re.search(r"applied|application|my\s+jobs|job\s+alert",
                           _t, _re.IGNORECASE))


def _rec_evidence(d, count, method):
    """Ground-truth dump on 0/None (mirrors _gate_evidence, text form)."""
    try:
        os.makedirs(_REC_EVIDENCE_DIR, exist_ok=True)
        _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            _url = d.current_url
        except Exception:
            _url = ""
        try:
            _body = d.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            _body = ""
        try:
            _src = d.page_source or ""
        except Exception:
            _src = ""
        with open(os.path.join(_REC_EVIDENCE_DIR, f"reconcile-{_ts}.txt"),
                  "w", encoding="utf-8") as _fh:
            _fh.write(f"url: {_url}\ncount: {count}\nmethod: {method}\n\n"
                      f"--- body[:4000] ---\n{_body[:4000]}\n\n"
                      f"--- page_source[:8000] ---\n{_src[:8000]}\n")
        _note(f"reconcile evidence dumped ({_REC_EVIDENCE_DIR}, "
              f"count={count}).")
    except Exception as e:
        _note(f"reconcile evidence dump failed: {e!r}")


def _reconcile_ln_periodic(force=False):
    now = time.time()
    if not force and (now - _REC_STATE["last"]) < _REC_EVERY:
        return
    _REC_STATE["last"] = now
    d = R.driver
    try:
        orig = d.current_window_handle
    except Exception as e:
        _report_official("linkedin", None, f"no window handle: {e!r}")
        return
    count, method = None, "error"
    try:
        d.switch_to.new_window("tab")
        try:
            d.get(_REC_URL)
            # L2: this tab's full page load is a mid-run check point too (the finally closes the tab). Checked right after
            # the load (a redirect to a wall) and again after the ready-wait (a wall the page draws itself).
            _check_wall()
            try:
                WebDriverWait(d, 25).until(_rec_ready)
            except Exception as e:
                _note(f"jobs-tracker wait timed out, parsing anyway: {e!r}")
            _check_wall()
            try:
                _tc = _tracker_linkedin_count()
            except Exception:
                _tc = None
            count, method = _parse_linkedin_official(d, tracker_count=_tc)
            if count is None or count == 0:
                try:
                    _rec_evidence(d, count, method)
                except Exception:
                    pass
        finally:
            try:
                d.close()
            except Exception:
                pass
            try:
                d.switch_to.window(orig)
            except Exception:
                pass
    except Exception as e:
        method = f"error: {e!r}"
    _report_official("linkedin", count, method)
    _note(f"official reconcile: linkedin count={count} via {method}")


# --- 4. Per-job session watchdog -------------------------------------------------
_orig_get_job_main_details = R.get_job_main_details


_TITLE_SKIPS_LOG = os.path.join(REPO, "logs", "linkedin_title_skips.jsonl")
_GATE_ERRORS = {"n": 0}


def _title_gate_error(where, e):
    """The gate never stops a run on a card it cannot read, but a gate that fails on EVERY card would silently do nothing: the first few
    errors of a run go to the hardening log."""
    _GATE_ERRORS["n"] += 1
    if _GATE_ERRORS["n"] <= 5:
        _note(f"title gate error in {where} ({_GATE_ERRORS['n']}): {type(e).__name__}: {str(e)[:160]}")


def _title_gate_on():
    """India pass only: the UAE pass (R.SEARCH_URL_EXTRA set by the overlay) has its own title gate (U3)."""
    return _FIT["rules"] is not None and not getattr(R, "SEARCH_URL_EXTRA", "")


def _title_gate_reasons(title):
    if not title or title == "Unknown" or not _title_gate_on():
        return []
    return _FIT["mod"].title_reasons(title, "linkedin", _FIT["rules"])


def _title_gate_log(job_id, title, company, reasons, stage):
    try:
        import json as _json
        with open(_TITLE_SKIPS_LOG, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "job_id": job_id, "title": title, "company": company,
                                  "reasons": reasons, "stage": stage, "rules": _FIT["rules"].version}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _title_gate_card(job, rejected_jobs):
    """Judge a job card by the title it shows, BEFORE any click or pacing wait (a skipped card costs about a second instead of 8-20 s plus
    a click). Returns get_job_main_details' tuple with skip=True when the title is ruled out, else None. Never raises: a card that cannot
    be read goes on to the normal path, where the parsed title is judged again (_title_gate_parsed)."""
    try:
        if not _title_gate_on():
            return None
        a = job.find_element(By.TAG_NAME, "a")
        title = (a.text or "").split("\n")[0].strip()
        if not title:                                   # the list renders only cards near the viewport: bring this one into view once
            R.scroll_to_view(R.driver, a, True)
            time.sleep(0.4)
            title = (a.text or "").split("\n")[0].strip()
        reasons = _title_gate_reasons(title)
        if not reasons:
            return None
        job_id = job.get_dom_attribute("data-occludable-job-id") or "unknown"
        try:
            company = job.find_element(By.CLASS_NAME, "artdeco-entity-lockup__subtitle").text.split("\n")[0].split(" · ")[0].strip()
        except Exception:
            company = ""
        rejected_jobs.add(job_id)
        R.print_lg(f'Title gate: skipping "{title}" ({"; ".join(reasons)}). Job ID: {job_id}!')
        _title_gate_log(job_id, title, company, reasons, "card")
        return (job_id, title, company or "Unknown", "", "", True)
    except Exception as e:
        if type(e).__name__ not in ("NoSuchElementException", "StaleElementReferenceException"):    # an unreadable or stale card is normal
            _title_gate_error("card", e)
        return None


def _title_gate_parsed(res, rejected_jobs):
    """The same judgement on the title the bot parsed after its own click (a card the early read could not use). Never raises."""
    try:
        job_id, title, company, location, style, skip = res
        if skip:
            return res
        reasons = _title_gate_reasons(title)
        if not reasons:
            return res
        rejected_jobs.add(job_id)
        R.print_lg(f'Title gate: skipping "{title} | {company}" ({"; ".join(reasons)}). Job ID: {job_id}!')
        _title_gate_log(job_id, title, company, reasons, "parsed")
        return (job_id, title, company, location, style, True)
    except Exception as e:
        _title_gate_error("parsed", e)
        return res


def _watched_get_job_main_details_inner(job, blacklisted_companies, rejected_jobs):
    try:
        # L2: a verification wall mid-run ends the run (exit 48; SystemExit passes the except below). It used to get a
        # 10-minute pause, after which the run carried on.
        _check_wall()
        if not R.is_logged_in_LN():
            R.print_lg("Session looks logged out mid-run — re-logging in.")
            try:
                R.login_LN()
            except Exception as e:
                R.print_lg("Re-login raised:", e)
            if not R.is_logged_in_LN():
                R.print_lg("Re-login failed — skipping this job.")
                return ("unknown", "Unknown", "Unknown", "", "", True)
            try:
                _GATE["misses"] = 0  # fresh session → fresh gate counter
            except Exception:
                pass
    except Exception as e:
        R.print_lg("Watchdog check failed (fail-open):", e)
    try:
        _pace()  # velocity pacing: space out apply clicks (§11)
    except Exception:
        pass
    try:
        _force = not _REC_STATE["first_done"]
        _reconcile_ln_periodic(force=_force)
        _REC_STATE["first_done"] = True
    except Exception as e:
        _note(f"reconcile skipped: {e!r}")
    try:
        _is_lim, _reason = R.check_daily_limit_signal(R.driver)
        if _is_lim:
            R.record_daily_limit_reached(_reason)
            _note(f"daily limit signal confirmed on page ({_reason!r}) — stopping loudly.")
            raise SystemExit(44)
    except SystemExit:
        raise
    except Exception:
        pass
    try:
        return _orig_get_job_main_details(job, blacklisted_companies, rejected_jobs)
    except Exception as e:
        # Killer-A armor: a stuck modal overlay makes the next card
        # unclickable (ElementClickIntercepted, a WebDriverException that used
        # to abort the whole run). Detach it and skip ONE job instead.
        R.print_lg(f"Job-card click failed ({type(e).__name__}) — hardened "
                   "discard + skip this job instead of aborting run.")
        try:
            _hardened_discard_job()
        except Exception:
            pass
        try:
            return _orig_get_job_main_details(job, blacklisted_companies, rejected_jobs)
        except Exception:
            return ("unknown", "Unknown", "Unknown", "", "", True)


_RENDER_MISS = {"n": 0}


def _render_job_card(job) -> None:
    """2026-10-21: LinkedIn draws a result card only while it is near the viewport; the others are empty placeholders with no link in them. Before this,
    such a card cost the pacing wait (8-20 s), a failed click, a discard and a second failed click: about 25 s, 2,085 of them in the three counts from
    19 Oct (47% of the 20 Oct count), 28% of all cards in one run; the UAE pass, which draws each card first, saw 0.5%. So every card is scrolled to, and
    given up to ~2 s to show its link, before anything is decided from it (linkedin_uae_overlay._render_card does the same). A card that is already
    drawn costs one script call and one lookup. A card that still shows no link after 2 s goes on to the old path, and the first five such misses of a
    run (then every 50th) are noted in the hardening log. Never raises."""
    try:
        R.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", job)
    except Exception:
        return
    # 2026-10-22: the first version waited for the link only. Live check of the 22 Oct window: 185 "Job-card click failed" (all NoSuchElementException) with
    # almost no render notes, so the link was there and the card's subtitle (company) was not drawn yet; get_job_main_details needs both. Wait for both.
    have = (False, False)
    for _ in range(10):
        try:
            have = (bool(job.find_elements(By.TAG_NAME, "a")), bool(job.find_elements(By.CLASS_NAME, "artdeco-entity-lockup__subtitle")))
        except Exception:
            return
        if all(have):
            return
        time.sleep(0.2)
    _RENDER_MISS["n"] += 1
    if _RENDER_MISS["n"] <= 5 or _RENDER_MISS["n"] % 50 == 0:
        _note(f"render: card still has no {'link' if not have[0] else 'subtitle'} after 2 s (miss {_RENDER_MISS['n']} this run)")


def _watched_get_job_main_details(job, blacklisted_companies, rejected_jobs):
    _render_job_card(job)                               # 2026-10-21: draw the card first, so an undrawn one is read instead of failing after the pacing wait
    pre = _title_gate_card(job, rejected_jobs)          # 2026-10-18: title gate first, before the pacing wait and the click
    if pre is not None:
        return pre
    return _title_gate_parsed(_watched_get_job_main_details_inner(job, blacklisted_companies, rejected_jobs), rejected_jobs)


R.get_job_main_details = _watched_get_job_main_details

# --- 5. Safe answers: explicit mappings, no random selects -----------------------
_orig_answer_common = R.answer_common_questions


def _safe_answer_common(label, answer):
    low = (label or "").lower()
    try:
        if "phone country code" in low:
            return "India (+91)"
        if low.strip() == "email address" or ("email" in low and "address" in low):
            return R.username
        if low.strip() in ("phone", "mobile phone number", "phone number") or \
                ("phone" in low and "country" not in low and "number" in low):
            return getattr(R, "phone_number", answer)
        if "country" in low:
            return getattr(R, "country", answer)
        if "state" in low or "province" in low:
            return getattr(R, "state", answer)
        if low.strip() in ("city", "location", "current location", "current city"):
            return getattr(R, "current_city", answer) or answer
    except Exception:
        pass
    return _orig_answer_common(label, answer)


R.answer_common_questions = _safe_answer_common

if _Select is not None:
    _orig_select_by_index = _Select.select_by_index
    _orig_select_by_visible_text = _Select.select_by_visible_text

    def _select_label(select_el):
        """Best-effort Easy Apply question label for a <select> element."""
        try:
            _form = select_el.find_element(
                By.XPATH, "./ancestor::div[@data-test-form-element][1]")
            try:
                return (_form.find_element(By.TAG_NAME, "label").text or "").strip()
            except Exception:
                return (_form.text or "").strip().split("\n")[0]
        except Exception:
            return ""

    def _safe_select_option(label, options_text):
        """Deterministic safe choice for known selects; None = truly unknown."""
        low = (label or "").lower()

        def _find(*needles, exclude=()):
            for _o in options_text:
                _ol = (_o or "").lower()
                if any(_n in _ol for _n in needles) and not any(_x in _ol for _x in exclude):
                    return _o
            return None

        try:
            if "phone country code" in low or ("country code" in low and "phone" in low):
                return _find("india (+91)", "+91", "india")
            if low.strip() == "email address" or ("email" in low and "address" in low):
                for _o in options_text:
                    if _o and _o.strip() == R.username:
                        return _o
                return _find("@")
            if low.strip() == "country" or low.startswith("country"):
                _want = str(getattr(R, "country", "India"))
                for _o in options_text:
                    if _o and _o.strip().lower() == _want.lower():
                        return _o
                return None
            if "state" in low or "province" in low:
                _want = str(getattr(R, "state", ""))
                if _want:
                    for _o in options_text:
                        if _o and _want.lower() in _o.lower():
                            return _o
                return None
            if "sponsorship" in low or "visa" in low:
                _want = str(getattr(R, "require_visa", ""))
                if _want:
                    for _o in options_text:
                        if _o and _want.lower() in _o.lower():
                            return _o
                return None
        except Exception:
            return None
        return None

    def _guarded_select_by_index(self, index):
        # Replaces the random-answer fallback: resolve known selects
        # deterministically (country-code/email/country/state/visa), discard
        # the job only when truly unknown.
        try:
            _label = _select_label(self._el)
            _opts = [(o.text or "") for o in self.options]
            _safe = _safe_select_option(_label, _opts)
            if _safe is not None:
                _note(f"select resolved '{_label[:60]}' -> '{_safe[:40]}' (was random).")
                return _orig_select_by_visible_text(self, _safe)
        except Exception as e:
            _note(f"select resolver failed ({e!r}); discarding job.")
        raise RuntimeError("unmapped select option — discarding job instead of guessing")

    def _guarded_select_by_visible_text(self, text):
        # Intercepts the placeholder-keep path: runAiBot re-selects
        # "Select an option" for email/phone labels, leaving required fields
        # empty (→ 15-loop stuck → discard). Substitute the safe option.
        try:
            if (text or "").strip().lower() == "select an option":
                _label = _select_label(self._el)
                _opts = [(o.text or "") for o in self.options]
                _safe = _safe_select_option(_label, _opts)
                if _safe is not None:
                    _note(f"placeholder replaced '{_label[:60]}' -> '{_safe[:40]}'.")
                    return _orig_select_by_visible_text(self, _safe)
        except Exception as e:
            _note(f"placeholder interceptor failed ({e!r}); keeping original.")
        return _orig_select_by_visible_text(self, text)

    _Select.select_by_index = _guarded_select_by_index
    _Select.select_by_visible_text = _guarded_select_by_visible_text
    _note("Select guarded (safe-default resolution + discard-on-unknown).")

# --- 6. Submit-flow click retry ---------------------------------------------------
try:
    _orig_wait_span_click = _clickers.wait_span_click
except Exception:
    _orig_wait_span_click = None

if _orig_wait_span_click is not None:
    def _retry_wait_span_click(driver, text, timeout=5.0, click=True, scroll=True, scrollTop=False):
        res = _orig_wait_span_click(driver, text, timeout, click, scroll, scrollTop)
        if (res is False and click and text in
                ("Submit application", "Review", "Done", "Next", "Discard")):
            time.sleep(1.5)
            try:
                return _orig_wait_span_click(driver, text, timeout + 3, click, scroll, scrollTop)
            except Exception:
                return False
        return res

    _clickers.wait_span_click = _retry_wait_span_click
    R.wait_span_click = _retry_wait_span_click

# --- 8. Stale→NoSuchElement mapping (submit rescue) -------------------------------
# Tracked Easy Apply flow handles NoSuchElement (errored="nose" → still tries
# Review/Submit with fresh driver refs) but lets StaleElementReference kill the
# job outright. The modal re-renders mid-flow, so map stale→nose after 1 retry.
try:
    from selenium.webdriver.remote.webelement import WebElement as _WE
    from selenium.common.exceptions import (
        NoSuchElementException as _NSEE,
        StaleElementReferenceException as _SERE,
    )

    _orig_we_find = _WE.find_element

    def _stale_safe_find(self, *args, **kwargs):
        try:
            return _orig_we_find(self, *args, **kwargs)
        except _SERE:
            time.sleep(1.0)
            try:
                return _orig_we_find(self, *args, **kwargs)
            except _SERE:
                raise _NSEE("mapped from stale element (auto-refreshed once)")

    _WE.find_element = _stale_safe_find
    _note("WebElement.find_element stale-guarded.")
except Exception as e:
    _note(f"stale guard skipped: {e!r}")

# --- 9. Resume step: select existing resume instead of re-uploading ---------------
# Proven stall (screenshot 2026-09-08 21:33): fresh send_keys upload leaves a
# permanent spinner + Next disabled. LinkedIn already holds the PDF, so prefer
# the existing radio and upload only when no resume is on file.
_orig_upload_resume = R.upload_resume


_resume_upload_tried = False   # one fresh upload per process (the 09-08 stall risk is per attempt)


def _resume_label(modal, radio) -> str:
    try:
        rid = radio.get_attribute("id")
        lab = modal.find_element(By.XPATH, f".//label[@for='{rid}']") if rid else None
        return " ".join((lab.text if lab is not None else "").split())
    except Exception:
        return ""


def _patched_upload_resume(modal, resume):
    # 2026-10-01: "Previous resume (already selected)" meant whatever LinkedIn last used, never
    # checked against the current resume. Prefer the stored resume whose name matches
    # default_resume_path; if LinkedIn has no copy yet, upload it once per process.
    global _resume_upload_tried
    try:
        if not modal.find_elements(By.NAME, "file"):
            return _orig_upload_resume(modal, resume)  # not a resume step
        _radios = modal.find_elements(By.XPATH, ".//input[@type='radio']")
        _want = os.path.splitext(os.path.basename(resume or ""))[0].lower()
        _labels = [(_r, _resume_label(modal, _r)) for _r in _radios]
        R.print_lg("Resume step: stored resumes " + " | ".join(l[:80] for _, l in _labels)
                   + f" ; want '{_want}'")
        _match = next((r for r, l in _labels if _want and _want in l.lower().replace(" ", "_")), None)
        if _match is not None:
            try:
                if not _match.is_selected():
                    _rid = _match.get_attribute("id")
                    modal.find_element(By.XPATH, f".//label[@for='{_rid}']").click()
                    time.sleep(1.0)
                return True, os.path.basename(resume)
            except Exception as e:
                R.print_lg("Selecting the current resume failed:", e)
        elif _want and not _resume_upload_tried:
            _resume_upload_tried = True
            R.print_lg(f"Current resume '{_want}' is not stored on LinkedIn yet; uploading it once.")
            return _orig_upload_resume(modal, resume)
        for _r in _radios:
            try:
                if _r.is_selected():
                    return True, "Previous resume (already selected)"
            except Exception:
                pass
        if _radios:
            try:
                _rid = _radios[0].get_attribute("id")
                _lab = (modal.find_element(By.XPATH, f".//label[@for='{_rid}']")
                        if _rid else None)
                if _lab is not None:
                    _lab.click()
                else:
                    _radios[0].click()
                time.sleep(1.0)
                return True, "Previous resume (selected existing)"
            except Exception as e:
                R.print_lg("Existing-resume select failed, falling back to upload:", e)
    except Exception as e:
        R.print_lg("Resume pre-check failed, falling back to upload:", e)
    return _orig_upload_resume(modal, resume)


R.upload_resume = _patched_upload_resume
_note("upload_resume patched (prefer existing resume).")

# runAiBot turns useNewResume off after the first successful resume step, after which LinkedIn's
# preselection (last-used resume) decides silently and the history CSV says "Previous resume".
# Re-arm it on every question page so each job's resume step is checked, selected and recorded.
_orig_answer_questions = R.answer_questions


def _answer_questions_recheck_resume(*args, **kwargs):
    R.useNewResume = True
    return _orig_answer_questions(*args, **kwargs)


R.answer_questions = _answer_questions_recheck_resume
_note("resume step re-checked on every job.")

# --- 10. Modal-missing gate detector ----------------------------------------------
# When LinkedIn throttles/gates apply actions, the Easy Apply button still
# renders but no modal ever opens (TimeoutException, empty message). Grinding
# that state for 16h is pure account-risk. Back off, dump evidence, then stop
# loudly (SystemExit → finally totals → supervisor sees GATE_STOPPED).
_GATE = {"misses": 0}
_GATE_K = 8
_GATE_DIR = os.path.join(REPO, "logs", "modal-missing")


def _dom_diagnostics():
    """Capture button/modal state to distinguish SPA-corruption from real gates."""
    _diag = {}
    try:
        d = R.driver
        _diag["url"] = d.current_url
    except Exception:
        _diag["url"] = "?"
    try:
        _diag["apply_buttons"] = d.execute_script(
            "return Array.from(document.querySelectorAll('button.jobs-apply-button'))"
            ".map(b=>({text:(b.innerText||'').slice(0,60),"
            " aria:b.getAttribute('aria-label'), cls:b.className}));")
    except Exception as _e:
        _diag["apply_buttons"] = f"err:{_e!r}"
    try:
        _diag["modal_count"] = d.execute_script(
            "return document.querySelectorAll('.jobs-easy-apply-modal').length;")
        _diag["artdeco_modal"] = d.execute_script(
            "return document.querySelectorAll('.artdeco-modal').length;")
        _diag["overlay"] = d.execute_script(
            "return document.querySelectorAll('.artdeco-modal-overlay').length;")
    except Exception:
        pass
    return _diag


def _gate_evidence(tag):
    try:
        os.makedirs(_GATE_DIR, exist_ok=True)
        _ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        try:
            R.driver.save_screenshot(os.path.join(_GATE_DIR, f"{tag}-{_ts}.png"))
        except Exception:
            pass
        try:
            _html = R.driver.page_source or ""
            with open(os.path.join(_GATE_DIR, f"{tag}-{_ts}.html"), "w",
                      encoding="utf-8") as _fh:
                _fh.write(_html)
        except Exception:
            pass
        try:
            import json as _json
            with open(os.path.join(_GATE_DIR, f"{tag}-{_ts}.json"), "w",
                      encoding="utf-8") as _fh:
                _fh.write(_json.dumps(_dom_diagnostics(), indent=2, default=str))
        except Exception:
            pass
    except Exception:
        pass


_orig_try_xp = _clickers.try_xp


def _logged_try_xp(driver, xpath, click=True):
    """Log Easy Apply click candidates so silent no-op clicks are diagnosable."""
    try:
        if "jobs-apply-button" in (xpath or "") and click:
            try:
                _cands = driver.execute_script(
                    "return Array.from(document.querySelectorAll('button.jobs-apply-button'))"
                    ".map((b,i)=>({i, text:(b.innerText||'').slice(0,40),"
                    " aria:(b.getAttribute('aria-label')||'').slice(0,80),"
                    " disp:(b.offsetParent!==null),"
                    " rect:b.getBoundingClientRect().width+'x'+b.getBoundingClientRect().height}));")
                _note(f"easy-apply click candidates: {_cands}")
            except Exception as _e:
                _note(f"easy-apply candidates unreadable: {_e!r}")
    except Exception:
        pass
    return _orig_try_xp(driver, xpath, click)


_clickers.try_xp = _logged_try_xp
R.try_xp = _logged_try_xp
_note("try_xp wrapped with click-candidate logging.")


_orig_find_by_class = _clickers.find_by_class


def _gate_find_by_class(driver, class_name, timeout=5.0):
    try:
        _el = _orig_find_by_class(driver, class_name, timeout)
        if class_name == "jobs-easy-apply-modal" and _GATE["misses"]:
            _note(f"modal found again — gate counter reset (was {_GATE['misses']}).")
            _GATE["misses"] = 0
        return _el
    except Exception:
        if class_name == "jobs-easy-apply-modal":
            try:
                _is_lim, _reason = R.check_daily_limit_signal(driver)
                if _is_lim:
                    R.record_daily_limit_reached(_reason)
                    _note(f"daily limit signal confirmed on page ({_reason!r}) — stopping loudly.")
                    raise SystemExit(44)
            except SystemExit:
                raise
            except Exception:
                pass
            try:
                _logged_in = R.is_logged_in_LN()
            except Exception:
                _logged_in = True
            if _logged_in:
                _GATE["misses"] += 1
                _n = _GATE["misses"]
                _note(f"Easy Apply modal missing after click ({_n}/{_GATE_K}, logged in).")
                _gate_evidence(f"modal-missing-{_n}")
                if _n in (3, 4, 5):
                    _note("backing off 60s (possible throttle).")
                    time.sleep(60)
                elif _n in (6, 7):
                    _note("backing off 300s (likely apply gate).")
                    time.sleep(300)
                elif _n >= _GATE_K:
                    R.print_lg(f"GATE_STOPPED: modal missing {_n}x consecutively while logged in. "
                               "Treating as LinkedIn apply gate/limit — stopping loudly.")
                    try:
                        with open(os.path.join(REPO, "logs", "GATE_STOPPED"), "w",
                                  encoding="utf-8") as _fh:
                            _fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} modal-missing x{_n}\n")
                    except Exception:
                        pass
                    raise SystemExit(42)
        raise


_clickers.find_by_class = _gate_find_by_class
R.find_by_class = _gate_find_by_class
_note("modal gate detector armed (K=8).")

# --- 11. Pacing (unlimited hourly applies) -------------------------------------------
import random as _random  # noqa: E402

_JOB_GAP = (8.0, 20.0)


def _pace():
    try:
        time.sleep(_random.uniform(*_JOB_GAP))
    except Exception:
        pass


_orig_submitted_jobs = R.submitted_jobs


_JOB_EVIDENCE_DIR = os.path.join(REPO, "logs", "job-evidence")


def _job_evidence(tag, job_id=""):
    """Per-job screenshot + full HTML + DOM diagnostics for success/fail compare."""
    try:
        os.makedirs(_JOB_EVIDENCE_DIR, exist_ok=True)
        _ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        _safe = "".join(c if c.isalnum() else "_" for c in str(job_id))[:40]
        _base = f"{tag}-{_safe}-{_ts}" if _safe else f"{tag}-{_ts}"
        try:
            R.driver.save_screenshot(os.path.join(_JOB_EVIDENCE_DIR, f"{_base}.png"))
        except Exception:
            pass
        try:
            _html = R.driver.page_source or ""
            with open(os.path.join(_JOB_EVIDENCE_DIR, f"{_base}.html"), "w",
                      encoding="utf-8") as _fh:
                _fh.write(_html)
        except Exception:
            pass
        try:
            import json as _json
            with open(os.path.join(_JOB_EVIDENCE_DIR, f"{_base}.json"), "w",
                      encoding="utf-8") as _fh:
                _fh.write(_json.dumps(_dom_diagnostics(), indent=2, default=str))
        except Exception:
            pass
    except Exception:
        pass


def _capped_submitted_jobs(*args, **kwargs):
    try:
        _jid = args[0] if args else kwargs.get("job_id", "")
    except Exception:
        _jid = ""
    _job_evidence("success", _jid)
    return _orig_submitted_jobs(*args, **kwargs)


R.submitted_jobs = _capped_submitted_jobs
_note(f"pacing {_JOB_GAP}s/job armed (hourly cap disabled/unlimited).")

try:
    _orig_failed_job = R.failed_job

    def _evidenced_failed_job(job_id="", *args, **kwargs):
        # 2026-10-13: a job skipped by the mandatory "Actively reviewing applicants" filter is not a failure. It gets neither the page dump
        # (about 2.5 MB: screenshot + HTML + JSON, for more than half of all cards) nor a row in the failed-jobs list; runAiBot.py
        # writes its own one-line record to logs/linkedin_filter_skips.jsonl.
        _err = args[3] if len(args) > 3 else kwargs.get("error", "")
        if str(_err).startswith("Filter:"):
            return None
        try:
            _job_evidence("failed", job_id)
        except Exception:
            pass
        return _orig_failed_job(job_id, *args, **kwargs)

    R.failed_job = _evidenced_failed_job
    _note("failed_job wrapped with per-job evidence screenshots.")
except Exception as _e:
    _note(f"failed_job evidence wrap skipped: {_e!r}")

# --- 7. Unattended config enforcement + resume check ------------------------------
R.pause_before_submit = False
R.pause_at_failed_question = False
try:
    R.pause_after_filters = False
except Exception:
    pass
R.close_tabs = True  # external links are still recorded in CSV; avoids >10-tab stall
_note("close_tabs=True, pauses=False enforced.")

try:
    from config.questions import default_resume_path as _resume
    if not os.path.exists(_resume):
        R.useNewResume = False
        _note(f"default resume missing ({_resume}) — continuing with LinkedIn resume.")
except Exception as e:
    _note(f"resume check skipped: {e!r}")

# --- 12. UAE pass overlay ------------------------------------------------------------
# Inert unless logs/LINKEDIN_UAE_ARMED holds a valid, unexpired arming (written on the user's go-ahead). Any failure to
# load it must never touch the India run, so everything here is best effort.
try:
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import linkedin_uae_overlay as _uae
    _uae.install(R, _note)
except Exception as _e:
    _note(f"UAE overlay not installed: {_e!r}")

_note(f"hardened runner ready (user={R.username!r} from {_src}).")
# L2 backstop: once a wall has stopped the run, the process ends with 48 whatever happened inside the bot. An exception raised
# in a finally block on the way out (the Easy Apply flow has one) can replace the SystemExit, or another exit code can win.
# Without a wall, every exit and exception passes through unchanged.
try:
    R.main()
except SystemExit as _exit:
    if _WALL_STATE["stopped"] and _exit.code != _WALL_EXIT:
        _note(f"wall stop ({_WALL_STATE['stopped']}) ends this run: exit {_WALL_EXIT}, not {_exit.code}.")
        sys.exit(_WALL_EXIT)
    raise
except BaseException as _exc:
    if _WALL_STATE["stopped"]:
        _note(f"wall stop ({_WALL_STATE['stopped']}) ends this run: exit {_WALL_EXIT}, not {type(_exc).__name__}.")
        sys.exit(_WALL_EXIT)
    raise
if _WALL_STATE["stopped"]:
    _note(f"wall stop ({_WALL_STATE['stopped']}) was swallowed inside the bot; exiting {_WALL_EXIT}.")
    sys.exit(_WALL_EXIT)
