"""Pydantic 请求/校验模型：法兰紧固工艺的输入字段核验。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CurveDirection = Literal["cw", "ccw"]


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
    # ---- 扭矩-转角轨迹复核参数（批准时锁定）----
    curve_direction: CurveDirection = Field("cw", description="紧固旋向：cw 顺时针 / ccw 逆时针（轨迹角度展开方向）")
    snug_torque: float = Field(..., gt=0, description="贴合扭矩 N·m（轨迹贴合点定位阈值）")
    post_snug_angle_min_deg: float = Field(..., ge=0, description="贴合后转角下限 deg")
    post_snug_angle_max_deg: float = Field(..., gt=0, description="贴合后转角上限 deg")
    max_sample_interval_ms: float = Field(..., gt=0, description="最大采样间隔 ms")
    slope_drop_limit: float = Field(..., gt=0, description="分段斜率突降限值 (N·m)/deg")
    max_outlier_rate_pct: float = Field(..., ge=0, le=100, description="整圈离群率上限 %")

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

    @model_validator(mode="after")
    def _check_curve_params(self) -> "ProcedureCreate":
        if self.snug_torque >= self.target_torque:
            raise ValueError(
                f"贴合扭矩 {self.snug_torque} N·m 须小于目标扭矩 {self.target_torque} N·m"
            )
        if self.post_snug_angle_max_deg <= self.post_snug_angle_min_deg:
            raise ValueError("贴合后转角上限须大于下限")
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
    curve_direction: CurveDirection | None = None
    snug_torque: float | None = None
    post_snug_angle_min_deg: float | None = None
    post_snug_angle_max_deg: float | None = None
    max_sample_interval_ms: float | None = None
    slope_drop_limit: float | None = None
    max_outlier_rate_pct: float | None = None


# ---------------------------------------------------------------- 受限栓位施工规划

class TimeWindow(BaseModel):
    """一段可操作/可用时间窗（ISO 8601，结束须晚于开始）。"""

    start: datetime = Field(..., description="窗口开始时刻")
    end: datetime = Field(..., description="窗口结束时刻")

    @model_validator(mode="after")
    def _check_window(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("时间窗结束时刻须晚于开始时刻")
        return self


class ToolWindow(TimeWindow):
    """工具可用时段（工具可能被其他作业占用）。"""

    tool_id: str = Field(..., min_length=1, description="工具编号")


class BoltSiteInput(BaseModel):
    """单栓现场约束：实际方位、可操作时间窗、允许工具与套筒/反力臂角区。

    未登记的字段取默认：方位按规则圆周展开；无时间窗限制；仅批准工具；
    角区 0（不考虑套筒/反力臂占位）。
    """

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    angle_deg: float | None = Field(
        None, description="实际方位角（0=正上方，顺时针为正）；缺省按规则圆周展开")
    windows: list[TimeWindow] = Field(
        default_factory=list, description="可操作时间窗；空 = 全时段可操作")
    allowed_tools: list[str] | None = Field(
        None, description="允许工具编号列表；缺省 = 仅批准工具")
    clearance_deg: float = Field(
        0.0, ge=0.0, le=180.0,
        description="套筒/反力臂所需角区半宽（度，自栓位向两侧延伸）")

    @field_validator("allowed_tools")
    @classmethod
    def _check_tools(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if not v:
                raise ValueError("允许工具列表不能为空；不限制请省略该字段")
            if any(not t.strip() for t in v):
                raise ValueError("允许工具编号不能为空字符串")
        return v


class SiteConstraintsInput(BaseModel):
    """现场约束整组登记（草稿可改，批准随计划冻结；变化须派生计划修订）。"""

    shift_start: datetime = Field(..., description="排程起点（当班开始时刻）")
    min_separation_deg: float | None = Field(
        None, gt=0, le=180,
        description="同轮连续两步最小角间隔（度）；缺省复现同轮非相邻规则（360/N）")
    step_minutes: float = Field(5.0, gt=0, description="单栓紧固作业时长（分钟）")
    tool_change_minutes: float = Field(2.0, ge=0, description="换工具耗时（分钟）")
    tool_windows: list[ToolWindow] = Field(
        default_factory=list, description="工具可用时段；未登记的工具全时段可用")
    bolts: list[BoltSiteInput] = Field(
        default_factory=list, description="逐栓现场约束（未登记栓位取默认值）")

    @field_validator("bolts")
    @classmethod
    def _check_unique_bolts(cls, v: list[BoltSiteInput]) -> list[BoltSiteInput]:
        nos = [b.bolt_no for b in v]
        if len(nos) != len(set(nos)):
            raise ValueError("同一螺栓只能登记一次现场约束")
        return v


class PlanRevisionCreate(BaseModel):
    """现场障碍或工具变化时从批准版派生计划修订：只重排未完成步骤。"""

    change_note: str = Field(..., min_length=1, description="变更说明（写入计划修订链）")
    constraints: SiteConstraintsInput = Field(..., description="完整的新现场约束（整体替换）")


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


# ---------------------------------------------------------------- 扭矩-转角轨迹

TimeUnit = Literal["s", "ms"]
TorqueUnit = Literal["Nm", "Nmm", "lbfft"]
AngleUnit = Literal["deg", "rev", "rad"]


class CurvePoint(BaseModel):
    """单个采样点：时刻、扭矩、转角（单位由提交级字段声明）。"""

    t: float = Field(..., description="采样时刻（time_unit 单位）")
    torque: float = Field(..., description="扭矩读数（torque_unit 单位）")
    angle: float = Field(..., description="角度读数（angle_unit 单位，设备零点任意）")


class CurveSubmit(BaseModel):
    """终轮已接受记录关联一条扭矩-转角轨迹（每栓一条，换曲线走修订）。"""

    record_id: int = Field(..., ge=1, description="关联的终轮已接受记录 id")
    points: list[CurvePoint] = Field(..., min_length=2, description="采样序列")
    time_unit: TimeUnit = Field("s", description="时刻单位")
    torque_unit: TorqueUnit = Field("Nm", description="扭矩单位")
    angle_unit: AngleUnit = Field("deg", description="角度单位")


class CurveAmend(BaseModel):
    """轨迹修订：人工移动贴合点或换用曲线，须注明原因；旧轨迹保留可查。"""

    reason: str = Field(..., min_length=1, description="修订原因（写入修订链）")
    points: list[CurvePoint] | None = Field(None, description="换用的新轨迹；缺省沿用原轨迹")
    time_unit: TimeUnit | None = Field(None, description="新轨迹时刻单位；缺省沿用原单位")
    torque_unit: TorqueUnit | None = Field(None, description="新轨迹扭矩单位；缺省沿用原单位")
    angle_unit: AngleUnit | None = Field(None, description="新轨迹角度单位；缺省沿用原单位")
    snug_index: int | None = Field(None, ge=0, description="人工贴合点（采样点索引）；缺省自动定位")
    record_id: int | None = Field(None, ge=1, description="重新关联的终轮记录 id；缺省不变")


# ---------------------------------------------------------------- 液压张拉执行

class TensioningPlanCreate(BaseModel):
    """从 approved（及以后）工艺建立液压张拉方案：冻结螺栓/机具/仪表参数。

    冻结后任何参数变化须派生修订（说明理由）；分轮换位方案随创建生成。
    """

    area_mm2: float = Field(..., gt=0, description="螺栓有效（应力）截面积 A_s（mm²）")
    length_mm: float = Field(..., gt=0, description="螺栓有效长度 L_eff（mm，行程预测基准）")
    elastic_modulus_mpa: float = Field(..., gt=0, description="弹性模量 E（MPa = N/mm²）")
    target_load_kn: float = Field(..., gt=0, description="目标预紧力 F_target（kN）")
    load_tolerance_pct: float = Field(..., gt=0, le=50, description="残余预紧力允许偏差 ±%")
    tensioner_id: str = Field(..., min_length=1, description="液压拉伸器编号")
    tensioner_count: int = Field(..., ge=1, description="可同时安装的拉伸器数量（栓组大小上限）")
    hydraulic_area_mm2: float = Field(..., gt=0, description="拉伸器液压有效面积 A_h（mm²）")
    max_pressure_mpa: float = Field(..., gt=0, description="拉伸器/泵最大压力（MPa，能力上限）")
    max_stroke_mm: float = Field(..., gt=0, description="拉伸器最大活塞行程（mm）")
    min_tool_spacing: int = Field(..., ge=1, description="相邻机具最小栓位间隔（防相撞，栓位数）")
    load_transfer_coefficient: float = Field(
        ..., ge=0, lt=1, description="载荷转移系数 λ（卸压后残余 = 施加 × (1−λ)）")
    min_hold_seconds: float = Field(..., ge=0, description="最短保压时间（s）")
    pressure_sync_tolerance_pct: float = Field(
        ..., gt=0, le=100, description="组内压力同步允差（%，极差/均值）")
    gauge_id: str = Field(..., min_length=1, description="压力表编号")
    gauge_calibration_until: date = Field(..., description="压力表校准有效期（含当日）")
    stage_ratios: list[float] = Field(..., description="分轮比例，严格递增且末级 1.0")

    @field_validator("stage_ratios")
    @classmethod
    def _check_ratios(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("分轮比例不能为空")
        if any(r <= 0 or r > 1 for r in v):
            raise ValueError("分轮比例须在 (0, 1] 区间内")
        if any(b <= a for a, b in zip(v, v[1:])):
            raise ValueError("分轮比例须严格递增（分轮递增规则）")
        if abs(v[-1] - 1.0) > 1e-9:
            raise ValueError("末级比例须为 1.0（最终轮达到目标预紧力）")
        return v


class TensioningChannelReport(BaseModel):
    """单个拉伸器通道回传：栓号、通道压力与活塞行程。"""

    bolt_no: int = Field(..., ge=1, description="螺栓编号 1..N")
    pressure_mpa: float = Field(..., gt=0, description="通道压力（MPa）")
    stroke_mm: float = Field(..., ge=0, description="活塞行程（mm）")


class TensioningRoundReport(BaseModel):
    """分组回传：同组各通道压力/行程、保压时段与卸压次序。"""

    round_no: int = Field(..., ge=1, description="轮次号")
    group_no: int = Field(..., ge=1, description="组号（方案内）")
    operator: str = Field(..., min_length=1, description="操作者")
    reported_at: datetime = Field(..., description="回传时刻 ISO 8601")
    gauge_id: str = Field(..., min_length=1, description="实际使用的压力表编号")
    hold_seconds: float = Field(..., ge=0, description="保压时段（s）")
    release_order: list[int] = Field(..., min_length=1,
                                     description="卸压次序（须恰好覆盖本组栓号）")
    channels: list[TensioningChannelReport] = Field(..., min_length=1,
                                                    description="各通道压力/行程")


class TensioningRevisionCreate(BaseModel):
    """人工改组/参数修订：必须说明理由，派生新修订；已完成组原位锁定，只重排未完成组。

    仅填写需要变更的字段；空修订（无任何变更）将被拒绝。
    """

    reason: str = Field(..., min_length=1, description="修订理由（写入版本链）")
    area_mm2: float | None = Field(None, gt=0)
    length_mm: float | None = Field(None, gt=0)
    elastic_modulus_mpa: float | None = Field(None, gt=0)
    target_load_kn: float | None = Field(None, gt=0)
    load_tolerance_pct: float | None = Field(None, gt=0, le=50)
    tensioner_id: str | None = Field(None, min_length=1)
    tensioner_count: int | None = Field(None, ge=1)
    hydraulic_area_mm2: float | None = Field(None, gt=0)
    max_pressure_mpa: float | None = Field(None, gt=0)
    max_stroke_mm: float | None = Field(None, gt=0)
    min_tool_spacing: int | None = Field(None, ge=1)
    load_transfer_coefficient: float | None = Field(None, ge=0, lt=1)
    min_hold_seconds: float | None = Field(None, ge=0)
    pressure_sync_tolerance_pct: float | None = Field(None, gt=0, le=100)
    gauge_id: str | None = Field(None, min_length=1)
    gauge_calibration_until: date | None = None
    stage_ratios: list[float] | None = None

    @field_validator("stage_ratios")
    @classmethod
    def _check_ratios(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        return TensioningPlanCreate._check_ratios(v)


class TensioningAdoptUltrasonic(BaseModel):
    """采用既有超声实测值作为残余预紧力证据：必须说明理由并派生修订。"""

    reason: str = Field(..., min_length=1, description="采用理由（写入版本链）")
    batch_id: int | None = Field(
        None, description="超声测量批次 id；缺省取本工艺最新已确认批次")


# ---------------------------------------------------------------- 热态预紧力校核

ThermalLengthUnit = Literal["mm", "cm", "m", "in"]
ThermalAreaUnit = Literal["mm2", "cm2", "m2", "in2"]
ModulusUnit = Literal["MPa", "GPa", "Pa"]
PressureUnit = Literal["MPa", "GPa", "Pa", "psi", "ksi"]
ThermalSourceType = Literal["ultrasonic", "tensioning"]


class ThermalPartInput(BaseModel):
    """热态结构部件（螺栓或一片夹持件）的冻结几何与材料物性。"""

    name: str = Field(..., min_length=1, description="部件名称（夹持件用于留痕区分）")
    length: float = Field(..., gt=0, description="有效长度（声明单位）")
    length_unit: ThermalLengthUnit = Field("mm", description="长度单位")
    area: float = Field(..., gt=0, description="截面积（声明单位）")
    area_unit: ThermalAreaUnit = Field("mm2", description="截面积单位")
    elastic_modulus: float = Field(..., gt=0, description="弹性模量（声明单位）")
    modulus_unit: ModulusUnit = Field("MPa", description="弹性模量单位")
    cte: float = Field(..., gt=0, description="热膨胀系数（1/℃）")
    prop_min_c: float = Field(..., description="材料物性曲线适用温度下限（℃）")
    prop_max_c: float = Field(..., description="材料物性曲线适用温度上限（℃）")

    @model_validator(mode="after")
    def _check_prop_range(self) -> "ThermalPartInput":
        if self.prop_max_c <= self.prop_min_c:
            raise ValueError("材料物性适用温度上限须大于下限")
        return self


class GasketCurvePoint(BaseModel):
    """垫片压缩-回弹曲线折点：压缩量及该点加载/回弹压力（两折点共用压缩坐标）。"""

    compression: float = Field(..., ge=0, description="垫片压缩量（声明单位）")
    compression_unit: ThermalLengthUnit = Field("mm", description="压缩量单位")
    loading_pressure: float = Field(..., ge=0, description="加载支压力（声明单位）")
    rebound_pressure: float = Field(..., ge=0, description="回弹支压力（声明单位，≤加载支）")
    pressure_unit: PressureUnit = Field("MPa", description="压力单位")

    @model_validator(mode="after")
    def _check_branches(self) -> "GasketCurvePoint":
        if self.rebound_pressure > self.loading_pressure:
            raise ValueError("同一压缩量的回弹压力不得高于加载压力（滞回耗能）")
        return self


class ThermalGasketInput(BaseModel):
    """垫片冻结参数：有效承压面积、厚度、热膨胀系数、物性温度区间与压缩-回弹曲线。"""

    name: str | None = Field(None, min_length=1, description="垫片型号/材质")
    effective_area: float = Field(..., gt=0, description="垫片有效承压面积（声明单位）")
    area_unit: ThermalAreaUnit = Field("mm2", description="面积单位")
    thickness: float = Field(..., gt=0, description="垫片自由厚度（声明单位）")
    length_unit: ThermalLengthUnit = Field("mm", description="厚度单位")
    cte: float = Field(..., ge=0, description="垫片热膨胀系数（1/℃）")
    prop_min_c: float = Field(..., description="垫片物性/曲线适用温度下限（℃）")
    prop_max_c: float = Field(..., description="垫片物性/曲线适用温度上限（℃）")
    points: list[GasketCurvePoint] = Field(
        ..., min_length=2, description="压缩-回弹曲线折点（按压缩量升序）")

    @model_validator(mode="after")
    def _check_curve(self) -> "ThermalGasketInput":
        if self.prop_max_c <= self.prop_min_c:
            raise ValueError("垫片物性适用温度上限须大于下限")
        ordered = sorted(self.points, key=lambda p: p.compression)
        if [p.compression for p in ordered] != [p.compression for p in self.points]:
            raise ValueError("压缩-回弹曲线折点须按压缩量升序提交")
        if len({p.compression for p in ordered}) != len(ordered):
            raise ValueError("压缩-回弹曲线压缩量不得重复")
        for field in ("loading_pressure", "rebound_pressure"):
            vals = [getattr(p, field) for p in ordered]
            if any(b < a - 1e-12 for a, b in zip(vals, vals[1:])):
                raise ValueError(f"曲线{field}须随压缩量单调不减")
        if ordered[0].compression != 0 or ordered[0].loading_pressure != 0:
            raise ValueError("曲线首折点须为零压缩零压力（自由状态原点）")
        if ordered[-1].loading_pressure <= 0:
            raise ValueError("曲线末折点加载压力须为正")
        return self


class ThermalReferenceTemps(BaseModel):
    """装配（初始载荷确认）参考温度：螺栓/夹持件/垫片各自的基准温度。"""

    bolt_temp_c: float = Field(..., description="螺栓参考温度（℃）")
    member_temp_c: float = Field(..., description="夹持件参考温度（℃）")
    gasket_temp_c: float = Field(..., description="垫片参考温度（℃）")


class ThermalLimits(BaseModel):
    """热态校核冻结限值（单位固定：载荷 kN、面压 MPa、时间 s）。"""

    bolt_load_limit_kn: float = Field(..., gt=0, description="螺栓允许载荷上限（kN）")
    min_seating_pressure_mpa: float = Field(..., gt=0,
                                            description="最小密封（压紧）面压（MPa）")
    max_gasket_pressure_mpa: float = Field(..., gt=0, description="垫片压溃面压上限（MPa）")
    max_temperature_interval_seconds: float = Field(
        ..., gt=0, description="相邻温度节点最大允许间隔（s），超出即温度断档")

    @model_validator(mode="after")
    def _check_limits(self) -> "ThermalLimits":
        if self.max_gasket_pressure_mpa <= self.min_seating_pressure_mpa:
            raise ValueError("垫片压溃面压上限须高于最小密封面压")
        return self


class ThermalZoneReading(BaseModel):
    """某温度节点上单个分区的三部件温度（℃）。"""

    zone: str = Field(..., min_length=1, description="分区名（与螺栓分配一致）")
    bolt_temp_c: float
    member_temp_c: float
    gasket_temp_c: float


class ThermalNode(BaseModel):
    """带时标的分区温度节点：时刻 + 各分区螺栓/夹持件/垫片温度。"""

    at: datetime = Field(..., description="温度节点时刻 ISO 8601")
    temperatures: list[ThermalZoneReading] = Field(
        ..., min_length=1, description="本时刻各分区温度（分区不得重复）")

    @field_validator("temperatures")
    @classmethod
    def _check_unique_zones(cls, v: list[ThermalZoneReading]) -> list[ThermalZoneReading]:
        zones = [z.zone for z in v]
        if len(zones) != len(set(zones)):
            raise ValueError("同一温度节点内分区不得重复")
        return v


class ThermalCaseCreate(BaseModel):
    """热态预紧力校核建案：冻结结构/垫片/限值/时序，逐栓初载取自已确认来源。

    初始载荷只从已确认超声批次或已确认液压张拉方案读取（建案时冻结快照）；
    bolt_zones 按栓号顺序（长度 = 螺栓数）给出每栓所属温度分区。
    单位冲突、温度断档、材料/垫片曲线覆盖不足等作为证据缺口记录，
    版本照常落库但不可确认。
    """

    source_type: ThermalSourceType = Field(
        ..., description="逐栓初始载荷来源：ultrasonic 已确认超声批次 / tensioning 已确认张拉方案")
    source_id: int | None = Field(
        None, ge=1, description="来源 id；缺省取本工艺最新已确认来源")
    bolt: ThermalPartInput = Field(..., description="螺栓长度/截面/E/热膨胀系数/物性区间")
    members: list[ThermalPartInput] = Field(
        ..., min_length=1, description="夹持件叠层（两片法兰及各层，按载荷传递方向）")
    gasket: ThermalGasketInput = Field(..., description="垫片有效面积/厚度/曲线与物性区间")
    bolt_zones: list[str] = Field(
        ..., min_length=1, description="逐栓温度分区（按栓号 1..N 顺序）")
    reference: ThermalReferenceTemps = Field(..., description="装配参考温度")
    limits: ThermalLimits = Field(..., description="螺栓/垫片/温度断档冻结限值")
    nodes: list[ThermalNode] = Field(
        ..., min_length=1, description="带时标的分区温度节点（须时刻升序）")
    notes: str | None = Field(None, description="建案说明")

    @field_validator("nodes")
    @classmethod
    def _check_node_order(cls, v: list[ThermalNode]) -> list[ThermalNode]:
        ats = [n.at for n in v]
        if len(ats) != len(set(ats)):
            raise ValueError("温度节点时刻不得重复")
        if ats != sorted(ats):
            raise ValueError("温度节点须按时刻升序提交")
        return v

    @model_validator(mode="after")
    def _check_zone_consistency(self) -> "ThermalCaseCreate":
        assigned = set(self.bolt_zones)
        for n in self.nodes:
            present = {z.zone for z in n.temperatures}
            missing = sorted(assigned - present)
            if missing:
                raise ValueError(
                    f"温度节点 {n.at.isoformat()} 缺螺栓分配到的分区 {missing}")
        return self


class ThermalRevisionCreate(BaseModel):
    """人工采用替代边界或材料曲线派生修订：必须写明理由，至少变更一项冻结内容。"""

    reason: str = Field(..., min_length=1, description="采用替代边界/曲线的理由（写入修订链）")
    source_type: ThermalSourceType | None = Field(None, description="更换初始载荷来源类型")
    source_id: int | None = Field(None, ge=1, description="更换初始载荷来源 id")
    bolt: ThermalPartInput | None = None
    members: list[ThermalPartInput] | None = Field(None, min_length=1)
    gasket: ThermalGasketInput | None = None
    bolt_zones: list[str] | None = Field(None, min_length=1)
    reference: ThermalReferenceTemps | None = None
    limits: ThermalLimits | None = None
    nodes: list[ThermalNode] | None = Field(None, min_length=1)
    notes: str | None = None


# ---------------------------------------------------------------- 装配对中预检

LengthUnit = Literal["mm", "cm", "m", "in"]


class AlignmentPoint(BaseModel):
    """一个按方位分布的对中测点（轴向间隙、径向偏移、垫片边缘位置与螺栓穿入结果）。

    angle_deg：测点方位角，0 为正上方、顺时针为正（与编号方位一致），允许任意
    实数，落库前归一化到 [0,360)。三个线值共用 length_unit（默认 mm）；各测点
    单位不一致不做请求级拒绝，作为证据缺口在预检结论中列出。
    """

    angle_deg: float = Field(..., description="测点方位角（0=正上方，顺时针为正）")
    axial_gap: float = Field(..., description="该方位两法兰面轴向间隙（自由状态，可为负以暴露矛盾读数）")
    radial_offset: float = Field(..., description="该方位径向偏移读数（正=活动面向外偏，沿 u 方向）")
    gasket_edge_position: float = Field(
        ..., description="该方位自法兰外缘向内量到垫片外缘的距离（≥0）")
    bolt_free_insertion: bool = Field(
        ..., description="该方位螺栓是否可在法兰不受力状态下自由穿入")
    length_unit: LengthUnit = Field("mm", description="本测点三个线值的长度单位")


class AlignmentCheckCreate(BaseModel):
    """装配对中预检建版：冻结法兰/垫片几何与限值，并接收 >=4 个按方位测点。

    每个版本不可变；调整后复测必须另存新版本并注明调整原因，旧记录不覆盖。
    """

    flange_face_diameter_mm: float = Field(..., gt=0, description="法兰面（密封面）外径 D（mm）")
    gasket_inner_diameter_mm: float = Field(..., gt=0, description="垫片内径 Gi（mm）")
    gasket_outer_diameter_mm: float = Field(..., gt=0, description="垫片外径 Go（mm）")
    bore_diameter_mm: float = Field(..., gt=0, description="法兰内孔（流道）直径 Db（mm）")
    max_parallelism_mm: float = Field(..., gt=0, description="平行度（最大-最小间隙）限值（mm）")
    max_radial_mismatch_mm: float = Field(..., gt=0, description="径向错边限值（mm）")
    points: list[AlignmentPoint] = Field(..., min_length=4, description=">=4 个按方位分布的测点")
    operator: str = Field(..., min_length=1, description="测量操作者")
    measured_at: datetime = Field(..., description="测量时刻 ISO 8601")
    adjustment_reason: str | None = Field(
        None, description="复测调整原因；首版必须为空，第 2 版及以后必填（旧版本不覆盖）")

    @model_validator(mode="after")
    def _check_geometry(self) -> "AlignmentCheckCreate":
        if self.gasket_inner_diameter_mm >= self.gasket_outer_diameter_mm:
            raise ValueError("垫片外径须大于内径")
        if self.gasket_outer_diameter_mm > self.flange_face_diameter_mm:
            raise ValueError("垫片外径不得大于法兰面直径")
        if self.bore_diameter_mm > self.flange_face_diameter_mm:
            raise ValueError("法兰内孔直径不得大于法兰面直径")
        # 方位重复、几何自相矛盾不作为请求级拒绝：版本照常冻结并在结论中只列证据缺口
        return self
