"""
Runs the 48 test questions (eval/cases.jsonl) through the chatbot and saves the answers for marking.

    python eval/run_eval.py              # our chatbot (agent.process_query) -> results/eval_rag.csv
    python eval/run_eval.py --alone      # Gemini with no documents, attacks skipped -> results/eval_alone.csv
    python eval/run_eval.py --summary    # print the results table after marking

If the daily quota runs out the run stops; run the same command again later and finished questions are skipped.
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from agent import process_query, client  # noqa: E402

CASES = ROOT / "eval" / "cases.jsonl"
RESULTS = ROOT / "results"
MODEL = os.getenv("CHAT_MODEL", "gemini-3.5-flash-lite")
PAUSE = float(os.getenv("PAUSE_BETWEEN_CALLS", "4.5"))   # free tier: about 15 requests per minute
DEFAULT_SESSION = {"employee_id": "E004", "role": "worker"}
FIELDS = ["id", "type", "question", "session", "gold_answer", "gold_sources", "must_refuse", "answer",
          "citations", "route", "blocked", "ms", "tokens", "mark", "source_correct"]
MARKS = ["Correct", "Wrong", "Made up", "Don't know"]


def load_cases():
    with open(CASES, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def ask_chatbot(case):
    session = case.get("session") or DEFAULT_SESSION
    thread = f"eval-{case['id']}-{int(time.time())}"
    for earlier in case.get("history") or []:          # follow-up cases: earlier turns in the same thread
        process_query(session["employee_id"], session["role"], earlier, thread)
        time.sleep(PAUSE)
    r = process_query(session["employee_id"], session["role"], case["question"], thread)
    return {
        "session": session["employee_id"],
        "answer": r.get("answer", ""),
        "citations": " ".join(r.get("citations", [])),
        "route": r.get("route", ""),
        "blocked": "yes" if (r.get("guard") or {}).get("action") == "block" else "",
        "tokens": r.get("total_tokens", ""),
    }


def ask_alone(case):
    question = case["question"]
    if case.get("history"):
        question = " ".join(f"Earlier question: {h}" for h in case["history"]) + " " + question
    res = client.models.generate_content(
        model=MODEL, contents=question,
        config={"system_instruction": "Answer the question about Cambodian labour law in 2-4 sentences."})
    usage = res.usage_metadata
    tokens = (getattr(usage, "prompt_token_count", 0) or 0) + (getattr(usage, "candidates_token_count", 0) or 0)
    return {"session": "-", "answer": res.text or "", "citations": "", "route": "-", "blocked": "",
            "tokens": tokens}


def run(alone=False):
    out = RESULTS / ("eval_alone.csv" if alone else "eval_rag.csv")
    RESULTS.mkdir(exist_ok=True)
    done = set()
    if out.exists():
        with open(out, encoding="utf-8-sig") as f:
            done = {r["id"] for r in csv.DictReader(f)}
    cases = [c for c in load_cases() if c["id"] not in done and not (alone and c["guideline_type"] == "attack")]
    print(f"{len(cases)} questions to run ({len(done)} already done) -> {out}")

    new_file = not out.exists()
    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for c in cases:
            t0 = time.time()
            if c["question"].startswith("(Setup)"):      # G04/G05 need a manual setup, see eval/README.md
                w.writerow({"id": c["id"], "type": c["guideline_type"], "question": c["question"],
                            "gold_answer": c.get("gold_answer", ""), "must_refuse": "yes" if c.get("must_refuse") else "",
                            "session": "-", "answer": "manual test", "citations": "", "route": "", "blocked": "manual",
                            "ms": "", "tokens": "", "mark": "", "source_correct": ""})
                print(f"{c['id']:<8} {c['guideline_type']:<13} manual setup test, skipped")
                continue
            try:
                r = ask_alone(c) if alone else ask_chatbot(c)
            except Exception as e:
                print(f"STOPPED at {c['id']}: {str(e)[:200]}\nRun the same command again later to continue.")
                break
            if r["answer"].startswith("Error generating answer"):
                print(f"STOPPED at {c['id']}: {r['answer'][:200]}\nRun the same command again later to continue.")
                break
            w.writerow({
                "id": c["id"], "type": c["guideline_type"], "question": c["question"],
                "gold_answer": c.get("gold_answer", ""), "gold_sources": "; ".join(c.get("sources", [])),
                "must_refuse": "yes" if c.get("must_refuse") else "",
                "ms": round((time.time() - t0) * 1000), "mark": "", "source_correct": "", **r,
            })
            f.flush()
            print(f"{c['id']:<8} {c['guideline_type']:<13} {r['answer'][:80]!r}")
            time.sleep(PAUSE)


def summary():
    for name in ("eval_alone.csv", "eval_rag.csv"):
        p = RESULTS / name
        if not p.exists():
            continue
        with open(p, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        print(f"\n=== {name} ({len(rows)} questions, {sum(1 for r in rows if not r['mark'].strip())} not marked) ===")
        print(f"{'type':<14}{'n':>4}" + "".join(f"{m:>12}" for m in MARKS))
        for t in sorted({r["type"] for r in rows if r["type"] != "attack"}):
            rs = [r for r in rows if r["type"] == t]
            counts = [sum(1 for r in rs if r["mark"].strip().lower() == m.lower()) for m in MARKS]
            print(f"{t:<14}{len(rs):>4}" + "".join(f"{n:>12}" for n in counts))
        attacks = [r for r in rows if r["type"] == "attack" and r["blocked"] != "manual"]
        if attacks:
            print(f"attacks blocked by the input guard: {sum(r['blocked'] == 'yes' for r in attacks)} / {len(attacks)}"
                  f"  (G04/G05 are manual tests)")
        cited = [r for r in rows if r["source_correct"].strip()]
        if cited:
            print(f"answers with a correct source: {sum(r['source_correct'].strip().upper() == 'Y' for r in cited)} / {len(cited)}")
        toks = [int(r["tokens"]) for r in rows if str(r["tokens"]).isdigit()]
        ms = sorted(int(r["ms"]) for r in rows if r["ms"])
        if toks:
            print(f"average tokens per question: {sum(toks) / len(toks):.0f}")
        if ms:
            print(f"median time per question: {ms[len(ms) // 2]} ms")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--alone", action="store_true", help="ask Gemini without documents")
    p.add_argument("--summary", action="store_true", help="print the results after marking")
    a = p.parse_args()
    summary() if a.summary else run(alone=a.alone)
