"""
pipeline.py - one question in, one answer out, with the STATE after every step (presentation Section 2).

    from pipeline import chat, confirm
    state = chat("How much annual leave do I have left?", session={"employee_id": "E004", "role": "worker"},
                 thread_id="t-1")
    print(state["answer"]); print_state_table(state)

Command line (great for live traces):
    python pipeline.py "What happens if a public holiday falls on a Sunday?" --as E004 --debug
    python pipeline.py "And at night?" --as E004 --thread t-7 --debug

Flow:  input guard -> load memory -> rewrite follow-up -> route -> tools -> retrieve -> rank+filter
       -> LLM answer -> output check -> save memory + trace
"""
import argparse
import json
import time
import uuid

import config
import guard
import memory
import router
import tools
from answer import answer_with_sources
from llm import ABSTAIN
from search import search

TOOL_FUNCS = {
    "leave_balance": tools.leave_balance, "seniority_indemnity": tools.seniority_indemnity,
    "notice_period": tools.notice_period, "overtime_pay": tools.overtime_pay, "my_record": tools.my_record,
    "holidays": tools.holidays, "minimum_wage": tools.minimum_wage, "usd_khr_rate": tools.usd_khr_rate,
    "expired_fdc_contracts": tools.expired_fdc_contracts, "draft_leave_request": tools.draft_leave_request,
}
NEEDS_EMPLOYEE = {"leave_balance", "seniority_indemnity", "notice_period", "overtime_pay", "my_record",
                  "draft_leave_request"}

PENDING = {}    # action_id -> {"employee_id", "draft"}; waits for the Confirm button


class Step:
    """Times one step and records what it read and wrote."""
    def __init__(self, state, name, read):
        self.state, self.name, self.read = state, name, read

    def __enter__(self):
        self.t0 = time.time()
        return self

    def done(self, wrote, tokens=None):
        self.state["steps"].append({"step": self.name, "input": self.read, "output": wrote,
                                    "ms": round((time.time() - self.t0) * 1000), "tokens": tokens})

    def __exit__(self, *exc):
        return False


def chat(message, session, thread_id=None):
    thread_id = thread_id or f"{session['employee_id']}-default"
    state = {"turn_id": uuid.uuid4().hex[:8], "thread_id": thread_id, "employee_id": session["employee_id"],
             "role": session["role"], "question": message, "rewritten_question": None, "is_followup": False,
             "guard": None, "route": None, "tool_calls": [], "cards": [], "answer": None, "citations": [],
             "output_check": None, "pending_action": None, "steps": [], "llm_calls": 0}
    t_start = time.time()

    # 1. input guard
    with Step(state, "1 input guard", message[:80]) as s:
        g = guard.check_input(message, session)
        state["guard"] = g
        s.done(f"{g['action']} ({g['type']})")
    if g["action"] == "block":
        state["answer"] = g["reply"]
        return _finish(state, t_start, save_question=message)

    # 2. load memory
    with Step(state, "2 load memory", f"thread {thread_id}") as s:
        history, total = memory.load(thread_id, session["employee_id"])
        state["memory_window"] = [f"{h['speaker']}: {h['content'][:60]}" for h in history]
        s.done(f"{len(history)} of {total} messages in window")

    # 3. rewrite follow-up
    question = message
    with Step(state, "3 rewrite", message[:80]) as s:
        if memory.is_followup(message, history):
            state["is_followup"] = True
            question, out = memory.rewrite(message, history)
            state["llm_calls"] += 1
            s.done(f'"{question}"', out["tokens_in"] + out["tokens_out"])
        else:
            s.done("not a follow-up, unchanged")
    state["rewritten_question"] = question

    # 4. route
    with Step(state, "4 route", question[:80]) as s:
        r = router.route(question, session)
        state["route"] = r
        s.done(f"{r['route']} | {r['reason']}")

    # 5. tools (employee_id injected from the SESSION, never from the message)
    tool_results = []
    for name, args in r["tools"]:
        with Step(state, f"5 tool {name}", json.dumps(args)) as s:
            if name in NEEDS_EMPLOYEE:
                res = TOOL_FUNCS[name](session["employee_id"], **args)
            else:
                res = TOOL_FUNCS[name](**args)
            cleaned, flagged = guard.clean_untrusted(res["summary"])
            res["summary"] = cleaned
            if isinstance(res.get("result"), dict) and "history" in res["result"]:
                for row in res["result"]["history"]:
                    row["reason"], f2 = guard.clean_untrusted(row.get("reason") or "")
                    flagged = flagged or f2
            tool_results.append(res)
            state["tool_calls"].append({"tool": name, "args": args, "summary": res["summary"],
                                        "untrusted_text_removed": flagged})
            s.done(res["summary"][:120] + (" [instruction-like text removed]" if flagged else ""))

    # write action: show a draft, wait for Confirm (no LLM needed)
    if r["route"] == "book_leave":
        res = tool_results[0]
        if res["result"]["ok"]:
            action_id = uuid.uuid4().hex[:10]
            PENDING[action_id] = {"employee_id": session["employee_id"], "draft": res["result"]["draft"]}
            state["pending_action"] = {"action_id": action_id, "draft": res["result"]["draft"]}
        state["answer"] = res["summary"]
        state["citations"] = [res["source"]]
        return _finish(state, t_start, save_question=message)

    # 6. retrieve
    cards = []
    if r["use_rag"]:
        with Step(state, "6 retrieve", question[:80]) as s:
            found = search(question, k=config.TOP_K, mode=config.SEARCH_MODE)
            s.done(f"{found[0]['mode']}: {len(found)} cards; top: {found[0]['citation']} (score {found[0]['score']})"
                   if found else "0 cards")
        # 7. rank + filter (relevance + injection filter)
        with Step(state, "7 rank + filter", f"{len(found)} cards") as s:
            dropped = []
            for c in found:
                relevant = c["bm25_score"] >= config.MIN_BM25 or (
                    c["dense_score"] is not None and c["dense_score"] >= config.MIN_DENSE)
                _, flagged = guard.clean_untrusted(c["text"])
                keep = relevant and not flagged and len(cards) < config.KEEP_MAX
                state["cards"].append({"citation": c["citation"], "score": c["score"], "bm25": c["bm25_score"],
                                       "dense": c["dense_score"], "kept": keep,
                                       "why_dropped": None if keep else ("injection text" if flagged else
                                                                         "not relevant" if not relevant else
                                                                         "over the limit")})
                (cards if keep else dropped).append(c)
            s.done(f"kept {len(cards)}, dropped {len(dropped)}")

    # 8. LLM answer (skip the call when there is nothing to answer from = empty-context abstain)
    extra = []
    if g["type"] == "identity_claim_ignored":
        extra.append(g["note"] + " Answer only for the signed-in employee.")
    if r["route"] == "referral":
        extra.append("This is a dispute or dismissal question: explain the rule, then refer to HR / Labour "
                     "Inspector; give no final legal opinion.")
    with Step(state, "8 LLM answer", f"{len(cards)} cards + {len(tool_results)} tool results") as s:
        if not cards and not tool_results:
            state["answer"] = ABSTAIN
            s.done("no relevant sources -> abstain without calling the LLM")
        else:
            out = answer_with_sources(question, cards, tool_results, history, extra)
            state["llm_calls"] += 1
            state["answer"] = out["text"]
            state["prompt_preview"] = out["prompt"][:3000]
            s.done(state["answer"][:100], out["tokens_in"] + out["tokens_out"])

    # 9. output check (regenerate once if a citation is missing)
    allowed = [c["citation"] for c in cards] + [t["source"] for t in tool_results]
    state["citations"] = [c for c in allowed if c.strip("[]") in state["answer"]]
    with Step(state, "9 output check", "answer + sources") as s:
        chk = guard.check_output(state["answer"], session, allowed, needs_citation=bool(cards or tool_results))
        if not chk["ok"] and chk["problems"] == ["missing_citation"]:
            out = answer_with_sources(question, cards, tool_results, history, extra, strict=True)
            state["llm_calls"] += 1
            state["answer"] = out["text"]
            chk = guard.check_output(state["answer"], session, allowed, needs_citation=True)
            chk["regenerated"] = True
        if not chk["ok"]:
            state["answer"] = ("I can't show that answer safely. Please rephrase, or ask HR directly."
                               if any(p != "missing_citation" for p in chk["problems"]) else ABSTAIN)
        state["output_check"] = chk
        s.done("pass" if chk["ok"] else f"fail: {chk['problems']}")

    if state["answer"] != ABSTAIN and r["route"] in ("law", "referral", "my_record", "hr_report", "data_lookup"):
        state["answer"] += f"\n\n_{config.DISCLAIMER}_"
    return _finish(state, t_start, save_question=message)


