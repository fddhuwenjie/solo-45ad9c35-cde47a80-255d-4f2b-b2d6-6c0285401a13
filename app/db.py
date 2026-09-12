"""SQLite 持久化：工艺版本、逐栓记录、异常（被拒回传）。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    parent_id INTEGER REFERENCES procedures(id),
    change_note TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    flange_class TEXT NOT NULL,
    bolt_count INTEGER NOT NULL,
    gasket TEXT NOT NULL,
    target_torque REAL NOT NULL,
    stage_ratios TEXT NOT NULL,          -- JSON 数组
    tolerance_pct REAL NOT NULL,
    tool_id TEXT NOT NULL,
    tool_range_min REAL NOT NULL,
    tool_range_max REAL NOT NULL,
    calibration_valid_until TEXT NOT NULL, -- ISO 日期
    start_angle_deg REAL NOT NULL DEFAULT 0,
    clockwise INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    reviewed_at TEXT,
    archived_at TEXT,
    reviewer TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    round_no INTEGER NOT NULL,
    bolt_no INTEGER NOT NULL,
    tool_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    measured_torque REAL NOT NULL,
    rework_of INTEGER REFERENCES records(id),  -- 补拧指向原记录，原记录永不覆盖
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id),
    bolt_no INTEGER,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,                            -- 被拒请求的 JSON
    created_at TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("FLANGE_DB", "flange.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
