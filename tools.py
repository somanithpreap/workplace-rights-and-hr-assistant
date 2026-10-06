"""
tools.py - calculators and database actions. Calculations live in code, never in the LLM.

Every tool returns a dict with:
  tool, result (numbers/rows), summary (one plain sentence WITH the formula),
  assumptions (list), source (citation of the rule it implements)

SAFETY RULES (guideline section 10.2 "tool safety"):
  * employee_id always comes from the login session (pipeline passes it), never from the message.
  * Read-only queries everywhere except confirm_leave_request(), which runs only after a Confirm click.
  * Every SQL statement uses ? placeholders. No SQL is ever built from user text.
"""
import json
import sqlite3
import urllib.request
from datetime import date, timedelta

import config


# ---------------------------------------------------------------- database helpers
def _db():
    if not config.DB_PATH.exists():
        import seed_db                      # builds hr.db from seed/*.csv the first time
        seed_db.main()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _q(sql, params=()):
    with _db() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_employee(employee_id):
    rows = _q("SELECT * FROM employees WHERE employee_id = ?", (employee_id,))
    return rows[0] if rows else None


def all_employee_names():
    return {r["employee_id"]: r["full_name"] for r in _q("SELECT employee_id, full_name FROM employees")}


def role_of(employee_id):
    emp = get_employee(employee_id)
    if not emp:
        return None
    return "hr_officer" if emp["position"].lower().startswith("hr") else "worker"


def _d(s):
    return date.fromisoformat(s)


def _full_years(start, end):
    years = end.year - start.year - ((end.month, end.day) < (start.month, start.day))
    return max(years, 0)


def _full_months(start, end):
    months = (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)
    return max(months, 0)


def _holiday_dates():
    return {r["date"]: r["name"] for r in _q("SELECT date, name FROM public_holidays")}


def is_working_day(d, holidays=None):
    holidays = holidays if holidays is not None else _holiday_dates()
    return d.weekday() != 6 and d.isoformat() not in holidays      # Mon-Sat, not a public holiday


def _service_text(start, on):
    y, m = _full_years(start, on), _full_months(start, on) % 12
    return f"{y} years {m} months"


# ---------------------------------------------------------------- read tools
def my_record(employee_id):
    """The signed-in worker's own contract record."""
    e = get_employee(employee_id)
    start = _d(e["start_date"])
    summary = (f"{e['full_name']} ({e['employee_id']}), {e['position']}, {e['contract_type']} contract since "
               f"{e['start_date']}" + (f", ending {e['fdc_end_date']}" if e["fdc_end_date"] else "")
               + f"; base wage USD {e['base_wage_usd_month']:.0f}/month; service {_service_text(start, config.today())}; "
               f"NSSF registered: {e['nssf_registered']}.")
    return {"tool": "my_record", "result": e, "summary": summary, "assumptions": [],
            "source": "[HR database: employees]"}


