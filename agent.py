import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from google import genai

from guard import check_input, clean_untrusted, check_output, CANARY, REFUSALS
import tools

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hr.db")
KB_PATH = os.path.join(BASE_DIR, "knowledge_base.json")
MODEL = os.getenv("CHAT_MODEL", "gemini-3.5-flash-lite")
MEMORY_WINDOW = 6          # messages sent to the model (3 questions + 3 answers)
ABSTAIN = "The provided sources do not cover this question."

client = genai.Client()

# In-memory storage for multi-turn conversations: { "employee_id:thread_id": [ {"role": ..., "content": ...} ] }
THREAD_MEMORY: dict[str, list[dict]] = {}

FOLLOWUP_START = re.compile(r"^(and|but|also|what about|how about|what if|who|same|then|so|or)\b", re.I)
PRONOUN = re.compile(r"\b(it|that|this|those|these|they|them|there|then)\b", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def get_employee_info(employee_id: str) -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}


def generate(prompt: str) -> tuple[str, int]:
    """Calls Gemini and returns (text, tokens used)."""
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    usage = resp.usage_metadata
    tokens = (getattr(usage, "prompt_token_count", 0) or 0) + (getattr(usage, "candidates_token_count", 0) or 0)
    return (resp.text or "").strip(), tokens


# --- RAG Retrieval Engine ---
_KB_CACHE = None


def load_knowledge_base() -> list[dict]:
    global _KB_CACHE
    if _KB_CACHE is None:
        if not os.path.exists(KB_PATH):
            return []
        with open(KB_PATH, "r", encoding="utf-8") as f:
            _KB_CACHE = json.load(f)
    return _KB_CACHE


def search_knowledge_base(query: str, top_k: int = 4) -> list[dict]:
    kb = load_knowledge_base()
    if not kb:
        return []

    words = set(re.findall(r"\w+", query.lower())) - {
        "what", "is", "the", "how", "and", "for", "to", "in", "of", "a", "an", "do", "does", "i", "can"
    }

    scored_chunks = []
    for item in kb:
        content = item.get("content", "").lower()
        title = item.get("title", "").lower()
        source = item.get("source", "").lower()

        # Score term matches
        score = 0.0
        for w in words:
            if w in title:
                score += 3.0
            if w in source:
                score += 2.0
            if w in content:
                score += 1.0

        if score > 0:
            scored_chunks.append((score, item))

    scored_chunks.sort(key=lambda x: x[0], reverse=True)

    cards = []
    for score, item in scored_chunks[:top_k]:
        title = item.get("title", item.get("source", "Document"))
        content = item.get("content", "")
        cards.append({
            "citation": title,
            "text": content,
            "bm25": round(score, 1),
            "kept": True,
            "source": item.get("source", ""),
        })

    return cards


# --- Follow-Up Detection and Query Rewriter ---
def is_followup_question(question: str, history: list[dict]) -> bool:
    """A follow-up needs earlier messages to make sense, e.g. 'And at night?' or 'Who pays during that time?'."""
    if not history:
        return False
    words = question.split()
    return (FOLLOWUP_START.search(question.strip()) is not None
            or len(words) <= 5
            or (len(words) <= 9 and PRONOUN.search(question) is not None))


def rewrite_followup(question: str, history: list[dict]) -> tuple[str, int]:
    """Turns follow-up questions like 'And at night?' into standalone queries using history."""
    history_str = "\n".join([f"{m['role'].capitalize()}: {m['content'][:300]}" for m in history[-MEMORY_WINDOW:]])
    prompt = f"""
Given the following conversation history and a new user question, rewrite the user question into a single standalone, self-contained search query.
Preserve all names, numbers, legal concepts, and subject context.
Output ONLY the rewritten question with no extra formatting or commentary.

History:
{history_str}

User Question: {question}
"""
    try:
        rewritten, tokens = generate(prompt)
        rewritten = rewritten.strip('"').split("\n")[0]
        return (rewritten or question), tokens
    except Exception:
        return question, 0


