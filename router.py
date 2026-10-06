"""
router.py - Level 2 "rule router" (design ladder, guideline section 4).

Reads the (rewritten) question and returns:
  {"route": law | my_record | data_lookup | book_leave | hr_report | referral,
   "tools": [(tool_name, args), ...], "use_rag": bool, "reason": "which rule matched"}

Why rules and not an LLM router: our question types have clear words ("my leave", "book",
"public holidays"), rules cost 0 tokens and 0 ms, and they are easy to test. Weakness: unusual
wording can fall through to "law" - that is the trigger to move up to a model router (bonus B3).
"""
import re
from datetime import date

import config

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "half": 0.5}

ME = re.compile(r"\b(my|i|me|i'm|mine)\b", re.I)


def _find_date(text):
    t = text.lower()
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    mon = "|".join(MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({mon})\b|\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if not m:
        return None
    day = int(m.group(1) or m.group(4))
    month = MONTHS[m.group(2) or m.group(3)]
    today = config.today()
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def _find_number(text, unit):
    t = text.lower()
    m = re.search(rf"\b(\d+(?:\.\d+)?)\s*(?:working\s+)?{unit}", t)
    if m:
        return float(m.group(1))
    m = re.search(rf"\b({'|'.join(NUMBERS)})\s+(?:working\s+)?{unit}", t)
    return float(NUMBERS[m.group(1)]) if m else None


def route(question, session):
    q = question.lower()
    me = bool(ME.search(question))

    # 1. write action: book leave (needs a date, otherwise it is a question about the rules)
    if re.search(r"\b(book|request|apply for|submit|take|reserve|schedule)\b.{0,40}\b(leave|days? off)\b", q) \
            and _find_date(question):
        days = _find_number(question, "days?") or 1
        kind = next((k for k in ("sick", "special", "maternity", "unpaid") if k in q), "annual")
        return {"route": "book_leave", "use_rag": False, "reason": "verb 'book/request' + leave + a date",
                "tools": [("draft_leave_request", {"start": _find_date(question).isoformat(),
                                                   "working_days": int(days), "leave_type": kind})]}

    # 2. HR-only report
    if session["role"] == "hr_officer" and re.search(r"(expired|passed|past) (their )?(end|end date)|fixed.duration "
                                                     r"contracts?.{0,40}(ended|expired|passed|past)", q):
        return {"route": "hr_report", "use_rag": True, "reason": "HR role + expired fixed-duration contracts",
                "tools": [("expired_fdc_contracts", {"role": session["role"]})]}

    tools_to_run = []
    # "for 3 years of service" / "after 12 years" = a general question, not about the worker's own record
    hypothetical = re.search(r"\b(for|with|after|of)\s+\d+\s*(years?|months?)\b", q)
    # 3. questions about the signed-in worker's own record
    if me and not hypothetical:
        if re.search(r"\bleave\b.{0,40}\b(left|remaining|balance|have|how many|how much)\b|"
                     r"\b(how many|how much)\b.{0,30}\bleave\b", q):
            tools_to_run.append(("leave_balance", {}))
        if re.search(r"seniority|indemnity", q):
            tools_to_run.append(("seniority_indemnity", {}))
        if re.search(r"\bnotice\b", q):
            tools_to_run.append(("notice_period", {}))
        if re.search(r"overtime|\bot\b|extra hours", q) and _find_number(question, "hours?"):
            when = "sunday" if re.search(r"sunday|weekend|rest day|day off", q) else (
                "night" if "night" in q else "normal")
            tools_to_run.append(("overtime_pay", {"hours": _find_number(question, "hours?"), "when": when}))
        if re.search(r"\b(my )(wage|salary|pay|contract|record|start date|details)\b|\bshow my\b", q) \
                and not tools_to_run:
            tools_to_run.append(("my_record", {}))
        if tools_to_run:
            return {"route": "my_record", "use_rag": True, "tools": tools_to_run,
                    "reason": "first person + " + ", ".join(t for t, _ in tools_to_run)}

    # 4. public data lookups (no personal data)
    years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", q)]
    if "minimum wage" in q and years and re.search(r"what (is|was)|how much|in \d{4}", q):
        tools_to_run.append(("minimum_wage", {"years": years}))
    if re.search(r"public holidays?|water festival|pchum ben|khmer new year", q) and \
            re.search(r"\bwhich\b|\blist\b|how many|\bwhen\b|\bdates?\b|20\d{2}", q):
        tools_to_run.append(("holidays", {}))
    if re.search(r"\briel\b|\bkhr\b|exchange rate|in (usd|dollars?)", q):
        tools_to_run.append(("usd_khr_rate", {}))
    if tools_to_run:
        return {"route": "data_lookup", "use_rag": True, "tools": tools_to_run,
                "reason": "public data: " + ", ".join(t for t, _ in tools_to_run)}

    # 5. disputes and dismissals: explain, then refer (never a final legal opinion)
    if re.search(r"dismiss|fired|fire me|terminat|unfair|dispute|\bsue\b|lawyer|court|harass|retaliat|"
                 r"strike|grievance|complain", q):
        return {"route": "referral", "use_rag": True, "tools": [], "reason": "dispute/dismissal words"}

    # 6. everything else: answer from the documents
    return {"route": "law", "use_rag": True, "tools": [], "reason": "default: document question"}
