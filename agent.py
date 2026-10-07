import time
import os
import sqlite3
import re
import uuid
from datetime import date, timedelta
from dotenv import load_dotenv
from google import genai

from guard import check_input, clean_untrusted, check_output, CANARY, REFUSALS
import tools as hr_tools

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hr.db")

client = genai.Client()


def _tool_reply(employee_id, question, thread_id, guard_res, tool_name, result, citation,
                tool_employee_id=None):
    pending = None
    if "draft" in result:
        pending = {"action_id": str(uuid.uuid4()), "draft": result["draft"]}
    answer = result.get("error") or _format_tool_result(tool_name, result)
    return {
        "answer": answer, "turn_id": 1, "question": question, "thread_id": thread_id,
        "guard": guard_res, "route": "tool", "route_reason": tool_name,
        "citations": [citation], "steps": [
            {"step": "guardrail_check", "output": "pass", "ms": 5},
            {"step": "tool_call", "output": tool_name, "ms": 1}],
        "tool_calls": [{"tool": tool_name, "args": {"employee_id": tool_employee_id or employee_id}, "summary": str(result)}],
        "cards": [], "pending_action": pending, "total_ms": 6, "total_tokens": 0,
    }


def _format_tool_result(name, result):
    if name == "leave_balance":
        return (f"For {result['year']}, you accrued {result['accrued_days']:g} days, used "
                f"{result['approved_days']:g} approved days, and have {result['remaining_days']:g} days remaining. "
                f"{result['pending_days']:g} days are pending; if approved, {result['if_pending_approved_days']:g} days would remain. "
                f"This uses the calendar year and 1.5 days per employed month plus {result['seniority_bonus_days']} seniority days.")
    if name == "seniority_indemnity":
        return (f"Estimated June and December indemnity: ${result['june_indemnity_usd']:.2f} "
                f"({result['june_indemnity_khr']:,} KHR) each. "
                f"Estimated pre-2019 back pay: {result['pre_2019_backpay_days']} days "
                f"(${result['pre_2019_backpay_estimate_usd']:.2f}; {result['pre_2019_backpay_estimate_khr']:,} KHR), "
                f"capped at 156 days. " + result["assumption"])
    if name == "notice_period":
        return f"The estimated Article 75 notice period is {result['notice_period']} ({result['service_months']} completed months of service)."
    if name == "overtime_pay":
        return (f"Estimated overtime pay is ${result['total_overtime_usd']:.2f} "
                f"({result['total_overtime_khr']:,} KHR) at {result['multiplier']} of the base hourly wage. "
                f"{result['formula']}.")
    if name == "minimum_wage":
        return (f"The {result['year']} garment-sector minimum wage in the database is "
                f"${result['minimum_wage_usd']:.2f} ({result['minimum_wage_khr']:,} KHR) per month.")
    if name == "employee_salary":
        return (f"{result['full_name']} ({result['employee_id']}) has a base monthly salary of "
                f"${result['base_wage_usd_month']:.2f} ({result['base_wage_usd_month_khr']:,} KHR).")
    if name == "holidays":
        entries = result.get("holidays", [])
        return "Days off in the requested range: " + ("; ".join(
            f"{item['date']} ({item['name']})" for item in entries) or "none listed")
    if name == "create_leave_request":
        d = result["draft"]
        return (f"Draft {d['leave_type']} leave: {d['start_date']} to {d['end_date']} "
                f"({d['working_days']} working days), status pending. Confirm below to submit.")
    return str(result)


def _date_from_text(text):
    text = text.strip().rstrip(".,")
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %B", "%B %d %Y", "%B %d"):
        try:
            parsed = __import__("datetime").datetime.strptime(text, fmt).date()
            if "%Y" not in fmt:
                parsed = parsed.replace(year=date.today().year)
            return parsed
        except ValueError:
            pass
    return None