# --- Helpers for tool inputs ---
def find_number(text: str, unit: str):
    m = re.search(rf"\b(\d+(?:\.\d+)?)\s*(?:working\s+)?{unit}", text)
    if m:
        return float(m.group(1))
    m = re.search(rf"\b({'|'.join(NUMBER_WORDS)})\s+(?:working\s+)?{unit}", text)
    return float(NUMBER_WORDS[m.group(1)]) if m else None


def find_date(text: str, today: date):
    months = "|".join(MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({months})\b|\b({months})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text)
    if not m:
        return None
    day = int(m.group(1) or m.group(4))
    month = MONTHS[m.group(2) or m.group(3)]
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def public_holidays() -> set:
    conn = sqlite3.connect(DB_PATH)
    try:
        return {r[0] for r in conn.execute("SELECT date FROM public_holidays")}
    finally:
        conn.close()


def leave_dates(start: date, working_days: int) -> list[date]:
    """Working days are Monday-Saturday, excluding public holidays."""
    holidays = public_holidays()
    days, d = [], start
    while len(days) < working_days:
        if d.weekday() != 6 and d.isoformat() not in holidays:
            days.append(d)
        d += timedelta(days=1)
    return days


# --- Core Pipeline Process ---
def process_query(employee_id: str, role: str, question: str, thread_id: str) -> dict:
    start_time = time.time()
    session = {"employee_id": employee_id, "role": role}
    steps = []
    total_tokens = 0
    llm_calls = 0

    # Initialize Thread Memory (one conversation per employee + thread)
    memory_key = f"{employee_id}:{thread_id}"
    history = THREAD_MEMORY.setdefault(memory_key, [])
    window = history[-MEMORY_WINDOW:]

    # 1. Guardrail Check (Input)
    t0 = time.time()
    guard_res = check_input(question, session)
    steps.append({"step": "guardrail_check", "output": f"{guard_res['action']} ({guard_res['type']})",
                  "ms": int((time.time() - t0) * 1000)})

    if guard_res["action"] == "block":
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": guard_res["reply"]})
        return {
            "answer": guard_res["reply"],
            "turn_id": len(history) // 2,
            "question": question,
            "thread_id": thread_id,
            "guard": guard_res,
            "route": "blocked",
            "route_reason": f"Blocked by guardrail ({guard_res['type']})",
            "citations": [],
            "steps": steps,
            "cards": [],
            "memory_window": [],
            "tool_calls": [],
            "pending_action": None,
            "llm_calls": 0,
            "total_ms": int((time.time() - start_time) * 1000),
            "total_tokens": 0,
        }

    # 2. Multi-turn Follow-Up Rewriting
    t0 = time.time()
    is_followup = is_followup_question(question, window)
    search_query = question
    rewritten_question = None
    if is_followup:
        rewritten_question, tokens = rewrite_followup(question, window)
        search_query = rewritten_question
        total_tokens += tokens
        llm_calls += 1 if tokens else 0
    steps.append({
        "step": "memory_rewrite",
        "output": f"{len(window)} messages in window; "
                  + (f'rewritten: "{rewritten_question}"' if is_followup else "not a follow-up"),
        "ms": int((time.time() - t0) * 1000),
        "tokens": total_tokens or None,
    })

    # 3. Router: Check if question needs a Tool / Database Query (uses the rewritten question)
    t0 = time.time()
    q_lower = search_query.lower()
    today = date.today()
    emp_info = get_employee_info(employee_id)
    tool_result = None
    tool_name = None
    tool_args = {}
    pending_action = None
    route = "rag"
    route_reason = "Standard Knowledge Base RAG"

    # Tool Intent Matching against hr.db
    if "book" in q_lower and "leave" in q_lower and find_date(q_lower, today):
        route = "multi_step"
        route_reason = "Drafted leave request for user confirmation"
        start = find_date(q_lower, today)
        working_days = int(find_number(q_lower, "days?") or 1)
        leave_type = next((t for t in ("sick", "special", "maternity", "unpaid") if t in q_lower), "annual")
        days = leave_dates(start, working_days)
        pending_action = {
            "action_id": f"act-{int(time.time())}",
            "draft": {
                "leave_type": leave_type,
                "start_date": days[0].isoformat(),
                "end_date": days[-1].isoformat(),
                "working_days": working_days,
            },
        }
        tool_name = "draft_leave_request"
        tool_args = {"start": start.isoformat(), "working_days": working_days, "leave_type": leave_type}
        tool_result = {"draft": pending_action["draft"],
                       "dates": [d.isoformat() for d in days],
                       "note": "Draft only. The worker must press Submit; working days are Monday-Saturday, "
                               "excluding public holidays."}

    elif "leave balance" in q_lower or ("leave" in q_lower and "left" in q_lower):
        route = "tool"
        route_reason = "Queried hr.db for personal leave balance"
        tool_name = "get_leave_balance"
        tool_args = {"year": today.year}
        tool_result = tools.get_leave_balance(employee_id, today.year)

    elif "seniority indemnity" in q_lower or ("seniority" in q_lower and ("get" in q_lower or "pay" in q_lower or "december" in q_lower)):
        route = "tool"
        route_reason = "Calculated seniority indemnity payout using hr.db employee records"
        tool_name = "get_seniority_indemnity"
        tool_result = tools.get_seniority_indemnity(employee_id)

    elif "overtime" in q_lower and ("earn" in q_lower or "extra" in q_lower or "calculate" in q_lower
                                    or "sunday" in q_lower or "night" in q_lower):
        route = "tool"
        route_reason = "Calculated overtime rate using hr.db wage records"
        hours = find_number(q_lower, "hours?") or 1.0
        is_sunday = "sunday" in q_lower or "night" in q_lower
        tool_name = "calculate_overtime"
        tool_args = {"hours": hours, "is_night_or_sunday": is_sunday}
        tool_result = tools.calculate_overtime(employee_id, hours=hours, is_night_or_sunday=is_sunday)

    elif "minimum wage" in q_lower:
        route = "tool"
        route_reason = "Queried hr.db minimum wage table"
        years = sorted({int(y) for y in re.findall(r"\b(20[0-3]\d)\b", q_lower)}) or [today.year]
        tool_name = "get_minimum_wage"
        tool_args = {"years": years}
        tool_result = {str(y): tools.get_minimum_wage(y) for y in years}

    elif "contract" in q_lower and ("end" in q_lower or "notice" in q_lower or "expire" in q_lower or "fdc" in q_lower or "udc" in q_lower):
        if role == "hr_officer" and ("passed" in q_lower or "all" in q_lower or "which" in q_lower):
            route = "tool"
            route_reason = "Queried hr.db for expired FDC contracts across all employees"
            tool_name = "sql_fdc_expired"
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT employee_id, full_name, fdc_end_date FROM employees "
                "WHERE contract_type = 'FDC' AND fdc_end_date < ? ORDER BY fdc_end_date",
                (today.isoformat(),)
            ).fetchall()
            conn.close()
            expired = [dict(r) for r in rows]
            tool_result = {"expired_fdc_contracts": expired, "note": "Contracts passed end date. Flagged for HR review under Article 67."}
        else:
            route = "hybrid"
            route_reason = "Fetched employee contract from hr.db and Labour Law rules from RAG"
            tool_name = "get_employee_contract"
            tool_args = {"employee_id": employee_id}
            tool_result = {
                "employee_id": employee_id,
                "contract_type": emp_info.get("contract_type"),
                "start_date": emp_info.get("start_date"),
                "fdc_end_date": emp_info.get("fdc_end_date"),
                "base_wage_usd_month": emp_info.get("base_wage_usd_month"),
            }

    steps.append({"step": "route", "output": f"{route}: {route_reason}" + (f" -> {tool_name}" if tool_name else ""),
                  "ms": int((time.time() - t0) * 1000)})

    # 4. Context Retrieval (RAG Search from knowledge_base.json)
    t0 = time.time()
    cards = search_knowledge_base(search_query, top_k=4)
    raw_context_str = "\n".join([f"- [{c['citation']}]: {c['text']}" for c in cards])
    clean_context, _ = clean_untrusted(raw_context_str)
    steps.append({"step": "rag_search", "output": f"retrieved {len(cards)} chunks"
                  + (f"; top: {cards[0]['citation']}" if cards else ""), "ms": int((time.time() - t0) * 1000)})

    # Format Tool Data if available
    tool_context_str = ""
    if tool_result:
        tool_text, _ = clean_untrusted(json.dumps(tool_result, indent=2, default=str))
        tool_context_str = f"\n{tool_text}\n"

    # Build Memory Context Window
    memory_window = [f"{m['role'].capitalize()}: {m['content'][:300]}" for m in window]
    history_context_str = "\n".join(memory_window) if memory_window else "None"

    # 5. System Prompt: Clear Separation Between hr.db and knowledge_base.json
    prompt = f"""
Internal Marker: {CANARY}
You are the Mekong Apparel HR Assistant. Answer directly, clearly, and concisely.

DATA SOURCE INSTRUCTIONS:
1. PERSONAL & CALCULATED DATA (from hr.db / Database Tools):
   - 'Tool Calculation Result' contains exact personal records from hr.db for employee {employee_id}. Use it as the primary truth for personal calculations, leave balances, overtime pay, seniority payout amounts, and contract status.
2. GENERAL LAWS & POLICIES (from knowledge_base.json / RAG Context):
   - 'Knowledge Base Context' contains legal articles from Cambodian Labour Law, Social Security Law, and Company Rules. Use it for general policy explanations and always cite specific articles or document names (e.g., [Labour Law Art. 166]).
3. HYBRID QUESTIONS:
   - When answering questions about an employee's personal situation AND the law, combine the Tool Result (for personal figures) with the Knowledge Base Context (for the governing law/article).
4. UNANSWERED QUESTIONS:
   - If neither source contains the answer, reply EXACTLY: {ABSTAIN}

Employee Profile:
- ID: {emp_info.get('employee_id', employee_id)}
- Name: {emp_info.get('full_name', 'Employee')}
- Position: {emp_info.get('position', 'Worker')}
- Department: {emp_info.get('department', 'N/A')}

Recent Conversation History:
{history_context_str}

Knowledge Base Context (from knowledge_base.json):
{clean_context if clean_context.strip() else "None"}

Tool Calculation Result (from hr.db):
{tool_context_str if tool_context_str.strip() else "None"}

User Question: {search_query}
"""

    t0 = time.time()
    try:
        answer, tokens = generate(prompt)
        total_tokens += tokens
        llm_calls += 1
        llm_output = "success"
    except Exception as e:
        answer = f"Error generating answer: {str(e)}"
        tokens = 0
        llm_output = "error"
    llm_ms = int((time.time() - t0) * 1000)
    steps.append({"step": "llm_generate", "output": llm_output, "ms": llm_ms, "tokens": tokens or None})

    # Deduce Citations
    citations = []
    for card in cards:
        if card["kept"]:
            cit = card["citation"]
            if cit not in citations:
                citations.append(cit)

    # 6. Output Guardrail Check
    t0 = time.time()
    out_check = check_output(answer, session, allowed_citations=citations, needs_citation=False)
    if not out_check["ok"] and "system_prompt_leak" in out_check["problems"]:
        answer = REFUSALS["prompt_leak"]
    steps.append({"step": "output_check", "output": "pass" if out_check["ok"] else f"fail: {out_check['problems']}",
                  "ms": int((time.time() - t0) * 1000)})

    # 7. Save Conversation to History
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})

    total_ms = int((time.time() - start_time) * 1000)

    return {
        "answer": answer,
        "turn_id": len(history) // 2,
        "question": question,
        "is_followup": is_followup,
        "rewritten_question": rewritten_question,
        "thread_id": thread_id,
        "guard": guard_res,
        "output_check": out_check,
        "route": route,
        "route_reason": route_reason,
        "citations": citations[:3],
        "steps": steps,
        "cards": cards,
        "memory_window": memory_window,
        "tool_calls": [{"tool": tool_name, "args": tool_args, "summary": str(tool_result)}] if tool_name else [],
        "pending_action": pending_action,
        "llm_calls": llm_calls,
        "total_ms": total_ms,
        "total_tokens": total_tokens,
    }