"""计量不确定度归因与校准优先级:验收测试。

覆盖:范围分解恒等式、相关来源不重复抵消、校准过期≠归零、缺失证书不可归因、
有限预算优先级、两种方法独立、历史版本保留、不改写主服务能量值。
"""


def _latest(client, auth, scope, scope_key, metric, method):
    r = client.get("/api/uncertainty/assessments", headers=auth, params={
        "scope": scope, "scope_key": scope_key,
        "target_metric": metric, "method": method})
    assert r.status_code == 200
    rows = r.json()
    assert rows, f"无评估 {scope}/{scope_key}/{metric}/{method}"
    return rows[0]


def _recompute(client, auth, scope, scope_key, metric, method, budget=5.0,
               persist=False):
    r = client.post("/api/uncertainty/recompute", headers=auth, params={
        "scope": scope, "scope_key": scope_key, "target_metric": metric,
        "method": method, "budget": budget, "persist": str(persist).lower()})
    assert r.status_code == 200, r.text
    return r.json()


def test_meta_and_meter_inputs(client, auth):
    meta = client.get("/api/uncertainty/meta", headers=auth).json()
    keys = {m["key"] for m in meta["target_metrics"]}
    assert {"NET_KWH", "KWH_PER_KM", "RECON_DIFF_KWH"} <= keys
    inputs = client.get("/api/meter-inputs", headers=auth,
                        params={"latest_only": "true"}).json()
    by_key = {m["input_key"]: m for m in inputs}
    # 缺证书的变电所表保留为 None,不是 0
    assert by_key["SUB-S3:import"]["cert_accuracy"] is None
    # 已过期但证书仍可用
    assert by_key["SUB-S2:import"]["expired"] is True
    assert by_key["SUB-S2:import"]["cert_accuracy"] > 0


