"""数据库迁移命令行工具（不改变服务启动入口）。

用法：
    python -m app.migrate status  [--db PATH]   查看结构版本与结构完整性
    python -m app.migrate backup  [--db PATH]   升级前在线一致性备份
    python -m app.migrate migrate [--db PATH]   顺序升级到最新版本（幂等）

环境变量 ``FLANGE_DB`` 与服务共用；--db 优先。建议升级流程：
``status`` 确认当前版本 -> ``backup`` 备份 -> ``migrate`` 升级 -> ``status`` 复核。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime

from .db import db_path
from .migrations import (LATEST_VERSION, MigrationError, UnknownVersionError,
                         backup_database, get_user_version, migrate, verify_schema)


def _resolve_db(arg: str | None) -> str:
    return arg or db_path()


def cmd_status(args: argparse.Namespace) -> int:
    path = _resolve_db(args.db)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"无法打开数据库 {path}: {exc}", file=sys.stderr)
        return 2
    try:
        version = get_user_version(conn)
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        print(f"database:     {path}")
        print(f"user_version: {version}（代码支持最高版本 {LATEST_VERSION}）")
        print(f"tables:       {', '.join(tables) if tables else '(空库)'}")
        if version > LATEST_VERSION:
            print("拒绝操作：数据库版本高于本代码，请升级服务程序。", file=sys.stderr)
            return 3
        if version == 0:
            if not tables:
                print("空库：启动或 migrate 时将顺序建立到最新版本。")
            else:
                print("user_version=0：迁移时将按现存对象推断旧库结构版本。")
            return 0
        problems = verify_schema(conn, version)
        if problems:
            print("结构核对不通过（疑似升级中断或被手工改动）：", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 4
        print("结构核对通过。")
        return 0
    finally:
        conn.close()


def cmd_backup(args: argparse.Namespace) -> int:
    src = _resolve_db(args.db)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = args.output or f"{src}.{stamp}.bak"
    try:
        backup_database(src, dst)
    except sqlite3.Error as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 2
    print(f"已备份 {src} -> {dst}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    path = _resolve_db(args.db)
    try:
        result = migrate(path,
                         progress=lambda v: print(f"应用迁移 v{v} ..."))
    except UnknownVersionError as exc:
        print(f"拒绝升级：{exc}", file=sys.stderr)
        return 3
    except MigrationError as exc:
        print(f"迁移失败（已回滚到版本 {getattr(exc, 'version', '?')} 之前）：{exc}",
              file=sys.stderr)
        if exc.objects:
            print("相关对象：", file=sys.stderr)
            for obj in exc.objects:
                print(f"  - {obj}", file=sys.stderr)
        return 4
    if result.legacy_detected:
        print(f"识别旧库结构版本 v{result.legacy_version}，"
              f"补盖 user_version（未改写业务对象）。")
    if result.applied:
        print(f"升级完成：v{result.from_version} -> v{result.to_version}，"
              f"执行步骤 {result.applied}。")
    else:
        print(f"已是最新版本 v{result.to_version}，无需升级。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.migrate", description="法兰紧固服务 SQLite 版本化迁移工具")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="数据库路径（缺省取 FLANGE_DB 或 ./flange.db）")
    parser.add_argument("--db", help="数据库路径（缺省取 FLANGE_DB 或 ./flange.db）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="查看结构版本与完整性")
    p_backup = sub.add_parser("backup", parents=[common], help="在线一致性备份")
    p_backup.add_argument("-o", "--output", help="备份输出路径")
    sub.add_parser("migrate", parents=[common], help="顺序升级到最新版本")
    args = parser.parse_args(argv)
    return {
        "status": cmd_status,
        "backup": cmd_backup,
        "migrate": cmd_migrate,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