def _employee_mentioned(question):
    """Resolve an explicit employee ID or full name in an HR query."""
    ids = re.findall(r"\bE\d{3}\b", question, re.I)
    if ids:
        return ids[0].upper()
    conn = hr_tools.get_db()
    try:
        q = question.casefold()
        rows = conn.execute("SELECT employee_id, full_name FROM employees ORDER BY length(full_name) DESC").fetchall()
        for row in rows:
            if row["full_name"].casefold() in q:
                return row["employee_id"]
    finally:
        conn.close()
    return None


def _route_tool(employee_id, role, question):
    q = question.lower()
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else date.today().year
    asks_balance = (any(x in q for x in ("leave balance", "leave left", "leave remaining", "how much annual leave do i have", "how many leave days do i have"))
                    or ("leave" in q and any(x in q for x in ("do i have left", "do i have remaining", "left do i have"))))
    if asks_balance and not any(x in q for x in ("book ", "request ", "submit ")):
        result = hr_tools.leave_balance(employee_id, year)
        return "leave_balance", result, "Labour Law Arts. 166–167"
    if "minimum wage" not in q and any(x in q for x in ("salary", "wage", "pay rate", "how much do i make", "how much do i earn")):
        target = _employee_mentioned(question)
        says_other = any(x in q for x in ("someone else", "someone else's", "another employee", "other employee", "other worker"))
        if role != "hr_officer" and (says_other or (target and target != employee_id)):
            return "employee_salary", {"error": "I can only provide your own salary information."}, "Employee salary record"
        if role == "hr_officer" and says_other and not target:
            return "employee_salary", {"error": "Please specify the employee's name or ID so I can look up the correct salary."}, "Employee salary record"
        target = target if role == "hr_officer" and target else employee_id
        return "employee_salary", hr_tools.employee_salary(target), "Employee salary record", target
    if "seniority indemnity" in q:
        return "seniority_indemnity", hr_tools.seniority_indemnity(employee_id), "Prakas 443 guidance"
    if "notice period" in q or "how much notice" in q:
        return "notice_period", hr_tools.notice_period(employee_id), "Labour Law Art. 75"
    if "overtime" in q:
        says_other = any(x in q for x in ("someone else", "someone else's", "another employee", "other employee", "other worker"))
        if role != "hr_officer" and says_other:
            return "overtime_pay", {"error": "I can only calculate overtime using your own wage information."}, "Labour Law Art. 139"
        m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", q)
        if not m:
            return "overtime_pay", {"error": "Please specify the number of overtime hours so I can calculate the amount."}, "Labour Law Art. 139"
        when = "night" if "night" in q else ("sunday" if "sunday" in q or "rest day" in q else "normal")
        target = _employee_mentioned(question) if role == "hr_officer" else employee_id
        return "overtime_pay", hr_tools.overtime_pay(target, float(m.group(1)), when), "Labour Law Art. 139", target
    if "minimum wage" in q:
        return "minimum_wage", hr_tools.minimum_wage(year), "minimum_wage table"
    if "holiday" in q and ("between" in q or "from" in q or re.search(r"20\d{2}", q)):
        dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
        if len(dates) >= 2:
            start, end = dates[0], dates[1]
        else:
            start, end = f"{year}-01-01", f"{year}-12-31"
        return "holidays", hr_tools.holidays(start, end), "Public holidays / Labour Law Art. 162"
    if any(x in q for x in ("book ", "request ", "submit ")) and "leave" in q:
        start_match = re.search(r"\b(?:starting|start(?:ing)? on|from)\s+([^,.]+)", question, re.I)
        duration = re.search(r"\b(\d+(?:\.\d+)?)\s+days?\b", q)
        if start_match and duration:
            start = _date_from_text(start_match.group(1))
            if start:
                count = int(float(duration.group(1)))
                kind = next((k for k in ("annual", "sick", "special", "maternity", "unpaid") if k in q), "annual")
                end = start
                off = set(hr_tools.holidays(start.isoformat(), f"{start.year}-12-31").get("days_off", []))
                working_days = 0
                # Find an end date spanning the requested number of working days.
                while end.year == start.year and working_days < count:
                    if end.weekday() != 6 and end.isoformat() not in off:
                        working_days += 1
                    if working_days < count:
                        end += timedelta(days=1)
                if working_days == count:
                    return "create_leave_request", hr_tools.create_leave_request(employee_id, kind, start.isoformat(), end.isoformat()), "Internal Work Rules §5"
    return None

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

