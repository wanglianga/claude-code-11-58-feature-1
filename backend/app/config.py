import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 能量求值与核对的全局常量(可随配置版本演进,见 ConfigVersion)
MAX_TRACTION_RATE_KWH_PER_MIN = 40.0   # 重启/失连空窗的牵引上限速率,用于不确定区间上界
MAX_AUX_RATE_KWH_PER_MIN = 3.0
LINE_LOSS_RATE = 0.06                  # 线网传输损耗经验比例(相对供电侧受电)
COVERAGE_THRESHOLD = 0.95              # 可闭合核对的最低时间覆盖率
GRID_BUCKET_MINUTES = 15               # 网格对齐路径的固定时间桶
LATE_ARRIVAL_SECONDS = 300             # 采样-到达超过该值视为迟到采样

# 计量不确定度归因(只读主服务能量事实,不改变其能量值与测量边界)
UNCERTAINTY_EXPIRY_INFLATION = 2.0     # 校准过期但仍在使用:证书误差按此倍数保守放大(过期≠归零)
UNCERTAINTY_SAMPLE_COUNT = 400         # 联合抽样情景数(固定随机种子,结果可复现)
UNCERTAINTY_SAMPLE_SEED = 20261120
UNCERTAINTY_SAMPLE_PCT = (5.0, 95.0)   # 抽样范围取 5%~95% 分位
UNCERTAINTY_DEFAULT_BUDGET = 5.0       # 默认校准预算(抽象成本单位)
# 候选行动成本(抽象单位):换表/送检、时钟同步、补测覆盖、取得缺失证书
UNCERTAINTY_COST_CALIBRATE = 3.0
UNCERTAINTY_COST_CLOCK = 1.0
UNCERTAINTY_COST_COVERAGE = 4.0
UNCERTAINTY_COST_OBTAIN_CERT = 2.0
UNCERTAINTY_COST_RESOLUTION = 5.0
UNCERTAINTY_GOOD_ACCURACY = 0.002      # 完成校准后达到的相对扩展不确定度(候选行动目标值)
UNCERTAINTY_UNCALIBRATED_ACCURACY = 0.02  # 取得证书后的先验精度(仅用于候选行动的假定效果)
UNCERTAINTY_RESOLUTION_ACTION_THRESHOLD = 0.01  # 分辨率相对贡献超过该值才建议换表
