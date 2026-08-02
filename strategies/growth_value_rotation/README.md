# B1-木星 成长价值轮动策略

> 成长100(480080.SZ) 与 价值100(480081.SZ) 之间的双标的轮动策略，基于比价T值的标准化偏离度进行方向判断，叠加6层过滤机制控制风险。
>
> **工程入口**: [PROJECT_GUIDE.md](PROJECT_GUIDE.md) | **数据文档**: [DATA_PIPELINE.md](DATA_PIPELINE.md)

## 策略架构

```
                    ┌─────────────────────────────────┐
                    │       信号生成 (signal_generator) │
                    │   输入: 成长100/价值100 指数日线  │
                    │   输出: df_factor CSV            │
                    └──────────────┬──────────────────┘
                                   │ date/symbol/signal/weight
                                   ▼
┌──────────────────────────────────────────────────────────┐
│              6层过滤机制                                   │
│                                                          │
│  第1层 DC   方向确认    T>0→成长, T<0→价值, 连续5天确认    │
│  第2层 DCD  方向冷却    换向后等6天才允许再次换向            │
│  第3层 弱信号 T弱+斜率弱 → 空仓                            │
│  第4层 B2   价值动量过滤  价值动量为负→切回成长             │
│  第5层 BIAS 超买保护    偏离MA20>19%→清仓                 │
│  第6层 E5   止损机制    20日跌幅>9%→降仓17%,冷却8天       │
│                                                          │
└──────────────────────────┬───────────────────────────────┘
                           │ df_factor
                           ▼
┌──────────────────────────────────────────────────────────┐
│              PandaAI 引擎回测 (strategy_shell)            │
│  initialize → before_trading(T+1延迟) → handle_data       │
│  读取昨日信号 → 执行调仓 → MongoDB存储结果                 │
└──────────────────────────────────────────────────────────┘
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `params.py` | 策略参数配置（标的、核心参数、E5参数、回测配置） |
| `signal_generator.py` | 信号生成器（6层过滤 → df_factor） |
| `strategy_shell.py` | PandaAI策略壳代码 + 回测启动/结果读取函数 |
| `precheck.py` | 数据预检模块（回测前检查MongoDB完整性，自动修复volume=0） |
| `update_data.py` | 数据更新模块（ETF缩放回填指数，同步CSV和MongoDB） |
| `run_backtest.py` | 一键回测脚本（集成预检→信号→回测→保存） |

## 快速开始

```powershell
# 进入策略目录
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation

# 1. 运行基线策略回测（自动含数据预检）
python run_backtest.py --variant baseline

# 2. 运行止损变体回测
python run_backtest.py --variant dir_lock_20d

# 3. 更新数据到最新日期
python update_data.py

# 4. 仅数据预检
python precheck.py

# 5. 仅生成信号（不回测）
python signal_generator.py
```

## 核心参数

| 参数 | 值 | 说明 |
|------|------|------|
| `dc` | 5 | 方向确认天数 |
| `dcd` | 6 | 方向冷却天数（PandaAI引擎） |
| `rt` | 1.3 | T弱信号阈值 |
| `slope_thresh` | 0.002 | 弱斜率阈值 |
| `bias_high` | 0.19 | BIAS超买阈值 |
| `st` | 0.09 | E5止损阈值（9%） |
| `cd` | 8 | E5冷却天数 |
| `sw_deep` | 0.17 | E5降仓比例 |

## 止损变体

| 变体 | 机制 | 回测结论 |
|------|------|------|
| `baseline` | E5: 降仓17%+冷却8天 | 基线最优（年化42.31%, Sharpe 1.591） |
| `strict_9pct` | 完全清仓+20日均线恢复 | 不如基线 |
| `dir_lock_20d` | 9%止损+方向锁定20天 | 不如基线 |

## 关键工程决策

| 决策 | 原因 | 影响范围 |
|------|------|----------|
| 指数价格÷1000缩放 | 原始价格17000+，100股取整后100万资金无法建仓 | CSV + MongoDB 所有价格字段 |
| volume=999999999占位 | 引擎要求 volume>0 才能撮合订单，指数无真实成交量 | update_data.py 写入MongoDB时 |
| 0.5%安全边际 | 引擎用开盘价撮合，用收盘价计算满仓会资金不足 | strategy_shell.py target_shares计算 |
| T+1延迟执行 | before_trading(T)读df_factor[T-1]，handle_data(T)执行 | strategy_shell.py 信号读取逻辑 |
| 信号读CSV/引擎读MongoDB | 信号生成用CSV计算，引擎从MongoDB取行情 | update_data.py 必须同步两处 |
| 回测前数据预检 | 防止volume=0等问题导致回测失真 | run_backtest.py 步骤1b自动执行 |

## 回测结果（PandaAI引擎）

基线B1（2013-01-01 ~ 2026-07-30, 3308天）:

| 指标 | 值 |
|------|------|
| 年化收益 | 42.31% |
| 最大回撤 | -22.70% |
| Sharpe | 1.591 |
| Calmar | 1.864 |
| 基准年化 | 1067.18% |
| Alpha | 28.46% |
| 最大回撤区间 | 2015-06-12 ~ 2015-09-02（股灾） |

结果文件: `results/result_baseline.csv`、`results/backtest_summary.csv`

## 注意事项

- 策略执行延迟1天：`before_trading(T)`读取`df_factor[T-1]`，`handle_data(T)`执行
- 使用后复权数据，以510500ETF为验证基准
- ETF交易无印花税，佣金万一免五
- 基准指数使用成长100(480080.SZ)，数据完整覆盖2012-2026年
- 回测前自动运行数据预检，无需手动检查
- 数据更新请运行 `python update_data.py`，详见 [DATA_PIPELINE.md](DATA_PIPELINE.md)
