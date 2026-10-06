"""
run_benchmark.py - "LLM alone vs RAG" (guideline section 7) on the 48 test cases.

    python run_benchmark.py --run alone        # Run A: Gemini with no documents
    python run_benchmark.py --run rag          # Run B: our full chatbot
    python run_benchmark.py --summarize        # after you have marked the answers

* Results go to results/benchmark_<run>.csv. If the daily limit stops you, run the same command
  tomorrow: finished cases are skipped (resume).
* Then the TEAM marks each answer in the "mark" column by hand:  Correct / Wrong / Made up / Don't know
  and for RAG also "source_correct": Y / N. Judging is your job - that is the honest part of the test.
* Attack cases are in run_attacks.py.
"""
import argparse
import csv
import json
import time
import uuid

import config
import memory
import pipeline
import tools
from answer import answer_alone
from llm import ABSTAIN

TYPES = ["single_place", "two_places", "follow_up", "not_in_docs", "tool", "multi_step"]
FIELDS = ["id", "type", "question", "session", "gold_answer", "answer", "route", "citations", "auto_abstained",
          "ms", "tokens", "llm_calls", "mark", "source_correct"]


def load_cases():
    with open(config.CASES_JSONL, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run(which, limit=None):
    out_path = config.RESULTS_DIR / f"benchmark_{which}.csv"
    config.RESULTS_DIR.mkdir(exist_ok=True)
    done = set()
    if out_path.exists():
        done = {r["id"] for r in csv.DictReader(open(out_path, encoding="utf-8"))}
    cases = [c for c in load_cases() if c["guideline_type"] in TYPES and c["id"] not in done][:limit]
    print(f"{len(cases)} cases to run ({len(done)} already done) -> {out_path}")
    new_file = not out_path.exists()
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for c in cases:
            session = c.get("session") or {"employee_id": "E004", "role": "worker"}
            session = {"employee_id": session["employee_id"], "role": tools.role_of(session["employee_id"])}
            history = c.get("history") or []
            t0 = time.time()
            try:
                if which == "alone":
                    q = (" ".join(f"Earlier question: {h}" for h in history) + " " + c["question"]).strip()
                    out = answer_alone(q)
                    row = {"answer": out["text"], "route": "-", "citations": "", "tokens":
                           out["tokens_in"] + out["tokens_out"], "llm_calls": 1}
                else:
                    thread = f"bench-{uuid.uuid4().hex[:6]}"
                    for h in history:                       # put earlier turns into memory (no extra calls)
                        memory.save(thread, session["employee_id"], "user", h)
                        memory.save(thread, session["employee_id"], "assistant", "(answer to the earlier question)")
                    st = pipeline.chat(c["question"], session, thread)
                    row = {"answer": st["answer"], "route": (st["route"] or {}).get("route", "blocked"),
                           "citations": " ".join(st["citations"]), "tokens": st["total_tokens"],
                           "llm_calls": st["llm_calls"]}
            except Exception as e:                           # e.g. daily quota reached: stop, resume later
                print(f"STOPPED at {c['id']}: {e}")
                break
            row.update({"id": c["id"], "type": c["guideline_type"], "question": c["question"],
                        "session": session["employee_id"], "gold_answer": c.get("gold_answer", ""),
                        "auto_abstained": "yes" if ABSTAIN in row["answer"] else "",
                        "ms": round((time.time() - t0) * 1000), "mark": "", "source_correct": ""})
            w.writerow(row)
            f.flush()
            print(f"{c['id']:<8} {row['ms']:>6} ms  {row['answer'][:90]!r}")
            if config.LLM_MODE != "mock":
                time.sleep(config.PAUSE_BETWEEN_CALLS)


def summarize():
    for which in ("alone", "rag"):
        p = config.RESULTS_DIR / f"benchmark_{which}.csv"
        if not p.exists():
            continue
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
        answerable = [r for r in rows if r["type"] != "not_in_docs"]
        uncovered = [r for r in rows if r["type"] == "not_in_docs"]
        count = lambda rs, m: sum(1 for r in rs if r["mark"].strip().lower() == m.lower())
        unmarked = sum(1 for r in rows if not r["mark"].strip())
        print(f"\n=== {which.upper()} ({len(rows)} cases, {unmarked} not marked yet) ===")
        print(f"Correct answers        : {count(answerable, 'Correct')} / {len(answerable)}")
        print(f"Made-up answers        : {count(rows, 'Made up')}")
        print(f"Wrong answers          : {count(rows, 'Wrong')}")
        dont_know = count(uncovered, "Don't know")
        print(f"Correct 'I don't know' : {dont_know} / {len(uncovered)}")
        if which == "rag":
            ok = sum(1 for r in answerable if r["source_correct"].strip().upper() == "Y")
            print(f"Answers with a correct source: {ok} / {count(answerable, 'Correct')}")
        toks = [int(r["tokens"] or 0) for r in rows]
        ms = sorted(int(r["ms"] or 0) for r in rows)
        print(f"Average tokens/question: {sum(toks) / max(len(toks), 1):.0f}   p50 time: {ms[len(ms) // 2]} ms")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", choices=["alone", "rag"])
    p.add_argument("--limit", type=int, default=None, help="only run the first N remaining cases")
    p.add_argument("--summarize", action="store_true")
    a = p.parse_args()
    if a.summarize:
        summarize()
    elif a.run:
        run(a.run, a.limit)
    else:
        p.print_help()
