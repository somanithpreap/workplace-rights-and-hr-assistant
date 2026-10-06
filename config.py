"""
config.py - every setting in one place. Change values here or in .env, not inside other files.
"""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- Gemini -----------------------------------------------------------------
# Copy the exact model ID from Google AI Studio (Model dropdown -> "Get code").
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.5-flash-lite")
# "gemini" = real API calls. "mock" = fake answers, no key, no quota (for testing the plumbing).
LLM_MODE = os.getenv("LLM_MODE", "gemini").lower()
MAX_ANSWER_TOKENS = int(os.getenv("MAX_ANSWER_TOKENS", "700"))
PAUSE_BETWEEN_CALLS = float(os.getenv("PAUSE_BETWEEN_CALLS", "4.5"))   # seconds, for test runs (15 RPM)

# --- Files ------------------------------------------------------------------
# data/raw/        = files in their ORIGINAL form (never edited)
# data/processed/  = what our scripts made from them (this is what the chatbot reads)
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
SEED_DIR = RAW_DIR / "csv"                                  # employees.csv, leave_requests.csv, ...
DB_PATH = Path(os.getenv("DB_PATH", PROCESSED_DIR / "hr.db"))
FX_SAMPLE_PATH = RAW_DIR / "exchange_rate_usd.json"
TRACE_LOG = ROOT / "logs" / "trace.jsonl"
MEMORY_DB = ROOT / "logs" / "memory.db"
CASES_JSONL = ROOT / "eval" / "cases.jsonl"
RESULTS_DIR = ROOT / "results"

# --- Retrieval --------------------------------------------------------------
SEARCH_MODE = os.getenv("SEARCH_MODE", "hybrid")   # bm25 | dense | hybrid
TOP_K = int(os.getenv("TOP_K", "5"))
KEEP_MAX = int(os.getenv("KEEP_MAX", "3"))          # cards sent to the LLM after filtering
MIN_BM25 = float(os.getenv("MIN_BM25", "3.0"))      # relevance filter (keyword score)
MIN_DENSE = float(os.getenv("MIN_DENSE", "0.55"))   # relevance filter (meaning score)

# --- Memory -----------------------------------------------------------------
MEMORY_WINDOW_TURNS = int(os.getenv("MEMORY_WINDOW_TURNS", "6"))  # messages the LLM sees

# --- HR rules (write each one in the Design Decision Record) -----------------
LEAVE_YEAR_BASIS = "calendar year (1 January - 31 December)"
WORKING_DAYS_PER_MONTH = 26          # MLVT guidance convention: daily wage = monthly / 26
HOURS_PER_DAY = 8                    # Labour Law Art. 137 / internal rules section 2
MIN_NOTICE_WORKING_DAYS = 3          # internal rules section 5.1


def today() -> date:
    """Set TODAY=2026-10-05 in .env to freeze the date for a repeatable demo."""
    fixed = os.getenv("TODAY")
    return date.fromisoformat(fixed) if fixed else date.today()


DISCLAIMER = ("This is general information from the documents, not legal advice. "
              "For a decision about your case, ask HR or the Labour Inspector.")
