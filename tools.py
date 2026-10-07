"""Database tools for the Mekong Apparel HR assistant.

All employee-scoped calls must receive the signed-in employee ID from the
server session, never an ID extracted from the user's message.
"""
import json
import os
import sqlite3
import uuid
from datetime import date, timedelta
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hr.db")


_RATE_CACHE = {"rate": None, "fetched_at": 0.0}
_RATE_CACHE_LOCK = threading.Lock()


def get_usd_to_khr_rate() -> float:
    """Fetch the USD/KHR rate from ExchangeRate-API (cached for one hour).

    The bundled JSON file is a response example, not a live data source. On
    network/API failure retain the previous 4,000 KHR/USD fallback.
    """
    with _RATE_CACHE_LOCK:
        if _RATE_CACHE["rate"] is not None and time.monotonic() - _RATE_CACHE["fetched_at"] < 3600:
            return _RATE_CACHE["rate"]
        request = Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "MekongApparelHRAssistant/1.0", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rate = payload.get("rates", {}).get("KHR")
            if payload.get("result") != "success" or payload.get("base_code") != "USD":
                raise ValueError("ExchangeRate-API returned an unsuccessful or unexpected response")
            rate = float(rate)
            if rate <= 0:
                raise ValueError("ExchangeRate-API returned a non-positive KHR rate")
        except (URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
            rate = 4000.0
        _RATE_CACHE.update(rate=rate, fetched_at=time.monotonic())
        return rate


def convert_usd_to_khr(usd_amount: float) -> dict:
    rate = get_usd_to_khr_rate()
    khr_amount = round(usd_amount * rate)
    return {"usd": usd_amount, "khr": khr_amount, "rate": round(rate, 2),
            "formatted": f"${usd_amount:,.2f} USD = {khr_amount:,} KHR (1 USD = {rate:,.2f} KHR)",
            "source": "https://www.exchangerate-api.com"}


def _add_khr_values(result: dict, usd_fields: tuple[str, ...]) -> dict:
    """Add integer KHR equivalents without removing the original USD values."""
    rate = get_usd_to_khr_rate()
    for field in usd_fields:
        if field in result and result[field] is not None:
            khr_field = field[:-4] + "_khr" if field.endswith("_usd") else f"{field}_khr"
            result[khr_field] = round(float(result[field]) * rate)
    result["exchange_rate_usd_to_khr"] = round(rate, 2)
    result["exchange_rate_source"] = "https://www.exchangerate-api.com"
    return result


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _employee(conn, employee_id):
    return conn.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id,)).fetchone()


def _working_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() != 6 and d not in holidays


def _holiday_dates(conn, start: date, end: date) -> tuple[set[date], list[dict]]:
    """Return listed and observed days off, applying Sunday observance once."""
    rows = conn.execute(
        "SELECT date, weekday, name FROM public_holidays WHERE date BETWEEN ? AND ? ORDER BY date",
        ((start - timedelta(days=1)).isoformat(), end.isoformat()),
    ).fetchall()
    all_listed = {date.fromisoformat(r["date"]) for r in rows}
    listed = {d for d in all_listed if start <= d <= end}
    observed = set(listed)
    details = [{"date": r["date"], "name": r["name"], "weekday": r["weekday"], "observed": False}
               for r in rows if start <= date.fromisoformat(r["date"]) <= end]
    # Include a Sunday immediately before the requested range when its Monday
    # substitute falls inside the range.
    for d in sorted(all_listed):
        if d.weekday() == 6 and start <= d + timedelta(days=1) <= end:
            next_day = d + timedelta(days=1)
            observed.add(next_day)
            if next_day not in all_listed:
                details.append({"date": next_day.isoformat(), "name": f"Day off in lieu of {d.isoformat()}",
                                "weekday": next_day.strftime("%A"), "observed": True})
    return observed, sorted(details, key=lambda item: item["date"])


def holidays(from_date: str, to_date: str) -> dict:
    """List public holidays and Sunday-observed days off in an inclusive range."""
    try:
        start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    except ValueError:
        return {"error": "Dates must use YYYY-MM-DD format."}
    if end < start:
        return {"error": "to_date must be on or after from_date."}
    conn = get_db()
    try:
        days, entries = _holiday_dates(conn, start, end)
        return {"from": from_date, "to": to_date, "holidays": entries,
                "days_off": [d.isoformat() for d in sorted(days)],
                "rule": "A public holiday on Sunday gives the following day off (Art. 162)."}
    finally:
        conn.close()


