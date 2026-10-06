-- Cambodian Workplace Rights / HR assistant: SQLite schema
-- public_holidays and minimum_wage: real public data. employees and leave_requests: fictional Mekong Apparel Co., Ltd.
PRAGMA foreign_keys = ON;

CREATE TABLE employees (
  employee_id          TEXT PRIMARY KEY,   -- E001 ... (the signed-in identity comes from the session, never from the model)
  full_name            TEXT NOT NULL,
  position             TEXT,
  department           TEXT,
  contract_type        TEXT CHECK (contract_type IN ('UDC','FDC')),  -- undetermined / fixed duration contract
  start_date           TEXT NOT NULL,
  fdc_end_date         TEXT,               -- only for FDC
  base_wage_usd_month  REAL NOT NULL,
  shift                TEXT,
  nssf_registered      TEXT CHECK (nssf_registered IN ('yes','no'))
);

CREATE TABLE leave_requests (              -- the chatbot's write target (status 'pending', confirm before insert)
  request_id    TEXT PRIMARY KEY,          -- LR0001 ...
  employee_id   TEXT NOT NULL REFERENCES employees(employee_id),
  leave_type    TEXT CHECK (leave_type IN ('annual','sick','special','maternity','unpaid')),
  start_date    TEXT NOT NULL,
  end_date      TEXT NOT NULL,
  working_days  REAL NOT NULL,
  status        TEXT CHECK (status IN ('pending','approved','rejected','cancelled')),
  reason        TEXT
);
CREATE INDEX leave_by_employee ON leave_requests(employee_id, start_date);

CREATE TABLE public_holidays (
  date     TEXT PRIMARY KEY,
  weekday  TEXT,
  name     TEXT NOT NULL,
  source   TEXT
);

CREATE TABLE minimum_wage (
  year                      INTEGER PRIMARY KEY,
  sector                    TEXT,
  monthly_minimum_wage_usd  REAL NOT NULL,
  note                      TEXT
);