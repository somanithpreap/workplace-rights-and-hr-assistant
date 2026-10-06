"""
eval_hit3.py - the "exam" for search.py.

For each test question R01-R24 we already know which card holds the answer
(eval/gold_cards.csv, labelled by hand from the gold answers in cases.csv).
This script asks search.py each question and checks:

    hit@3 = is a correct card in the top 3 results?   (the number the guideline asks for)
    hit@5 = is a correct card in the top 5 results?
    rank  = position of the first correct card (1 = best)

Run:
    python eval_hit3.py --mode bm25
    python eval_hit3.py --mode hybrid
Results are saved to results/hit3_<mode>.csv so you can compare versions
(that comparison is your bonus B2 "proof of optimization" table).
"""
import argparse
import csv
import os
import time
from pathlib import Path

from search import search, CARDS

HERE = Path(__file__).resolve().parent
CASES_PATH = Path(os.getenv("CASES_PATH", HERE / "eval" / "cases.csv"))
GOLD_PATH = HERE / "eval" / "gold_cards.csv"
OUT_DIR = HERE / "results"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["bm25", "dense", "hybrid"], default="hybrid")
    args = p.parse_args()

    questions = {r["id"]: r["question"] for r in csv.DictReader(open(CASES_PATH, encoding="utf-8"))}
    gold_rows = list(csv.DictReader(open(GOLD_PATH, encoding="utf-8")))
    known_ids = {c["id"] for c in CARDS}

    rows, hits3, hits5 = [], 0, 0
    times = []
    for g in gold_rows:
        gold = set(g["gold_cards"].split(";"))
        missing = gold - known_ids
        if missing:
            print(f"WARNING {g['case_id']}: gold card(s) not in knowledge base: {missing}")

        t0 = time.time()
        results = search(questions[g["case_id"]], k=5, mode=args.mode)
        times.append((time.time() - t0) * 1000)
        if args.mode != "bm25":
            time.sleep(4.5)          # stay under the per-minute limit when embedding questions

        top_ids = [r["id"] for r in results]
        rank = next((i + 1 for i, cid in enumerate(top_ids) if cid in gold), None)
        hit3, hit5 = rank is not None and rank <= 3, rank is not None
        hits3 += hit3
        hits5 += hit5
        rows.append({
            "case_id": g["case_id"],
            "question": questions[g["case_id"]],
            "gold_cards": g["gold_cards"],
            "top3": " | ".join(r["citation"] for r in results[:3]),
            "first_gold_rank": rank or "not in top 5",
            "hit@3": "yes" if hit3 else "NO",
            "note": g["note"],
        })
        print(f"{g['case_id']}  {'HIT ' if hit3 else 'MISS'}  rank={rank or '-':<2}  "
              f"{questions[g['case_id']][:60]}")

    n = len(rows)
    times.sort()
    print(f"\nMode: {args.mode}")
    print(f"hit@3: {hits3}/{n}   hit@5: {hits5}/{n}   p50 search time: {times[n // 2]:.0f} ms")
    misses = [r for r in rows if r["hit@3"] == "NO"]
    if misses:
        print("\nMisses (explain these on your slide):")
        for r in misses:
            print(f"  {r['case_id']}: wanted {r['gold_cards']}\n           got    {r['top3']}")

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"hit3_{args.mode}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
