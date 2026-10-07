import os
import re
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_DB = os.getenv("MEMORY_DB_PATH", os.path.join(BASE_DIR, "memory.db"))   # separate from hr.db, ignored by Git (*.db)
WINDOW = 6                                        # messages the LLM sees (3 questions + 3 answers)

REWRITE_PROMPT = (
    "Rewrite the latest message as one standalone search question about Cambodian workplace rules. "
    "Use the history only to understand words like 'it', 'that', 'and for...', 'what about...'. "
    "Keep all names, numbers, article numbers and dates exactly. Output only the question.\n\n"
    "History:\n{history}\n\nLatest message: {question}"
)

FOLLOWUP_START = re.compile(r"^(and|but|also|what about|how about|what if|who|same|then|so|or)\b", re.I)
PRONOUN = re.compile(r"\b(it|that|this|those|these|they|them|there|then)\b", re.I)


def _connect():
    conn = sqlite3.connect(MEMORY_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " thread_id TEXT NOT NULL,"
        " employee_id TEXT NOT NULL,"
        " role TEXT NOT NULL,"
        " content TEXT NOT NULL,"
        " timestamp TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    return conn


def load(thread_id, employee_id, window=WINDOW):
    """Last `window` messages of this thread, only if it belongs to this employee."""
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE thread_id = ? AND employee_id = ? "
        "ORDER BY id DESC LIMIT ?", (thread_id, employee_id, window)).fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def save(thread_id, employee_id, question, answer):
    conn = _connect()
    conn.executemany(
        "INSERT INTO messages (thread_id, employee_id, role, content) VALUES (?, ?, ?, ?)",
        [(thread_id, employee_id, "user", question), (thread_id, employee_id, "assistant", answer)])
    conn.commit()
    conn.close()


def is_followup(question, history):
    if not history:
        return False
    words = question.split()
    return bool(FOLLOWUP_START.search(question.strip())) or len(words) <= 5 or (
        len(words) <= 9 and PRONOUN.search(question) is not None)


def rewrite(question, history, generate):
    """Turn a follow-up into a full question. `generate(prompt) -> text` calls the LLM."""
    lines = "\n".join(f"{h['role']}: {h['content'][:300]}" for h in history)
    text = generate(REWRITE_PROMPT.format(history=lines, question=question)) or ""
    return text.strip().strip('"').split("\n")[0] or question
