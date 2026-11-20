"""计量不确定度归因与校准优先级。

本服务只读主服务已经生成的运行能耗事实(片段/区间),不修改任何能量值或测量边界;
它把每个计量输入携带的倍率证书、校准有效期、分辨率、时钟容差、覆盖比例和适用能量片段
映射到运行/班次指标,给出:

- 可归因的范围宽度贡献(逐来源);
- 相关来源(同一台物理表支撑多个量/多列车)共同影响的宽度;
- 无法独立拆分的残余(方法相关);
- 给定校准预算下最能缩小关键指标范围的候选行动。

两条路径都是独立的第一类方法:
- INTERVAL:沿能量计算链逐项传播上下界,假设少、易审计;共享来源同向传播,结果偏保守。
- JOINT_SAMPLING:保存输入误差与共享校准关系的联合抽样情景,可表达共同漂移与非线性,
  但计算更多、结果依赖情景分布维护。

共用规则:
- 校准过期不等于读数为零:证书仍使用,但误差按保守倍数放大并标注。
- 同一变电所表支撑多列车时不得当作独立误差重复抵消:按 shared_key 同因子传播。
- 倍率证书晚到只形成新的范围版本:仅追加输入/评估版本,不回写旧版本。
- 关键输入缺失(无证书/无记录/无覆盖)时返回不可归因,不虚构精确贡献。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import (
    UNCERTAINTY_COST_CALIBRATE, UNCERTAINTY_COST_CLOCK, UNCERTAINTY_COST_COVERAGE,
    UNCERTAINTY_COST_OBTAIN_CERT, UNCERTAINTY_COST_RESOLUTION,
    UNCERTAINTY_DEFAULT_BUDGET, UNCERTAINTY_EXPIRY_INFLATION,
    UNCERTAINTY_GOOD_ACCURACY, UNCERTAINTY_RESOLUTION_ACTION_THRESHOLD,
    UNCERTAINTY_SAMPLE_COUNT, UNCERTAINTY_SAMPLE_PCT, UNCERTAINTY_SAMPLE_SEED,
    UNCERTAINTY_UNCALIBRATED_ACCURACY,
)
from ..models import (Device, EnergyFragment, MeterInput, Run, RunSegment, Section,
                      UncertaintyAssessment)

SOURCE_TYPES = ("cert", "clock", "coverage")
FIELD_SETS = {
    "NET_KWH": {"T", "A", "R"},
    "KWH_PER_KM": {"T", "A", "R"},
    "REGEN_RATIO": {"T", "A", "R"},
    "SUPPLY_NET_KWH": {"I", "E"},
    "RECON_DIFF_KWH": {"T", "A", "R", "I", "E"},
}
METRIC_LABEL = {
    "NET_KWH": "净能耗(kWh)", "KWH_PER_KM": "单位里程能耗(kWh/km)",
    "REGEN_RATIO": "回馈比例", "SUPPLY_NET_KWH": "供电净受电(kWh)",
    "RECON_DIFF_KWH": "账实差异(kWh)",
}
SCOPE_LABEL = {"RUN": "运行", "SHIFT": "班次"}
SOURCE_LABEL = {"cert": "倍率/校准证书", "clock": "采样时钟容差",
                "coverage": "输入覆盖比例", "resolution": "分辨率"}


# ---------------- 数据装配 ----------------

@dataclass
class Term:
    """一个输入在某运行内的一个能量量(已按适用片段聚合)。"""
    run_idx: int
    field: str                     # T/A/R/I/E
    device_serial: str
    quantity: str                  # traction/regen/aux/import/export
    value: Optional[float]         # 标称值(kWh)
    value_lo: Optional[float]
    value_hi: Optional[float]
    duration_s: float
    input_key: str
    group: str                     # 共享计量关系分组(=shared_key)
    segment_seqs: list = field(default_factory=list)
    has_record: bool = True        # 是否存在 MeterInput 记录


@dataclass
class RunState:
    T: Optional[float] = 0.0
    A: Optional[float] = 0.0
    R: Optional[float] = 0.0
    I: dict = field(default_factory=dict)
    E: dict = field(default_factory=dict)
    distance: float = 1.0

    def copy(self) -> "RunState":
        return RunState(self.T, self.A, self.R, dict(self.I), dict(self.E),
                        self.distance)

    def sup(self) -> Optional[float]:
        # 无供电侧片段(如供电表缺失)时不能把空集合当作 0
        if not self.I or not self.E:
            return None
        vals = list(self.I.values()) + list(self.E.values())
        if any(v is None for v in vals):
            return None
        return sum(self.I.values()) - sum(self.E.values())


@dataclass
class Model:
    runs: list
    states: list
    lo_states: list
    hi_states: list
    terms: list
    inputs: dict                  # (device_serial, quantity) -> MeterInput
    missing_records: list


def _latest_inputs(db: Session) -> dict:
    out: dict[str, MeterInput] = {}
    for mi in (db.query(MeterInput).order_by(MeterInput.input_key,
                                             MeterInput.version).all()):
        out[mi.input_key] = mi        # 升序遍历,后者(高版本)覆盖
    # 再按 (serial, quantity) 建索引
    by_dev: dict = {}
    for mi in out.values():
        dev = db.get(Device, mi.device_id)
        if dev:
            by_dev[(dev.serial, mi.quantity)] = mi
    return by_dev


def _latest_runs(db: Session, scope: str, scope_key: str) -> list[Run]:
    if scope == "RUN":
        return (db.query(Run).filter(Run.run_key == scope_key)
                .order_by(Run.version.desc()).limit(1).all())
    # SHIFT:该班次每个 run_key 的最新版本
    rows = (db.query(Run).filter(Run.shift == scope_key)
            .order_by(Run.run_key, Run.version.desc()).all())
    seen, out = set(), []
    for r in rows:
        if r.run_key not in seen:
            seen.add(r.run_key)
            out.append(r)
    return out


def _onboard_device(db: Session, run: Run) -> Optional[Device]:
    return (db.query(Device).filter(Device.kind == "ONBOARD",
                                    Device.train_id == run.train_id)
            .order_by(Device.id).first())


def build_model(db: Session, runs: list[Run]) -> Model:
    inputs = _latest_inputs(db)
    states, lo_states, hi_states = [], [], []
    terms: list[Term] = []
    missing_records = []

    for ri, run in enumerate(runs):
        segs = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                .order_by(RunSegment.seq).all())
        st = RunState(distance=run.distance_km or 1.0)
        lo = RunState(distance=run.distance_km or 1.0)
        hi = RunState(distance=run.distance_km or 1.0)

        # ---- 车载侧 ----
        dev = _onboard_device(db, run)
        if dev:
            for field_name, qname in (("T", "traction"), ("A", "aux"),
                                      ("R", "regen")):
                vals, los, his, dur, seqs = [], [], [], 0.0, []
                for seg in segs:
                    frag = (db.query(EnergyFragment)
                            .filter(EnergyFragment.run_segment_id == seg.id,
                                    EnergyFragment.source == "ONBOARD").first())
                    if not frag:
                        continue
                    band = (frag.quantities or {}).get(qname, {})
                    vals.append(band.get("v"))
                    los.append(band.get("lo"))
                    his.append(band.get("hi"))
                    dur += max(0.0, (seg.end_ts - seg.start_ts).total_seconds())
                    seqs.append(seg.seq)
                rec = inputs.get((dev.serial, qname))
                if not rec:
                    missing_records.append({"device": dev.serial,
                                            "quantity": qname})
                value = None if any(v is None for v in vals) or not vals \
                    else round(sum(vals), 6)
                vlo = None if value is None or any(v is None for v in los) \
                    else round(sum(los), 6)
                vhi = None if value is None or any(v is None for v in his) \
                    else round(sum(his), 6)
                _set(st, lo, hi, field_name, None, value, vlo, vhi)
                terms.append(Term(ri, field_name, dev.serial, qname, value,
                                  vlo, vhi, dur, f"{dev.serial}:{qname}",
                                  rec.shared_key if rec else dev.serial,
                                  seqs, bool(rec)))

        # ---- 供电侧(按变电所设备聚合) ----
        by_dev: dict[str, dict] = {}
        for seg in segs:
            section = db.get(Section, seg.section_id)
            if not section or not section.substation_device_id:
                continue
            sdev = db.get(Device, section.substation_device_id)
            if not sdev:
                continue
            frag = (db.query(EnergyFragment)
                    .filter(EnergyFragment.run_segment_id == seg.id,
                            EnergyFragment.source == "SUPPLY").first())
            if not frag:
                continue
            dur = max(0.0, (seg.end_ts - seg.start_ts).total_seconds())
            d = by_dev.setdefault(sdev.serial, {
                "import": [], "export": [], "imp_lo": [], "imp_hi": [],
                "exp_lo": [], "exp_hi": [], "dur": 0.0, "seqs": []})
            q = frag.quantities or {}
            imp, exp = q.get("import", {}), q.get("export", {})
            d["import"].append(imp.get("v"))
            d["imp_lo"].append(imp.get("lo"))
            d["imp_hi"].append(imp.get("hi"))
            d["export"].append(exp.get("v"))
            d["exp_lo"].append(exp.get("lo"))
            d["exp_hi"].append(exp.get("hi"))
            d["dur"] += dur
            d["seqs"].append(seg.seq)
        for serial, d in by_dev.items():
            for field_name, qname in (("I", "import"), ("E", "export")):
                key = qname
                vals = d["import"] if qname == "import" else d["export"]
                los = d["imp_lo"] if qname == "import" else d["exp_lo"]
                his = d["imp_hi"] if qname == "import" else d["exp_hi"]
                rec = inputs.get((serial, qname))
                if not rec:
                    missing_records.append({"device": serial, "quantity": qname})
                value = None if any(v is None for v in vals) or not vals \
                    else round(sum(vals), 6)
                vlo = None if value is None or any(v is None for v in los) \
                    else round(sum(los), 6)
                vhi = None if value is None or any(v is None for v in his) \
                    else round(sum(his), 6)
                _set(st, lo, hi, field_name, serial, value, vlo, vhi)
                terms.append(Term(ri, field_name, serial, qname, value,
                                  vlo, vhi, d["dur"], f"{serial}:{qname}",
                                  rec.shared_key if rec else serial,
                                  d["seqs"], bool(rec)))
        states.append(st)
        lo_states.append(lo)
        hi_states.append(hi)

    return Model(runs, states, lo_states, hi_states, terms, inputs,
                 missing_records)


def _set(st: RunState, lo: RunState, hi: RunState, field_name: str,
         dev: Optional[str], v, vlo, vhi):
    def put(s: RunState, val):
        if dev is None:
            setattr(s, field_name, val)      # None 保留为未知,不冒充 0
        else:
            (s.I if field_name == "I" else s.E)[dev] = val
    put(st, v)
    put(lo, vlo)
    put(hi, vhi)


# ---------------- 指标函数 ----------------

def _per_run(st: RunState, metric: str) -> Optional[float]:
    if metric in ("NET_KWH", "KWH_PER_KM", "REGEN_RATIO"):
        if st.T is None or st.A is None or st.R is None:
            return None
        onb = st.T + st.A - st.R
        if metric == "NET_KWH":
            return onb
        if metric == "KWH_PER_KM":
            return onb / st.distance if st.distance else None
        denom = st.T + st.A
        return st.R / denom if denom > 0 else None
    sup = st.sup()
    if sup is None:
        return None
    if metric == "SUPPLY_NET_KWH":
        return sup
    if metric == "RECON_DIFF_KWH":
        if st.T is None or st.A is None or st.R is None:
            return None
        return sup - (st.T + st.A - st.R)
    return None


def _eval_metric(states: list[RunState], metric: str) -> Optional[float]:
    vals = []
    for st in states:
        m = _per_run(st, metric)
        if m is None or not math.isfinite(m):
            return None
        vals.append(m)
    return sum(vals) / len(vals) if vals else None


def _apply_deltas(states: list[RunState], deltas: list) -> list[RunState]:
    """deltas: list of (run_idx, field, device, delta)。"""
    if not deltas:
        return states
    out = [s.copy() for s in states]
    for ri, field_name, dev, delta in deltas:
        s = out[ri]
        if field_name == "T":
            s.T += delta
        elif field_name == "A":
            s.A += delta
        elif field_name == "R":
            s.R += delta
        elif field_name == "I":
            s.I[dev] = s.I.get(dev, 0.0) + delta
        elif field_name == "E":
            s.E[dev] = s.E.get(dev, 0.0) + delta
    return out


# ---------------- 输入误差模型 ----------------

def _eff_source(model: Model, term: Term, override: dict) -> dict:
    """返回该 term 当前生效的输入误差属性(含行动覆盖)。"""
    rec = model.inputs.get((term.device_serial, term.quantity))
    attrs = {
        "cert_acc": rec.cert_accuracy if rec else None,
        "expired": bool(rec and rec.calib_valid_to
                        and rec.calib_valid_to < datetime.utcnow()),
        "resolution": rec.resolution if rec else 0.1,
        "tol_s": rec.clock_tolerance_s if rec else 0.0,
        "coverage_ratio": rec.coverage_ratio if rec else 1.0,
        "multiplier": (rec.multiplier_cert if rec and rec.multiplier_cert
                       else 1.0),
    }
    if rec is None:
        attrs["cert_acc"] = None
    ov = override.get(term.device_serial)
    if ov:
        attrs.update(ov)
    return attrs


def _term_half(term: Term, stype: str, attrs: dict) -> Optional[float]:
    """某来源在该 term 上的绝对半宽(kWh);None 表示无法界定。"""
    v = abs(term.value or 0.0)
    if stype == "resolution":
        return attrs["resolution"] * attrs["multiplier"] / 2.0
    if stype == "cert":
        acc = attrs.get("cert_acc")
        if acc is None:
            return None
        if attrs.get("expired"):
            acc *= UNCERTAINTY_EXPIRY_INFLATION
        return v * acc
    if stype == "clock":
        if term.duration_s <= 0:
            return 0.0
        return v * (attrs.get("tol_s", 0.0) / term.duration_s)
    if stype == "coverage":
        return v * max(0.0, 1.0 - attrs.get("coverage_ratio", 1.0))
    return 0.0


# ---------------- 归因计算 ----------------

def _relevant_terms(model: Model, metric: str) -> list[Term]:
    fields = FIELD_SETS[metric]
    return [t for t in model.terms if t.field in fields]


def _check_unattributable(model: Model, metric: str,
                          override: Optional[dict] = None) -> list[dict]:
    """关键输入缺失 -> 不可归因。只检查与该指标相关的输入。

    override 用于评估候选行动的效果(如补证书/补覆盖);未覆盖时按实际输入判断。
    """
    override = override or {}
    reasons = []
    seen = set()
    for term in _relevant_terms(model, metric):
        key = term.input_key
        if key in seen:
            continue
        seen.add(key)
        attrs = _eff_source(model, term, override)
        if attrs.get("cert_acc") is None:
            if not term.has_record:
                reasons.append({"code": "MISSING_METER_INPUT",
                                "detail": f"{key} 无计量输入记录,无法界定误差"})
            else:
                reasons.append({
                    "code": "MISSING_CALIBRATION_CERTIFICATE",
                    "detail": f"{key} 缺少校准/倍率证书,不虚构精确贡献"})
        if (attrs.get("coverage_ratio") or 0.0) <= 0.0:
            reasons.append({"code": "INPUT_COVERAGE_ZERO",
                            "detail": f"{key} 覆盖比例为 0,读数不可用"})
        if term.value is None:
            reasons.append({"code": "UNKNOWN_ENERGY_VALUE",
                            "detail": f"{key} 主服务能量值未知,不虚构精确贡献"})
    return reasons


def _components(model: Model, metric: str, override: dict):
    """构造传播分量:
    - 分组来源('g', group, stype):同组同向传播(共享计量关系);
    - 独立来源('r', term_index):分辨率逐项独立。
    返回 (components, term_components, missing_cert)
    """
    terms = _relevant_terms(model, metric)
    components = []
    term_components: dict[int, list] = {}
    missing_cert = []
    groups: dict[tuple, list] = {}
    for idx, t in enumerate(terms):
        attrs = _eff_source(model, t, override)
        for stype in SOURCE_TYPES:
            half = _term_half(t, stype, attrs)
            if half is None:
                if stype == "cert":
                    missing_cert.append(t.input_key)
                continue
            if half <= 1e-12:
                continue
            comp = ("g", t.group, stype)
            if comp not in components:
                components.append(comp)
            groups.setdefault(comp, []).append((t, half))
        rh = _term_half(t, "resolution", attrs)
        if rh and rh > 1e-12:
            comp = ("r", id(t), t.input_key)
            components.append(comp)
            term_components[id(t)] = [(t, rh, comp)]
            groups.setdefault(comp, [(t, rh)])
    return components, groups, missing_cert, terms


def _component_deltas(component, groups, eps: float) -> list:
    comp_type = component[0]
    members = groups.get(component, [])
    if comp_type == "g":
        # ('g', group, stype)
        _, group, stype = component
        return [(t.run_idx, t.field, t.device_serial, eps * half)
                for t, half in members]
    # ('r', id, key) -> single term
    return [(t.run_idx, t.field, t.device_serial, eps * half)
            for t, half in members]


def _base_and_value(model: Model, metric: str):
    v = _eval_metric(model.states, metric)
    lo = _eval_metric(model.lo_states, metric)
    hi = _eval_metric(model.hi_states, metric)
    # 区间必须 lo<=v<=hi(比率类指标经 lo/hi 状态求值可能倒挂,这里归一)
    if lo is not None and hi is not None and v is not None:
        lo, hi = min(lo, v, hi), max(lo, v, hi)
    return v, lo, hi


def _interval(model: Model, metric: str, override: dict) -> dict:
    v, base_lo, base_hi = _base_and_value(model, metric)
    components, groups, missing_cert, terms = _components(model, metric, override)
    if v is None:
        return {"ok": False, "reason": "UNKNOWN_ENERGY_VALUE",
                "attributable": False}
    base_half = max(abs(v - base_lo), abs(base_hi - v)) if (
        base_lo is not None and base_hi is not None) else None

    comp_rows = []
    add_hi, add_lo = [], []          # 用于相关角点
    additive_width = 0.0
    for comp in components:
        if comp[0] == "g":
            _, group, stype = comp
            d_plus = _apply_deltas(model.states,
                                   _component_deltas(comp, groups, +1.0))
            d_minus = _apply_deltas(model.states,
                                    _component_deltas(comp, groups, -1.0))
            m_plus, m_minus = _eval_metric(d_plus, metric), \
                _eval_metric(d_minus, metric)
            if m_plus is None or m_minus is None:
                continue
            delta = max(abs(m_plus - v), abs(m_minus - v))
            additive_width += 2 * delta
            # 角点:选择使指标更大/更小的符号
            s_hi = +1.0 if m_plus >= m_minus else -1.0
            add_hi.extend(_component_deltas(comp, groups, s_hi))
            add_lo.extend(_component_deltas(comp, groups, -s_hi))
            comp_rows.append({
                "kind": "correlated" if len(groups[comp]) > 1 else "attributable",
                "source_type": stype,
                "source_label": SOURCE_LABEL.get(stype, stype),
                "group": group,
                "members": sorted({t.input_key for t, _ in groups[comp]}),
                "width": round(2 * delta, 4),
                "half": round(delta, 4),
            })
        else:
            _, _, key = comp
            d_plus = _apply_deltas(model.states,
                                   _component_deltas(comp, groups, +1.0))
            d_minus = _apply_deltas(model.states,
                                    _component_deltas(comp, groups, -1.0))
            m_plus, m_minus = _eval_metric(d_plus, metric), \
                _eval_metric(d_minus, metric)
            if m_plus is None or m_minus is None:
                continue
            delta = max(abs(m_plus - v), abs(m_minus - v))
            additive_width += 2 * delta
            s_hi = +1.0 if m_plus >= m_minus else -1.0
            add_hi.extend(_component_deltas(comp, groups, s_hi))
            add_lo.extend(_component_deltas(comp, groups, -s_hi))
            t = groups[comp][0][0]
            comp_rows.append({
                "kind": "attributable", "source_type": "resolution",
                "source_label": SOURCE_LABEL["resolution"],
                "group": t.group, "members": [key],
                "width": round(2 * delta, 4), "half": round(delta, 4),
            })

    # 相关角点:所有分量同向取最坏
    m_corner_hi = _eval_metric(_apply_deltas(model.states, add_hi), metric)
    m_corner_lo = _eval_metric(_apply_deltas(model.states, add_lo), metric)
    corner_extra = 0.0
    if m_corner_hi is not None and m_corner_lo is not None:
        corner_extra = max(abs(m_corner_hi - v), abs(m_corner_lo - v))
    total_extra = max(additive_width / 2.0, corner_extra)
    total_extra_width = 2 * total_extra
    residual = round(max(0.0, total_extra_width - additive_width), 4)
    return {
        "ok": True, "value": v, "base_half": base_half or 0.0,
        "components": comp_rows, "missing_cert": sorted(set(missing_cert)),
        "additive_width": additive_width, "extra_width": total_extra_width,
        "residual": residual, "range_lo": v - (base_half or 0.0) - total_extra,
        "range_hi": v + (base_half or 0.0) + total_extra,
        "method": "INTERVAL",
    }


def _sample_values(model: Model, metric: str, override: dict,
                   components: list, groups: dict, scenarios: list,
                   disabled=None) -> list:
    disabled = disabled or set()
    vals = []
    for eps_map in scenarios:
        deltas = []
        for comp in components:
            if comp in disabled:
                continue
            eps = eps_map[comp]
            deltas.extend(_component_deltas(comp, groups, eps))
        m = _eval_metric(_apply_deltas(model.states, deltas), metric)
        if m is not None:
            vals.append(m)
    return vals


def _pct(vals: list, p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


_SCENARIO_CACHE: dict[tuple, list] = {}


def _scenarios_for(components: list) -> list:
    """确定性情景(固定种子)。按分量结构缓存:优先级评估重复调用时复用。"""
    key = tuple(components)
    cached = _SCENARIO_CACHE.get(key)
    if cached is not None:
        return cached
    rng = random.Random(UNCERTAINTY_SAMPLE_SEED)
    scenarios = [{comp: rng.uniform(-1.0, 1.0) for comp in components}
                 for _ in range(UNCERTAINTY_SAMPLE_COUNT)]
    _SCENARIO_CACHE[key] = scenarios
    return scenarios


def _joint_sampling(model: Model, metric: str, override: dict,
                    detail: bool = True) -> dict:
    v, base_lo, base_hi = _base_and_value(model, metric)
    components, groups, missing_cert, terms = _components(model, metric, override)
    if v is None:
        return {"ok": False, "reason": "UNKNOWN_ENERGY_VALUE",
                "attributable": False}
    base_half = max(abs(v - base_lo), abs(base_hi - v)) if (
        base_lo is not None and base_hi is not None) else 0.0

    scenarios = _scenarios_for(components)

    vals = _sample_values(model, metric, override, components, groups, scenarios)
    if not vals:
        return {"ok": False, "reason": "SAMPLING_EMPTY", "attributable": False}
    lo = _pct(vals, UNCERTAINTY_SAMPLE_PCT[0])
    hi = _pct(vals, UNCERTAINTY_SAMPLE_PCT[1])
    lo, hi = min(lo, v), max(hi, v)
    extra_width = max(0.0, hi - lo)

    if not detail:
        # 只求宽度(校准优先级评估用):跳过留一法分解
        return {
            "ok": True, "value": v, "base_half": base_half,
            "components": [], "missing_cert": sorted(set(missing_cert)),
            "additive_width": 0.0, "extra_width": extra_width,
            "residual": 0.0, "overlap": False,
            "range_lo": lo - base_half, "range_hi": hi + base_half,
            "method": "JOINT_SAMPLING",
        }

    # 留一法(leave-one-out):固定某分量后范围宽度的减少 = 该分量边际贡献
    comp_rows, contrib_sum = [], 0.0
    for comp in components:
        base_vals = _sample_values(model, metric, override, components, groups,
                                   scenarios, disabled={comp})
        if not base_vals:
            continue
        w = max(0.0, _pct(base_vals, UNCERTAINTY_SAMPLE_PCT[1])
                - _pct(base_vals, UNCERTAINTY_SAMPLE_PCT[0]))
        reduction = max(0.0, extra_width - w)
        if reduction < 1e-12:
            continue
        if comp[0] == "g":
            _, group, stype = comp
            comp_rows.append({
                "kind": "correlated" if len(groups[comp]) > 1 else "attributable",
                "source_type": stype,
                "source_label": SOURCE_LABEL.get(stype, stype),
                "group": group,
                "members": sorted({t.input_key for t, _ in groups[comp]}),
                "width": round(reduction, 4), "half": round(reduction / 2, 4),
            })
        else:
            _, _, key = comp
            t = groups[comp][0][0]
            comp_rows.append({
                "kind": "attributable", "source_type": "resolution",
                "source_label": SOURCE_LABEL["resolution"],
                "group": t.group, "members": [key],
                "width": round(reduction, 4), "half": round(reduction / 2, 4),
            })
        contrib_sum += reduction

    overlap = contrib_sum > extra_width + 1e-6
    residual = round(max(0.0, extra_width - contrib_sum), 4)
    return {
        "ok": True, "value": v, "base_half": base_half,
        "components": comp_rows, "missing_cert": sorted(set(missing_cert)),
        "additive_width": contrib_sum, "extra_width": extra_width,
        "residual": residual, "overlap": overlap,
        "range_lo": lo - base_half, "range_hi": hi + base_half,
        "method": "JOINT_SAMPLING",
    }


# ---------------- 对外:归因结论 ----------------

def _finalize(res: dict, metric: str) -> dict:
    if not res.get("ok"):
        return {
            "attributable": False,
            "base_band": None,
            "range_band": None,
            "decomposition": {"boundary_width": None, "attributable": [],
                              "correlated": [], "residual": None,
                              "extra_width": None, "overlap": False},
            "contributions": [], "correlated_groups": [],
            "unattributable": [{"code": res.get("reason", "UNKNOWN"),
                                "detail": "能量值未知,无法传播计量不确定度"}],
            "method": res.get("method"),
        }
    v = round(res["value"], 4)
    extra = res["extra_width"]
    base_half = res["base_half"]
    comps = res["components"]
    comp_sum = sum(c["width"] for c in comps)
    residual = res["residual"]
    if comp_sum > extra + 1e-6:
        scale = extra / comp_sum if comp_sum > 0 else 0.0
        for c in comps:
            c["width"] = round(c["width"] * scale, 4)
            c["half"] = round(c["width"] / 2, 4)
        residual = 0.0
        overlap = True
    else:
        overlap = res.get("overlap", False)
    attributable = [c for c in comps if c["kind"] == "attributable"]
    correlated = [c for c in comps if c["kind"] == "correlated"]
    boundary_width = round(2 * base_half, 4)
    range_band = {"v": v, "lo": round(res["range_lo"], 4),
                  "hi": round(res["range_hi"], 4)}
    decomposition = {
        "boundary_width": boundary_width,
        "attributable": attributable,
        "correlated": correlated,
        "residual": round(residual, 4),
        "extra_width": round(extra, 4),
        "overlap": overlap,
        "identity_width": round(boundary_width
                                + sum(c["width"] for c in comps)
                                + residual, 4),
    }
    contributions = []
    for c in comps:
        shared = c["kind"] == "correlated" and len(c["members"]) > 1
        for member in c["members"]:
            contributions.append({
                "input_key": member, "source_type": c["source_type"],
                "source_label": c["source_label"], "group": c["group"],
                "kind": c["kind"], "half_width": c["half"],
                # 共享来源无法把宽度精确拆给单个成员:给出组级宽度并标注
                "width": c["width"] if not shared else None,
                "group_width": c["width"] if shared else c["width"],
                "shared": shared,
            })
    return {
        "attributable": True,
        "base_band": {"v": v,
                      "lo": round(v - base_half, 4),
                      "hi": round(v + base_half, 4)},
        "range_band": range_band,
        "decomposition": decomposition,
        "contributions": contributions,
        "correlated_groups": correlated,
        "unattributable": [],
        "method": res["method"],
    }


def compute(db: Session, scope: str, scope_key: str, target_metric: str,
            method: str, override: Optional[dict] = None,
            budget: float = UNCERTAINTY_DEFAULT_BUDGET,
            model: Optional[Model] = None, detail: bool = True) -> dict:
    if model is None:
        runs = _latest_runs(db, scope, scope_key)
        model = build_model(db, runs)
    reasons = _check_unattributable(model, target_metric, override)
    override = override or {}

    if method == "INTERVAL":
        raw = _interval(model, target_metric, override)
    elif method == "JOINT_SAMPLING":
        raw = _joint_sampling(model, target_metric, override, detail=detail)
    else:
        raise ValueError(f"未知方法 {method}")

    if not detail:
        # 只求宽度:不构造逐来源分解
        return {
            "attributable": not reasons and not raw.get("missing_cert"),
            "base_band": None if not raw.get("ok") else
            {"v": raw["value"], "lo": round(raw["value"] - raw["base_half"], 4),
             "hi": round(raw["value"] + raw["base_half"], 4)},
            "range_band": None if (reasons or raw.get("missing_cert")
                                   or not raw.get("ok")) else
            {"v": round(raw["value"], 4), "lo": round(raw["range_lo"], 4),
             "hi": round(raw["range_hi"], 4)},
            "decomposition": {}, "contributions": [],
            "correlated_groups": [], "unattributable": reasons or [],
            "method": method,
        }

    out = _finalize(raw, target_metric)
    # 缺失关键输入:整体不可归因(不虚构精确贡献)
    if reasons:
        out["unattributable"] = reasons
        out["attributable"] = False
        out["range_band"] = None
    elif raw.get("missing_cert"):
        # 有输入缺失证书,但 _check 未覆盖(如无记录已覆盖);保守置不可归因
        out["unattributable"] = [{
            "code": "MISSING_CALIBRATION_CERTIFICATE",
            "detail": "、".join(raw["missing_cert"]) + " 缺少证书"}]
        out["attributable"] = False
        out["range_band"] = None

    out["budget"] = budget
    out["inputs"] = _inputs_snapshot(model)
    out["run_ids"] = [r.id for r in model.runs]
    out["run_versions"] = [r.version for r in model.runs]
    return out


def _inputs_snapshot(model: Model) -> list[dict]:
    seen = {}
    for term in model.terms:
        rec = model.inputs.get((term.device_serial, term.quantity))
        if term.input_key in seen:
            continue
        if rec is None:
            seen[term.input_key] = {
                "input_key": term.input_key, "device": term.device_serial,
                "quantity": term.quantity, "version": None,
                "cert_accuracy": None, "cert_ref": None,
                "calib_valid_to": None, "resolution": None,
                "clock_tolerance_s": None, "coverage_ratio": None,
                "shared_key": term.group, "status": "MISSING_INPUT",
                "segments": term.segment_seqs,
            }
            continue
        expired = bool(rec.calib_valid_to and rec.calib_valid_to < datetime.utcnow())
        status = ("EXPIRED" if expired else
                  "MISSING_CERT" if rec.cert_accuracy is None else "VALID")
        seen[term.input_key] = {
            "input_key": term.input_key, "device": term.device_serial,
            "quantity": term.quantity, "version": rec.version,
            "cert_accuracy": rec.cert_accuracy, "cert_ref": rec.cert_ref,
            "calib_valid_to": rec.calib_valid_to.isoformat()
            if rec.calib_valid_to else None,
            "resolution": rec.resolution,
            "clock_tolerance_s": rec.clock_tolerance_s,
            "coverage_ratio": rec.coverage_ratio, "shared_key": rec.shared_key,
            "status": status, "segments": term.segment_seqs,
        }
    return list(seen.values())


def method_guidance(db: Session, scope: str, scope_key: str,
                    target_metric: str) -> dict:
    """方法选择建议:根据误差资料形式、共享设备程度、看板响应时限给出。

    不把抽样只用来验证区间,也不把区间只用来筛选抽样输入;两种方法各自独立给出
    可归因范围与校准优先级。
    """
    runs = _latest_runs(db, scope, scope_key)
    model = build_model(db, runs)
    terms = _relevant_terms(model, target_metric)
    missing_cert = any(t.has_record and model.inputs.get(
        (t.device_serial, t.quantity)) and
        model.inputs[(t.device_serial, t.quantity)].cert_accuracy is None
        for t in terms)
    group_counts: dict[str, int] = {}
    for t in terms:
        group_counts[t.group] = group_counts.get(t.group, 0) + 1
    shared_groups = {g: n for g, n in group_counts.items() if n > 1}
    shared_frac = (sum(n for n in shared_groups.values()) / len(terms)
                   if terms else 0.0)
    recommendations = []
    if missing_cert:
        recommendations.append(
            "存在缺证书/无记录的输入:两种方法都返回不可归因;先补资料,再谈方法选择。")
    if shared_frac >= 0.2:
        recommendations.append(
            "共享计量关系占比高:联合抽样能表达共同漂移;区间法会因同向传播偏保守,"
            "两者都可用,但看板应同时展示相关来源分组。")
    else:
        recommendations.append(
            "共享计量关系占比低:区间算术假设少、审计易解释,可作为默认。")
    recommendations.append(
        "看板响应时限紧时用区间法(线性复杂度);需要非线性比率/共同漂移解释时用联合抽样"
        "(固定种子,结果可复现)。")
    return {
        "shared_groups": shared_groups,
        "shared_fraction": round(shared_frac, 4),
        "missing_certificate": missing_cert,
        "recommendations": recommendations,
        "default_method": "INTERVAL",
    }


# ---------------- 校准优先级 ----------------

def _range_width(result: dict) -> Optional[float]:
    if not result.get("attributable") or result["range_band"] is None:
        return None
    return result["range_band"]["hi"] - result["range_band"]["lo"]


def _merge_override(base: dict, dev: str, update: dict) -> dict:
    out = {k: dict(v) for k, v in base.items()}
    out.setdefault(dev, {}).update(update)
    return out


def _actions(model: Model, metric: str) -> list[dict]:
    """按设备生成候选行动(抽象成本)。"""
    dev_attrs: dict[str, dict] = {}
    for term in _relevant_terms(model, metric):
        attrs = _eff_source(model, term, {})
        rec = model.inputs.get((term.device_serial, term.quantity))
        d = dev_attrs.setdefault(term.device_serial, {
            "serials": set(), "quantities": set(), "cert_accs": set(),
            "missing_cert": False, "expired": False, "tol": 0.0,
            "cov": 1.0, "res": 0.0, "res_rel": 0.0, "record": bool(rec),
        })
        d["quantities"].add(term.quantity)
        d["expired"] = d["expired"] or attrs.get("expired", False)
        d["tol"] = max(d["tol"], attrs.get("tol_s", 0.0))
        d["cov"] = min(d["cov"], attrs.get("coverage_ratio", 1.0))
        rh = _term_half(term, "resolution", attrs) or 0.0
        d["res"] = max(d["res"], rh)
        if term.value:
            d["res_rel"] = max(d["res_rel"], rh / abs(term.value))
        if attrs.get("cert_acc") is None:
            d["missing_cert"] = True
        else:
            d["cert_accs"].add(attrs["cert_acc"])

    actions = []
    for dev, d in dev_attrs.items():
        if d["missing_cert"]:
            actions.append({
                "action": "OBTAIN_CERT", "device": dev,
                "label": f"取得 {dev} 缺失的校准/倍率证书",
                "cost": UNCERTAINTY_COST_OBTAIN_CERT,
                "update": {"cert_acc": UNCERTAINTY_UNCALIBRATED_ACCURACY,
                           "expired": False},
            })
        elif d["expired"] or max(d["cert_accs"] or {0}) > UNCERTAINTY_GOOD_ACCURACY:
            actions.append({
                "action": "RECALIBRATE", "device": dev,
                "label": f"重新校准/换发 {dev} 证书",
                "cost": UNCERTAINTY_COST_CALIBRATE,
                "update": {"cert_acc": UNCERTAINTY_GOOD_ACCURACY,
                           "expired": False},
            })
        if d["tol"] > 0:
            actions.append({
                "action": "CLOCK_SYNC", "device": dev,
                "label": f"同步 {dev} 采样时钟(消除时钟错位)",
                "cost": UNCERTAINTY_COST_CLOCK, "update": {"tol_s": 0.0},
            })
        if d["cov"] < 1.0:
            actions.append({
                "action": "COVERAGE_FILL", "device": dev,
                "label": f"补测 {dev} 缺失覆盖片段",
                "cost": UNCERTAINTY_COST_COVERAGE, "update": {"coverage_ratio": 1.0},
            })
        if d["res_rel"] > UNCERTAINTY_RESOLUTION_ACTION_THRESHOLD:
            actions.append({
                "action": "IMPROVE_RESOLUTION", "device": dev,
                "label": f"更换 {dev} 高分辨率表计",
                "cost": UNCERTAINTY_COST_RESOLUTION,
                "update": {"resolution": 0.0},
            })
    return actions


def _priority(db, model, scope, scope_key, metric, method, budget) -> dict:
    base = compute(db, scope, scope_key, metric, method, budget=budget,
                   model=model)
    current_width = _range_width(base)
    candidates = _actions(model, metric)

    def _eval_override(ov):
        # 优先级只需宽度,跳过逐来源分解(detail=False)以控制响应时间
        return compute(db, scope, scope_key, metric, method, override=ov,
                       budget=budget, model=model, detail=False)

    # 每个行动的独立收益(基于当前输入)
    evaluated = []
    for act in candidates:
        ov = _merge_override({}, act["device"], act["update"])
        res = _eval_override(ov)
        w = _range_width(res)
        if current_width is None and w is not None:
            benefit, restores = None, True
        elif current_width is not None and w is not None:
            benefit, restores = max(0.0, current_width - w), False
        else:
            benefit, restores = 0.0, False
        evaluated.append({**act, "standalone_width": w,
                          "benefit": None if benefit is None else round(benefit, 4),
                          "benefit_per_cost": None if benefit is None or act["cost"] <= 0
                          else round(benefit / act["cost"], 4),
                          "restores_attribution": restores})

    # 贪心:在预算内按 收益/成本 选择(恢复可归因优先)
    remaining = budget
    applied: dict = {}
    selected, chosen = [], set()
    cur_width = current_width
    while True:
        best, best_key = None, None
        for idx, ev in enumerate(evaluated):
            if idx in chosen or ev["cost"] > remaining + 1e-9:
                continue
            ov = _merge_override(applied, ev["device"], ev["update"])
            res = _eval_override(ov)
            w = _range_width(res)
            if cur_width is None and w is not None:
                key = (1, float("inf"))          # 恢复可归因,最优先
                gain = None
            elif cur_width is not None and w is not None:
                gain = max(0.0, cur_width - w)
                if gain <= 1e-9:
                    continue
                key = (0, gain / ev["cost"] if ev["cost"] > 0 else gain)
            else:
                continue
            if best_key is None or key > best_key:
                best_key, best = key, (idx, w, gain, ov)
        if best is None:
            break
        idx, w, gain, ov = best
        ev = evaluated[idx]
        ev["selected"] = True
        ev["marginal_benefit"] = None if gain is None else round(gain, 4)
        ev["width_after"] = None if w is None else round(w, 4)
        selected.append(ev)
        chosen.add(idx)
        applied = ov
        cur_width = w
        remaining -= ev["cost"]
    for ev in evaluated:
        ev.setdefault("selected", False)

    total_reduction = None
    if current_width is not None:
        final_width = cur_width if cur_width is not None else current_width
        total_reduction = round(current_width - (final_width or current_width), 4)

    return {
        "budget": budget,
        "remaining_budget": round(remaining, 4),
        "current_width": None if current_width is None else round(current_width, 4),
        "final_width": None if cur_width is None else round(cur_width, 4),
        "total_reduction": total_reduction,
        "selected": [e for e in evaluated if e["selected"]],
        "evaluated": sorted(
            evaluated,
            key=lambda e: ((1 if e["restores_attribution"] else 0),
                           e["benefit_per_cost"] or 0), reverse=True),
        "method": method,
    }


def compute_full(db: Session, scope: str, scope_key: str, target_metric: str,
                 method: str, budget: float = UNCERTAINTY_DEFAULT_BUDGET) -> dict:
    """重算结果 + 校准优先级(不落库)。"""
    runs = _latest_runs(db, scope, scope_key)
    model = build_model(db, runs)
    out = compute(db, scope, scope_key, target_metric, method, budget=budget,
                  model=model)
    out["candidates"] = _priority(db, model, scope, scope_key, target_metric,
                                  method, budget)
    return out


# ---------------- 评估发布(带历史版本) ----------------

def assess(db: Session, scope: str, scope_key: str, target_metric: str,
           method: str = "INTERVAL",
           budget: float = UNCERTAINTY_DEFAULT_BUDGET,
           note: Optional[str] = None) -> UncertaintyAssessment:
    if scope not in ("RUN", "SHIFT"):
        raise ValueError("scope 必须为 RUN 或 SHIFT")
    if target_metric not in FIELD_SETS:
        raise ValueError(f"未知指标 {target_metric}")
    if method not in ("INTERVAL", "JOINT_SAMPLING"):
        raise ValueError(f"未知方法 {method}")

    runs = _latest_runs(db, scope, scope_key)
    model = build_model(db, runs)
    res = compute(db, scope, scope_key, target_metric, method, budget=budget)
    prio = _priority(db, model, scope, scope_key, target_metric, method, budget)

    prev = (db.query(UncertaintyAssessment)
            .filter(UncertaintyAssessment.scope == scope,
                    UncertaintyAssessment.scope_key == scope_key,
                    UncertaintyAssessment.target_metric == target_metric,
                    UncertaintyAssessment.method == method)
            .order_by(UncertaintyAssessment.version.desc()).first())

    ra = UncertaintyAssessment(
        scope=scope, scope_key=scope_key, target_metric=target_metric,
        method=method, version=(prev.version + 1) if prev else 1,
        supersedes_id=prev.id if prev else None,
        run_version=max((r.version for r in runs), default=1),
        attributable="TRUE" if res["attributable"] else "FALSE",
        base_band=res["base_band"] or {"v": None, "lo": None, "hi": None},
        range_band=res["range_band"] or {"v": None, "lo": None, "hi": None},
        decomposition=res["decomposition"],
        contributions=res["contributions"],
        correlated_groups=res["correlated_groups"],
        unattributable=res["unattributable"],
        candidates=prio,
        budget=budget,
        inputs_snapshot=res["inputs"],
        source_run_ids=res["run_ids"],
        note=note or (f"证书修订后重算" if prev else "首次评估"),
    )
    db.add(ra)
    db.flush()
    return ra


def assess_all_methods(db: Session, scope: str, scope_key: str,
                       target_metric: str,
                       budget: float = UNCERTAINTY_DEFAULT_BUDGET,
                       note: Optional[str] = None) -> list[UncertaintyAssessment]:
    return [assess(db, scope, scope_key, target_metric, m, budget, note)
            for m in ("INTERVAL", "JOINT_SAMPLING")]
