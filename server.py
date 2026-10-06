"""
server.py - our own FastAPI backend (bonus B1).

Run:   uvicorn server:app --port 8000
Open:  http://127.0.0.1:8000        -> the React chat screen (web/index.html)
Test:  http://127.0.0.1:8000/docs   -> FastAPI's automatic test page

Endpoints
  POST /login    {"employee_id": "E004"}                 -> {"token", "employee_id", "role", "name"}
  POST /chat     {"message": "...", "thread_id": "t-1"}  + header  Authorization: Bearer <token>
  POST /confirm  {"action_id": "..."}                    + header  -> inserts the leave request as PENDING
  GET  /trace/{turn_id}                                  + header  -> the state of one turn (own turns only)

The role comes from the database at login. The /chat body has NO employee_id field on purpose:
identity can only come from the token, never from the message.
"""
import json
import secrets
import traceback

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import config
import pipeline
import tools

app = FastAPI(title="Mekong Apparel HR Assistant", version="1.0")
# only needed if someone runs the React app from a separate dev server (e.g. Vite on port 5173)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
WEB_DIR = config.ROOT / "web"


@app.exception_handler(Exception)
async def show_error(request: Request, exc: Exception):
    """Send the real error back as JSON (and print it here) instead of a bare 'Internal Server Error'."""
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {str(exc)[:300]}"})


SESSIONS = {}     # token -> {"employee_id", "role"}   (demo login; a real system would check a password)


class LoginIn(BaseModel):
    employee_id: str = Field(pattern=r"^E\d{3}$")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    thread_id: str | None = Field(default=None, max_length=64)


class ConfirmIn(BaseModel):
    action_id: str


def current_session(authorization: str = Header(...)):
    token = authorization.removeprefix("Bearer ").strip()
    if token not in SESSIONS:
        raise HTTPException(401, "Not logged in")
    return SESSIONS[token]


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/employees")
def employees():
    """Demo login list. A real system would use passwords instead of a public list of names."""
    return [{"employee_id": eid, "name": name, "role": tools.role_of(eid)}
            for eid, name in tools.all_employee_names().items()]


@app.post("/login")
def login(body: LoginIn):
    emp = tools.get_employee(body.employee_id)
    if not emp:
        raise HTTPException(404, "Unknown employee")
    token = secrets.token_urlsafe(16)
    SESSIONS[token] = {"employee_id": emp["employee_id"], "role": tools.role_of(emp["employee_id"])}
    return {"token": token, **SESSIONS[token], "name": emp["full_name"]}


@app.post("/chat")
def chat(body: ChatIn, session: dict = Depends(current_session)):
    thread = f"{session['employee_id']}:{body.thread_id or 'default'}"     # threads are per employee
    state = pipeline.chat(body.message, session, thread)
    r = state["route"] or {}
    return {"answer": state["answer"], "turn_id": state["turn_id"], "thread_id": state["thread_id"],
            "question": state["question"], "rewritten_question": state["rewritten_question"],
            "is_followup": state["is_followup"], "route": r.get("route"), "route_reason": r.get("reason"),
            "guard": state["guard"], "tool_calls": state["tool_calls"], "cards": state["cards"],
            "memory_window": state.get("memory_window", []), "output_check": state["output_check"],
            "citations": state["citations"], "pending_action": state["pending_action"], "steps": state["steps"],
            "total_ms": state["total_ms"], "total_tokens": state["total_tokens"], "llm_calls": state["llm_calls"]}


@app.post("/confirm")
def confirm(body: ConfirmIn, session: dict = Depends(current_session)):
    return pipeline.confirm(body.action_id, session)


@app.get("/trace/{turn_id}")
def trace(turn_id: str, session: dict = Depends(current_session)):
    if config.TRACE_LOG.exists():
        for line in reversed(config.TRACE_LOG.read_text(encoding="utf-8").splitlines()):
            t = json.loads(line)
            if t["turn_id"] == turn_id and t["employee_id"] == session["employee_id"]:
                return t
    raise HTTPException(404, "Trace not found")


@app.get("/health")
def health():
    return {"ok": True, "llm_mode": config.LLM_MODE, "model": config.CHAT_MODEL, "search_mode": config.SEARCH_MODE}
