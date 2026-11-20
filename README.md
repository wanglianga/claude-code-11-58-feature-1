# 轨道交通牵引能耗运行事实与对标平台

判断一趟列车的**牵引取电、制动回馈、辅助负荷**与**站场供电表**是否描述了同一段可计量运行,
形成可用于线路对标、节能评估和事故复盘的能耗事实。

技术栈:Vue 3 + Element Plus(前端)/ FastAPI + SQLAlchemy(后端)/ PostgreSQL(存储)。

## 核心规则

| 环节 | 规则 |
| --- | --- |
| 采集接入 | 保存原始累计值、采样时刻、设备序列号、通信状态、计量倍率;迟到采样按到达时间识别 |
| 运行切分 | 按列车、编组、线路、区段、停车与折返记录切成可计量片段;**换编组、进入不同倍率区间、设备重启/读数倒退必须切断** |
| 能量求值 | 累计值重启或倒退**不直接相减**,只累加单调段,空窗按上限速率扩展不确定区间上界;供电表缺失或覆盖不足时输出**未知范围**,不以车载值冒充闭合计量 |
| 账实核对 | 计算列车侧与供电侧可比较范围、差异去向(线损/回馈未上网/计量延迟/不可知)与不可判定原因 |
| 双路径 | 路径A 运行对齐:逐片段对齐两侧时间边界再汇总;路径B 网格对齐:区段×15min 固定网格累计双侧能量、运行映射进网格,跨格按时间比例分摊为区间。两者对同一运行范围独立给出可比较能耗与差异边界 |
| 指标服务 | 按运行/区段/班次/车型发布单位里程、单位载客能耗与回馈比例,带来源覆盖率 |
| 迟到补传 | 只生成新运行版本;**已发布指标保留原水位**,新指标带差异说明,复盘可回查完整链路 |
| 计量不确定度归因 | 主服务能耗事实之上新增归因层:每个计量输入带倍率证书、校准有效期、分辨率、时钟容差、覆盖比例与适用片段;把来源误差映射到运行/班次指标,输出可归因贡献、相关来源共同影响、不可拆分残余、不可归因原因,以及有限预算下最能缩小关键指标范围的校准候选;只读,不改能量值/测量边界 |

## 一键启动(Docker Compose)

```bash
docker compose up --build
```

- 前端:http://localhost:8080
- 后端 API:http://localhost:8000(Swagger 文档 `/docs`,FastAPI 自带)
- 首次启动自动建库并注入五类验收场景演示数据;`SEED_ON_START=0` 可关闭重建

## 本地开发

前置:PostgreSQL 运行于 127.0.0.1:5432(库/用户/密码均为 `ccdev`)。

