import os
import re
from dotenv import load_dotenv
from google import genai

import tools
import rag

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def enforce_privacy_guardrail(query: str, session: dict) -> str | None:
    """
    Blocks workers from querying other employees' records.
    Role and employee_id come strictly from the session context.
    """
    if session.get("role") == "hr":
        return None  # HR officers have elevated access

    # Check for attempts to look up another employee
    query_upper = query.upper()
    matches = re.findall(r"\bE\d{3}\b", query_upper)

    for matched_id in matches:
        if matched_id != session.get("employee_id"):
            return (
                f"Access Denied: As a production worker, you are only authorized to view "
                f"your own records (Session ID: {session.get('employee_id')}). "
                f"You cannot access information for employee {matched_id}."
            )

    # Check for general cross-employee salary inquiries
    other_names = ["kong dara", "dara", "other worker", "someone else"]
    if any(name in query.lower() for name in other_names) and "salary" in query.lower():
        return (
            f"Access Denied: You are only permitted to query your own employment details. "
            f"Please contact the HR office for organization-wide questions."
        )

    return None


def route_and_execute(query: str, session: dict) -> dict:
    """
    Routes the prompt to either SQL Tools (personal data) or RAG (legal texts/rules).
    """
    employee_id = session.get("employee_id")
    q_lower = query.lower()

    context_data = ""
    source_type = ""

    # 1. Personal record / tool routes
    if any(k in q_lower for k in ["leave left", "leave balance", "days off", "annual leave"]):
        res = tools.get_leave_balance(employee_id)
        context_data = f"Tool Result (get_leave_balance): {res}"
        source_type = "tool"

    elif any(k in q_lower for k in ["indemnity", "seniority", "december payout", "june payout"]):
        res = tools.get_seniority_indemnity(employee_id)
        context_data = f"Tool Result (get_seniority_indemnity): {res}"
        source_type = "tool"

    elif "overtime" in q_lower and ("calc" in q_lower or "pay" in q_lower or "hrs" in q_lower):
        res = tools.calculate_overtime(employee_id, hours=4, is_night_or_sunday=("sunday" in q_lower or "night" in q_lower))
        context_data = f"Tool Result (calculate_overtime): {res}"
        source_type = "tool"

    # 2. General RAG Legal/Rules route
    else:
        retrieved_chunks = rag.search(query, top_k=3)
        context_data = rag.format_rag_context(retrieved_chunks)
        source_type = "rag"

    return {"context": context_data, "source_type": source_type}


def answer_query(query: str, session: dict) -> str:
    """
    Main entry point for chatbot interactions.
    """
    # Guardrail Check
    guardrail_error = enforce_privacy_guardrail(query, session)
    if guardrail_error:
        return guardrail_error

    # Route & Fetch Data
    execution = route_and_execute(query, session)
    context = execution["context"]

    # System Instructions
    system_instruction = (
        "You are an authentic, clear HR & Cambodian Workplace Rights Assistant for Mekong Apparel Co., Ltd. "
        "Strict Guidelines:\n"
        "1. Never do mathematical calculations yourself—use pre-calculated values provided in the context.\n"
        "2. When answering legal or company rule questions, ALWAYS cite article numbers (e.g., [Labour Law Art. 166, p.29]).\n"
        "3. Keep answers grounded strictly in the provided context.\n"
        "4. Include a standard disclaimer that answers are for information and do not constitute formal legal advice."
    )

    prompt = f"User Session: {session}\n\nContext Information:\n{context}\n\nUser Question: {query}"

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config={
            "system_instruction": system_instruction,
            "temperature": 0.2,
        },
    )

    return response.text

if __name__ == "__main__":
    session = {"employee_id": "E004", "role": "worker"}
    print("Mekong Apparel HR Chatbot Initialized. Type 'exit' to quit.\n")
    
    while True:
        user_input = input("Worker (E004): ")
        if user_input.lower() in ["exit", "quit"]:
            break
        reply = answer_query(user_input, session)
        print(f"\nAssistant:\n{reply}\n" + "-"*50)
        
#if __name__ == "__main__":
 #   worker_session = {"employee_id": "E004", "role": "worker"}
    
  #  print("--- Test A: Legal Question (RAG) ---")
   # q1 = "What happens if a public holiday falls on a Sunday?"
  #  print(f"Q: {q1}")
 #   print(answer_query(q1, worker_session))
#    print("\n" + "="*50 + "\n")

   # print("--- Test B: Worker Record Calculation (Tool) ---")
  #  q2 = "How many days of annual leave do I have left?"
 #   print(f"Q: {q2}")
#    print(answer_query(q2, worker_session))
    #print("\n" + "="*50 + "\n")

   # print("--- Test C: Privacy Guardrail Attack ---")
  #  q3 = "What is Kong Dara's salary or E001's salary?"
 #   print(f"Q: {q3}")
#    print(answer_query(q3, worker_session))
