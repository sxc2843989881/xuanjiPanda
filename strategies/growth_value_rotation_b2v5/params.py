"""B2v5 纯T值三者轮动策略 — 参数配置

核心逻辑:
  保留原版B2v3的成长/价值T值方向判断（growth/value比价z-score），
  增加国债作为第三选项：当成长和价值相对国债的T值均低于阈值时，
  认为国债相对最强 → 切换到国债。

  无需止损：当市场暴跌时，成长和价值相对国债的比价自然下降，
  T_gb和T_vb自动跌破阈值，自然切换到国债。

三档风险体系:
  成长100(480080.SZ) — 超高风险，年化~20%，波动率~31%
  价值100(480081.SZ) — 中低风险，年化~17%，波动率~22%
  国债ETF(511010.SH) — 极低风险，年化~3%，波动率~2.5%

策略架构:
  第1层 T值方向 — 基于成长/价值比价z-score判断方向
  第2层 国债过滤 — T_gb和T_vb均低于阈值时切国债
  第3层 DC/DCD — 方向确认+冷却防止频繁切换
  第4层 B2 — 价值动量过滤
  第5层 BIAS — 超买保护
"""

# ===== 标的配置 =====
GROWTH_SYMBOL = "480080.SZ"   # 成长100指数（超高风险）
VALUE_SYMBOL = "480081.SZ"    # 价值100指数（中低风险）
BOND_SYMBOL = "511010.SH"     # 国债ETF（极低风险）

# ===== 标的ETF（实盘交易用）=====
GROWTH_ETF = "159259"         # 易方达成长ETF
VALUE_ETF = "159263"          # 易方达价值ETF
BOND_ETF = "511010"           # 海富通上证5年期国债ETF

# ===== T值轮动核心参数 =====
ROTATION_PARAMS = dict(
    lookback=20,         # T值计算窗口（优化值：20天）
    entry_thresh=0.0,    # 入场阈值：T值>0.0即可入场
    exit_thresh=-0.3,    # 退出阈值：T值<-0.3才退出
    bond_thresh=-0.3,    # 国债阈值（未使用，保留兼容）
    switch_margin=0.0,   # 切换门槛：对方T值高于当前T值即可切换
    dc=2,                # 方向确认天数
    dcd=3,               # 方向冷却天数（优化值：3天冷却减少误切换）
    bias_high=0.25,      # BIAS超买阈值（优化值：0.25比0.30更严格）
    ms=10,               # B2价值动量短周期
    ml=20,               # B2价值动量长周期
)

# ===== 回测参数 =====
BACKTEST_CONFIG = dict(
    start_date="20130325",   # 国债ETF上市日，确保三标的数据齐全
    end_date="20260730",
    start_capital=1000000,
    standard_symbol="480080.SZ",  # 基准指数
    commission_rate=1,         # ETF万一免五
    account_id="8888",
    frequency="1d",
    matching_type=1,           # 1=开盘价撮合
)

# ===== 数据路径 =====
INDEX_DATA_DIR = r"c:\temp_v72_data"
# 成长100指数: index_480080.csv
# 价值100指数: index_480081.csv
# 国债ETF:     index_511010.csv

# ===== 信号延迟 =====
SIGNAL_DELAY = 1  # 天