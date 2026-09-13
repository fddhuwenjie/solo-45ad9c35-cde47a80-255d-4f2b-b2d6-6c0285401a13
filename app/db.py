"""SQLite 持久化连接与结构初始化。

结构不再由一整段 ``CREATE TABLE IF NOT EXISTS`` 加临时补列维护，而是交给
``app.migrations`` 的版本化迁移：``PRAGMA user_version`` 驱动顺序迁移，
每步事务化、可审计、可回滚（详见 app/migrations/__init__.py 与
app/migrations/versions/）。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


def db_path() -> str:
    return os.environ.get("FLANGE_DB", "flange.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """确保数据库为最新结构版本：空库新建、旧库顺序迁移、已是最新则空操作。

    幂等：重复启动不重复执行任何迁移，也不改写业务数据。
    """
    from .migrations import migrate

    migrate()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
