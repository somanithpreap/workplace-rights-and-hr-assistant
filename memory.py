"""
memory.py - conversation memory (guideline section 9).

  * store:   every message saved in SQLite (logs/memory.db), grouped by thread_id AND employee_id
  * window:  only the last MEMORY_WINDOW_TURNS messages are given to the LLM
  * rewrite: a follow-up ("And at night?") becomes a full question BEFORE search
  * isolation: a thread can only be read by the employee who owns it
"""
import re
import sqlite3
import time

import config
from llm import generate

REWRITE_SYSTEM = (
    "Rewrite the latest message as one standalone search question about Cambodian workplace rules. "
    "Use the history only to understand words like 'it', 'that', 'and for...', 'what about...'. "
    "Keep all names, numbers, article numbers and dates exactly. Output only the question.")

FOLLOWUP_START = re.compile(r"^(and|but|also|what about|how about|what if|who|same|then|so|or|in that case|"
                            r"and at|and for|and if|what else)\b", re.I)
PRONOUN = re.compile(r"\b(it|that|this|those|these|they|them|there|then)\b", re.I)


def _con():
    config.MEMORY_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(config.MEMORY_DB)
    con.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, "
                "employee_id TEXT, speaker TEXT, content TEXT, rewritten TEXT, ts REAL)")
    return con


def save(thread_id, employee_id, speaker, content, rewritten=None):
    with _con() as con:
        con.execute("INSERT INTO messages (thread_id, employee_id, speaker, content, rewritten, ts) "
                    "VALUES (?,?,?,?,?,?)", (thread_id, employee_id, speaker, content, rewritten, time.time()))


def load(thread_id, employee_id, window=None):
    """Last N messages of this thread, only if it belongs to this employee (thread isolation)."""
    window = window or config.MEMORY_WINDOW_TURNS
    with _con() as con:
        rows = con.execute("SELECT speaker, content, rewritten FROM messages WHERE thread_id = ? AND "
                           "employee_id = ? ORDER BY id DESC LIMIT ?", (thread_id, employee_id, window)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM messages WHERE thread_id = ? AND employee_id = ?",
                            (thread_id, employee_id)).fetchone()[0]
    history = [{"speaker": s, "content": c, "rewritten": r} for s, c, r in reversed(rows)]
    return history, total


def is_followup(message, history):
    if not history:
        return False
    words = message.split()
    return bool(FOLLOWUP_START.search(message.strip())) or len(words) <= 5 or (
        len(words) <= 9 and PRONOUN.search(message) is not None)


def rewrite(message, history):
    """Returns (standalone_question, llm_stats)."""
    lines = []
    for h in history:
        if h["speaker"] == "user":
            lines.append(f"User: {h['rewritten'] or h['content']}")
        else:
            lines.append(f"Assistant: {h['content'][:300]}")
    prompt = "History:\n" + "\n".join(lines) + f"\n\nLatest message: {message}"
    out = generate(REWRITE_SYSTEM, prompt, purpose="rewrite", max_tokens=120)
    q = out["text"].strip().strip('"').split("\n")[0] or message
    return q, out
