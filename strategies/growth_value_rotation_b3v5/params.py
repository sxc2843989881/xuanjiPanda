"""B3v5-动量止损版 成长价值轮动策略 — 参数配置

B3v5 = B3 + 第5层(牛市高点回撤止损·新高可解锁) + 第8层(黑天鹅保护)

共6层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第5层 牛市高点止损 — 成长60日动量>50%+5日动量转负 → 清仓成长，锁定60天
        若成长指数突破历史最高价，可提前解锁
  第8层 黑天鹅保护 — 5日内成长跌>6%且价值跌>3% → 全部空仓
        成长/价值独立锁定，基于60日波动率动态调整动量周期解锁
"""

# ===== 标的配置 =====
GROWTH_SYMBOL = "480080.SZ"   # 成长100指数（成长方向标的）
VALUE_SYMBOL = "480081.SZ"    # 价值100指数（价值方向标的）

# ===== 标的ETF（实盘交易用）=====
GROWTH_ETF = "159259"         # 易方达成长ETF
VALUE_ETF = "159263"          # 易方达价值ETF

# ===== 核心参数（B3-火星 + B3v5保留第5层+第8层）=====
BASELINE_PARAMS = dict(
    dc=5,                # 方向确认天数：T值方向需连续5天一致才确认
    dcd=6,               # 方向冷却天数：换向后需等6天才允许再次换向

    # B3原有参数
    rt=1.3,              # T弱信号阈值：|T|<1.3 且斜率弱 → 空仓
    slope_thresh=0.002,  # 弱斜率阈值：比价MA20斜率<0.2%视为无趋势
    bias_high=0.19,      # BIAS超买阈值：偏离MA20>19%清仓
    bias_mode='clear',   # BIAS模式：clear=超买时清仓

    # B3v4新增 — 第5层：牛市高点止损（B3v5保留）
    mom_60d_period=60,   # 60日动量计算周期
    mom_3m_thresh=0.50,  # 60日动量阈值：>50%视为涨幅过大
    mom_5d_lock=60,      # 成长锁定天数：触发止损后锁定60个交易日

    # 第8层：黑天鹅保护 — 动态动量解锁（基于波动率自适应）
    black_swan_g_thresh=0.06,    # 成长5日跌幅阈值：>6%视为黑天鹅
    black_swan_v_thresh=0.03,    # 价值5日跌幅阈值：>3%视为黑天鹅
    black_swan_lookback=5,       # 黑天鹅回看窗口：5日
    vol_window=60,               # 波动率计算窗口（日）：用60日滚动年化波动率
    vol_mom_base=5,              # 基础动量周期（低波动时用5日）
    vol_mom_min=5,               # 最小动量周期
    vol_mom_max=30,              # 最大动量周期
    vol_step=0.05,               # 波动率每增加5%，周期增加5天
    vol_floor=0.10,              # 波动率基准值（低于此值用基础周期5日）
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