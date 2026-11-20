"""领域模型:采集、运行切分、能量求值、账实核对、指标与复盘。

约定:
- 所有时间戳为 naive UTC(datetime),由写入方保证。
- 能量单位统一为 kWh;累计表读数为表码原值,倍率单独记录,求值时相乘。
- 带不确定范围的能量用 {"v": 估值, "lo": 下界, "hi": 上界} 的 JSON 表示;
  lo/hi 为 None 表示该侧未知(不闭合),禁止用估值冒充闭合计量。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base, engine


# ---------------- 基础档案 ----------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)  # 演示环境明文,生产应哈希
    role = Column(String(32), nullable=False, default="viewer")  # admin|viewer
    token = Column(String(64), unique=True, nullable=False)


class Line(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(64), nullable=False)


class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False)           # 区内顺序
    length_km = Column(Float, nullable=False)
    multiplier_zone = Column(String(32), nullable=False, default="Z1")  # 计量倍率区间
    substation_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    line = relationship("Line")


class Train(Base):
    __tablename__ = "trains"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    vehicle_model = Column(String(32), nullable=False)  # 车型


class Consist(Base):
    """编组版本:同一列车换编组后必须切断能量片段。"""
    __tablename__ = "consists"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    code = Column(String(32), nullable=False)       # 如 6B / 6A
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    train = relationship("Train")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True)
    serial = Column(String(64), unique=True, nullable=False)  # 设备序列号
    kind = Column(String(16), nullable=False)       # ONBOARD|SUBSTATION
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=True)
    station_name = Column(String(64), nullable=True)  # 变电所名
    train = relationship("Train")


# ---------------- 采集接入 ----------------

class MeterReading(Base):
    """原始累计值采样。车载表填 traction/regen/aux;变电所表填 import/export。"""
    __tablename__ = "meter_readings"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    sampled_at = Column(DateTime, nullable=False, index=True)   # 采样时刻(表侧)
    received_at = Column(DateTime, nullable=False)              # 平台到达时刻
    seq = Column(Integer, nullable=False)                       # 设备采样序列号
    traction_cum = Column(Float, nullable=True)
    regen_cum = Column(Float, nullable=True)
    aux_cum = Column(Float, nullable=True)
    import_cum = Column(Float, nullable=True)   # 变电所受电累计
    export_cum = Column(Float, nullable=True)   # 变电所返送(回馈上网)累计
    multiplier = Column(Float, nullable=False, default=1.0)     # 计量倍率
    comm_status = Column(String(16), nullable=False, default="OK")  # OK|DEGRADED|LOST
    device = relationship("Device")
    __table_args__ = (UniqueConstraint("device_id", "seq", name="uq_reading_device_seq"),)


# ---------------- 运行切分 ----------------

class Run(Base):
    """一趟可计量运行。迟到补传会产生 version+1 的新行,旧版本保留供复盘。"""
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    run_key = Column(String(64), nullable=False, index=True)  # 业务键,跨版本稳定
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    driver_mode = Column(String(16), nullable=False, default="AM")  # AM|CM 司机模式
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    distance_km = Column(Float, nullable=False)
    passengers = Column(Integer, nullable=False, default=0)
    shift = Column(String(32), nullable=False, default="")          # 班次
    status = Column(String(16), nullable=False, default="OPEN")     # OPEN|EVALUATED|PUBLISHED
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    train = relationship("Train")
    line = relationship("Line")


class RunEvent(Base):
    """切分输入:停车/起动/折返/进出区段/换编组等原始记录。"""
    __tablename__ = "run_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False)
    type = Column(String(24), nullable=False)
    # ENTER_SECTION|EXIT_SECTION|STOP|START_MOVE|TURNBACK|CONSIST_CHANGE
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    direction = Column(String(8), nullable=True)  # UP|DOWN
    payload = Column(JSON, nullable=True)         # 如 {"consist_id": 2}
    run = relationship("Run")


class RunConsist(Base):
    """运行内的编组有效期(换编组切断)。"""
    __tablename__ = "run_consists"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    consist = relationship("Consist")


class RunSegment(Base):
    """可计量运行片段:同车、同编组、同倍率区间,由停车/折返/换编组/换倍率/设备重启切断。"""
    __tablename__ = "run_segments"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    direction = Column(String(8), nullable=False)
    start_ts = Column(DateTime, nullable=False)
    end_ts = Column(DateTime, nullable=False)
    seq = Column(Integer, nullable=False)
    cut_reason = Column(String(24), nullable=False)  # START|STOP|TURNBACK|SECTION_EXIT|CONSIST_CHANGE|MULTIPLIER_CHANGE|DEVICE_RESET|END
    run = relationship("Run")
    section = relationship("Section")


# ---------------- 能量求值 ----------------

class EnergyFragment(Base):
    """片段级能量事实。quantities 示例:
    车载: {"traction": {"v":..,"lo":..,"hi":..}, "regen": {...}, "aux": {...}}
    供电: {"import": {...}, "export": {...}}
    """
    __tablename__ = "energy_fragments"
    id = Column(Integer, primary_key=True)
    run_segment_id = Column(Integer, ForeignKey("run_segments.id"), nullable=False, index=True)
    source = Column(String(8), nullable=False)      # ONBOARD|SUPPLY
    quantities = Column(JSON, nullable=False)
    quality = Column(String(16), nullable=False)    # GOOD|ESTIMATED|UNKNOWN
    coverage = Column(Float, nullable=False)        # 有效计量时间覆盖率 0..1
    detail = Column(JSON, nullable=True)            # resets/gaps/notes 等解释
    segment = relationship("RunSegment")


class GridCell(Base):
    """网格对齐路径:固定 区段×时间桶 双侧能量累计。"""
    __tablename__ = "grid_cells"
    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False, index=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    bucket_minutes = Column(Integer, nullable=False)
    onboard = Column(JSON, nullable=False)  # {"traction": {...}, "regen": {...}, "aux": {...}}
    supply = Column(JSON, nullable=False)   # {"import": {...}, "export": {...}}
    run_ids = Column(JSON, nullable=False)  # 落入该桶的运行 id 列表
    section = relationship("Section")


# ---------------- 账实核对 ----------------

class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    run_version = Column(Integer, nullable=False)
    method = Column(String(16), nullable=False)     # RUN_ALIGNED|GRID_ALIGNED
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    comparable = Column(String(8), nullable=False)  # FULL|PARTIAL|NONE
    coverage_onboard = Column(Float, nullable=False)
    coverage_supply = Column(Float, nullable=False)
    onboard_net = Column(JSON, nullable=False)      # {"v","lo","hi"} 车载净能耗(牵引+辅助-回馈)
    supply_net = Column(JSON, nullable=False)       # {"v","lo","hi"} 供电净受电(受电-返送)
    diff = Column(JSON, nullable=False)             # supply_net - onboard_net 的区间
    attribution = Column(JSON, nullable=False)      # {line_loss, regen_not_returned, metering_delay, unknown}
    undetermined = Column(JSON, nullable=False)     # 不可判定原因列表
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


# ---------------- 指标与复盘 ----------------

class Metric(Base):
    """已发布指标。迟到数据只产生新版本指标,旧指标保留原水位与差异说明。"""
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)      # RUN|SECTION|SHIFT|VEHICLE_MODEL
    scope_key = Column(String(64), nullable=False)  # 如 run_key / section code / 班次 / 车型
    name = Column(String(32), nullable=False)       # KWH_PER_KM|KWH_PER_PASSENGER|REGEN_RATIO|NET_KWH
    value = Column(Float, nullable=True)            # None 表示不可判定
    coverage = Column(Float, nullable=False)
    method = Column(String(16), nullable=False)
    run_version = Column(Integer, nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    supersedes_id = Column(Integer, ForeignKey("metrics.id"), nullable=True)
    diff_note = Column(Text, nullable=True)         # 与上一已发布水位的差异说明
    source_run_ids = Column(JSON, nullable=False)   # 构成该指标的运行版本 id


class ConfigVersion(Base):
    """计量配置版本:倍率、设备映射、回馈上网是否被电网接受等,供复盘追溯。"""
    __tablename__ = "config_versions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(24), nullable=False)       # MULTIPLIER|DEVICE_MAP|REGEN_ACCEPT|LOSS_RATE
    payload = Column(JSON, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)


# ---------------- 计量不确定度归因 ----------------

class MeterInput(Base):
    """一台设备的一个计量输入的量值证据(倍率证书/校准/分辨率/时钟/覆盖)。

    - 每个 input_key(如 OB-T01:traction)独立版本化:倍率证书晚到只新增一版,
      旧版本保留,已发布的不确定度范围只升版本不回写。
    - cert_accuracy=None 表示该输入缺少校准证书:贡献不可归因,不得虚构精确值。
    - shared_key 标识共享的物理计量关系(同一台表支撑多列车/多个量),
      传播时按同一随机因子或同一最坏方向处理,而非各自独立。
    """
    __tablename__ = "meter_inputs"
    id = Column(Integer, primary_key=True)
    input_key = Column(String(96), nullable=False, index=True)  # 稳定业务键
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("meter_inputs.id"), nullable=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    quantity = Column(String(16), nullable=False)   # traction|regen|aux|import|export
    multiplier_cert = Column(Float, nullable=True)  # 证书标称倍率(仅作证据,不改写主服务)
    cert_accuracy = Column(Float, nullable=True)    # 相对扩展不确定度(如 0.008 = ±0.8%)
    cert_ref = Column(String(64), nullable=True)    # 证书编号
    calibrated_at = Column(DateTime, nullable=True)
    calib_valid_to = Column(DateTime, nullable=True)  # 校准有效期(过期不等于读数为零)
    resolution = Column(Float, nullable=False, default=0.1)  # 分辨率(kWh/表码)
    clock_tolerance_s = Column(Float, nullable=False, default=0.0)  # 采样时钟容差(秒)
    coverage_ratio = Column(Float, nullable=False, default=1.0)     # 输入侧有效覆盖比例
    shared_key = Column(String(96), nullable=False, index=True)     # 相关来源分组键
    note = Column(String(255), nullable=True)
    valid_from = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    device = relationship("Device")
    __table_args__ = (UniqueConstraint("input_key", "version",
                                       name="uq_meter_input_key_version"),)


class UncertaintyAssessment(Base):
    """某范围/指标/方法的一份不确定度归因结论。

    只读主服务能量事实,不改变能量值与测量边界;倍率证书修订只新增版本。
    range_band 为目标指标(非能量值)的扩展范围;decomposition 把范围宽度拆成
    可归因贡献 + 相关来源合并贡献 + 不可独立拆分的残余。
    """
    __tablename__ = "uncertainty_assessments"
    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)        # RUN|SHIFT
    scope_key = Column(String(64), nullable=False, index=True)
    target_metric = Column(String(24), nullable=False)  # KWH_PER_KM|NET_KWH|SUPPLY_NET_KWH|REGEN_RATIO
    method = Column(String(16), nullable=False)       # INTERVAL|JOINT_SAMPLING
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("uncertainty_assessments.id"),
                           nullable=True)
    run_version = Column(Integer, nullable=False, default=1)
    attributable = Column(String(8), nullable=False, default="TRUE")  # TRUE|FALSE
    base_band = Column(JSON, nullable=False)          # 主服务指标范围(未加计量不确定度)
    range_band = Column(JSON, nullable=False)         # 传播后的指标范围
    decomposition = Column(JSON, nullable=False)      # attributable/correlated/residual
    contributions = Column(JSON, nullable=False)      # 逐输入逐来源
    correlated_groups = Column(JSON, nullable=False)  # 共享计量关系分组
    unattributable = Column(JSON, nullable=False)     # 缺失关键输入等原因
    candidates = Column(JSON, nullable=False)         # 预算内校准优先级
    budget = Column(Float, nullable=False, default=5.0)
    inputs_snapshot = Column(JSON, nullable=False)    # 使用的输入版本快照
    source_run_ids = Column(JSON, nullable=False)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def create_all():
    from . import models as _  # noqa
    Base.metadata.create_all(engine)


def drop_all():
    Base.metadata.drop_all(engine)
