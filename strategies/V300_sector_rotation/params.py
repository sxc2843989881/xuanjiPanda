"""V300-极光 行业ETF轮动策略 — 参数配置

基于文档《量化行业ETF轮动策略 - 实.doc》的0-100评分框架，
纯分数驱动：1-10分买入区，85分以上卖出区，无其他条件。

核心设计:
  1. 10只行业ETF组成标的池，覆盖科技/新能源/消费/医药/金融/军工/周期
  2. 0-100分评分系统: 动量(50%)+均值回归(25%)+趋势偏离(25%)
  3. 纯分数驱动：1-10分买入区，85分以上卖出区
  4. Top 3持仓，缓冲机制(掉出Top 3才换)
  5. 月频调仓，滚动Z-score防未来函数
"""

# ===== 行业ETF标的池（10只，覆盖A股主要行业板块） =====
# 格式: {代码: 名称}
INDUSTRY_ETF_POOL = {
    '159995': '芯片ETF/半导体',    # 科技——半导体
    '515030': '新能源车ETF',       # 新能源——新能源车
    '515790': '光伏ETF',           # 新能源——光伏
    '159928': '消费ETF',           # 消费
    '512010': '医药ETF',           # 医药
    '512880': '券商ETF',           # 金融——券商
    '512660': '军工ETF',           # 军工
    '515220': '煤炭ETF',           # 周期——煤炭
    '515880': '通信ETF',           # 科技——通信
    '512690': '酒ETF',             # 消费——白酒
}

# 行业板块分组（用于分散化，避免同一板块选多只）
INDUSTRY_GROUPS = {
    '科技': ['159995', '515880'],
    '新能源': ['515030', '515790'],
    '消费': ['159928', '512690'],
    '医药': ['512010'],
    '金融': ['512880'],
    '军工': ['512660'],
    '周期': ['515220'],
}

# ===== 0-100评分系统权重 =====
# 动量占主导，均值回归和趋势偏离辅助
SCORING_WEIGHTS = dict(
    momentum=0.40,          # 多周期风险调整动量
    mean_reversion=0.30,    # 均值回归因子（BIAS+RSI+布林带）
    trend_deviation=0.30,   # 趋势均线偏离度
)

# 动量子权重（多周期合成）
MOMENTUM_PERIOD_WEIGHTS = {
    21: 0.40,   # 1个月动量(约21个交易日) — 权重40%
    42: 0.30,   # 2个月动量 — 权重30%
    63: 0.20,   # 3个月动量 — 权重20%
    126: 0.10,  # 6个月动量 — 权重10%
}

# 均值回归子权重
MR_SUB_WEIGHTS = dict(
    bias=0.30,     # BIAS乖离率
    rsi=0.40,      # RSI超买超卖
    bollinger=0.30, # 布林带位置
)

# ===== 核心参数 =====
CORE_PARAMS = dict(
    # 持仓参数
    top_n=3,                # 最大持仓ETF数量
    buffer_top_n=3,         # 缓冲机制：掉出Top N才换仓
    min_holding_days=5,     # 最小持有天数（避免频繁切换）
    max_etf_per_group=1,    # 同一板块最多选1只

    # ★ 每日信号参数（10份资金，每份10%）
    total_units=10,                 # 总份数（100万拆成10份）
    buy_zone_upper=30,              # 评分<30：买入区
    sell_full_zone=70,              # 评分>70：全仓卖出
    reduce_zone=50,                 # 评分>50：减仓一半（生命周期仅一次）
    stop_loss_zone=30,              # 评分<30：全仓卖出（趋势转弱）
    stop_loss_days=9999,            # 关闭硬止损
    stop_loss_pct=0.99,             # 关闭硬止损
    divergence_lookback=0,          # 关闭MACD底背离条件

    # 均值回归参数
    bias_window=20,         # BIAS均线周期
    rsi_window=14,          # RSI周期
    rsi_overbought=80,      # RSI超买阈值
    rsi_oversold=20,        # RSI超卖阈值
    bb_window=20,           # 布林带周期
    bb_std=2,               # 布林带标准差倍数

    # 动量参数
    mom_vol_window=21,      # 动量风险调整的波动率窗口

    # ★ 趋势均线偏离度参数（60日季线，短期乖离捕捉）
    trend_ma_window=60,     # 60日季线，比半年线更敏感
    trend_dev_clip=0.15,    # 偏离度截断值（±15%），60日线偏离幅度通常小于半年线

    # ★ 滚动Z-score窗口（防止未来函数）
    zscore_window=504,      # 滚动Z-score窗口（504日≈2年）

    # 调仓频率
    rebalance_freq='ME',    # ME=月末, W=周, Q=季度
)

# ===== 跟踪止损参数 =====
TRAILING_STOP_PARAMS = dict(
    enabled=False,          # 暂关闭，让评分系统处理退出
    stop_pct=0.15,          # 从持仓最高点回撤>15%触发止损
    cool_days=5,            # 冷却期天数
)

# ===== 回测参数 =====
BACKTEST_CONFIG = dict(
    start_date='20200101',
    end_date='20250516',
    start_capital=1000000,
    commission_rate=0.0003,  # 万三佣金
    slippage=0.001,          # 千一滑点
)

# ===== 数据缓存目录 =====
DATA_CACHE_DIR = r'c:\XuanJLH\pandaai\strategies\V300_sector_rotation\data_cache'