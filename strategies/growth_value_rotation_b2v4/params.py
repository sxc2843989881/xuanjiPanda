"""B1-木星 成长价值轮动策略 — 参数配置

所有策略参数集中管理，便于版本对比和参数扫描。
参数分为「核心参数」和「E5止损参数」两组，修改时请注释原因。

历史参数变更记录:
  - dcd: 4(QBot引擎) → 6(PandaAI引擎)，因引擎间成本模型差异
  - E5权重: 0.17(原版) → 0.3(PandaAI最优区间0.2~0.3)
  - 数据: 统一使用后复权数据，以510500ETF为验证基准
  - B2v4: 新增动态止损（DYNAMIC_STOP），比价过热时止损阈值收紧
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

# ===== E5 止损参数（备用基线，DYNAMIC_STOP禁用时使用）=====
# st=0.05, cd=3, sw=0.30
E5_PARAMS = dict(
    st=0.05,
    cd=3,
    sw_mid=0.30,
    sw_deep=0.30,
    e5_reset=True,
)

# ===== 股灾判定参数（B2v4，覆盖系统性暴跌）=====
# 触发条件: 成长20日跌幅>crash_dd AND 价值20日跌幅>crash_dd
#           AND 成长5日动量<0 AND 价值5日动量<0
# → 直接空仓，不依赖比值超涨判断
# 设计依据: 2015熔断期(1月11-29日)成长价值普跌，比值不极端，
#   动态止损无法触发；股灾判定在该期覆盖12天(20.3%)，精准捕捉底部暴跌
CRASH_DETECTION = dict(
    enabled=True,        # 启用股灾判定
    crash_dd=0.08,       # 20日跌幅阈值：成长和价值均>8%
    mom5_period=5,       # 5日动量周期
    cool_days=20,        # 触发后强制空仓天数（模拟扫描最优值）
)

# ===== 动态止损参数（B2v4核心新特性）=====
# 核心思想：比价偏离度（成长/价值 vs 自身MA20）过大时，
# 说明持仓方向严重超涨（成长比值过大=成长过热，价值比值过小=价值过热），
# 此时回调风险大，止损应更快触发。
# 双模式机制（mode='dual'，推荐）:
#   |overheat| <= tight_thresh  → 正常模式：20日跌幅 > st_base 触发（同E5的5%）
#   |overheat| >  tight_thresh  → 超涨模式：改用5日跌幅 > st_tight 触发（快得多）
# 为什么用5日跌幅：20日跌幅在急涨后急跌时严重滞后
#   （6月暴涨+29%后7月初5日跌10%，但20日跌幅仍为正，旧止损完全失效）
# 参数依据（2012-2026数据）:
#   ratio_dev 90分位≈5.4%，故 tight_thresh=0.05 使约11%的天数进入超涨模式
DYNAMIC_STOP = dict(
    enabled=True,        # 启用动态止损（替代固定E5）
    mode='dual',         # 'threshold':仅收紧20日阈值; 'dual':超涨时改用5日跌幅(推荐)
    st_base=0.05,        # 正常模式：20日跌幅阈值（同E5）
    st_tight=0.02,       # 超涨模式：5日跌幅阈值（2%即触发）
    tight_thresh=0.05,   # 超涨模式起点：|比价偏离|>5%
    cd=3,                # 冷却天数（同E5）
    sw_deep=0.30,        # 止损后降仓权重（同E5）
    e5_reset=True,       # 冷却期内再次跌破阈值时重置冷却
)

# ===== 全局净值止损参数（预留，待进一步调优）=====
# 引擎验证：模拟结果优秀但开盘价撮合导致效果打折扣
NAV_STOP = dict(
    enabled=False,       # 当前禁用（引擎结果不如E5）
    stop_pct=0.10,       # 净值从高点下跌>10%时触发
    min_cool=3,          # 最少冷却天数
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
