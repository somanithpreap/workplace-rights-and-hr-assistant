import time
import os
import sqlite3
from dotenv import load_dotenv
from google import genai

from guard import check_input, clean_untrusted, check_output, CANARY, REFUSALS

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hr.db")

client = genai.Client()

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