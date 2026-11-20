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
