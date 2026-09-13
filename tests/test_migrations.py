"""版本化迁移测试：逐级/跨级升级、中断恢复、未知版本拒绝、备份与业务数据可读。

冻结旧库夹具在 tests/fixtures/*.db（user_version=0 的各历史阶段快照），
由 fixtures/build_fixtures.py 从 fixtures/legacy_fixtures.py 生成。测试始终
把冻结文件复制到 tmp_path 再升级，绝不改写夹具本身。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import migrations as mig
from app.migrations import (LATEST_VERSION, MigrationError, UnknownVersionError,
                            backup_database, get_user_version, migrate, split_sql,
                            verify_schema)
from fixtures.legacy_fixtures import FIXTURE_NAMES, build_fixture

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# 夹具名 -> 该冻结库所代表的结构版本
FIXTURE_VERSION = {
    "legacy_v1": 1,
    "legacy_ultrasonic_v2": 2,
    "legacy_curve_v3": 3,
    "legacy_alignment_v4": 4,
    "legacy_plan_v5": 5,
    "legacy_tensioning_v6": 6,
    "legacy_full_v7": 7,
}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def copy_fixture(name: str, tmp_path: Path) -> str:
    src = FIXTURE_DIR / f"{name}.db"
    dst = tmp_path / f"{name}.db"
    assert src.exists(), f"缺少冻结夹具 {src}，请先运行 tests/fixtures/build_fixtures.py"
    import shutil
    shutil.copy2(src, dst)
    return str(dst)


def table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def make_empty_db(path: Path) -> str:
    p = str(path / "empty.db")
    sqlite3.connect(p).close()
    return p


def make_v1_fresh(path: Path) -> str:
    """空库只升到 v1（模拟服务在首个结构版本上运行）。"""
    p = str(path / "v1.db")
    sqlite3.connect(p).close()
    conn = sqlite3.connect(p)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")
        for stmt in split_sql(mig.MIGRATIONS[0].sql):
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return p


# --------------------------------------------------------------------------- #
# 清单完整性与 SQL 切分
# --------------------------------------------------------------------------- #


def test_migration_chain_is_contiguous():
    assert [m.version for m in mig.MIGRATIONS] == list(
        range(1, LATEST_VERSION + 1))


@pytest.mark.parametrize("migration", mig.MIGRATIONS, ids=lambda m: f"v{m.version}")
def test_migration_sql_parses(migration):
    assert split_sql(migration.sql), f"{migration.name} 没有可执行语句"


# --------------------------------------------------------------------------- #
# 空库
# --------------------------------------------------------------------------- #


def test_empty_db_builds_full_schema(tmp_path):
    path = make_empty_db(tmp_path)
    result = migrate(path)
    assert result.from_version == 0
    assert result.to_version == LATEST_VERSION
    assert result.applied == list(range(1, LATEST_VERSION + 1))
    conn = open_db(path)
    try:
        assert get_user_version(conn) == LATEST_VERSION
        assert verify_schema(conn, LATEST_VERSION) == []
        assert mig.foreign_key_violations(conn) == []
    finally:
        conn.close()


def test_repeated_startup_is_noop_and_preserves_user_version(tmp_path):
    path = make_empty_db(tmp_path)
    migrate(path)
    # 用错误的 user_version 之外的对象指纹确认第二次启动不触碰任何表
    conn = open_db(path)
    before = [tuple(r) for r in conn.execute(
        "SELECT name, rootpage FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()

    second = migrate(path)
    assert second.applied == []
    assert second.legacy_stamped is False
    assert second.changed is False

    conn = open_db(path)
    after = [tuple(r) for r in conn.execute(
        "SELECT name, rootpage FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    assert before == after


# --------------------------------------------------------------------------- #
# 逐级升级（v1 -> v2 -> ... -> v7，每步一个版本）
# --------------------------------------------------------------------------- #


def test_stepwise_upgrade(tmp_path, monkeypatch):
    path = make_v1_fresh(tmp_path)
    conn = open_db(path)
    conn.execute(
        "INSERT INTO procedures (version, status, flange_class, bolt_count, gasket,"
        " target_torque, stage_ratios, tolerance_pct, tool_id, tool_range_min,"
        " tool_range_max, calibration_valid_until, created_at)"
        " VALUES (1,'in_progress','PN40',8,'G',320,'[1.0]',5,'T',50,500,'2026-12-31','t')")
    proc_row = conn.execute("SELECT * FROM procedures").fetchone()
    proc_keys = set(proc_row.keys())
    proc_snapshot = dict(proc_row)
    assert "curve_direction" not in proc_snapshot  # v1 结构尚无轨迹复核列
    conn.commit()
    conn.close()

    all_migrations = mig.MIGRATIONS
    for target in range(2, LATEST_VERSION + 1):
        # 模拟服务程序每次只新到下一个版本：迁移链只到 target
        monkeypatch.setattr(mig, "MIGRATIONS", all_migrations[:target])
        result = migrate(path)
        assert result.to_version == target
        assert result.applied == [target]
        conn = open_db(path)
        try:
            assert get_user_version(conn) == target
            assert verify_schema(conn, target) == []
            # 每一步后既有工艺行原列原样保留
            row = dict(conn.execute(
                "SELECT * FROM procedures WHERE id=1").fetchone())
            assert {k: row[k] for k in proc_keys} == proc_snapshot
        finally:
            conn.close()
    monkeypatch.undo()


# --------------------------------------------------------------------------- #
# 跨级升级（每个冻结旧库夹具直接升到最新）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_legacy_fixture_is_user_version_zero(name):
    src = open_db(FIXTURE_DIR / f"{name}.db")
    try:
        assert get_user_version(src) == 0
    finally:
        src.close()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_cross_version_upgrade_from_frozen_fixture(name, tmp_path):
    path = copy_fixture(name, tmp_path)
    start_version = FIXTURE_VERSION[name]

    conn = open_db(path)
    inferred = mig._infer_legacy_version(conn)
    conn.close()
    assert inferred == start_version

    result = migrate(path)
    assert result.from_version == 0
    assert result.legacy_detected is True
    assert result.legacy_stamped is True
    assert result.to_version == LATEST_VERSION
    assert result.applied == list(range(start_version + 1, LATEST_VERSION + 1))

    conn = open_db(path)
    try:
        assert get_user_version(conn) == LATEST_VERSION
        assert verify_schema(conn, LATEST_VERSION) == []
        assert mig.foreign_key_violations(conn) == []
    finally:
        conn.close()

    # 升级到最新后再次启动：空操作
    again = migrate(path)
    assert again.applied == [] and again.legacy_stamped is False


def test_frozen_full_v7_is_stamped_without_running_any_step(tmp_path):
    """旧 init_db() 一次建成的全表库：只补盖版本号，不执行任何建表/补列。"""
    path = copy_fixture("legacy_full_v7", tmp_path)
    conn = open_db(path)
    before_master = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    before_master = [r[0] for r in before_master]
    conn.close()

    result = migrate(path)
    assert result.applied == []
    assert result.legacy_stamped is True
    assert result.to_version == LATEST_VERSION

    conn = open_db(path)
    after_master = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    assert after_master == before_master  # 没有任何对象被重建/改写


# --------------------------------------------------------------------------- #
# 升级前后业务数据可读：工艺 / 测量 / 张拉 / 热态
# --------------------------------------------------------------------------- #


def test_business_data_readable_after_upgrade(tmp_path):
    path = copy_fixture("legacy_full_v7", tmp_path)
    migrate(path)
    conn = open_db(path)
    try:
        # 工艺与逐栓扭矩记录
        proc = conn.execute("SELECT * FROM procedures WHERE id=1").fetchone()
        assert proc["flange_class"] == "PN40 DN200"
        assert proc["bolt_count"] == 8
        assert proc["curve_direction"] == "cw"  # 旧库临时补列写入的默认值
        records = conn.execute(
            "SELECT bolt_no, measured_torque FROM records ORDER BY id").fetchall()
        assert [r["measured_torque"] for r in records] == [98.5, 318.0]
        anomaly = conn.execute("SELECT reason FROM anomalies").fetchone()
        assert anomaly["reason"] == "out_of_sequence"

        # 超声测量：批次、基线、读数（含 supersedes 链）、缺口
        batch = conn.execute("SELECT status, revision FROM measurement_batches").fetchone()
        assert (batch["status"], batch["revision"]) == ("confirmed", 2)
        baselines = conn.execute(
            "SELECT bolt_no FROM measurement_baselines ORDER BY bolt_no").fetchall()
        assert [r["bolt_no"] for r in baselines] == [1, 2]
        readings = conn.execute(
            "SELECT id, supersedes, amendment_note FROM measurement_readings ORDER BY id"
        ).fetchall()
        assert readings[1]["supersedes"] == readings[0]["id"]
        assert readings[1]["amendment_note"] == "重测：复核离群"
        assert conn.execute(
            "SELECT reason FROM measurement_gaps").fetchone()["reason"] == "baseline_missing"

        # 扭矩-转角轨迹与修订、对中、冻结计划
        curve = conn.execute("SELECT revision, record_id FROM torque_curves").fetchone()
        assert (curve["revision"], curve["record_id"]) == (1, 2)
        rev = conn.execute(
            "SELECT usable, time_unit FROM curve_revisions").fetchone()
        assert rev["usable"] == 1 and rev["time_unit"] == "ms"
        assert conn.execute(
            "SELECT operator FROM alignment_checks").fetchone()["operator"] == "王五"
        assert conn.execute(
            "SELECT change_note FROM plan_revisions").fetchone()["change_note"] == "批准冻结"

        # 液压张拉：方案、回传、逐通道
        plan = conn.execute(
            "SELECT status, gauge_id FROM tensioning_plans").fetchone()
        assert (plan["status"], plan["gauge_id"]) == ("confirmed", "PG-9")
        report = conn.execute(
            "SELECT hold_seconds, release_order FROM tensioning_reports").fetchone()
        assert report["hold_seconds"] == 45.0
        channels = conn.execute(
            "SELECT bolt_no, residual_load_kn FROM tensioning_channels ORDER BY bolt_no"
        ).fetchall()
        assert [(r["bolt_no"], r["residual_load_kn"]) for r in channels] == [
            (1, 144.0), (5, 140.0)]

        # 热态校核
        case = conn.execute(
            "SELECT status, source_type, decided_by FROM thermal_cases").fetchone()
        assert (case["status"], case["source_type"], case["decided_by"]) == (
            "confirmed", "tensioning", "钱七")
        gap = conn.execute("SELECT reason, bolt_no FROM thermal_gaps").fetchone()
        assert (gap["reason"], gap["bolt_no"]) == ("initial_load_missing", 5)
    finally:
        conn.close()


def test_measurement_and_tensioning_readable_upgrading_from_early_db(tmp_path):
    """早期 v1 旧库升级后，旧业务数据（工艺/记录/异常）仍按 v7 结构可读。"""
    path = copy_fixture("legacy_v1", tmp_path)
    migrate(path)
    conn = open_db(path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM procedures").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"] == 1
        # 新增表为空但可查询
        assert conn.execute(
            "SELECT COUNT(*) c FROM thermal_cases").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM tensioning_channels").fetchone()["c"] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 中断恢复：失败整步回滚，重跑成功，业务数据无损
# --------------------------------------------------------------------------- #


def test_failed_step_rolls_back_and_rerun_recovers(tmp_path, monkeypatch):
    # 从冻结的 v1 旧库（user_version=0、procedures 无补列）出发，先正常升到 v4，
    # 再用会失败的 v5 伪迁移制造“升级中断”，验证整步回滚与恢复。
    path = copy_fixture("legacy_v1", tmp_path)
    real = mig.MIGRATIONS

    class BrokenMigration(mig.Migration):
        @property
        def sql(self):  # type: ignore[override]
            return (
                "CREATE TABLE site_constraints (id INTEGER PRIMARY KEY);\n"
                "CREATE TABLE plan_revisions (\n"  # 故意引用不存在的列/表触发外键错误
                "  id INTEGER PRIMARY KEY, bad INTEGER REFERENCES no_such_table_missing(x)\n"
                ");\n"
            )

    broken_v5 = BrokenMigration(5, "broken_v5", Path("broken_v5.sql"))
    chain_to_4 = [m if m.version != 5 else broken_v5 for m in real]
    monkeypatch.setattr(mig, "MIGRATIONS", chain_to_4[:4])
    migrate(path)  # 旧库识别为 v1，补盖并升到 v4
    assert get_user_version(open_db(path)) == 4

    # 换成含损坏 v5 的完整链：v5 失败
    monkeypatch.setattr(mig, "MIGRATIONS",
                        [m if m.version != 5 else broken_v5 for m in real])
    with pytest.raises(MigrationError) as exc:
        migrate(path)
    assert exc.value.version == 5
    assert exc.value.stage in ("apply_sql", "post_objects")

    conn = open_db(path)
    try:
        # 整步回滚：停在 v4，v5 的表不存在
        assert get_user_version(conn) == 4
        assert "site_constraints" not in table_names(conn)
        assert "plan_revisions" not in table_names(conn)
        # v2..v4 已完成，结构完整
        assert verify_schema(conn, 4) == []
        # 业务数据无损
        assert conn.execute(
            "SELECT flange_class FROM procedures WHERE id=1"
        ).fetchone()["flange_class"] == "PN40 DN200"
    finally:
        conn.close()

    # 恢复：用回真实迁移链重新启动，从 v5 继续直到 v7
    monkeypatch.undo()
    result = migrate(path)
    assert result.applied == [5, 6, 7]
    conn = open_db(path)
    try:
        assert get_user_version(conn) == LATEST_VERSION
        assert verify_schema(conn, LATEST_VERSION) == []
        assert conn.execute(
            "SELECT flange_class FROM procedures WHERE id=1"
        ).fetchone()["flange_class"] == "PN40 DN200"
    finally:
        conn.close()


def test_legacy_interrupted_db_is_rejected(tmp_path):
    """user_version=0 但对象残缺（疑似旧升级中断/手工拼库）：拒绝猜测升级。"""
    path = str(tmp_path / "broken_legacy.db")
    con = sqlite3.connect(path)
    try:
        # 有 v2 标记表 rework_jobs，但 measurement 关键表缺失（残缺）
        con.executescript("""
        CREATE TABLE procedures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL,
            parent_id INTEGER REFERENCES procedures(id), change_note TEXT,
            status TEXT NOT NULL DEFAULT 'draft', flange_class TEXT NOT NULL,
            bolt_count INTEGER NOT NULL, gasket TEXT NOT NULL, target_torque REAL NOT NULL,
            stage_ratios TEXT NOT NULL, tolerance_pct REAL NOT NULL, tool_id TEXT NOT NULL,
            tool_range_min REAL NOT NULL, tool_range_max REAL NOT NULL,
            calibration_valid_until TEXT NOT NULL, start_angle_deg REAL NOT NULL DEFAULT 0,
            clockwise INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            approved_at TEXT, started_at TEXT, completed_at TEXT, reviewed_at TEXT,
            archived_at TEXT, reviewer TEXT, review_note TEXT);
        CREATE TABLE records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL REFERENCES procedures(id),
            round_no INTEGER NOT NULL, bolt_no INTEGER NOT NULL, tool_id TEXT NOT NULL,
            operator TEXT NOT NULL, reported_at TEXT NOT NULL, measured_torque REAL NOT NULL,
            rework_of INTEGER REFERENCES records(id), created_at TEXT NOT NULL);
        CREATE TABLE anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL REFERENCES procedures(id),
            bolt_no INTEGER, reason TEXT NOT NULL, message TEXT NOT NULL, payload TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE rework_jobs (id INTEGER PRIMARY KEY);
        """)
        con.commit()
    finally:
        con.close()
    with pytest.raises(MigrationError) as exc:
        migrate(path)
    assert exc.value.stage == "legacy_verify"
    assert exc.value.version == 2
    assert any(o.startswith("table:measurement_") for o in exc.value.objects)


def test_unknown_table_in_v0_db_is_rejected(tmp_path):
    path = str(tmp_path / "unknown.db")
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE procedures (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TABLE some_future_table (id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()
    with pytest.raises(MigrationError) as exc:
        migrate(path)
    assert exc.value.stage == "legacy_detect"
    assert "table:some_future_table" in exc.value.objects


# --------------------------------------------------------------------------- #
# 未知高版本拒绝
# --------------------------------------------------------------------------- #


def test_higher_user_version_is_rejected(tmp_path):
    path = make_empty_db(tmp_path)
    con = sqlite3.connect(path)
    try:
        con.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
        con.commit()
    finally:
        con.close()
    with pytest.raises(UnknownVersionError) as exc:
        migrate(path)
    assert exc.value.version == LATEST_VERSION + 1
    assert exc.value.stage == "unknown_version"
    # 库未被改写
    assert get_user_version(sqlite3.connect(path)) == LATEST_VERSION + 1


# --------------------------------------------------------------------------- #
# 升级前备份
# --------------------------------------------------------------------------- #


def test_backup_database_is_consistent_copy(tmp_path):
    src_path = copy_fixture("legacy_v1", tmp_path)
    dst_path = str(tmp_path / "backup.db")
    backup_database(src_path, dst_path)
    src = open_db(src_path)
    dst = open_db(dst_path)
    try:
        assert get_user_version(dst) == 0
        src_rows = [tuple(r) for r in src.execute(
            "SELECT id, bolt_no, measured_torque FROM records ORDER BY id")]
        dst_rows = [tuple(r) for r in dst.execute(
            "SELECT id, bolt_no, measured_torque FROM records ORDER BY id")]
        assert dst_rows == src_rows
    finally:
        src.close()
        dst.close()


# --------------------------------------------------------------------------- #
# init_db 契约不变：环境变量驱动 + 幂等
# --------------------------------------------------------------------------- #


def test_init_db_uses_flange_db_env_and_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "sub" / "flange.db"  # 子目录不存在时 sqlite3 会报错，故先建
    db_file.parent.mkdir(parents=True)
    monkeypatch.setenv("FLANGE_DB", str(db_file))
    from app import db as db_module
    db_module.init_db()
    assert get_user_version(sqlite3.connect(str(db_file))) == LATEST_VERSION
    db_module.init_db()  # 重复启动
    assert get_user_version(sqlite3.connect(str(db_file))) == LATEST_VERSION


def test_fixture_regenerator_matches_frozen_binaries(tmp_path):
    """即时重建的夹具与冻结二进制结构一致（防止夹具定义漂移）。"""
    for name in FIXTURE_NAMES:
        rebuilt = tmp_path / f"{name}.db"
        build_fixture(str(rebuilt), name)
        frozen = open_db(FIXTURE_DIR / f"{name}.db")
        fresh = open_db(rebuilt)
        try:
            assert table_names(frozen) == table_names(fresh)
            assert get_user_version(frozen) == get_user_version(fresh) == 0
        finally:
            frozen.close()
            fresh.close()


# --------------------------------------------------------------------------- #
# 运维 CLI（python -m app.migrate）
# --------------------------------------------------------------------------- #


def test_cli_status_backup_migrate(tmp_path, capsys, monkeypatch):
    from app import migrate as cli

    db_path = copy_fixture("legacy_v1", tmp_path)
    assert cli.main(["status", "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "user_version: 0" in out

    backup_path = str(tmp_path / "bak.db")
    assert cli.main(["backup", "--db", db_path, "-o", backup_path]) == 0
    assert get_user_version(sqlite3.connect(backup_path)) == 0

    assert cli.main(["migrate", "--db", db_path]) == 0
    out = capsys.readouterr().out
    assert "v0 -> v7" in out
    assert get_user_version(sqlite3.connect(db_path)) == LATEST_VERSION

    # 再次迁移：无操作
    assert cli.main(["migrate", "--db", db_path]) == 0
    assert "已是最新版本" in capsys.readouterr().out


def test_cli_rejects_unknown_high_version(tmp_path, capsys):
    from app import migrate as cli

    path = make_empty_db(tmp_path)
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    con.commit()
    con.close()
    assert cli.main(["migrate", "--db", path]) == 3
    assert "高于本代码" in capsys.readouterr().err
    # 库未被改写
    assert get_user_version(sqlite3.connect(path)) == LATEST_VERSION + 5


def test_pre_version_guard_rejects_skipped_migration(tmp_path):
    """user_version 落在迁移链中间但前置对象缺失：前置核对拦截（防手工改版本）。"""
    path = make_empty_db(tmp_path)
    con = sqlite3.connect(path)
    # 只有空库却被标成 v6：v7 前置核对应报缺表
    con.execute("PRAGMA user_version = 6")
    con.commit()
    con.close()
    with pytest.raises(MigrationError) as exc:
        migrate(path)
    assert exc.value.stage == "pre_objects"
    assert exc.value.version == 7
    assert any(o.startswith("table:thermal") or o.startswith("table:")
               for o in exc.value.objects)
    # 失败后版本不被推进
    assert get_user_version(sqlite3.connect(path)) == 6
