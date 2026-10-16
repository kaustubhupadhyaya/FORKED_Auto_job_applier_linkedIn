"""Read-only report on what the knowledge-grounded AI answered in the LinkedIn India bot (plan section E, 2026-10-17).

Reads logs/ai_answers_india.jsonl (every question the proxy model was asked: type, raw reply, the text typed or the option chosen, or None) and
logs/knowledge_gaps.jsonl (questions about a tool the knowledge file has no number for). Prints: counts per day and type, the answers given, the
questions the model could not settle (those fall back to the bot's old answer), and the tools whose years are missing, most asked first, so the
missing numbers can be added to C:\\Users\\Admin\\.secrets\\candidate_knowledge.json.

    python ai_answers_report.py            # last 7 days
    python ai_answers_report.py --days 1   # today and yesterday
Writes nothing.
"""
import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.abspath(__file__))
AI_LOG = os.path.join(REPO, "logs", "ai_answers_india.jsonl")
GAP_LOG = os.path.join(REPO, "logs", "knowledge_gaps.jsonl")


def rows(path, since):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if str(r.get("ts", ""))[:10] >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")
    all_rows, gaps = rows(AI_LOG, since), rows(GAP_LOG, since)
    events = [r for r in all_rows if r.get("type") in ("probe", "budget")]      # the proxy going off / coming back, and the run budget
    ai = [r for r in all_rows if r.get("type") not in ("probe", "budget")]
    for r in events:
        print(f"  [{str(r.get('ts'))[:19]}] proxy {r.get('type')}: {r.get('why')}")
    print(f"AI answers since {since}: {len(ai)} questions asked ({AI_LOG})")
    by_day = Counter((str(r['ts'])[:10], r.get("type", "?")) for r in ai)
    for (day, typ), n in sorted(by_day.items()):
        print(f"  {day}  {typ:9} {n}")
    answered = [r for r in ai if r.get("answer")]
    unsettled = [r for r in ai if not r.get("answer")]
    print(f"\nanswered {len(answered)}, not settled {len(unsettled)} (the bot's old answer was used for those)")
    failed = [r for r in unsettled if str(r.get("why", "")).startswith("no model answered")]
    if failed:
        print(f"  of which the proxy gave no reply: {len(failed)}; reasons: {dict(Counter(str(r.get('why'))[:60] for r in failed))}")
    print("\nanswers given (latest 30):")
    for r in answered[-30:]:
        print(f"  [{r.get('type')}] {str(r.get('question'))[:90]!r} -> {str(r.get('answer'))[:80]!r}")
    print("\nquestions the model could not settle, most asked first (add the facts to the knowledge file to fix them):")
    for q, n in Counter(str(r.get("question"))[:100] for r in unsettled if not str(r.get("why", "")).startswith("no model answered")).most_common(15):
        print(f"  {n:3}  {q}")
    print(f"\ntools with no years in the knowledge file ({len(gaps)} questions):")
    tools = Counter()
    for r in gaps:
        for s in r.get("skills") or []:
            tools[s] += 1
    for s, n in tools.most_common(25):
        print(f"  {n:3}  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