def leave_balance(employee_id: str, year: int = 2026) -> dict:
    """Annual leave estimate: 1.5 days per employed month plus 1/3 years' service."""
    conn = get_db()
    try:
        emp = _employee(conn, employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found."}
        hire = date.fromisoformat(emp["start_date"])
        year_start, year_end = date(year, 1, 1), date(year, 12, 31)
        # Count calendar months with at least one day of employment in the selected year.
        first_month = max(1, hire.month) if hire.year == year else 1
        months_worked = 0 if hire > year_end else 13 - first_month
        service_years = max(0, year_end.year - hire.year - ((year_end.month, year_end.day) < (hire.month, hire.day)))
        bonus = service_years // 3
        accrued = round(months_worked * 1.5 + bonus, 2)
        used, pending = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='approved' THEN working_days ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='pending' THEN working_days ELSE 0 END),0)
                 FROM leave_requests WHERE employee_id=? AND strftime('%Y',start_date)=?""",
            (employee_id, str(year)),
        ).fetchone()
        return {"employee_id": employee_id, "year": year, "months_worked": months_worked,
                "seniority_bonus_days": bonus, "accrued_days": accrued, "approved_days": used,
                "pending_days": pending, "remaining_days": round(accrued-used, 2),
                "if_pending_approved_days": round(accrued-used-pending, 2),
                "assumption": "Leave year is the calendar year; requests are counted in the year their start date falls."}
    finally:
        conn.close()


def seniority_indemnity(employee_id: str) -> dict:
    """Estimate June/December payment and pre-2019 garment-sector back pay."""
    conn = get_db()
    try:
        emp = _employee(conn, employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found."}
        wage = float(emp["base_wage_usd_month"])
        hire = date.fromisoformat(emp["start_date"])
        daily = wage / 26.0
        semester = round(7.5 * daily, 2)
        # Prakas 443 guidance: 6 days for each pre-2019 year of service, capped at 156.
        pre2019_years = max(0, 2019 - hire.year - ((hire.month, hire.day) > (1, 1)))
        backpay_days = min(156, pre2019_years * 6)
        result = {"employee_id": employee_id, "contract_type": emp["contract_type"],
                "base_wage_usd_month": wage, "daily_rate_usd": round(daily, 2),
                "june_indemnity_usd": semester, "december_indemnity_usd": semester,
                "pre_2019_backpay_days": backpay_days,
                "pre_2019_backpay_estimate_usd": round(backpay_days*daily, 2),
                "pre_2019_backpay_cap_days": 156,
                "assumption": "Estimate uses base monthly wage ÷ 26; pre-2019 back pay is estimated at 6 days per completed service year before 2019, capped at 156 days."}
        return _add_khr_values(result, ("base_wage_usd_month", "daily_rate_usd", "june_indemnity_usd",
                                        "december_indemnity_usd", "pre_2019_backpay_estimate_usd"))
    finally:
        conn.close()


def notice_period(employee_id: str, as_of: str | None = None) -> dict:
    """Return Article 75 notice band based on completed continuous service."""
    conn = get_db()
    try:
        emp = _employee(conn, employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found."}
        if emp["contract_type"] != "UDC":
            return {"error": "Article 75 notice bands apply to undetermined duration contracts (UDCs); this employee is on an FDC."}
        today = date.fromisoformat(as_of) if as_of else date.today()
        hire = date.fromisoformat(emp["start_date"])
        months = max(0, (today.year-hire.year)*12 + today.month-hire.month - (today.day < hire.day))
        if months < 6: period = "7 days"
        elif months <= 24: period = "15 days"
        elif months <= 60: period = "1 month"
        elif months <= 120: period = "2 months"
        else: period = "3 months"
        return {"employee_id": employee_id, "contract_type": emp["contract_type"],
                "service_months": months, "notice_period": period,
                "source": "Labour Law Article 75 (UDC notice bands)."}
    finally:
        conn.close()


def overtime_pay(employee_id: str, hours: float, when: str = "normal") -> dict:
    """Estimate overtime at 150%, or 200% at night/on weekly rest day."""
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return {"error": "hours must be a number."}
    if hours <= 0:
        return {"error": "hours must be greater than zero."}
    normalized = when.strip().lower().replace(" ", "_")
    premium = normalized in {"night", "sunday", "weekly_rest_day", "night_or_sunday", "night_and_sunday"}
    if normalized not in {"normal", "day", "weekday", "night", "sunday", "weekly_rest_day", "night_or_sunday", "night_and_sunday"}:
        return {"error": "when must be normal, night, Sunday, or weekly_rest_day."}
    conn = get_db()
    try:
        emp = _employee(conn, employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found."}
        wage = float(emp["base_wage_usd_month"])
        hourly = wage/(26*8)
        multiplier = 2.0 if premium else 1.5
        result = {"employee_id": employee_id, "hours": hours, "when": normalized,
                "multiplier": multiplier, "hourly_base_usd": round(hourly, 4),
                "total_overtime_usd": round(hours*hourly*multiplier, 2),
                "formula": f"{hours:g} hours × monthly wage / 26 / 8 × {multiplier:g}"}
        return _add_khr_values(result, ("hourly_base_usd", "total_overtime_usd"))
    finally:
        conn.close()


def minimum_wage(year: int) -> dict:
    conn = get_db()
    try:
        row = conn.execute("SELECT monthly_minimum_wage_usd, note, sector FROM minimum_wage WHERE year=?", (year,)).fetchone()
        if not row:
            return {"error": f"Minimum wage data for year {year} not found."}
        result = {"year": year, "sector": row["sector"], "minimum_wage_usd": row["monthly_minimum_wage_usd"], "note": row["note"]}
        return _add_khr_values(result, ("minimum_wage_usd",))
    finally:
        conn.close()


def employee_salary(employee_id: str) -> dict:
    """Read an employee's base monthly salary; authorization is enforced by the caller."""
    conn = get_db()
    try:
        emp = _employee(conn, employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found."}
        result = {"employee_id": employee_id, "full_name": emp["full_name"],
                  "base_wage_usd_month": float(emp["base_wage_usd_month"])}
        return _add_khr_values(result, ("base_wage_usd_month",))
    finally:
        conn.close()


def _workdays(start: date, end: date, days_off: set[date]) -> list[date]:
    return [start + timedelta(days=i) for i in range((end-start).days+1)
            if _working_day(start + timedelta(days=i), days_off)]


def create_leave_request(employee_id: str, leave_type: str, start_date: str, end_date: str,
                         reason: str = "", as_of: str | None = None) -> dict:
    """Prepare (do not insert) a leave request; server must get explicit confirmation."""
    if leave_type not in {"annual", "sick", "special", "maternity", "unpaid"}:
        return {"error": "Unsupported leave type."}
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        today = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        return {"error": "Dates must use YYYY-MM-DD format."}
    if end < start or start <= today:
        return {"error": "Leave dates must be in order and start after today."}
    conn = get_db()
    try:
        if not _employee(conn, employee_id):
            return {"error": f"Employee {employee_id} not found."}
        # Need preceding range as well so Sunday holiday observation affects the request date.
        range_start = min(today, start) - timedelta(days=1)
        days_off, _ = _holiday_dates(conn, range_start, end)
        notice_days = sum(1 for d in _workdays(today+timedelta(days=1), start-timedelta(days=1), days_off)) if start > today+timedelta(days=1) else 0
        if notice_days < 3:
            return {"error": "Annual leave requests require at least 3 working days' notice.",
                    "working_days_notice": notice_days}
        requested_days = _workdays(start, end, days_off)
        if not requested_days:
            return {"error": "The selected range contains no working days."}
        if leave_type == "annual":
            balance = leave_balance(employee_id, start.year)
            if balance.get("error"):
                return balance
            if balance["remaining_days"] < len(requested_days):
                return {"error": "Insufficient annual leave balance.", "available_days": balance["remaining_days"],
                        "requested_days": len(requested_days)}
        return {"draft": {"employee_id": employee_id, "leave_type": leave_type,
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "working_days": len(requested_days), "reason": reason,
                "status": "pending", "request_id": "LR" + uuid.uuid4().hex[:12].upper()},
                "working_days_notice": notice_days,
                "message": "Review this pending request and confirm to submit it."}
    finally:
        conn.close()


def submit_leave_request(draft: dict) -> dict:
    """Insert a previously confirmed draft as pending."""
    required = ("request_id", "employee_id", "leave_type", "start_date", "end_date", "working_days", "reason")
    if any(key not in draft for key in required):
        return {"error": "Invalid leave request draft."}
    conn = get_db()
    try:
        conn.execute("""INSERT INTO leave_requests
            (request_id,employee_id,leave_type,start_date,end_date,working_days,status,reason)
            VALUES (?,?,?,?,?,?, 'pending',?)""",
            tuple(draft[key] for key in required))
        conn.commit()
        return {"status": "submitted", "request_id": draft["request_id"], "leave_status": "pending"}
    except sqlite3.IntegrityError as exc:
        return {"error": f"Could not submit leave request: {exc}"}
    finally:
        conn.close()


# Backwards-compatible names used by earlier scripts.
get_leave_balance = leave_balance
get_seniority_indemnity = seniority_indemnity
calculate_overtime = overtime_pay
get_minimum_wage = minimum_wage


if __name__ == "__main__":
    print("Leave balance:", leave_balance("E004"))
    print("Seniority indemnity:", seniority_indemnity("E004"))
    print("Overtime:", overtime_pay("E004", 4))
    print("Minimum wage:", minimum_wage(2026))
