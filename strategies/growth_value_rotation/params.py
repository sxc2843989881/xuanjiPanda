"""B1-木星 成长价值轮动策略 — 参数配置

所有策略参数集中管理，便于版本对比和参数扫描。
参数分为「核心参数」和「E5止损参数」两组，修改时请注释原因。

历史参数变更记录:
  - dcd: 4(QBot引擎) → 6(PandaAI引擎)，因引擎间成本模型差异
  - E5权重: 0.17(原版) → 0.3(PandaAI最优区间0.2~0.3)
  - 数据: 统一使用后复权数据，以510500ETF为验证基准
"""

# ===== 标的配置 =====
GROWTH_SYMBOL = "480080.SZ"   # 成长100指数（成长方向标的）
VALUE_SYMBOL = "480081.SZ"    # 价值100指数（价值方向标的）

# ===== 标的ETF（实盘交易用）=====
GROWTH_ETF = "159259"         # 易方达成长ETF
VALUE_ETF = "159263"          # 易方达价值ETF

# ===== 核心参数（B1-木星 基线）=====
# 这些参数经消融验证，修改需重新回测确认
BASELINE_PARAMS = dict(
    dc=5,                # 方向确认天数：T值方向需连续5天一致才确认
    dcd=6,               # 方向冷却天数：换向后需等6天才允许再次换向（PandaAI引擎）
    rt=1.3,              # T弱信号阈值：|T|<1.3 且斜率弱 → 空仓
    slope_thresh=0.002,  # 弱斜率阈值：比价MA20斜率<0.2%视为无趋势
    bias_high=0.19,      # BIAS超买阈值：偏离MA20>19%清仓
    bias_mode='clear',   # BIAS模式：clear=超买时清仓
    ms=10,               # B2价值动量短周期
    ml=20,               # B2价值动量长周期
)

# ===== E5 止损参数（原版B1机制）=====
# E5: 持仓标的N日跌幅>st%时触发，降仓至sw_deep，冷却cd天
# lookback: 跌幅计算窗口（默认20天）
# st: 止损触发阈值（默认9%）
# cd: 冷却期天数（默认8天）
# sw_deep: 冷却期内仓位比例（默认17%）
# e5_reset: 冷却期内再次触发是否完全重置
E5_PARAMS = dict(
    lookback=20,         # 跌幅计算窗口（N日跌幅）
    st=0.09,             # 止损触发阈值：N日跌幅>9%
    cd=8,                # 冷却期天数
    sw_mid=0.17,         # 中档降仓比例（冷却期内仓位）
    sw_deep=0.17,        # 深跌降仓比例
    e5_reset=True,       # 冷却期内再次触发是否完全重置
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