def leave_balance(employee_id, year=None):
    """Art. 166-167: 1.5 days/month = 18 days/year, +1 day per 3 years of service; minus approved leave."""
    today = config.today()
    year = year or today.year
    e = get_employee(employee_id)
    start = _d(e["start_date"])
    years = _full_years(start, today)
    rows = _q("SELECT request_id, leave_type, start_date, end_date, working_days, status, reason "
              "FROM leave_requests WHERE employee_id = ? AND substr(start_date,1,4) = ? "
              "ORDER BY start_date", (employee_id, str(year)))
    annual = [r for r in rows if r["leave_type"] == "annual"]
    approved = sum(r["working_days"] for r in annual if r["status"] == "approved")
    pending = sum(r["working_days"] for r in annual if r["status"] == "pending")
    assumptions = [f"Leave year = {config.LEAVE_YEAR_BASIS} (the law does not say; our design decision).",
                   "Full yearly entitlement shown; sick, maternity and special leave are not deducted."]
    if years < 1:
        accrued = 1.5 * _full_months(start, today)
        summary = (f"You have {_service_text(start, today)} of service, so you have accrued {accrued:g} days "
                   f"(1.5 days x {_full_months(start, today)} months), but you can only start using annual leave "
                   f"after one year of service.")
        return {"tool": "leave_balance", "result": {"accrued": accrued, "usable_now": 0, "history": rows},
                "summary": summary, "assumptions": assumptions, "source": "[Labour Law Art. 166-167]"}
    extra = years // 3
    entitlement = 18 + extra
    left = entitlement - approved
    summary = (f"Entitlement for {year}: 18 days + {extra} seniority day(s) ({years} years of service / 3) = "
               f"{entitlement} days. Approved annual leave in {year}: {approved:g} day(s); pending: {pending:g}. "
               f"Left: {entitlement} - {approved:g} = {left:g} days"
               + (f" ({left - pending:g} if pending requests are approved)." if pending else "."))
    return {"tool": "leave_balance",
            "result": {"year": year, "entitlement": entitlement, "approved": approved, "pending": pending,
                       "left": left, "left_if_pending_approved": left - pending, "history": rows},
            "summary": summary, "assumptions": assumptions, "source": "[Labour Law Art. 166-167]"}


def seniority_indemnity(employee_id):
    """Prakas 443 (MLVT guidance): UDC only; 7.5 days in June + 7.5 in December; daily wage = monthly / 26."""
    today = config.today()
    e = get_employee(employee_id)
    wage = e["base_wage_usd_month"]
    daily = wage / config.WORKING_DAYS_PER_MONTH
    if e["contract_type"] == "FDC":
        severance = 0.05 * wage * _full_months(_d(e["start_date"]), _d(e["fdc_end_date"]))
        summary = (f"You are on a fixed duration contract (FDC). Seniority indemnity under Prakas 443 applies to "
                   f"undetermined contracts (UDC) only. At the end of an FDC, Article 73 gives severance pay of at "
                   f"least 5% of wages received during the contract (unless a collective agreement says more): "
                   f"about 5% x USD {wage:.0f} x {_full_months(_d(e['start_date']), _d(e['fdc_end_date']))} months "
                   f"= USD {severance:.2f}. Please confirm with HR.")
        return {"tool": "seniority_indemnity", "result": {"applies": False, "fdc_severance_estimate": round(severance, 2)},
                "summary": summary, "assumptions": ["Base wage only; overtime and bonuses excluded."],
                "source": "[MLVT Seniority Guidance 1. Scope, p.2] [Labour Law Art. 73, p.13]"}

    next_pay = date(today.year, 6, 30) if today.month <= 6 else date(today.year, 12, 31)
    label = "June" if next_pay.month == 6 else "December"
    start = _d(e["start_date"])
    months = _full_months(start, next_pay)
    days = 7.5 if months >= 1 else 0
    # pre-2019 back pay: 15 days per year of seniority 2008-2018, max 156 days, garment sector pays 15 + 15 per year
    back_total = 0
    if start < date(2019, 1, 1):
        back_years = _full_years(max(start, date(2008, 1, 1)), date(2019, 1, 1))
        back_total = min(15 * back_years, 156)
    payments_before = sum(1 for y in range(2019, next_pay.year + 1) for m in (6, 12)
                          if date(y, m, 28) < next_pay.replace(day=28))
    back_left = max(back_total - 15 * payments_before, 0)
    back_now = min(15, back_left)
    amount = (days + back_now) * daily
    summary = (f"{label} {next_pay.year}: {days:g} days x (USD {wage:.0f} / 26) = USD {days * daily:.2f}"
               + (f", plus {back_now:g} days of pre-2019 back pay = USD {amount:.2f} in total." if back_now
                  else (". No pre-2019 back pay left to pay." if back_total else
                        f". No pre-2019 back pay (started {e['start_date']}).")))
    return {"tool": "seniority_indemnity",
            "result": {"payment_month": label, "days": days, "daily_wage": round(daily, 2),
                       "back_pay_days_this_payment": back_now, "amount_usd": round(amount, 2)},
            "summary": summary,
            "assumptions": ["Base wage only (the guidance counts other benefits too).",
                            "26 working days per month, the guidance's own convention.",
                            "Garment sector back-pay pace: 15 days in June + 15 days in December."],
            "source": "[MLVT Seniority Guidance 3A. Payment of seniority indemnity from 2019, p.4]"}


