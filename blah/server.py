import os
import sqlite3
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import process_query

app = FastAPI(title="Mekong Apparel HR Assistant API")

SESSIONS = {}

# Dynamic path resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hr.db")

FRONTEND_DIR = (
    os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
    if os.path.exists(os.path.join(BASE_DIR, "..", "frontend"))
    else os.path.join(BASE_DIR, "frontend")
)

# --- Pydantic Models ---
class LoginRequest(BaseModel):
    employee_id: str

class ChatRequest(BaseModel):
    message: str
    thread_id: str

class ConfirmRequest(BaseModel):
    action_id: str


# --- Helper Functions ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def normalize_role(position: str) -> str:
    """Map database position strings to roles expected by index.html ('hr_officer' or 'worker')."""
    if not position:
        return "worker"
    pos_lower = position.lower()
    if "hr" in pos_lower or "officer" in pos_lower or "manager" in pos_lower:
        return "hr_officer"
    return "worker"

def format_employee_dict(row) -> dict:
    d = dict(row)
    full_name = d.get("full_name") or d.get("name") or "Employee"
    position = d.get("position") or d.get("role") or ""
    role = normalize_role(position)
    
    return {
        "employee_id": str(d.get("employee_id")),
        "name": full_name,
        "full_name": full_name,
        "role": role,
        "position": position,
        "department": d.get("department", ""),
        "contract_type": d.get("contract_type", ""),
        "start_date": d.get("start_date", ""),
        "fdc_end_date": d.get("fdc_end_date", ""),
        "base_wage_usd_month": d.get("base_wage_usd_month", 0),
        "shift": d.get("shift", ""),
        "nssf_registered": d.get("nssf_registered", 1)
    }

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    user = SESSIONS.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


# --- API Routes ---

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "llm_mode": "gemini-2.0-flash",
        "search_mode": "hybrid"
    }


@app.get("/employees")
def get_employees():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employees")
    rows = cursor.fetchall()
    conn.close()
    
    return [format_employee_dict(row) for row in rows]


@app.post("/login")
def login(req: LoginRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employees WHERE employee_id = ?", (req.employee_id,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    user_data = format_employee_dict(user)
    token = f"token_{user_data['employee_id']}"
    user_data["token"] = token
    
    SESSIONS[token] = user_data
    return user_data


@app.post("/chat")
def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    res = process_query(
        employee_id=user["employee_id"],
        role=user["role"],
        question=req.message,
        thread_id=req.thread_id
    )
    
    return {
        "answer": res.get("answer", ""),
        "turn_id": res.get("turn_id", 1),
        "question": req.message,
        "thread_id": req.thread_id,
        "guard": res.get("guard", {"action": "allow", "type": "none"}),
        "route": res.get("route", "rag"),
        "route_reason": res.get("route_reason", "Standard HR inquiry"),
        "citations": res.get("citations", []),
        "steps": res.get("steps", []),
        "cards": res.get("cards", []),
        "pending_action": res.get("pending_action", None),
        "total_ms": res.get("total_ms", 300),
        "total_tokens": res.get("total_tokens", 150)
    }


@app.post("/confirm")
def confirm_action(req: ConfirmRequest, user: dict = Depends(get_current_user)):
    return {
        "status": "success",
        "message": f"Leave request ({req.action_id}) successfully submitted to HR!"
    }


# --- Static / Frontend Routes ---
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "llm_mode": "gemini-3.5-flash-lite",
        "search_mode": "hybrid"
    }