def test_interval_decomposition_identity(client, auth):
    """范围 = 主服务边界 + 可归因 + 相关来源 + 残余(恒等式闭合)。"""
    a = _latest(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH", "INTERVAL")
    assert a["attributable"] == "TRUE"
    d = a["decomposition"]
    assert a["range_band"]["lo"] is not None
    # 逐来源贡献之和 + 残余 = 计量扩展宽度
    comp_sum = sum(c["width"] for c in d["attributable"]) \
        + sum(c["width"] for c in d["correlated"])
    assert abs(comp_sum + d["residual"] - d["extra_width"]) < 0.01
    # 总宽 = 边界宽 + 扩展宽
    total = a["range_band"]["hi"] - a["range_band"]["lo"]
    assert abs(total - (d["boundary_width"] + d["extra_width"])) < 0.01


def test_shared_substation_not_double_counted(client, auth):
    """同一变电所表支撑多列车/多个量:归为相关来源,按组给宽度而非各自独立。"""
    a = _latest(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH", "INTERVAL")
    groups = {g["group"]: g for g in a["correlated_groups"]}
    assert "SUB-S1:meter" in groups
    # 受电与返送同表:成员数 > 1(共同影响)
    assert len(groups["SUB-S1:meter"]["members"]) >= 2
    # 相关组在逐来源贡献中标记为 shared,且不重复给单成员宽度
    shared_rows = [c for c in a["contributions"] if c["shared"]]
    assert shared_rows
    # 组宽度只计一次:相关组宽度之和应等于 correlated 宽度之和
    assert abs(sum(g["width"] for g in a["correlated_groups"])
               - sum(c["width"] for c in a["decomposition"]["correlated"])) < 0.01


def test_expired_calibration_not_zero(client, auth):
    """校准过期不等于读数为零:过期表仍在传播,且误差被放大,状态标注过期。"""
    row = _latest(client, auth, "RUN", "RUN-1003", "RECON_DIFF_KWH", "INTERVAL")
    # RUN-1003 缺证书不可归因,但输入快照仍应列出 SUB-S3(缺证书,非零)
    inputs = {m["input_key"]: m for m in row["inputs"]}
    assert inputs["SUB-S3:import"]["status"] in ("MISSING_CERT", "MISSING_INPUT")
    # 过期表(SUB-S2)在可归因范围里仍贡献宽度,且状态为过期
    a = _latest(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH", "INTERVAL")
    inputs2 = {m["input_key"]: m for m in a["inputs"]}
    assert inputs2["SUB-S2:import"]["status"] == "EXPIRED"
    subs2 = [c for c in a["contributions"]
             if c["input_key"].startswith("SUB-S2") and c["source_type"] == "cert"]
    assert subs2, "过期表仍应贡献范围(不归零)"
    assert all((c["group_width"] or 0) > 0 for c in subs2)


def test_missing_certificate_unattributable(client, auth):
    """关键输入缺校准证书:返回不可归因,不虚构精确贡献。"""
    a = _latest(client, auth, "RUN", "RUN-1003", "RECON_DIFF_KWH", "INTERVAL")
    assert a["attributable"] == "FALSE"
    assert a["range_band"]["lo"] is None
    codes = {u["code"] for u in a["unattributable"]}
    assert "MISSING_CALIBRATION_CERTIFICATE" in codes


def test_unknown_energy_unattributable(client, auth):
    """供电侧能量未知(表缺失):不可归因,不虚构。"""
    a = _latest(client, auth, "RUN", "RUN-1006", "RECON_DIFF_KWH", "INTERVAL")
    assert a["attributable"] == "FALSE"
    codes = {u["code"] for u in a["unattributable"]}
    assert "UNKNOWN_ENERGY_VALUE" in codes


def test_budget_priority_reduces_key_metric(client, auth):
    """有限预算下优先级候选能缩小关键指标范围,且不超预算。"""
    a = _latest(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH", "INTERVAL")
    prio = a["candidates"]
    assert prio["budget"] == 5.0
    assert prio["selected"], "预算内应给出候选行动"
    assert sum(s["cost"] for s in prio["selected"]) <= prio["budget"] + 1e-9
    assert prio["final_width"] < prio["current_width"]  # 范围确实缩小
    assert prio["total_reduction"] > 0
    # 时钟错位(SUB-S1/SUB-S2 容差)应出现在候选中
    actions = {(s["action"], s["device"]) for s in prio["evaluated"]}
    assert any(a == "CLOCK_SYNC" for a, _ in actions)


def test_budget_zero_no_action(client, auth):
    """预算为 0 时不产生任何候选行动,范围与有预算时一致。"""
    zero = _recompute(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH",
                      "INTERVAL", budget=0.0)
    full = _recompute(client, auth, "RUN", "RUN-1004", "RECON_DIFF_KWH",
                      "INTERVAL", budget=5.0)
    assert zero["candidates"]["selected"] == []
    assert zero["candidates"]["remaining_budget"] == 0.0
    # 预算不改变当前范围本身(只改变可选的优化行动)
    assert abs(zero["range_band"]["hi"] - zero["range_band"]["lo"]
               - (full["range_band"]["hi"] - full["range_band"]["lo"])) < 1e-6
    assert full["candidates"]["selected"]


def test_two_methods_independent(client, auth):
    """区间与联合抽样各自独立给出范围与归因,而不是互相校验。"""
    a = _latest(client, auth, "RUN", "RUN-1001", "NET_KWH", "INTERVAL")
    s = _latest(client, auth, "RUN", "RUN-1001", "NET_KWH", "JOINT_SAMPLING")
    assert a["method"] == "INTERVAL" and s["method"] == "JOINT_SAMPLING"
    assert a["attributable"] == s["attributable"] == "TRUE"
    # 两者都给逐来源贡献和校准候选
    assert a["contributions"] and s["contributions"]
    assert a["candidates"]["evaluated"] and s["candidates"]["evaluated"]
    # 区间法保守:总宽不小于抽样法的(对同向共享来源)
    wa = a["range_band"]["hi"] - a["range_band"]["lo"]
    ws = s["range_band"]["hi"] - s["range_band"]["lo"]
    assert wa >= ws - 1e-6


def test_history_versions_preserved(client, auth):
    """证书晚到:只新增范围版本,旧版保留,新版本范围收窄。"""
    rows = client.get("/api/uncertainty/history", headers=auth, params={
        "scope": "RUN", "scope_key": "RUN-1004",
        "target_metric": "RECON_DIFF_KWH", "method": "INTERVAL"}).json()
    assert len(rows) >= 2
    v1, v2 = rows[0], rows[-1]
    assert v1["version"] == 1
    assert v2["version"] == 2 and v2["supersedes_id"] == v1["id"]
    assert v1["id"] in [r["id"] for r in rows]  # 旧版保留
    w1 = v1["range_band"]["hi"] - v1["range_band"]["lo"]
    w2 = v2["range_band"]["hi"] - v2["range_band"]["lo"]
    assert w2 < w1  # 新输入到达后范围收窄


def test_history_sample_run1001(client, auth):
    """RUN-1001 净能耗:倍率证书晚到只形成 v2 新范围版本。"""
    rows = client.get("/api/uncertainty/history", headers=auth, params={
        "scope": "RUN", "scope_key": "RUN-1001",
        "target_metric": "NET_KWH", "method": "INTERVAL"}).json()
    assert len(rows) == 2
    assert rows[0]["version"] == 1 and rows[1]["version"] == 2
    assert rows[1]["supersedes_id"] == rows[0]["id"]


def test_assessment_does_not_change_energy(client, auth):
    """归因只读:重算前后主服务能量/核对/指标不变。"""
    def snap():
        runs = client.get("/api/runs?all_versions=true", headers=auth).json()
        recs = []
        for r in runs:
            recs.append(client.get(f"/api/runs/{r['id']}/reconciliation",
                                   headers=auth, params={"method": "RUN_ALIGNED"}).json())
        metrics = client.get("/api/metrics", headers=auth).json()
        return ([(r["id"], r["version"], r["status"]) for r in runs],
                [(x["diff"], x["onboard_net"], x["supply_net"]) for x in recs],
                [(m["id"], m["value"]) for m in metrics])
    before = snap()
    _recompute(client, auth, "RUN", "RUN-1001", "NET_KWH", "INTERVAL",
               budget=3.0, persist=True)
    after = snap()
    assert before == after


def test_shift_scope_shared_meter(client, auth):
    """班次范围含多列车,共享变电所表按相关组传播,宽度不重复抵消。"""
    a = _latest(client, auth, "SHIFT", "平峰-1", "RECON_DIFF_KWH", "INTERVAL")
    assert a["attributable"] == "TRUE"
    assert a["correlated_groups"], "跨运行共享表应形成相关来源组"
    # 相关组宽度只计一次:恒等式仍闭合
    d = a["decomposition"]
    comp = sum(c["width"] for c in d["attributable"]) + \
        sum(c["width"] for c in d["correlated"])
    assert abs(comp + d["residual"] - d["extra_width"]) < 0.01


def test_coverage_input_contributes(client, auth):
    """输入覆盖比例不足:形成覆盖来源贡献。"""
    a = _latest(client, auth, "SHIFT", "早高峰-2", "KWH_PER_KM", "INTERVAL")
    covs = [c for c in a["contributions"] if c["source_type"] == "coverage"]
    assert covs, "OB-T03 辅助表覆盖 90% 应产生覆盖贡献"


def test_register_meter_input_new_version(client, auth):
    """登记新一版证书:生成新版本且不回写旧版本。"""
    payload = {
        "device_serial": "SUB-S2", "quantity": "import",
        "cert_accuracy": 0.002, "cert_ref": "CERT-SUB-S2-V3",
        "calib_valid_to": "2027-12-31T00:00:00", "resolution": 0.05,
        "clock_tolerance_s": 60.0, "coverage_ratio": 1.0,
        "note": "测试新证书版本",
    }
    r = client.post("/api/meter-inputs", json=payload, headers=auth)
    assert r.status_code == 200
    rec = r.json()
    assert rec["version"] >= 3 and rec["supersedes_id"]
    allv = client.get("/api/meter-inputs", headers=auth,
                      params={"latest_only": "false"}).json()
    versions = sorted(m["version"] for m in allv if m["input_key"] == "SUB-S2:import")
    assert len(versions) >= 3  # 旧版本仍在


def test_regression_existing_scenarios(client, auth):
    """主服务旧结论不受影响:核对与指标仍按原口径。"""
    runs = client.get("/api/runs", headers=auth).json()
    r1 = next(r for r in runs if r["run_key"] == "RUN-1001")
    rec = client.get(f"/api/runs/{r1['id']}/reconciliation", headers=auth,
                     params={"method": "RUN_ALIGNED"}).json()
    assert rec["comparable"] == "FULL"
    assert rec["diff"]["v"] == 4.0
