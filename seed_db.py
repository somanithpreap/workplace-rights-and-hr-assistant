"""Build the SQLite database for this topic from the CSV files in seed/.

Usage:
    python seed_db.py            # creates hr.db next to this script
    python seed_db.py --reset    # deletes and rebuilds it

Only the Python standard library is used (sqlite3, csv).
"""
import csv, os, sqlite3, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("HR_DB_PATH", HERE / "hr.db"))
# CSV file in seed/  ->  table name in schema.sql
TABLES = {'employees.csv': 'employees',
 'leave_requests.csv': 'leave_requests',
 'public_holidays_2026.csv': 'public_holidays',
 'minimum_wage_garment_sector.csv': 'minimum_wage'}

def main() -> None:
    if "--reset" in sys.argv and DB.exists():
        DB.unlink()
    if DB.exists():
        print(f"{DB.name} already exists; run with --reset to rebuild")
        return
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    for csv_name, table in TABLES.items():
        with open(HERE / "seed" / csv_name, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames
            rows = [[(r[c] if r[c] != "" else None) for c in cols] for r in reader]
        placeholders = ",".join("?" for _ in cols)
        con.executemany(
            f'INSERT INTO {table} ({",".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({placeholders})', rows)
        print(f"{table:<22} {len(rows):>7} rows  <- seed/{csv_name}")
    con.commit()
    con.close()
    print(f"Done: {DB}")

if __name__ == "__main__":
    main()