```bash
# 后端(端口 8000)
cd backend
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv/bin/python -m app.seed          # 建表 + 注入验收场景数据
.venv/bin/uvicorn app.main:app --port 8000

# 前端(端口 5173,代理 /api -> 8000)
cd frontend
npm install
npm run dev                           # 打开 http://localhost:5173
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理(可采集写入) |
| viewer | viewer123 | 只读 |

## 验证方式

### 自动化测试(已实际执行,34 项全部通过)

```bash
cd backend
.venv/bin/python -m pytest tests/ -q    # 使用独立测试库 ccdev_test
```

覆盖:计数器重启/倒退不相减、倍率切断、未知范围不冒充 0、五类场景的端到端结论、
迟到补传触发新版本且旧指标保留、双路径同范围结果一致、复盘链路完整;
以及本轮**不确定度归因**:范围分解恒等式闭合、共享变电所表按相关组传播不重复抵消、
校准过期≠归零、缺证书/能量未知→不可归因、有限预算优先级缩小关键指标、两种方法独立、
历史版本保留且范围收窄、登记新证书只增版本、归因不改写主服务能量。

### 五类验收场景(种子数据自带,可在页面直接核对)

| 运行 | 场景 | 页面验证点 |
| --- | --- | --- |
| RUN-1001 | 跨区段折返 | 运行详情:折返/停车切断、方向 UP→DOWN 翻转;核对 FULL,差异=线损+不可知 |
| RUN-1002 | 设备重启+换编组+倍率切换 | 片段切断原因含 DEVICE_RESET/CONSIST_CHANGE/MULTIPLIER_CHANGE;重启段质量 ESTIMATED、上界>下界 |
| RUN-1003 | 回馈受限 | 核对归因"回馈未上网">0,假设中 regen_accepted=false |
| RUN-1004 | 车载与供电表差异 | 差异 10.4 kWh 分解为线损/回馈未上网/不可知,归因之和=差异 |
| RUN-1005 | 补传读数 | v1 指标为"未知"(覆盖 37.5%),v2 闭合;指标页 diff_note 含"迟到补传";复盘页见历史水位 |
| RUN-1006 | 供电表缺失 | 核对 NONE,供电净受电"未知",不可判定原因 SUPPLY_COVERAGE_INSUFFICIENT |

页面路径:总览 → 运行与能量(点运行进详情,可切换 路径A/路径B)→ 指标服务 →
不确定度归因 → 复盘回查。

## 计量不确定度归因与校准优先级(本轮新增)

在双路径核对与指标之上,新增一个**只读**归因层:不改变主服务的能量值与测量边界,
只回答“这趟运行/这个班次的指标范围由哪些计量事实贡献,应该先校哪块表”。

### 输入(计量证据)

`meter_inputs` 每个 `input_key`(如 `OB-T01:traction`、`SUB-S1:import`)独立版本化:

| 字段 | 含义 |
| --- | --- |
| `multiplier_cert` / `cert_ref` | 倍率证书标称值与证书编号(只作证据,不改写主服务) |
| `cert_accuracy` | 相对扩展不确定度;`None` = 缺证书 → 该指标不可归因 |
| `calib_valid_to` | 校准有效期;**过期不等于读数为零**,证书仍用,误差按倍数放大并标注 |
| `resolution` | 分辨率(kWh/表码) |
| `clock_tolerance_s` | 采样时钟容差(秒) |
| `coverage_ratio` | 输入侧有效覆盖比例 |
| `shared_key` | 共享计量关系分组:同一变电所表支撑多列车/同表多量时同组,传播不重复抵消 |

### 两条传播路径(各自独立,不是互相校验)

- **INTERVAL(区间传播)**:沿能量计算链逐项传播上下界,相关组同向取最坏角点。
  假设少、易审计;共享来源多时偏保守。
- **JOINT_SAMPLING(联合抽样)**:对输入误差与共享校准关系做联合情景抽样(固定随机种子,
  结果可复现),用留一法分解边际贡献,可表达共同漂移与非线性比率。计算更多,结果依赖情景分布。

`GET /api/uncertainty/guidance` 依据误差资料形式、共享设备比例、看板响应时限给出方法建议;
不把抽样只用来验证区间,也不把区间只用来筛选抽样输入。

### 范围分解与优先级

- `decomposition`:`boundary_width`(主服务时间桶边界)+ `attributable`(独立输入)+
  `correlated`(相关来源共同影响)+ `residual`(不可独立拆分残余),恒等式闭合。
- `unattributable`:关键输入缺失(缺证书/无记录/覆盖为 0/能量未知)时返回,不虚构精确贡献。
- `candidates`:有限校准预算下的贪心优先级(`RECALIBRATE`/`CLOCK_SYNC`/`COVERAGE_FILL`/
  `OBTAIN_CERT`/`IMPROVE_RESOLUTION`),给出每项独立收益、收益/成本与贪心边际收益。
- `history`:倍率证书晚到只新增范围版本,旧版本保留;不回写主服务。

### 页面验证点

- 不确定度归因页选 运行/RUN-1004 + 账实差异:可见范围分解、共享组(`SUB-S1:meter`、
  `SUB-S2:meter`)、不可归因原因、历史版本时间线。
- RUN-1006(供电表缺失):显示“不可归因”(`UNKNOWN_ENERGY_VALUE` + 缺证书)。
- RUN-1003(SUB-S3 缺证书):显示“不可归因”(`MISSING_CALIBRATION_CERTIFICATE`)。
- RUN-1004 历史:RUN-1004 差异范围 v1(时钟容差 60s)宽于 v2(时钟同步后 5s),旧版保留;
  RUN-1001 净能耗因倍率证书晚到生成 v2。

### 手工 API 验证示例

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 归因结论(区间 / 抽样两法)
curl -s "http://localhost:8000/api/uncertainty/assessments?scope=RUN&scope_key=RUN-1004&target_metric=RECON_DIFF_KWH&method=INTERVAL" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/uncertainty/assessments?scope=RUN&scope_key=RUN-1004&target_metric=RECON_DIFF_KWH&method=JOINT_SAMPLING" -H "Authorization: Bearer $TOKEN"

# 不落库重算(含校准优先级)
curl -s -X POST "http://localhost:8000/api/uncertainty/recompute?scope=RUN&scope_key=RUN-1004&target_metric=RECON_DIFF_KWH&method=INTERVAL&budget=5&persist=false" -H "Authorization: Bearer $TOKEN"

# 历史版本
curl -s "http://localhost:8000/api/uncertainty/history?scope=RUN&scope_key=RUN-1001&target_metric=NET_KWH&method=INTERVAL" -H "Authorization: Bearer $TOKEN"

# 登记新版证书(自动成为新版本,不覆盖旧证书)
curl -s -X POST http://localhost:8000/api/meter-inputs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"device_serial":"SUB-S2","quantity":"import","cert_accuracy":0.002,"cert_ref":"CERT-NEW","calib_valid_to":"2027-12-31T00:00:00"}'
```


