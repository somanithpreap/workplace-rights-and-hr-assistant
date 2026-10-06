from datetime import datetime
import sqlite3

DB_PATH = "hr.db"

import json
import os

def get_usd_to_khr_rate(json_path: str = "api/exchange_rate_usd.json") -> float:
    """Reads the USD to KHR exchange rate from the local JSON file."""
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("rates", {}).get("KHR", 4000.0)
    return 4000.0  # Fallback standard rate

def convert_usd_to_khr(usd_amount: float) -> dict:
    """Converts USD amount to KHR for legal fines/penalties."""
    rate = get_usd_to_khr_rate()
    khr_amount = round(usd_amount * rate)
    return {
        "usd": usd_amount,
        "khr": khr_amount,
        "rate": round(rate, 2),
        "formatted": f"${usd_amount:,.2f} USD = {khr_amount:,} KHR (1 USD = {rate:,.2f} KHR)"
    }

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_leave_balance(employee_id: str, year: int = 2026) -> dict:
    """Calculates accrued vs used annual leave days (Labour Law Art. 166-167)."""
    conn = get_db()
    cursor = conn.cursor()

    emp = cursor.execute(
        "SELECT start_date FROM employees WHERE employee_id = ?", (employee_id,)
    ).fetchone()
    if not emp:
        return {"error": f"Employee {employee_id} not found."}

    # 1. Calculate tenure & accrued days (18 base days/year + 1 bonus day per 3 years service)
    start_date = datetime.strptime(emp["start_date"], "%Y-%m-%d")
    current_year_end = datetime(year, 12, 31)
    years_of_service = (current_year_end - start_date).days // 365
    bonus_days = years_of_service // 3
    total_accrued = 18 + bonus_days

    # 2. Query used approved leave using 'working_days' column
    used = cursor.execute(
        """
        SELECT COALESCE(SUM(working_days), 0) 
        FROM leave_requests 
        WHERE employee_id = ? AND status = 'approved' AND strftime('%Y', start_date) = ?
        """,
        (employee_id, str(year)),
    ).fetchone()[0]

    conn.close()

    remaining = total_accrued - used
    return {
        "employee_id": employee_id,
        "year": year,
        "accrued_days": total_accrued,
        "used_days": used,
        "remaining_days": remaining,
        "breakdown": f"18 base days + {bonus_days} seniority days - {used} used days = {remaining} days remaining",
    }


def get_seniority_indemnity(employee_id: str) -> dict:
    """Calculates semi-annual seniority indemnity pay (Prakas 443 guidance)."""
    conn = get_db()
    cursor = conn.cursor()

    emp = cursor.execute(
        "SELECT base_wage_usd_month, contract_type FROM employees WHERE employee_id = ?",
        (employee_id,),
    ).fetchone()

    if not emp:
        return {"error": f"Employee {employee_id} not found."}

    wage = emp["base_wage_usd_month"]
    daily_rate = wage / 26.0
    semester_payout = round(7.5 * daily_rate, 2)  # 7.5 days per semester

    conn.close()
    return {
        "employee_id": employee_id,
        "contract_type": emp["contract_type"],
        "base_wage_usd_month": wage,
        "daily_rate_usd": round(daily_rate, 2),
        "june_indemnity_usd": semester_payout,
        "december_indemnity_usd": semester_payout,
        "formula": f"7.5 days x (${wage:.2f} / 26 working days) = ${semester_payout:.2f}",
    }


def calculate_overtime(
    employee_id: str, hours: float, is_night_or_sunday: bool = False
) -> dict:
    """Calculates overtime pay rate (Labour Law Art. 139)."""
    conn = get_db()
    cursor = conn.cursor()

    emp = cursor.execute(
        "SELECT base_wage_usd_month FROM employees WHERE employee_id = ?",
        (employee_id,),
    ).fetchone()

    if not emp:
        return {"error": f"Employee {employee_id} not found."}

    wage = emp["base_wage_usd_month"]
    hourly_rate = wage / (26 * 8)
    multiplier = 2.0 if is_night_or_sunday else 1.5
    total_ot_pay = hours * hourly_rate * multiplier

    conn.close()
    return {
        "employee_id": employee_id,
        "hours": hours,
        "multiplier": f"{multiplier}x",
        "hourly_base_usd": round(hourly_rate, 2),
        "total_overtime_usd": round(total_ot_pay, 2),
        "formula": f"{hours} hrs x ${hourly_rate:.2f}/hr x {multiplier}x = ${total_ot_pay:.2f}",
    }


def get_minimum_wage(year: int = 2026) -> dict:
    """Queries minimum wage history for garment sector."""
    conn = get_db()
    cursor = conn.cursor()

    row = cursor.execute(
        "SELECT monthly_minimum_wage_usd, note FROM minimum_wage WHERE year = ?",
        (year,),
    ).fetchone()
    conn.close()

    if row:
        return {
            "year": year,
            "minimum_wage_usd": row["monthly_minimum_wage_usd"],
            "note": row["note"],
        }
    return {"error": f"Minimum wage data for year {year} not found."}


if __name__ == "__main__":
    print("--- Testing Employee E004 ---")
    print("Leave Balance:", get_leave_balance("E004"))
    print("Seniority Indemnity:", get_seniority_indemnity("E004"))
    print("Overtime Pay (4 hrs normal):", calculate_overtime("E004", hours=4))
    print("Minimum Wage 2026:", get_minimum_wage(2026))