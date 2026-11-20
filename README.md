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

### 自动化测试(已实际执行,18 项全部通过)

```bash
cd backend
.venv/bin/python -m pytest tests/ -q    # 使用独立测试库 ccdev_test
```

覆盖:计数器重启/倒退不相减、倍率切断、未知范围不冒充 0、五类场景的端到端结论、
迟到补传触发新版本且旧指标保留、双路径同范围结果一致、复盘链路完整。

### 五类验收场景(种子数据自带,可在页面直接核对)

| 运行 | 场景 | 页面验证点 |
| --- | --- | --- |
| RUN-1001 | 跨区段折返 | 运行详情:折返/停车切断、方向 UP→DOWN 翻转;核对 FULL,差异=线损+不可知 |
| RUN-1002 | 设备重启+换编组+倍率切换 | 片段切断原因含 DEVICE_RESET/CONSIST_CHANGE/MULTIPLIER_CHANGE;重启段质量 ESTIMATED、上界>下界 |
| RUN-1003 | 回馈受限 | 核对归因"回馈未上网">0,假设中 regen_accepted=false |
| RUN-1004 | 车载与供电表差异 | 差异 10.4 kWh 分解为线损/回馈未上网/不可知,归因之和=差异 |
| RUN-1005 | 补传读数 | v1 指标为"未知"(覆盖 37.5%),v2 闭合;指标页 diff_note 含"迟到补传";复盘页见历史水位 |
| RUN-1006 | 供电表缺失 | 核对 NONE,供电净受电"未知",不可判定原因 SUPPLY_COVERAGE_INSUFFICIENT |

页面路径:总览 → 运行与能量(点运行进详情,可切换 路径A/路径B)→ 指标服务 → 复盘回查。

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
  models.py              # 领域模型:读数/运行/片段/能量/网格/核对/指标/配置版本
  services/energy.py     # 计数器求差:重启倒退、倍率切断、不确定区间
  services/segmentation.py  # 运行切分(停车/折返/换编组/倍率/重启)
  services/evaluate.py   # 片段求值(路径A) + 网格重建(路径B)
  services/reconcile.py  # 账实核对:可比范围、差异归因、不可判定
  services/metrics.py    # 指标发布与迟到版本化
  api/routes.py          # REST API(登录鉴权)
  seed.py                # 五类验收场景演示数据
backend/tests/           # pytest:单元 + 场景端到端
frontend/src/views/      # 总览/采集/运行/指标/复盘 页面
docker-compose.yml       # 一键启动(db + backend + frontend)
```

## 已执行的验证结论

- `pytest`:18 项全部通过(测试库 ccdev_test,与开发库隔离)。
- 本地开发模式全链路冒烟:后端 uvicorn + 前端 vite dev 代理下,登录、总览、
  运行详情双路径核对、指标、复盘接口均返回预期数据。
- 本交付容器内无 docker 命令,Docker 镜像未实际构建;Compose 配置按标准
  多阶段构建编写,本地开发验证均使用原生运行时完成。
