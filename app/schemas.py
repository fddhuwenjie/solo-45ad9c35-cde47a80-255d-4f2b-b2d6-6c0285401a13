"""Pydantic 请求/校验模型：法兰紧固工艺的输入字段核验。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class ProcedureCreate(BaseModel):
    """创建紧固工艺（草稿）。批准后这些参数即锁定。"""

    flange_class: str = Field(..., min_length=1, description="法兰等级，如 PN40 DN200 / Class300")
    bolt_count: int = Field(..., description="螺栓数量（偶数，4~96）")
    gasket: str = Field(..., min_length=1, description="垫片型号/材质")
    target_torque: float = Field(..., gt=0, description="目标扭矩 N·m")
    stage_ratios: list[float] = Field(..., description="分级比例，严格递增且末级为 1.0，如 [0.3, 0.6, 1.0]")
    tolerance_pct: float = Field(..., gt=0, le=50, description="允许偏差 ±%")
    tool_id: str = Field(..., min_length=1, description="扭矩扳手编号")
    tool_range_min: float = Field(..., ge=0, description="工具量程下限 N·m")
    tool_range_max: float = Field(..., description="工具量程上限 N·m")
    calibration_valid_until: date = Field(..., description="校准有效期（含当日）")
    start_angle_deg: float = Field(0.0, description="1 号螺栓方位角，0 为正上方，顺时针为正")
    clockwise: bool = Field(True, description="编号是否顺时针递增")

    @field_validator("bolt_count")
    @classmethod
    def _check_bolt_count(cls, v: int) -> int:
        if v < 4 or v > 96 or v % 2 != 0:
            raise ValueError("螺栓数量须为 4~96 的偶数（对角交叉紧固要求）")
        return v

    @field_validator("stage_ratios")
    @classmethod
    def _check_ratios(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("分级比例不能为空")
        if any(r <= 0 or r > 1 for r in v):
            raise ValueError("分级比例须在 (0, 1] 区间内")
        if any(b <= a for a, b in zip(v, v[1:])):
            raise ValueError("分级比例须严格递增（分轮递增规则）")
        if abs(v[-1] - 1.0) > 1e-9:
            raise ValueError("末级比例须为 1.0（最终轮达到目标扭矩）")
        return v

    @model_validator(mode="after")
    def _check_tool_range(self) -> "ProcedureCreate":
        if self.tool_range_max <= self.tool_range_min:
            raise ValueError("工具量程上限须大于下限")
        if not (self.tool_range_min <= self.target_torque <= self.tool_range_max):
            raise ValueError(
                f"目标扭矩 {self.target_torque} N·m 超出工具量程 "
                f"[{self.tool_range_min}, {self.tool_range_max}] N·m"
            )
        return self


class TorqueReport(BaseModel):
    """逐栓回传：工具、操作者、时刻与实测扭矩。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    tool_id: str = Field(..., min_length=1, description="实际使用的扭矩扳手编号")
    operator: str = Field(..., min_length=1, description="操作者")
    reported_at: datetime = Field(..., description="紧固时刻 ISO 8601")
    measured_torque: float = Field(..., gt=0, description="实测扭矩 N·m")
    rework_of: int | None = Field(None, description="补拧时指向原记录 id；不覆盖原记录")


class ReviewRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, description="复核人")
    note: str = Field("", description="复核意见")


class DeriveRequest(BaseModel):
    """批准版本参数锁定；目标或工具变化须派生新版本。仅填写需要变更的字段。"""

    change_note: str = Field(..., min_length=1, description="变更说明（写入修订链）")
    flange_class: str | None = None
    gasket: str | None = None
    target_torque: float | None = None
    stage_ratios: list[float] | None = None
    tolerance_pct: float | None = None
    tool_id: str | None = None
    tool_range_min: float | None = None
    tool_range_max: float | None = None
    calibration_valid_until: date | None = None


# ---------------------------------------------------------------- 超声伸长复核

class MeasurementBatchCreate(BaseModel):
    """从 approved（及以后）工艺建立超声测量批次：冻结螺栓/材料/仪器参数。

    冻结后任何参数变化须新建批次；批次及修订全部留痕。
    """

    length_mm: float = Field(..., gt=0, description="螺栓有效长度 L_eff（mm），声程基准")
    area_mm2: float = Field(..., gt=0, description="螺栓公称应力截面积 A（mm²）")
    elastic_modulus_mpa: float = Field(..., gt=0, description="弹性模量 E（MPa = N/mm²）")
    sound_velocity: float = Field(..., gt=0, description="参考温度下纵波声速 v0（m/s）")
    temp_coefficient: float = Field(..., description="声速温度系数 α（1/℃），可负")
    reference_temp_c: float = Field(..., description="声速参考温度 t0（℃）")
    temp_comp_min_c: float = Field(..., description="温度补偿范围下限（℃，含边界）")
    temp_comp_max_c: float = Field(..., description="温度补偿范围上限（℃，含边界）")
    target_load_min_kn: float = Field(..., gt=0, description="目标预紧力区间下限（kN）")
    target_load_max_kn: float = Field(..., gt=0, description="目标预紧力区间上限（kN）")
    material_load_limit_kn: float = Field(..., gt=0, description="材料允许载荷上限（kN）")
    max_imbalance_pct: float = Field(..., gt=0, description="对径不平衡限值（%），|F_a-F_b|/均值")
    instrument_id: str = Field(..., min_length=1, description="超声仪编号")
    instrument_calibration_until: date = Field(..., description="超声仪校准有效期（含当日）")

    @model_validator(mode="after")
    def _check_ranges(self) -> "MeasurementBatchCreate":
        if self.temp_comp_max_c <= self.temp_comp_min_c:
            raise ValueError("温度补偿范围上限须大于下限")
        if self.target_load_max_kn <= self.target_load_min_kn:
            raise ValueError("目标预紧力区间上限须大于下限")
        if not (self.target_load_min_kn <= self.material_load_limit_kn
                and self.target_load_max_kn <= self.material_load_limit_kn):
            raise ValueError("目标预紧力区间不得高于材料允许载荷上限")
        return self


class BaselineRequest(BaseModel):
    """开工前逐栓提交基线（未承载）飞行时间。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    tof_s: float = Field(..., gt=0, description="基线脉冲回波飞行时间（s）")


class RemeasurementRequest(BaseModel):
    """completed/reviewed 后逐栓提交复测：飞行时间、温度、操作者与时刻。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    tof_s: float = Field(..., gt=0, description="复测脉冲回波飞行时间（s）")
    temperature_c: float = Field(..., description="复测时螺栓温度（℃）")
    operator: str = Field(..., min_length=1, description="测量操作者")
    measured_at: datetime = Field(..., description="复测时刻 ISO 8601")


class RetestRequest(BaseModel):
    """重测：针对某栓已有读数重新测量，须注明理由；原读数保留不覆盖，修订号 +1。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    tof_s: float = Field(..., gt=0, description="重测飞行时间（s）")
    temperature_c: float = Field(..., description="重测温度（℃）")
    operator: str = Field(..., min_length=1, description="操作者")
    measured_at: datetime = Field(..., description="重测时刻 ISO 8601")
    reason: str = Field(..., min_length=1, description="重测理由（与原值一并留痕）")


class ExcludeRequest(BaseModel):
    """排除读数：注明理由后该读数不参与确认，原值保留，修订号 +1，须重测补证。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    reason: str = Field(..., min_length=1, description="排除理由")
