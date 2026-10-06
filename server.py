import os
import sqlite3
import uuid
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import process_query

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hr.db")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="Mekong Apparel HR Assistant")

SESSIONS: Dict[str, dict] = {}


# --- Request Models ---
class LoginRequest(BaseModel):
    employee_id: str


class ChatRequest(BaseModel):
    question: Optional[str] = None
    message: Optional[str] = None
    thread_id: Optional[str] = "default"


class ConfirmRequest(BaseModel):
    action: str
    data: dict


# --- Database & Safe Data Normalization ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_employee(row_dict: dict) -> dict:
    """Converts DB values into safe primitive types with field aliases for React component compatibility."""
    item = {}
    for k, v in row_dict.items():
        item[k] = "" if v is None else v

    # Standard ID Mapping
    eid = str(item.get("employee_id") or item.get("id") or "").upper()
    item["employee_id"] = eid
    item["id"] = eid

    # Standard Name Mapping
    name = str(
        item.get("full_name")
        or item.get("name")
        or item.get("employee_name")
        or "Employee"
    )
    item["full_name"] = name
    item["name"] = name
    item["employee_name"] = name

    # Position & Role Mapping
    position = str(
        item.get("position")
        or item.get("role")
        or item.get("title")
        or "Worker"
    )
    item["position"] = position

    pos_lower = position.lower()
    item["role"] = (
        "hr_officer" if ("hr" in pos_lower or "officer" in pos_lower) else "worker"
    )

    # Secondary Profile Fields
    item["department"] = str(item.get("department") or "Production")
    item["gender"] = str(item.get("gender") or "N/A")

    # Safe Numeric Conversion
    try:
        wage = float(
            item.get("base_wage_usd_month")
            or item.get("wage")
            or item.get("salary")
            or 0
        )
    except (ValueError, TypeError):
        wage = 0.0

    item["base_wage_usd_month"] = wage
    item["wage"] = wage
    item["salary"] = wage

    try:
        leave = int(
            item.get("leave_balance_days") or item.get("leave_balance") or 18
        )
    except (ValueError, TypeError):
        leave = 18

    item["leave_balance_days"] = leave
    item["leave_balance"] = leave

    # Dates
    start = str(
        item.get("start_date") or item.get("hire_date") or "2023-01-01"
    )
    item["start_date"] = start
    item["hire_date"] = start

    return item


def get_all_employees():
    if not os.path.exists(DB_PATH):
        return []
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employees")
    rows = cursor.fetchall()
    conn.close()

    return [normalize_employee(dict(r)) for r in rows]


# --- API Routes ---


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "llm_mode": "gemini-3.5-flash-lite",
        "search_mode": "hybrid",
    }


@app.get("/employees")
def list_employees():
    return get_all_employees()


@app.post("/login")
def login(req: LoginRequest):
    employees = get_all_employees()
    target_id = req.employee_id.strip().upper()

    emp = next((e for e in employees if e["employee_id"] == target_id), None)
    if not emp:
        emp = next((e for e in employees if target_id in e["employee_id"]), None)

    if not emp:
        raise HTTPException(
            status_code=404, detail=f"Employee '{req.employee_id}' not found"
        )

    token = str(uuid.uuid4())
    SESSIONS[token] = emp

    # Universal payload satisfying all React response mappings
    payload = {
        "token": token,
        "access_token": token,
        "user": emp,
        "employee": emp,
        "history": [],
        "messages": [],
        "threads": [],
        **emp,  # Flattened keys directly on response
    }
    return payload


@app.post("/chat")
def chat(req: ChatRequest, authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.split(" ")[1]
    session = SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")

    # Fallback between 'question' and 'message' key
    user_query = req.question or req.message or ""
    if not user_query.strip():
        raise HTTPException(status_code=400, detail="Query text cannot be empty")

    response = process_query(
        employee_id=session["employee_id"],
        role=session["role"],
        question=user_query,
        thread_id=req.thread_id or "default"
    )
    return response


@app.post("/confirm")
def confirm_action(
    req: ConfirmRequest, authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return {
        "status": "success",
        "message": f"Action '{req.action}' confirmed successfully.",
        "details": req.data,
    }


# --- Static Frontend Serving ---
@app.get("/")
def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="index.html missing")


if os.path.exists(FRONTEND_DIR):
    app.mount(
        "/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static"
    )