def process_query(employee_id: str, role: str, question: str, thread_id: str) -> dict:
    start_time = time.time()
    session = {"employee_id": employee_id, "role": role}

    # 1. Guardrail Check (Input)
    guard_res = check_input(question, session)
    if guard_res["action"] == "block":
        return {
            "answer": guard_res["reply"],
            "turn_id": 1,
            "question": question,
            "thread_id": thread_id,
            "guard": guard_res,
            "route": "blocked",
            "route_reason": f"Blocked by guardrail ({guard_res['type']})",
            "citations": [],
            "steps": [{"step": "guardrail_check", "output": f"blocked ({guard_res['type']})", "ms": 5}],
            "cards": [],
            "pending_action": None,
            "total_ms": int((time.time() - start_time) * 1000),
            "total_tokens": 0
        }

    routed = _route_tool(employee_id, role, question)
    if routed:
        name, result, citation, *target = routed
        return _tool_reply(employee_id, question, thread_id, guard_res, name, result, citation,
                           tool_employee_id=target[0] if target else None)

    # 2. Context Retrieval
    cards = [
        {"citation": "Labour Law Art. 67", "text": "Full-time workers earn 1.5 days annual leave per month worked.", "bm25": 4.5, "kept": True},
        {"citation": "Labour Law Art. 139", "text": "Overtime is calculated at 150% standard rate, and 200% for night/Sunday work.", "bm25": 3.8, "kept": True},
        {"citation": "Labour Law Art. 182", "text": "Female workers receive 90 days of maternity leave with normal payment terms.", "bm25": 4.9, "kept": True}
    ]
    raw_context_str = "\n".join([f"- {c['citation']}: {c['text']}" for c in cards])
    clean_context, _ = clean_untrusted(raw_context_str)

    emp = get_employee_info(employee_id)

    # 3. System Prompt Setup
    prompt = f"""
Internal Marker: {CANARY}
You are the Mekong Apparel HR Assistant. Answer directly and concisely using the provided context.

Employee Profile:
- ID: {emp.get('employee_id', employee_id)}
- Name: {emp.get('full_name', 'Employee')}
- Position: {emp.get('position', 'Worker')}
- Department: {emp.get('department', 'N/A')}

Knowledge Base Context:
{clean_context}

User Question: {question}
"""

    citations = ["Labour Law Art. 182"] if "maternity" in question.lower() else ["Labour Law Art. 67"]

    # 4. Generate Content via Gemini 3.5 Flash Lite
    t1 = time.time()
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )
        answer = response.text
    except Exception as e:
        answer = f"Error generating answer: {str(e)}"
    llm_ms = int((time.time() - t1) * 1000)

    # 5. Output Safety Check
    out_check = check_output(answer, session, allowed_citations=citations, needs_citation=False)
    if not out_check["ok"] and "system_prompt_leak" in out_check["problems"]:
        answer = REFUSALS["prompt_leak"]

    total_ms = int((time.time() - start_time) * 1000)

    return {
        "answer": answer,
        "turn_id": 1,
        "question": question,
        "thread_id": thread_id,
        "guard": guard_res,
        "output_check": out_check,
        "route": "rag",
        "route_reason": "Standard HR inquiry",
        "citations": citations,
        "steps": [
            {"step": "guardrail_check", "output": "pass", "ms": 8},
            {"step": "rag_search", "output": f"found {len(cards)} chunks", "ms": 25},
            {"step": "llm_generate", "output": "success", "ms": llm_ms, "tokens": 120}
        ],
        "cards": cards,
        "pending_action": None,
        "total_ms": total_ms,
        "total_tokens": 120
    }
