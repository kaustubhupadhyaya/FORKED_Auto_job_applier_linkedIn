"""Untracked supervisor: keeps the LinkedIn bot applying across crashes.

- Launches run_linkedin_local.py as a child process (separate browser/session).
- Restarts it when it dies from renderer crashes / overlay kills.
- STOPS (no restart) when: daily Easy Apply limit reached, or two consecutive
  runs apply nothing (persistent breakage → needs a human), or 6 restarts used,
  or LinkedIn showed a verification wall (exit 48, or a wall recorded in
  logs/wall-evidence during the attempt), or the sign-in failed (exit 45): each of
  the last two → NEEDS_HUMAN, no restart today.
- A run that ends on a local-network error (net::ERR_NAME_NOT_RESOLVED & co: the home internet is down) is not a breakage:
  the supervisor waits for LinkedIn to be reachable (linkedin_net.py, up to 8 h; logs/NETWORK_DOWN while it waits, one toast
  after 30 min), then relaunches without using the restart budget or the zero-apply streak. Six such runs in a row that saved
  nothing still stop it (NEEDS_HUMAN), as does an outage past 8 h.
- Single-instance guarded via supervisor.lock.
"""
import os
import re
import sys
import time
import subprocess
from datetime import datetime

import linkedin_net

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)
LOCK = os.path.join(REPO, "logs", "supervisor.lock")
SLOG = os.path.join(REPO, "logs", "supervisor.log")
LOG = os.path.join(REPO, "logs", "log.txt")
PYTHON = os.path.join(REPO, ".venv", "Scripts", "python.exe")
MAX_RESTARTS = 6
GATE_MARKER = os.path.join(REPO, "logs", "GATE_STOPPED")
# 2026-09-30: written on every "needs a human" stop; linkedin_keepalive.ps1 does not relaunch
# while today's marker exists (before this, a failed browser launch looped all night).
NEEDS_HUMAN = os.path.join(REPO, "logs", "NEEDS_HUMAN")
LAUNCH_FAILED_RC = 43   # run_linkedin_local.py: Chrome/driver could not start
WALL_RC = 48            # run_linkedin_local.py: LinkedIn verification wall (L2, 2026-10-13); it wrote logs/LINKEDIN_WALL_<date>
WALL_EVIDENCE_DIR = os.path.join(REPO, "logs", "wall-evidence")   # run_linkedin_local.py: wall-<time>-<where>.json per wall
SIGNIN_RC = 45          # run_linkedin_local.py: signed out, and its one vault sign-in failed or was already used (2026-10-14)
# 2026-10-02: at ~260 MB of free RAM Chrome could not start (rc 43) and that single failure parked
# the bot for the rest of the day. Now: wait (bounded) for free memory before every launch, and
# retry rc 43 a few times, 5 minutes apart, before asking for a human.
RAM_MIN_MB = 1024
RAM_WAIT_MAX_MIN = 20
RAM_POLL_SEC = 30
LAUNCH_RETRIES = 3
LAUNCH_RETRY_WAIT_SEC = 300
NET_MARKER = os.path.join(REPO, "logs", "NETWORK_DOWN")   # 2026-10-17: present only while the supervisor waits for the home internet
JOB_TRACKER_DIR = r"C:\Users\Admin\GitHub\job-tracker"     # portal_kit.site_guard.notify_once: the toast registry shared with the keepalive
TRACKER_PATH = r"C:\Users\Admin\GitHub\job-tracker\track_job_applications.py"
_tracker_proc = None