### 手工 API 验证示例

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 双路径核对结论对比(同一运行范围)
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=RUN_ALIGNED"  -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=GRID_ALIGNED" -H "Authorization: Bearer $TOKEN"

# 补传一条迟到读数 -> 自动生成新运行版本并重发指标
curl -s -X POST http://localhost:8000/api/readings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[{"device_serial":"OB-T01","sampled_at":"2026-10-21T14:05:00",
        "received_at":"2026-10-21T18:00:00","seq":99001,
        "traction_cum":0,"regen_cum":0,"aux_cum":0,"multiplier":1.0}]'
```

## 目录结构

```
backend/app/
  models.py              # 领域模型:读数/运行/片段/能量/网格/核对/指标/配置版本/计量输入/不确定度评估
  services/energy.py     # 计数器求差:重启倒退、倍率切断、不确定区间
  services/segmentation.py  # 运行切分(停车/折返/换编组/倍率/重启)
  services/evaluate.py   # 片段求值(路径A) + 网格重建(路径B)
  services/reconcile.py  # 账实核对:可比范围、差异归因、不可判定
  services/metrics.py    # 指标发布与迟到版本化
  services/uncertainty.py   # 计量不确定度归因:区间/联合抽样传播、分解、校准优先级、版本化
  api/routes.py          # REST API(登录鉴权)
  seed.py                # 五类验收场景 + 计量输入与不确定度归因演示数据
backend/tests/           # pytest:单元 + 场景端到端 + 不确定度归因
frontend/src/views/      # 总览/采集/运行/指标/不确定度归因/复盘 页面
docker-compose.yml       # 一键启动(db + backend + frontend)
```

## 已执行的验证结论

- `pytest`:34 项全部通过(测试库 ccdev_test,与开发库隔离)。其中既有 18 项为原主服务回归,
  新增 16 项覆盖不确定度归因与校准优先级。
- 前端 `vite build` 成功,产物含 `Uncertainty` 视图。
- 本地开发模式全链路冒烟:后端 uvicorn 下实测登录、不确定度元数据、RUN-1004 区间归因
  (`SUB-S1:meter`/`SUB-S2:meter` 相关组、时钟同步候选、宽度 1.978→0.838)、RUN-1006
  不可归因(`UNKNOWN_ENERGY_VALUE`+缺证书)、RUN-1001 历史 v1→v2 收窄、计量输入与总览计数均返回预期。
- 本交付容器内无 docker 命令,Docker 镜像未实际构建;Compose 配置按标准
  多阶段构建编写,本地开发验证均使用原生运行时完成。