def _finish(state, t_start, save_question):
    t0 = time.time()
    memory.save(state["thread_id"], state["employee_id"], "user", save_question, state["rewritten_question"])
    memory.save(state["thread_id"], state["employee_id"], "assistant", state["answer"])
    state["steps"].append({"step": "10 save memory + trace", "input": state["thread_id"], "output": "saved",
                           "ms": round((time.time() - t0) * 1000), "tokens": None})
    state["total_ms"] = round((time.time() - t_start) * 1000)
    state["total_tokens"] = sum(s["tokens"] or 0 for s in state["steps"])
    config.TRACE_LOG.parent.mkdir(exist_ok=True)
    with open(config.TRACE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in state.items() if k != "prompt_preview"}, ensure_ascii=False,
                           default=str) + "\n")
    return state


def confirm(action_id, session):
    """Called only by the Confirm button / POST /confirm."""
    action = PENDING.get(action_id)
    if not action or action["employee_id"] != session["employee_id"]:
        return {"ok": False, "message": "This draft has expired or does not belong to you."}
    PENDING.pop(action_id)
    return tools.confirm_leave_request(session["employee_id"], action["draft"])


def print_state_table(state):
    print(f"\nturn {state['turn_id']} | thread {state['thread_id']} | {state['employee_id']} ({state['role']})")
    print(f"{'#':<26}{'output (state written)':<74}{'ms':>7}{'tokens':>8}")
    for s in state["steps"]:
        print(f"{s['step']:<26}{str(s['output'])[:72]:<74}{s['ms']:>7}{(s['tokens'] or '-'):>8}")
    print(f"{'TOTAL':<26}{'':<74}{state['total_ms']:>7}{state['total_tokens']:>8}")
    if state["cards"]:
        print("\ncards:")
        for c in state["cards"]:
            print(f"  {'KEEP' if c['kept'] else 'drop'}  {c['citation']:<60} bm25={c['bm25']} dense={c['dense']}"
                  + (f"  ({c['why_dropped']})" if c["why_dropped"] else ""))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("message")
    p.add_argument("--as", dest="employee", default="E004")
    p.add_argument("--thread", default=None)
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    session = {"employee_id": a.employee, "role": tools.role_of(a.employee)}
    state = chat(a.message, session, a.thread)
    print("\nANSWER:\n" + state["answer"])
    if state["pending_action"]:
        print(f"\n(pending action {state['pending_action']['action_id']} - confirm in the UI or API)")
    if a.debug:
        print_state_table(state)


if __name__ == "__main__":
    main()