def notice_period(employee_id):
    """Art. 75 (UDC) / Art. 73 (FDC)."""
    today = config.today()
    e = get_employee(employee_id)
    start = _d(e["start_date"])
    if e["contract_type"] == "FDC":
        end = _d(e["fdc_end_date"])
        months = _full_months(start, end)
        notice = "15 days" if months > 12 else ("10 days" if months > 6 else "no minimum stated")
        summary = (f"Your fixed duration contract runs {e['start_date']} to {e['fdc_end_date']} ({months} months). "
                   f"Under Article 73 the employer must tell you about expiry or non-renewal {notice} in advance; "
                   f"without that notice the contract is extended.")
        return {"tool": "notice_period", "result": {"contract": "FDC", "notice": notice}, "summary": summary,
                "assumptions": [], "source": "[Labour Law Art. 73, p.13]"}
    months = _full_months(start, today)
    if months < 6:
        notice = "7 days"
    elif months <= 24:
        notice = "15 days"
    elif months <= 60:
        notice = "1 month"
    elif months <= 120:
        notice = "2 months"
    else:
        notice = "3 months"
    summary = f"With {_service_text(start, today)} of continuous service, the minimum notice is {notice}."
    return {"tool": "notice_period", "result": {"contract": "UDC", "months_of_service": months, "notice": notice},
            "summary": summary, "assumptions": ["Service counted from start date to today."],
            "source": "[Labour Law Art. 75, p.14]"}


def overtime_pay(employee_id, hours=1.0, when="normal"):
    """Art. 139: +50% normal overtime, +100% at night or on the weekly rest day (Sunday)."""
    e = get_employee(employee_id)
    wage = e["base_wage_usd_month"]
    hourly = wage / config.WORKING_DAYS_PER_MONTH / config.HOURS_PER_DAY
    rate = 2.0 if when in ("night", "sunday", "rest_day") else 1.5
    pay = hours * hourly * rate
    other = hours * hourly * (1.5 if rate == 2.0 else 2.0)
    when_text = {"night": "at night", "sunday": "on Sunday (weekly rest day)",
                 "rest_day": "on the weekly rest day"}.get(when, "on a normal day")
    summary = (f"Hourly wage = USD {wage:.0f} / 26 / 8 = USD {hourly:.2f}. {hours:g} hour(s) {when_text} at "
               f"{rate:.0%} = {hours:g} x {hourly:.2f} x {rate} = USD {pay:.2f} "
               f"(for comparison, at {'150%' if rate == 2.0 else '200%'} it would be USD {other:.2f}).")
    return {"tool": "overtime_pay", "result": {"hourly": round(hourly, 2), "rate": rate, "pay_usd": round(pay, 2)},
            "summary": summary,
            "assumptions": ["Base wage only; 26 working days x 8 hours per month.",
                            "Internal rules limit overtime to 2 hours per day and it must be voluntary."],
            "source": "[Labour Law Art. 139, p.25]"}