def sync_tracker():
    """Refresh the Excel tracker in the background. 2026-09-30: this used to block the supervisor
    (subprocess.run, 120 s timeout) before every launch, and the timeout killed the tracker,
    possibly mid-save. Now: start it if none is running, never wait, never kill."""
    global _tracker_proc
    try:
        if _tracker_proc is not None and _tracker_proc.poll() is None:
            return
        _tracker_proc = subprocess.Popen([PYTHON, TRACKER_PATH, "sync"], cwd=REPO,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        slog(f"tracker sync could not start: {e!r}")


def needs_human(reason):
    slog(reason)
    try:
        with open(NEEDS_HUMAN, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {reason}\n")
    except Exception:
        pass


def net_toast(minutes):
    """One toast per day for a long internet outage: the same registry and key scheme as the keepalive's toasts. Raises on failure
    (linkedin_net logs it and carries on waiting)."""
    if JOB_TRACKER_DIR not in sys.path:
        sys.path.insert(0, JOB_TRACKER_DIR)
    from portal_kit import site_guard
    site_guard.notify_once("linkedin:NETWORK_DOWN", "LinkedIn bot waiting: no internet",
                           f"LinkedIn has been unreachable for {minutes:.0f} min. The bot relaunches by itself when the connection is "
                           "back. Details: logs\\supervisor.log in the LinkedIn bot folder.", by="linkedin_supervisor")


def handle_outage(gained, what):
    """A run ended because the home internet was down (what says how). True: LinkedIn is reachable again, relaunch; nothing was counted against
    the restart budget or the zero-apply streak. False: NEEDS_HUMAN was written, stop. Progress still decides: NET_IDLE_RUN_LIMIT of these in
    a row, none of them having saved an application, stop the supervisor, and so does an outage past linkedin_net.NET_WAIT_MAX_MIN."""
    global net_idle_runs
    net_idle_runs = 0 if (gained and gained > 0) else net_idle_runs + 1
    if net_idle_runs >= linkedin_net.NET_IDLE_RUN_LIMIT:
        needs_human(f"{net_idle_runs} runs in a row ended on a network error and saved nothing — NOT restarting; "
                    "see logs/log.txt (net::ERR_...) and the internet connection.")
        return False
    slog(f"{what}.")
    sweep_stale_browsers()      # no Chrome idling through the outage on a machine that is short of memory
    down_min = linkedin_net.wait_until_reachable(slog, NET_MARKER, toast=net_toast)
    if down_min is None:
        needs_human(f"LinkedIn unreachable for {linkedin_net.NET_WAIT_MAX_MIN} min (home internet down?) — NOT restarting. "
                    "Once the connection is back, delete logs/NEEDS_HUMAN.")
        return False
    slog(f"relaunching in 60 s (runs in a row that ended on a network error without a saved application: "
         f"{net_idle_runs}/{linkedin_net.NET_IDLE_RUN_LIMIT}; restart budget unchanged at {restarts}/{MAX_RESTARTS}).")
    time.sleep(60)
    return True


def _pid_alive(pid):
    """os.kill(pid, 0) is NOT a liveness probe on Windows (signal 0 == CTRL_C_EVENT)."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False
YIELD_WINDOW_MIN = 180       # rolling window for yield check
YIELD_MIN_APPLIES = 1        # stop if fewer applies than this per window
POLL_SEC = 600               # mid-run progress poll interval


def gate_marker_fresh(since_ts):
    try:
        return os.path.exists(GATE_MARKER) and os.path.getmtime(GATE_MARKER) >= since_ts
    except Exception:
        return False


def wall_recorded_since(since_ts):
    """True when run_linkedin_local.py recorded a verification wall (logs/wall-evidence/wall-*.json) at or after since_ts,
    whatever exit code the run ended with: the supervisor never restarts into a recorded wall."""
    try:
        with os.scandir(WALL_EVIDENCE_DIR) as entries:
            return any(e.name.startswith("wall-") and e.name.endswith(".json") and e.stat().st_mtime >= since_ts
                       for e in entries)
    except Exception:
        return False


def slog(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [supervisor] {msg}"
    try:
        os.makedirs(os.path.dirname(SLOG), exist_ok=True)
        with open(SLOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def saved_count():
    try:
        n = 0
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "Successfully saved" in line:
                    n += 1
        return n
    except Exception:
        return -1


def log_tail_has(needle, lines=60):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
        return any(needle in ln for ln in tail)
    except Exception:
        return False


def parse_last_run_stats():
    """Return (applied, failed) from the most recent totals block, or (None, None)."""
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        m = list(re.finditer(r"Jobs Easy Applied:\s+(\d+)\s*\nExternal job links collected:\s+(\d+)"
                             r"[\s\S]{0,400}?Failed jobs:\s+(\d+)", content))
        if not m:
            return None, None
        last = m[-1]
        return int(last.group(1)) + int(last.group(2)), int(last.group(3))
    except Exception:
        return None, None


def free_ram_mb():
    """Available physical memory in MB (what Task Manager calls Available), or None if unreadable."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None


def wait_for_ram():
    """Bounded: waits at most RAM_WAIT_MAX_MIN for RAM_MIN_MB of free memory, then launches anyway
    (the rc 43 retry in the main loop is the second line of defence). Never an open loop."""
    free = free_ram_mb()
    if free is None or free >= RAM_MIN_MB:
        return
    slog(f"free RAM {free} MB < {RAM_MIN_MB} MB - waiting up to {RAM_WAIT_MAX_MIN} min before launching.")
    deadline = time.time() + RAM_WAIT_MAX_MIN * 60
    while time.time() < deadline:
        time.sleep(RAM_POLL_SEC)
        free = free_ram_mb()
        if free is None or free >= RAM_MIN_MB:
            slog(f"free RAM {free} MB - launching.")
            return
    slog(f"free RAM still {free} MB after {RAM_WAIT_MAX_MIN} min - launching anyway (bounded wait).")


def sweep_stale_browsers():
    """Close Chrome processes still holding the bot's profile (linkedin-1): a crashed or half-started run leaves them behind, and a new Chrome
    on the same profile then fails with "DevToolsActivePort file doesn't exist". The keepalive does the same before it starts a supervisor.
    Only called when our own child has already exited. Never raises."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*linkedin-1*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }")
    try:
        import base64
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True, text=True,
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        n = len([ln for ln in r.stdout.splitlines() if ln.strip().isdigit()])
        if n:
            slog(f"closed {n} stale Chrome process(es) holding the bot profile.")
    except Exception as e:
        slog(f"stale-browser sweep failed: {e!r}")


# Single-instance guard
try:
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    if os.path.exists(LOCK):
        try:
            with open(LOCK, encoding="utf-8") as fh:
                old_pid = int(fh.read().strip())
            if old_pid != os.getpid() and _pid_alive(old_pid):
                print(f"supervisor already running (pid {old_pid}); exiting.", flush=True)
                sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            pass  # stale lock
    with open(LOCK, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
except Exception as e:
    print(f"lock failed ({e!r}); continuing anyway.", flush=True)

slog("started.")
try:
    os.remove(NET_MARKER)   # a marker left by a supervisor that died while waiting
except OSError:
    pass
restarts = 0
zero_apply_streak = 0
launch_fails = 0   # consecutive rc 43 (browser could not start); separate from the restart budget
net_idle_runs = 0  # consecutive runs that ended on a network error and saved nothing

while True:
    wait_for_ram()   # before `start` is taken, so the wait never counts as run time
    attempt = restarts + 1
    before = saved_count()
    try:
        log_offset = os.path.getsize(LOG)   # this attempt's part of the bot log starts here (linkedin_net.log_ended_on_network_error)
    except OSError:
        log_offset = 0
    start = time.time()
    out = os.path.join(REPO, "logs", f"supervised-attempt-{attempt}.log")
    err = os.path.join(REPO, "logs", f"supervised-attempt-{attempt}.err.log")
    # (2026-09-30: no PATH prepend any more - a stale chromedriver there pinned the driver
    # version; Selenium Manager now picks the one matching the installed Chrome.)
    env = dict(os.environ)
    slog(f"attempt {attempt}: launching bot (saved={before}).")
    attempt_start_ts = time.time()
    killed_zero_yield = False   # a flag, not a value of `gained`: that variable is recomputed from the log after the run (the old -2 sentinel was lost)
    try:
        with open(out, "ab") as fo, open(err, "ab") as fe:
            proc = subprocess.Popen([PYTHON, "run_linkedin_local.py"], cwd=REPO,
                                    stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env)
        with open(LOCK, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        # Polled wait: enforces the rolling yield watchdog mid-run so a
        # zero-yield grind can never run 16h again.
        rc = None
        while True:
            try:
                rc = proc.wait(timeout=POLL_SEC)
                break
            except subprocess.TimeoutExpired:
                pass
            sync_tracker()
            elapsed = (time.time() - attempt_start_ts) / 60
            cur = saved_count()
            if before < 0 <= cur:
                before = cur        # the log could not be read at launch: the first readable count is the baseline (else the watchdog stays blind all attempt)
            gained = (cur - before) if (cur >= 0 and before >= 0) else -1
            if gate_marker_fresh(attempt_start_ts):
                slog("GATE_STOPPED marker appeared mid-run — terminating grind.")
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=120)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                rc = proc.returncode
                break
            if elapsed >= YIELD_WINDOW_MIN and 0 <= gained < YIELD_MIN_APPLIES:     # -1 = the log could not be read: unknown, not zero yield
                slog(f"yield watchdog: +{gained} applies in {elapsed:.0f}min "
                     f"(< {YIELD_MIN_APPLIES}/{YIELD_WINDOW_MIN}min) — stopping grind.")
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=120)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                rc = proc.returncode
                killed_zero_yield = True  # needs a human, do not restart (see the check after the run)
                break
    except Exception as e:
        slog(f"attempt {attempt}: launch failed ({e!r}); cooldown 120s.")
        time.sleep(120)
        continue

    mins = (time.time() - start) / 60
    after = saved_count()
    gained = (after - before) if (after >= 0 and before >= 0) else -1
    applied, failed = parse_last_run_stats()
    slog(f"attempt {attempt}: exited rc={rc} after {mins:.1f}min "
         f"saved+{gained} run_stats(applied={applied}, failed={failed}).")

    if rc == WALL_RC or wall_recorded_since(attempt_start_ts):
        # Never restart into a wall (before this, exit 48 fell through to a plain restart, up to 6 times). The keepalive
        # reads this NEEDS_HUMAN: no relaunch today, one toast.
        needs_human("LinkedIn verification wall: YOUR TURN, open the linkedin-1 browser profile and clear it")
        break
    if rc == SIGNIN_RC:
        # 2026-10-14: signed out and the run's one vault sign-in failed or was used up. Not a wall: no marker, no hold.
        needs_human("LinkedIn sign-in failed: YOUR TURN, sign in in the linkedin-1 profile")
        break
    today_marker = os.path.join(REPO, "logs", f"LINKEDIN_DAILY_LIMIT_{datetime.now():%Y-%m-%d}")
    if os.path.exists(today_marker) or rc == 44:
        slog("daily Easy Apply limit reached — goal achieved, stopping supervisor.")
        break
    if rc == LAUNCH_FAILED_RC and not linkedin_net.reachable():
        # 2026-10-17: the browser or its driver could not start while LinkedIn cannot be reached (the driver may need the network): the same
        # outage, not a launch failure; it does not use the 3 launch retries.
        if not handle_outage(gained, "browser/driver launch failed (rc 43) while LinkedIn is unreachable"):
            break
        continue
    if rc == LAUNCH_FAILED_RC:
        launch_fails += 1
        if launch_fails <= LAUNCH_RETRIES:
            slog(f"browser/driver launch failed (rc 43), retry {launch_fails}/{LAUNCH_RETRIES} in "
                 f"{LAUNCH_RETRY_WAIT_SEC // 60} min (free RAM {free_ram_mb()} MB).")
            sweep_stale_browsers()      # a half-started Chrome still holding the profile makes the next start fail the same way
            time.sleep(LAUNCH_RETRY_WAIT_SEC)
            sweep_stale_browsers()      # and again: a Chrome can finish starting during the wait
            continue
        needs_human(f"browser/driver launch failed (rc 43) {launch_fails} times in a row — NOT restarting; "
                    "see logs/log.txt and logs/supervised-attempt-*.err.log.")
        break
    launch_fails = 0
    if rc == 42:
        needs_human("apply gate stop (modal missing) — cooling off, needs human review. NOT restarting.")
        break
    if killed_zero_yield:
        needs_human("killed for zero yield — needs human review. NOT restarting.")
        break
    if gained and gained > 0:
        restarts = 0                # a run that saved applications is progress, not a crash loop: the restart budget starts over
    # 2026-10-17: a run that ended because the home internet was down is not a breakage of the bot (it used to end the day through the
    # zero-apply rule below). The signal is the run's own ending in the bot log: main() caught a WebDriverException whose text is a
    # local-network error (linkedin_net.NET_ERR_RE). Wait for the connection, relaunch, spend no restart. Whatever else ended the run
    # is handled exactly as before.
    if linkedin_net.log_ended_on_network_error(LOG, log_offset):
        if gained and gained > 0:
            zero_apply_streak = 0           # a run that saved applications is progress, as in the rule below
        if not handle_outage(gained, "run ended on a network error"):
            break
        continue
    net_idle_runs = 0
    if restarts >= MAX_RESTARTS:
        needs_human("max restarts used — stopping (needs human review).")
        break
    if gained == 0 and mins < 10:
        zero_apply_streak += 1
        if zero_apply_streak >= 2:
            needs_human("two consecutive zero-apply short runs — persistent breakage, stopping.")
            break
    elif gained and gained > 0:
        zero_apply_streak = 0
    else:
        # Long run (crash after hours of work, e.g. renderer OOM): always retry.
        zero_apply_streak = 0

    restarts += 1
    slog(f"cooldown 60s, then restart ({restarts}/{MAX_RESTARTS} used).")
    time.sleep(60)

try:
    os.remove(LOCK)
except Exception:
    pass
slog("stopped.")
