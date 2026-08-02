"""B3v1-火星净值止损版 成长价值轮动策略 — 参数配置

B3v1 = B3 + 净值止损机制

净值止损条件：
  - 净值从最高点回撤>9% → 清仓，进入锁定

恢复逻辑：
  - MACD恢复: 跟踪成长100和价值100的MACD柱状态
  - 两者皆为绿柱(负值)时 → 继续锁定，绝不再入场
  - 至少一个转红柱(正值)时 → 止跌企稳，解锁恢复正常信号
"""

# ===== 标的配置 =====
GROWTH_SYMBOL = "480080.SZ"   # 成长100指数（成长方向标的）
VALUE_SYMBOL = "480081.SZ"    # 价值100指数（价值方向标的）

# ===== 标的ETF（实盘交易用）=====
GROWTH_ETF = "159259"         # 易方达成长ETF
VALUE_ETF = "159263"          # 易方达价值ETF

# ===== 核心参数（B3基线）=====
BASELINE_PARAMS = dict(
    dc=5,                # 方向确认天数：T值方向需连续5天一致才确认
    dcd=6,               # 方向冷却天数：换向后需等6天才允许再次换向（PandaAI引擎）
    rt=1.3,              # T弱信号阈值：|T|<1.3 且斜率弱 → 空仓
    slope_thresh=0.002,  # 弱斜率阈值：比价MA20斜率<0.2%视为无趋势
    bias_high=0.19,      # BIAS超买阈值：偏离MA20>19%清仓
    bias_mode='clear',   # BIAS模式：clear=超买时清仓
)

# ===== 净值止损参数 =====
NAV_STOP_PARAMS = dict(
    enabled=True,         # 启用净值止损
    nav_dd=0.09,          # 净值从最高点回撤>9%触发清仓
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
INDEX_DATA_DIR = r"c:\temp_v72_data"
SIGNAL_DELAY = 1  # 天