def holidays(start=None, end=None):
    """Public holidays table + Art. 162 (Sunday holiday -> next day off)."""
    start = start or f"{config.today().year}-01-01"
    end = end or f"{config.today().year}-12-31"
    rows = _q("SELECT date, weekday, name FROM public_holidays WHERE date BETWEEN ? AND ? ORDER BY date",
              (start, end))
    hdates = {r["date"] for r in _q("SELECT date FROM public_holidays")}
    sundays = []
    for r in rows:
        if r["weekday"] == "Sunday":
            nxt = (_d(r["date"]) + timedelta(days=1)).isoformat()
            sundays.append({**r, "next_day": nxt, "next_day_already_holiday": nxt in hdates})
    flag = [s for s in sundays if s["next_day_already_holiday"]]
    summary = (f"{len(rows)} public holiday dates between {start} and {end} in our table (Sub-Decree 167; some news "
               f"articles say 22 days, so HR should confirm the total). Holidays on a Sunday: "
               + (", ".join(f"{s['date']} {s['name']}" for s in sundays) or "none")
               + ". Under Article 162 the next day is off"
               + (f"; but {', '.join(s['next_day'] for s in flag)} is already a holiday, so HR must decide the "
                  f"replacement day." if flag else "."))
    return {"tool": "holidays", "result": {"holidays": rows, "sunday_holidays": sundays}, "summary": summary,
            "assumptions": [], "source": "[Public holidays table 2026] [Labour Law Art. 162, p.29]"}


def minimum_wage(years):
    years = sorted(set(int(y) for y in years))
    marks = ",".join("?" for _ in years)
    rows = _q(f"SELECT year, monthly_minimum_wage_usd AS usd FROM minimum_wage WHERE year IN ({marks}) "
              "ORDER BY year", tuple(years))
    found = {r["year"]: r["usd"] for r in rows}
    summary = "Garment-sector minimum wage: " + "; ".join(
        f"{y}: USD {found[y]:.0f}/month" if y in found else f"{y}: not in our table" for y in years) + "."
    return {"tool": "minimum_wage", "result": rows, "summary": summary, "assumptions": [],
            "source": "[Minimum wage table, garment sector]"}


def expired_fdc_contracts(role):
    """HR-only: FDC contracts past their end date (Art. 67 - flag, do not decide)."""
    if role != "hr_officer":
        return {"tool": "expired_fdc_contracts", "result": [], "summary": "Only HR officers can run this report.",
                "assumptions": [], "source": ""}
    rows = _q("SELECT employee_id, full_name, fdc_end_date FROM employees WHERE contract_type = 'FDC' "
              "AND fdc_end_date < ? ORDER BY fdc_end_date", (config.today().isoformat(),))
    summary = (f"{len(rows)} fixed duration contracts are past their end date: "
               + "; ".join(f"{r['employee_id']} (ended {r['fdc_end_date']})" for r in rows)
               + ". These workers are still on the payroll. Under Article 67 such contracts may have become "
                 "undetermined contracts. Flag for HR review; this is not a legal conclusion.")
    return {"tool": "expired_fdc_contracts", "result": rows, "summary": summary, "assumptions": [],
            "source": "[HR database: employees] [Labour Law Art. 67, p.12]"}


def usd_khr_rate():
    """Live USD->KHR rate from open.er-api.com (free, no key); falls back to the saved sample."""
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=4) as r:
            data, origin = json.load(r), "live API open.er-api.com"
    except Exception:
        data, origin = json.loads(config.FX_SAMPLE_PATH.read_text()), "saved sample (API unreachable)"
    rate = data["rates"]["KHR"]
    return {"tool": "usd_khr_rate", "result": {"khr_per_usd": rate, "updated": data.get("time_last_update_utc")},
            "summary": f"1 USD = {rate:,.0f} KHR ({origin}, updated {data.get('time_last_update_utc')}).",
            "assumptions": ["Exchange rate changes daily."], "source": "[open.er-api.com]"}


