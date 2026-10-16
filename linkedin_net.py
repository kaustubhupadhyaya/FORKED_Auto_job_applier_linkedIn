"""Home-internet awareness for run_linkedin_supervised.py (2026-10-17).

Why: three nights running (10-15, 10-16, 10-17) the home connection lost DNS for a few minutes. Each time the bot's page load failed
(net::ERR_NAME_NOT_RESOLVED), the run ended with exit 0 after ~20 s, the supervisor relaunched it after 60 s into the same outage,
and its "two consecutive zero-apply short runs" rule then stopped the bot for the day (logs/NEEDS_HUMAN). A dead connection is not a
breakage of the bot: the supervisor now waits for LinkedIn to be reachable and relaunches, without using its restart budget.

Progress still decides: a run that ends on a network error without having saved a single application counts toward
NET_IDLE_RUN_LIMIT consecutive such runs, then the supervisor stops with NEEDS_HUMAN as before. An outage that outlasts
NET_WAIT_MAX_MIN stops it too.

CLI (real network, read-only apart from the optional marker path you give `wait`):
  python linkedin_net.py status
  python linkedin_net.py classify --log logs/log.txt --offset 0     # does the end of the log hold a local-network error?
  python linkedin_net.py wait --host nonexistent.invalid --max-min 1 --poll-sec 5 --marker <path>   # the wait loop against a real DNS failure
  python linkedin_net.py wait --host 127.0.0.1 --port 47999 --marker <path>   # ... or against a loopback port that opens later
"""
import argparse
import os
import re
import socket
import sys
import time
from datetime import datetime

# Local-network failures only. ERR_CONNECTION_RESET / _CLOSED can come from LinkedIn's side and are not treated as an outage.
# The run's ENDING is what counts: runAiBot.main() catches the WebDriverException that stopped the run and logs
# "Browser window closed or session is invalid. Exiting." followed by the exception text ("Message: unknown error: net::ERR_..."). A network
# error that is logged earlier in the run, and did not end it, does not match.
NET_ERR_RE = re.compile(r"Browser window closed or session is invalid\. Exiting\.[\s\S]{0,300}?"
                        r"net::ERR_(?:NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|NETWORK_CHANGED|ADDRESS_UNREACHABLE|NETWORK_IO_SUSPENDED)")
HOST = "www.linkedin.com"
NET_WAIT_MAX_MIN = 480       # one outage: wait at most 8 h, then ask for a human
NET_POLL_SEC = 30
NET_STABLE_SEC = 20          # reachable, and still reachable this long after: only then is the connection back
NET_NOTE_EVERY_MIN = 10      # a progress line in the supervisor log
NET_TOAST_AFTER_MIN = 30     # one toast per outage day
NET_IDLE_RUN_LIMIT = 6       # consecutive network-ended runs that saved nothing, then NEEDS_HUMAN
NET_TAIL_BYTES = 12000       # the end of the attempt's part of the bot log that is searched for the ending error (the stack dump, the totals block)


def reachable(host=HOST, port=443, timeout=5):
    """True when this machine resolves the host and opens a TCP connection to it (the DNS lookup is what failed on 10-15/16/17)."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def log_ended_on_network_error(log_path, offset, tail_bytes=NET_TAIL_BYTES):
    """True when the last tail_bytes of the log written since `offset` (this attempt's part of it) show the run ending on a local-network
    error (NET_ERR_RE). A log shorter than `offset` was rotated: the whole file is this attempt's. Never raises."""
    try:
        size = os.path.getsize(log_path)
        begin = offset if 0 <= offset <= size else 0
        begin = max(begin, size - tail_bytes)
        with open(log_path, "rb") as fh:
            fh.seek(begin)
            return bool(NET_ERR_RE.search(fh.read().decode("utf-8", errors="replace")))
    except Exception:
        return False


def wait_until_reachable(log, marker_path, toast=None, host=HOST, max_min=NET_WAIT_MAX_MIN, poll_sec=NET_POLL_SEC,
                         stable_sec=NET_STABLE_SEC, port=443):
    """Blocks while `host` is unreachable. Returns the minutes waited (0.0 when it was reachable at once) or None when it was still
    unreachable after max_min. While it waits, marker_path holds one line saying so; `toast(minutes)` is called once after
    NET_TOAST_AFTER_MIN (an exception in it is logged and ignored); `log(text)` gets a line when the wait starts, every
    NET_NOTE_EVERY_MIN and when it ends."""
    if reachable(host, port):
        return 0.0
    t0 = time.time()
    log(f"{host} is unreachable (home internet down?): waiting up to {max_min} min before relaunching the bot. "
        "This does not use the restart budget.")
    try:
        with open(marker_path, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {host} unreachable since {datetime.fromtimestamp(t0):%H:%M:%S} (home internet down?). "
                     "The supervisor is waiting and relaunches the bot by itself when the connection is back.\n")
    except OSError:
        pass
    last_note = t0
    toasted = False
    try:
        while time.time() - t0 < max_min * 60:
            time.sleep(poll_sec)
            mins = (time.time() - t0) / 60
            if reachable(host, port):
                time.sleep(stable_sec)
                if reachable(host, port):
                    mins = (time.time() - t0) / 60
                    log(f"{host} is reachable again after {mins:.1f} min.")
                    return mins
            if toast is not None and not toasted and mins >= NET_TOAST_AFTER_MIN:
                toasted = True
                try:
                    toast(mins)
                except Exception as e:      # an alarm that cannot be shown must not stop the wait
                    log(f"outage toast could not be shown: {e!r}")
            if time.time() - last_note >= NET_NOTE_EVERY_MIN * 60:
                last_note = time.time()
                log(f"{host} still unreachable after {mins:.0f} min.")
        log(f"{host} still unreachable after {max_min} min: giving up.")
        return None
    finally:
        try:
            os.remove(marker_path)
        except OSError:
            pass


def _main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("classify")
    c.add_argument("--log", required=True)
    c.add_argument("--offset", type=int, default=0)
    w = sub.add_parser("wait")
    w.add_argument("--host", default=HOST)
    w.add_argument("--port", type=int, default=443)
    w.add_argument("--max-min", type=float, default=1)
    w.add_argument("--poll-sec", type=float, default=5)
    w.add_argument("--stable-sec", type=float, default=2)
    w.add_argument("--marker", required=True)
    a = ap.parse_args()
    if a.cmd == "status":
        t = time.time()
        up = reachable()
        print(f"{HOST}: {'reachable' if up else 'UNREACHABLE'} ({(time.time() - t) * 1000:.0f} ms)")
        return 0 if up else 1
    if a.cmd == "classify":
        hit = log_ended_on_network_error(a.log, a.offset)
        print(f"{a.log} from byte {a.offset}: {'ENDS ON A NETWORK ERROR' if hit else 'no network error at the end'}")
        return 0
    res = wait_until_reachable(lambda s: print(f"{datetime.now():%H:%M:%S} {s}", flush=True), a.marker, host=a.host,
                               max_min=a.max_min, poll_sec=a.poll_sec, stable_sec=a.stable_sec, port=a.port)
    print("result:", res)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
