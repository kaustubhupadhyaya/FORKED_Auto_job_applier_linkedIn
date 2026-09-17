"""Untracked supervisor: keeps the LinkedIn bot applying across crashes.

- Launches run_linkedin_local.py as a child process (separate browser/session).
- Restarts it when it dies from renderer crashes / overlay kills.
- STOPS (no restart) when: daily Easy Apply limit reached, or two consecutive
  runs apply nothing (persistent breakage → needs a human), or 6 restarts used.
- Single-instance guarded via supervisor.lock.
"""
import os
import re
import sys
import time
import subprocess
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)
LOCK = os.path.join(REPO, "logs", "supervisor.lock")
SLOG = os.path.join(REPO, "logs", "supervisor.log")
LOG = os.path.join(REPO, "logs", "log.txt")
PYTHON = os.path.join(REPO, ".venv", "Scripts", "python.exe")
MAX_RESTARTS = 6
GATE_MARKER = os.path.join(REPO, "logs", "GATE_STOPPED")
TRACKER_PATH = r"C:\Users\Admin\GitHub\job-tracker\track_job_applications.py"


def sync_tracker():
    """Refresh the Excel tracker from both bots' records. Never blocks."""
    try:
        subprocess.run([PYTHON, TRACKER_PATH, "sync"], cwd=REPO, timeout=120,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        slog(f"tracker sync failed: {e!r}")
YIELD_WINDOW_MIN = 180       # rolling window for yield check
YIELD_MIN_APPLIES = 1        # stop if fewer applies than this per window
POLL_SEC = 600               # mid-run progress poll interval


def gate_marker_fresh(since_ts):
    try:
        return os.path.exists(GATE_MARKER) and os.path.getmtime(GATE_MARKER) >= since_ts
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


# Single-instance guard
try:
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    if os.path.exists(LOCK):
        try:
            with open(LOCK, encoding="utf-8") as fh:
                old_pid = int(fh.read().strip())
            os.kill(old_pid, 0)
            print(f"supervisor already running (pid {old_pid}); exiting.", flush=True)
            sys.exit(0)
        except Exception:
            pass  # stale lock
    with open(LOCK, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
except Exception as e:
    print(f"lock failed ({e!r}); continuing anyway.", flush=True)

slog("started.")
restarts = 0
zero_apply_streak = 0

while True:
    attempt = restarts + 1
    before = saved_count()
    start = time.time()
    out = os.path.join(REPO, "logs", f"supervised-attempt-{attempt}.log")
    err = os.path.join(REPO, "logs", f"supervised-attempt-{attempt}.err.log")
    env = dict(os.environ)
    env["PATH"] = r"C:\WebDrivers;" + env.get("PATH", "")
    slog(f"attempt {attempt}: launching bot (saved={before}).")
    sync_tracker()
    attempt_start_ts = time.time()
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
            if elapsed >= YIELD_WINDOW_MIN and gained < YIELD_MIN_APPLIES:
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
                gained = -2  # sentinel: killed for zero yield → do not restart
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

    today_marker = os.path.join(REPO, "logs", f"LINKEDIN_DAILY_LIMIT_{datetime.now():%Y-%m-%d}")
    if os.path.exists(today_marker) or rc == 44 or log_tail_has("Daily application limit"):
        slog("daily Easy Apply limit reached — goal achieved, stopping supervisor.")
        break
    if rc == 42:
        slog("apply gate stop (modal missing) — cooling off, needs human review. NOT restarting.")
        break
    if gained == -2:
        slog("killed for zero yield — needs human review. NOT restarting.")
        break
    if restarts >= MAX_RESTARTS:
        slog("max restarts used — stopping (needs human review).")
        break
    if gained == 0 and mins < 10:
        zero_apply_streak += 1
        if zero_apply_streak >= 2:
            slog("two consecutive zero-apply short runs — persistent breakage, stopping.")
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
