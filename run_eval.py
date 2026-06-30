"""
Evaluation harness for the de-anonymization agent.

Give it a CSV of postings whose REAL employer you already know (your team
de-anonymized them by hand). It runs the agent on each and reports how often it
was right, so you can trust the numbers before pointing this at live volume.

CSV format (see known_answers.example.csv):
    url,known_employer
    https://board.com/job/123,Acme Manufacturing

Run:
    pip install anthropic requests beautifulsoup4
    export ANTHROPIC_API_KEY=sk-...
    export SERPER_API_KEY=...
    export SCRAPINGBEE_API_KEY=...        # recommended; needed for LinkedIn/Indeed
    python run_eval.py known_answers.csv

Outputs a summary to the console and writes eval_results.json with full detail.

Metrics:
    top1   - the agent's #1 pick matched the known employer
    top5   - the known employer appeared anywhere in the candidate shortlist
    miss   - known employer found, agent got it wrong (FALSE POSITIVE — the costly case)
    unknown- agent returned no candidates (didn't guess)
    fetch_fail - couldn't even read the source posting (infra problem, not a model miss)
"""

import csv
import json
import re
import sys
from difflib import SequenceMatcher

from deanonymize_employer import investigate

# --- company-name matching ----------------------------------------------------

_SUFFIXES = r"\b(inc|llc|l\.l\.c|ltd|corp|corporation|co|company|group|holdings|" \
            r"plc|gmbh|sa|llp|lp|the)\b"


def _norm(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[.,/&'\"-]", " ", n)
    n = re.sub(_SUFFIXES, " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def names_match(a: str, b: str, threshold: float = 0.85) -> bool:
    """True if two company names refer to the same company, tolerant of
    suffixes (Inc/LLC), punctuation, and minor wording differences."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


# --- evaluation ---------------------------------------------------------------

def classify(known: str, result: dict) -> str:
    if result.get("fetch_failed"):
        return "fetch_fail"
    candidates = result.get("candidates") or []
    if not candidates:
        return "unknown"
    top = result.get("top_pick")
    if top and names_match(known, top):
        return "top1"
    if any(names_match(known, c.get("company", "")) for c in candidates):
        return "top5"
    return "miss"


def main(path: str):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("url") and row.get("known_employer"):
                rows.append((row["url"].strip(), row["known_employer"].strip()))

    if not rows:
        print("No usable rows. Need columns: url,known_employer")
        sys.exit(1)

    print(f"Evaluating {len(rows)} postings...\n")
    tally = {"top1": 0, "top5": 0, "miss": 0, "unknown": 0, "fetch_fail": 0}
    scores = []          # match_score of the top pick on top1 hits
    detail = []

    for i, (url, known) in enumerate(rows, 1):
        try:
            result = investigate(url)
        except Exception as e:
            result = {"candidates": [], "top_pick": None,
                      "overall_confidence": "unknown", "notes": f"crash: {e}"}
        outcome = classify(known, result)
        tally[outcome] += 1

        top = result.get("top_pick")
        if outcome == "top1" and result.get("candidates"):
            scores.append(result["candidates"][0].get("match_score", 0))

        flag = {"top1": "OK ", "top5": "~5 ", "miss": "XX ",
                "unknown": "?? ", "fetch_fail": "!! "}[outcome]
        print(f"[{i:>3}/{len(rows)}] {flag} known={known!r:<30} got={top!r}")

        detail.append({"url": url, "known_employer": known, "outcome": outcome,
                       "top_pick": top, "result": result})

    n = len(rows)
    answerable = n - tally["fetch_fail"]          # exclude infra failures from accuracy
    found = tally["top1"] + tally["top5"]

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Total postings        : {n}")
    print(f"Fetch failures (infra): {tally['fetch_fail']}  "
          f"(fix scraping before trusting the rest)")
    print(f"Answerable            : {answerable}")
    print("-" * 60)
    if answerable:
        print(f"Top-1 correct         : {tally['top1']:>3}  "
              f"({100*tally['top1']/answerable:.0f}% of answerable)")
        print(f"Top-5 recall (in list): {found:>3}  "
              f"({100*found/answerable:.0f}% of answerable)")
        print(f"False positives (miss): {tally['miss']:>3}  "
              f"({100*tally['miss']/answerable:.0f}%  <- the costly errors)")
        print(f"Returned unknown      : {tally['unknown']:>3}  "
              f"({100*tally['unknown']/answerable:.0f}%  <- safe non-guesses)")
    if scores:
        scores.sort()
        print("-" * 60)
        print(f"Top-pick match_score on hits: "
              f"min={scores[0]} median={scores[len(scores)//2]} max={scores[-1]}")
        print("(If misses score as high as hits, your thresholds need tuning.)")

    with open("eval_results.json", "w") as f:
        json.dump({"summary": tally, "detail": detail}, f, indent=2)
    print("\nFull detail written to eval_results.json")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_eval.py <known_answers.csv>")
        sys.exit(1)
    main(sys.argv[1])
