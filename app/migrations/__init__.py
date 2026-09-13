"""版本化数据库迁移：以 ``PRAGMA user_version`` 驱动顺序迁移。

设计要点：

- 每个版本一个可审计 SQL 步骤（``versions/0NN_*.sql``），内容对应结构真实演进；
- 每步在单个事务内顺序执行全部语句，先核对前置 ``user_version`` 与前置对象
  （表、列、唯一索引、外键），成功后核对目标对象并在同一事务内提升
  ``user_version``；任一条语句或核对失败即整步回滚，业务数据不受影响；
- ``user_version=0`` 的旧库按现存对象推断所属版本并核对完整性后再补盖版本号，
  兼容空库、只含早期 procedures/records/anomalies 的旧库，以及旧 ``init_db()``
  一次建成的全表库；推断不明或对象残缺时拒绝猜测并报告版本与对象；
- ``user_version`` 高于代码已知版本时拒绝打开，避免新库被旧代码降级改写。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """迁移失败：携带失败阶段、版本与相关对象，便于定位与恢复。"""

    def __init__(self, message: str, *, stage: str, version: int | None = None,
                 objects: list[str] | None = None):
        super().__init__(message)
        self.stage = stage
        self.version = version
        self.objects = objects or []

    def as_detail(self) -> dict:
        return {"reason": "migration_failed", "stage": self.stage,
                "version": self.version, "objects": self.objects,
                "message": str(self)}


class UnknownVersionError(MigrationError):
    """库的 user_version 高于本代码已知最新版本：拒绝操作。"""


# ---------------------------------------------------------------------------
# 结构清单（迁移完成后每个版本必须具备的对象，用于前/后置核对）
# ---------------------------------------------------------------------------

# 表首次出现的版本
TABLE_SINCE: dict[str, int] = {
    "procedures": 1, "records": 1, "anomalies": 1,
    "measurement_batches": 2, "measurement_baselines": 2,
    "measurement_readings": 2, "measurement_gaps": 2, "rework_jobs": 2,
    "torque_curves": 3, "curve_revisions": 3,
    "alignment_checks": 4, "alignment_points": 4,
    "site_constraints": 5, "plan_revisions": 5,
    "tensioning_plans": 6, "tensioning_reports": 6, "tensioning_channels": 6,
    "thermal_cases": 7, "thermal_gaps": 7,
    "friction_calibrations": 8, "fastener_states": 8,
}

# 每表当前全量列（NOT NULL/默认值差异由 SQL 迁移本身保证，这里核对存在性）
COLUMNS: dict[str, tuple[str, ...]] = {
    "procedures": (
        "id", "version", "parent_id", "change_note", "status", "flange_class",
        "bolt_count", "gasket", "target_torque", "stage_ratios", "tolerance_pct",
        "tool_id", "tool_range_min", "tool_range_max", "calibration_valid_until",
        "start_angle_deg", "clockwise",
        # v3 扭矩-转角轨迹复核参数
        "curve_direction", "snug_torque", "post_snug_angle_min_deg",
        "post_snug_angle_max_deg", "max_sample_interval_ms", "slope_drop_limit",
        "max_outlier_rate_pct",
        # v8 批准时引用的摩擦标定版
        "adopted_calibration_id",
        "created_at", "approved_at", "started_at", "completed_at", "reviewed_at",
        "archived_at", "reviewer", "review_note",
    ),
    "records": (
        "id", "procedure_id", "round_no", "bolt_no", "tool_id", "operator",
        "reported_at", "measured_torque", "rework_of", "created_at",
    ),
    "anomalies": (
        "id", "procedure_id", "bolt_no", "reason", "message", "payload",
        "created_at",
    ),
    "measurement_batches": (
        "id", "procedure_id", "revision", "status", "scope_bolts", "locked_bolts",
        "locked_results", "length_mm", "area_mm2", "elastic_modulus_mpa",
        "sound_velocity", "temp_coefficient", "reference_temp_c",
        "temp_comp_min_c", "temp_comp_max_c", "target_load_min_kn",
        "target_load_max_kn", "material_load_limit_kn", "max_imbalance_pct",
        "instrument_id", "instrument_calibration_until", "derived_from_batch_id",
        "confirmed_revision", "confirmed_at", "created_at",
    ),
    "measurement_baselines": ("id", "batch_id", "bolt_no", "tof_s", "created_at"),
    "measurement_readings": (
        "id", "batch_id", "bolt_no", "tof_s", "temperature_c", "operator",
        "measured_at", "supersedes", "excluded", "amendment_note", "created_at",
    ),
    "measurement_gaps": (
        "id", "batch_id", "revision", "bolt_no", "reason", "message", "payload",
        "created_at",
    ),
    "rework_jobs": (
        "id", "source_batch_id", "source_procedure_id", "rework_procedure_id",
        "locked_bolts", "target_bolts", "created_at",
    ),
    "torque_curves": (
        "id", "procedure_id", "bolt_no", "revision", "record_id", "created_at",
    ),
    "curve_revisions": (
        "id", "curve_id", "revision", "record_id", "time_unit", "torque_unit",
        "angle_unit", "points", "snug_override", "amendment_note", "analysis",
        "usable", "created_at",
    ),
    "alignment_checks": (
        "id", "procedure_id", "version", "flange_face_diameter_mm",
        "gasket_inner_diameter_mm", "gasket_outer_diameter_mm", "bore_diameter_mm",
        "max_parallelism_mm", "max_radial_mismatch_mm", "operator", "measured_at",
        "adjustment_reason", "analysis", "created_at",
    ),
    "alignment_points": (
        "id", "check_id", "angle_deg", "axial_gap", "radial_offset",
        "gasket_edge_position", "bolt_free_insertion", "length_unit",
    ),
    "site_constraints": ("id", "procedure_id", "revision", "payload", "created_at"),
    "plan_revisions": (
        "id", "procedure_id", "revision", "constraint_revision", "plan",
        "change_note", "created_at",
    ),
    "tensioning_plans": (
        "id", "procedure_id", "revision", "parent_id", "status", "area_mm2",
        "length_mm", "elastic_modulus_mpa", "target_load_kn", "load_tolerance_pct",
        "tensioner_id", "tensioner_count", "hydraulic_area_mm2", "max_pressure_mpa",
        "max_stroke_mm", "min_tool_spacing", "load_transfer_coefficient",
        "min_hold_seconds", "pressure_sync_tolerance_pct", "gauge_id",
        "gauge_calibration_until", "stage_ratios", "scheme",
        "ultrasonic_batch_id", "ultrasonic_snapshot", "change_note",
        "approved_at", "confirmed_at", "created_at",
    ),
    "tensioning_reports": (
        "id", "plan_id", "plan_revision", "round_no", "group_no", "operator",
        "reported_at", "gauge_id", "hold_seconds", "release_order", "created_at",
    ),
    "tensioning_channels": (
        "id", "report_id", "bolt_no", "pressure_mpa", "stroke_mm",
        "applied_load_kn", "residual_load_kn",
    ),
    "thermal_cases": (
        "id", "procedure_id", "revision", "parent_id", "status", "source_type",
        "source_id", "payload", "frozen", "initial_loads", "result", "change_note",
        "decided_by", "decision_note", "confirmed_at", "created_at",
    ),
    "thermal_gaps": (
        "id", "case_id", "revision", "bolt_no", "reason", "message", "interval",
        "created_at",
    ),
    "friction_calibrations": (
        "id", "procedure_id", "revision", "parent_id", "status", "identity_key",
        "payload", "frozen", "analysis", "change_note", "decided_by",
        "decision_note", "confirmed_at", "created_at",
    ),
    "fastener_states": (
        "id", "procedure_id", "revision", "payload", "created_at",
    ),
}

# procedures 的轨迹复核列自 v3 才有
_PROCEDURE_COLUMNS_V3 = {
    "curve_direction", "snug_torque", "post_snug_angle_min_deg",
    "post_snug_angle_max_deg", "max_sample_interval_ms", "slope_drop_limit",
    "max_outlier_rate_pct",
}

# procedures 的标定引用列自 v8 才有
_PROCEDURE_COLUMNS_V8 = {"adopted_calibration_id"}

# 表 -> 应当存在的 UNIQUE 索引列组（DDL 中 UNIQUE(...) 约束）
UNIQUE_INDEXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "measurement_baselines": (("batch_id", "bolt_no"),),
    "torque_curves": (("procedure_id", "bolt_no"),),
    "curve_revisions": (("curve_id", "revision"),),
    "alignment_checks": (("procedure_id", "version"),),
    "site_constraints": (("procedure_id", "revision"),),
    "plan_revisions": (("procedure_id", "revision"),),
    "tensioning_plans": (("procedure_id", "revision"),),
    "tensioning_reports": (("plan_id", "round_no", "group_no"),),
    "thermal_cases": (("procedure_id", "revision"),),
    "friction_calibrations": (("procedure_id", "revision"),),
    "fastener_states": (("procedure_id", "revision"),),
}

# 表 -> 应当存在的外键（列, 引用表）
FOREIGN_KEYS: dict[str, frozenset[tuple[str, str]]] = {
    "procedures": frozenset({("parent_id", "procedures"),
                             ("adopted_calibration_id", "friction_calibrations")}),
    "records": frozenset({("procedure_id", "procedures"),
                          ("rework_of", "records")}),
    "anomalies": frozenset({("procedure_id", "procedures")}),
    "measurement_batches": frozenset({
        ("procedure_id", "procedures"),
        ("derived_from_batch_id", "measurement_batches")}),
    "measurement_baselines": frozenset({("batch_id", "measurement_batches")}),
    "measurement_readings": frozenset({
        ("batch_id", "measurement_batches"),
        ("supersedes", "measurement_readings")}),
    "measurement_gaps": frozenset({("batch_id", "measurement_batches")}),
    "rework_jobs": frozenset({
        ("source_batch_id", "measurement_batches"),
        ("source_procedure_id", "procedures"),
        ("rework_procedure_id", "procedures")}),
    "torque_curves": frozenset({("procedure_id", "procedures"),
                                ("record_id", "records")}),
    "curve_revisions": frozenset({("curve_id", "torque_curves"),
                                  ("record_id", "records")}),
    "alignment_checks": frozenset({("procedure_id", "procedures")}),
    "alignment_points": frozenset({("check_id", "alignment_checks")}),
    "site_constraints": frozenset({("procedure_id", "procedures")}),
    "plan_revisions": frozenset({("procedure_id", "procedures")}),
    "tensioning_plans": frozenset({
        ("procedure_id", "procedures"), ("parent_id", "tensioning_plans"),
        ("ultrasonic_batch_id", "measurement_batches")}),
    "tensioning_reports": frozenset({("plan_id", "tensioning_plans")}),
    "tensioning_channels": frozenset({("report_id", "tensioning_reports")}),
    "thermal_cases": frozenset({("procedure_id", "procedures"),
                                ("parent_id", "thermal_cases")}),
    "thermal_gaps": frozenset({("case_id", "thermal_cases")}),
    "friction_calibrations": frozenset({
        ("procedure_id", "procedures"),
        ("parent_id", "friction_calibrations")}),
    "fastener_states": frozenset({("procedure_id", "procedures")}),
}

# user_version=0 旧库的版本标记表（取现存最高标记推断结构版本）
MARKER_TABLES: tuple[tuple[str, int], ...] = (
    ("friction_calibrations", 8),
    ("thermal_gaps", 7),
    ("tensioning_channels", 6),
    ("plan_revisions", 5),
    ("alignment_points", 4),
    ("curve_revisions", 3),
    ("rework_jobs", 2),
    ("anomalies", 1),
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _load_migrations() -> list[Migration]:
    migrations = []
    for path in sorted(VERSIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:3])
        migrations.append(Migration(version, path.stem[4:], path))
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise RuntimeError(
            f"迁移版本必须自 1 连续编号，实际为 {[m.version for m in migrations]}")
    return migrations


MIGRATIONS: list[Migration] = _load_migrations()
LATEST_VERSION: int = MIGRATIONS[-1].version
ALL_TABLES: frozenset[str] = frozenset(TABLE_SINCE)


@dataclass
class MigrationResult:
    path: str
    from_version: int
    to_version: int
    applied: list[int] = field(default_factory=list)
    legacy_detected: bool = False          # user_version=0 但含旧表，按对象推断
    legacy_stamped: bool = False           # 推断后补盖 user_version
    legacy_version: int | None = None      # 推断出的旧库结构版本

    @property
    def changed(self) -> bool:
        return bool(self.applied) or self.legacy_stamped


# ---------------------------------------------------------------------------
# SQL 语句切分（去掉 -- 行注释，按分号切；尊重单引号字符串）
# ---------------------------------------------------------------------------


def split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# ---------------------------------------------------------------------------
# 结构内省与核对
# ---------------------------------------------------------------------------


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def user_tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _unique_index_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    groups = []
    for idx in conn.execute(f"PRAGMA index_list({table})"):
        if not idx["unique"]:
            continue
        info = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
        groups.append(tuple(r[2] for r in info))
    return groups


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, str]]:
    return {(r["from"], r["table"])
            for r in conn.execute(f"PRAGMA foreign_key_list({table})")}


def _expected_columns(table: str, version: int) -> set[str]:
    cols = set(COLUMNS[table])
    if table == "procedures":
        if version < 3:
            cols -= _PROCEDURE_COLUMNS_V3
        if version < 8:
            cols -= _PROCEDURE_COLUMNS_V8
    return cols


# procedures 的标定引用外键自 v8 才有（列同时新增）
_PROCEDURE_FKS_V8 = frozenset({("adopted_calibration_id", "friction_calibrations")})


def _expected_fks(table: str, version: int) -> frozenset[tuple[str, str]]:
    fks = FOREIGN_KEYS.get(table, frozenset())
    if table == "procedures" and version < 8:
        fks = fks - _PROCEDURE_FKS_V8
    return fks


def verify_schema(conn: sqlite3.Connection, version: int) -> list[str]:
    """核对库结构与指定版本一致；返回缺失/不符对象清单（空列表=完整）。"""
    problems: list[str] = []
    present = user_tables(conn)
    for table, since in TABLE_SINCE.items():
        if since > version:
            continue
        if table not in present:
            problems.append(f"table:{table}")
            continue
        missing_cols = _expected_columns(table, version) - _table_columns(conn, table)
        problems.extend(f"column:{table}.{c}" for c in sorted(missing_cols))
        actual_fks = _foreign_keys(conn, table)
        for column, ref in _expected_fks(table, version):
            if (column, ref) not in actual_fks:
                problems.append(f"foreign_key:{table}.{column}->{ref}")
        actual_indexes = _unique_index_columns(conn, table)
        for group in UNIQUE_INDEXES.get(table, ()):  # pragma: no branch
            if group not in actual_indexes:
                problems.append(f"unique_index:{table}({','.join(group)})")
    return problems


def foreign_key_violations(conn: sqlite3.Connection) -> list[str]:
    return [f"row:{','.join(str(v) for v in row)}"
            for row in conn.execute("PRAGMA foreign_key_check")]


def integrity_problems(conn: sqlite3.Connection) -> list[str]:
    """整库完整性（损坏页/索引等）；非 'ok' 结果逐条返回。"""
    rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    return [f"integrity:{r}" for r in rows if r != "ok"]


# ---------------------------------------------------------------------------
# 迁移执行
# ---------------------------------------------------------------------------


def _infer_legacy_version(conn: sqlite3.Connection) -> int:
    tables = user_tables(conn)
    if not tables:
        return 0  # 空库：从 v1 全新建立
    unknown = tables - ALL_TABLES
    if unknown:
        raise MigrationError(
            "user_version=0 的库含未知表，无法判定结构版本，拒绝猜测升级",
            stage="legacy_detect", objects=sorted(f"table:{t}" for t in unknown))
    if "procedures" not in tables:
        raise MigrationError(
            "user_version=0 的库缺少 procedures 表，不是本服务的已知旧库",
            stage="legacy_detect", objects=["table:procedures"])
    for marker, version in MARKER_TABLES:
        if marker in tables:
            return version
    raise MigrationError(  # 理论不可达（procedures 在但 anomalies 不在）
        "user_version=0 的库无法匹配任何历史结构版本",
        stage="legacy_detect", objects=sorted(tables))


def _stamp_legacy(conn: sqlite3.Connection, version: int) -> None:
    """对完整的 user_version=0 旧库补盖版本号（不改任何业务对象/数据）。"""
    integrity = integrity_problems(conn)
    if integrity:
        raise MigrationError(
            f"旧库完整性检查未通过，拒绝判定结构版本（请先用备份恢复）",
            stage="legacy_integrity", version=version, objects=integrity)
    missing = verify_schema(conn, version)
    if missing:
        raise MigrationError(
            f"旧库结构疑似版本 {version} 但对象不完整（可能升级中断），"
            "拒绝继续；请修复或用备份恢复后再升级",
            stage="legacy_verify", version=version, objects=missing)
    conn.execute("BEGIN")
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    target = migration.version
    current = get_user_version(conn)
    if current != target - 1:
        raise MigrationError(
            f"迁移 v{target} 要求前置版本 {target - 1}，实际为 {current}",
            stage="pre_version", version=target)
    pre_missing = verify_schema(conn, target - 1)
    if pre_missing:
        raise MigrationError(
            f"迁移 v{target} 前置结构不完整，拒绝执行（请先用备份恢复）",
            stage="pre_objects", version=target, objects=pre_missing)

    statements = split_sql(migration.sql)
    conn.execute("BEGIN")
    try:
        for stmt in statements:
            conn.execute(stmt)
        post_missing = verify_schema(conn, target)
        if post_missing:
            raise MigrationError(
                f"迁移 v{target} 执行后核对发现对象缺失，整步回滚",
                stage="post_objects", version=target, objects=post_missing)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.execute("COMMIT")
    except MigrationError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise MigrationError(
            f"迁移 v{target} 失败已整步回滚：{exc}",
            stage="apply_sql", version=target,
            objects=[f"sql:{statements[0][:80]}"]) from exc


def migrate(path: str | None = None, *, progress=None) -> MigrationResult:
    """把 ``path`` 指向的库顺序迁移到最新版本；幂等，重复启动不改业务数据。"""
    from ..db import db_path

    path = path or db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.isolation_level = None  # 事务由迁移器显式控制（executescript 会强制提交，禁用）
    try:
        start = get_user_version(conn)
        if start > LATEST_VERSION:
            raise UnknownVersionError(
                f"数据库结构版本 {start} 高于本代码支持的最高版本 {LATEST_VERSION}，"
                "拒绝打开以免被旧代码降级改写；请升级服务程序",
                stage="unknown_version", version=start)
        legacy = stamped = False
        legacy_version: int | None = None
        current = start
        if current == 0:
            current = _infer_legacy_version(conn)
            if current > 0:
                legacy = True
                legacy_version = current
                _stamp_legacy(conn, current)
                stamped = True
        applied: list[int] = []
        for migration in MIGRATIONS[current:]:
            _apply_migration(conn, migration)
            applied.append(migration.version)
            if progress is not None:
                progress(migration.version)
        violations = foreign_key_violations(conn)
        if violations:
            raise MigrationError(
                "迁移完成但存在外键悬挂行，拒绝交付该库",
                stage="foreign_key_check", objects=violations)
        integrity = integrity_problems(conn)
        if integrity:
            raise MigrationError(
                "迁移完成但完整性检查未通过，拒绝交付该库",
                stage="integrity_check", objects=integrity)
        return MigrationResult(path=path, from_version=start,
                               to_version=get_user_version(conn), applied=applied,
                               legacy_detected=legacy, legacy_stamped=stamped,
                               legacy_version=legacy_version)
    finally:
        conn.close()


def backup_database(src_path: str, dst_path: str) -> None:
    """用 SQLite 在线备份 API 复制一份一致性快照（升级前备份用）。"""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