# ---------------------------------------------------------------- write tool (two steps)
def draft_leave_request(employee_id, start, working_days, leave_type="annual", reason=""):
    """Step 1: build a draft and check the rules. Nothing is written to the database."""
    today = config.today()
    hol = _holiday_dates()
    first = _d(start)
    problems, notes = [], []
    if first <= today:
        problems.append("The start date must be in the future.")
    while not is_working_day(first, hol):                      # move start off a Sunday/holiday
        notes.append(f"{first.isoformat()} is not a working day, so the leave starts on the next working day.")
        first += timedelta(days=1)
    days, d = [], first
    while len(days) < working_days:
        if is_working_day(d, hol):
            days.append(d)
        d += timedelta(days=1)
    end = days[-1]
    notice = sum(1 for i in range(1, (first - today).days) if is_working_day(today + timedelta(days=i), hol))
    if notice < config.MIN_NOTICE_WORKING_DAYS:
        problems.append(f"Only {notice} working days' notice; the internal rules need at least "
                        f"{config.MIN_NOTICE_WORKING_DAYS} (section 5.1).")
    if leave_type == "annual":
        bal = leave_balance(employee_id, first.year)["result"]
        avail = bal.get("left_if_pending_approved", bal.get("usable_now", 0))
        if working_days > avail:
            problems.append(f"Not enough annual leave: {working_days} requested, {avail:g} available after "
                            f"approved and pending requests.")
        if first.month in (8, 9) and working_days > 3:
            notes.append("August/September: more than 3 days needs the Production Manager's approval (section 5.1).")
    clash = _q("SELECT request_id, start_date, end_date FROM leave_requests WHERE employee_id = ? AND status IN "
               "('pending','approved') AND start_date <= ? AND end_date >= ?",
               (employee_id, end.isoformat(), first.isoformat()))
    if clash:
        problems.append(f"Overlaps existing request {clash[0]['request_id']} ({clash[0]['start_date']} to "
                        f"{clash[0]['end_date']}).")
    draft = {"employee_id": employee_id, "leave_type": leave_type, "start_date": first.isoformat(),
             "end_date": end.isoformat(), "working_days": working_days, "reason": reason[:200],
             "dates": [x.isoformat() for x in days]}
    ok = not problems
    summary = (f"Draft {leave_type} leave: {first:%a %d %b %Y} to {end:%a %d %b %Y}, {working_days} working day(s) "
               f"({', '.join(x.strftime('%d %b') for x in days)}); {notice} working days' notice. "
               + ("All checks passed. Press Confirm to submit it as PENDING for your manager."
                  if ok else "Cannot submit: " + " ".join(problems))
               + (" Note: " + " ".join(notes) if notes else ""))
    return {"tool": "draft_leave_request", "result": {"ok": ok, "draft": draft, "problems": problems, "notes": notes},
            "summary": summary, "assumptions": ["Working days are Monday-Saturday, excluding public holidays."],
            "source": "[Internal Work Rules §5.1 Annual leave] [Labour Law Art. 166-167]"}


def confirm_leave_request(employee_id, draft):
    """Step 2: only called by the Confirm button. Re-checks, then INSERTs with status 'pending'."""
    if draft["employee_id"] != employee_id:
        raise PermissionError("Draft belongs to another employee.")
    check = draft_leave_request(employee_id, draft["start_date"], draft["working_days"],
                                draft["leave_type"], draft.get("reason", ""))
    if not check["result"]["ok"]:
        return {"ok": False, "message": check["summary"]}
    with _db() as con:
        last = con.execute("SELECT MAX(CAST(substr(request_id,3) AS INTEGER)) FROM leave_requests").fetchone()[0]
        rid = f"LR{(last or 0) + 1:04d}"
        con.execute("INSERT INTO leave_requests (request_id, employee_id, leave_type, start_date, end_date, "
                    "working_days, status, reason) VALUES (?,?,?,?,?,?, 'pending', ?)",
                    (rid, employee_id, draft["leave_type"], draft["start_date"], draft["end_date"],
                     draft["working_days"], draft.get("reason", "")))
    return {"ok": True, "request_id": rid,
            "message": f"Submitted {rid}: {draft['start_date']} to {draft['end_date']}, status PENDING. "
                       f"Only a manager can approve or reject it."}
