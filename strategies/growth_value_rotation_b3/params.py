"""B3-火星 成长价值轮动策略 — 参数配置

B3 = B1 去掉第4层(B2价值动量过滤)和第6层(E5止损)。

仅保留4层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓

无止损、无B2反手逻辑，纯粹的趋势+风险控制。
"""

# ===== 标的配置 =====
GROWTH_SYMBOL = "480080.SZ"   # 成长100指数（成长方向标的）
VALUE_SYMBOL = "480081.SZ"    # 价值100指数（价值方向标的）

# ===== 标的ETF（实盘交易用）=====
GROWTH_ETF = "159259"         # 易方达成长ETF
VALUE_ETF = "159263"          # 易方达价值ETF

# ===== 核心参数（B3-火星）=====
# 继承B1的DC/DCD/弱信号/BIAS参数，去掉B2和E5相关参数
BASELINE_PARAMS = dict(
    dc=5,                # 方向确认天数：T值方向需连续5天一致才确认
    dcd=6,               # 方向冷却天数：换向后需等6天才允许再次换向（PandaAI引擎）
    rt=1.3,              # T弱信号阈值：|T|<1.3 且斜率弱 → 空仓
    slope_thresh=0.002,  # 弱斜率阈值：比价MA20斜率<0.2%视为无趋势
    bias_high=0.19,      # BIAS超买阈值：偏离MA20>19%清仓
    bias_mode='clear',   # BIAS模式：clear=超买时清仓
)

# ===== 回测参数 =====
BACKTEST_CONFIG = dict(
    start_date="20130101",
    end_date="20260730",
    start_capital=1000000,     # 初始资金100万
    standard_symbol="480080.SZ",  # 基准指数（成长100，数据完整覆盖2012-2026）
    commission_rate=1,         # 费率档位1（ETF万一免五）
    account_id="8888",
    frequency="1d",
    matching_type=1,           # 1=开盘价撮合, 0=收盘价撮合
)

# ===== 数据路径 =====
INDEX_DATA_DIR = r"c:\temp_v72_data"  # 指数日线数据目录
# 成长100指数文件: index_480080.csv
# 价值100指数文件: index_480081.csv

# ===== 信号延迟 =====
# PandaAI引擎要求T+1执行：before_trading(T)读取df_factor[T-1]，handle_data(T)执行
SIGNAL_DELAY = 1  # 天