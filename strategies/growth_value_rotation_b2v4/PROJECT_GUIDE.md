# B1-木星 成长价值轮动策略 — 工程总纲

> **本文档是任何 AI 或人类开发者进入本项目的唯一入口。**
> 从这里出发，你可以找到所有代码、数据、调用链和运行命令。

---

## 1. 项目定位

成长100(480080.SZ) 与 价值100(480081.SZ) 之间的双标的轮动策略，基于比价 T 值的标准化偏离度进行方向判断，叠加 6 层过滤机制控制风险，使用 PandaAI 回测引擎执行。

---

## 2. 目录结构

```
c:\XuanJLH\pandaai\
├── .ai/                                    # AI 协作文档（引擎级）
│   ├── README.md                           #   引擎使用导航
│   └── workflows/
│       └── run_backtest.md                 #   引擎回测通用流程
│
├── panda_quantflow/                        # PandaAI 回测引擎（勿改）
│   ├── src/panda_backtest/                 #   引擎源码
│   ├── .env                                #   环境变量（MongoDB连接等）
│   └── docs/                               #   引擎文档
│
├── strategies/                             # ★ 策略开发目录（所有策略在此）
│   └── growth_value_rotation/              #   B1-木星 成长价值轮动策略
│       ├── PROJECT_GUIDE.md                #   ★ 本文件 — 工程总纲（入口）
│       ├── DATA_PIPELINE.md                #   数据流水线文档
│       ├── README.md                       #   策略说明文档
│       ├── params.py                       #   参数配置（标的/核心参数/E5/回测配置）
│       ├── signal_generator.py             #   信号生成器（6层过滤 → df_factor）
│       ├── strategy_shell.py               #   PandaAI策略壳代码 + 回测函数
│       ├── precheck.py                     #   数据预检模块（回测前自动检查MongoDB）
│       ├── update_data.py                  #   数据更新模块（ETF缩放回填指数）
│       ├── run_backtest.py                 #   ★ 一键回测启动脚本
│       ├── df_factors/                     #   生成的信号文件
│       │   └── df_factor_baseline.csv
│       └── results/                        #   回测结果
│           ├── result_baseline.csv
│           └── backtest_summary.csv
│
└── mongodb/                                # MongoDB 数据库文件（勿改）

c:\temp_v72_data\                           # ★ 指数原始数据目录（项目外部）
├── index_480080.csv                        #   成长100指数日线（价格已÷1000缩放）
├── index_480081.csv                        #   价值100指数日线（价格已÷1000缩放）
├── index_480080.csv.bak_*                  #   缩放前备份
└── index_480081.csv.bak_*
```

### 新建策略时

在 `strategies/` 下创建新子目录（如 `strategies/dual_momentum/`），复制以下文件结构：
- `params.py` — 参数配置
- `signal_generator.py` — 信号生成
- `strategy_shell.py` — 策略壳代码（可复用通用壳）
- `precheck.py` — 数据预检（可复用）
- `run_backtest.py` — 回测启动脚本
- `PROJECT_GUIDE.md` — 本文件（参考本目录格式编写）

---

## 3. 文件职责与调用链

### 3.1 调用链总览

```
                    ┌─────────────────────────────────────────────┐
                    │              一键启动                         │
                    │  python run_backtest.py --variant baseline   │
                    └────────────────────┬────────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────────┐
                    │  run_backtest.py (编排器)                     │
│  职责：串联以下6个步骤                         │
                    └────────────────────┬────────────────────────┘
                                         │
         ┌───────────────────────────────┼───────────────────────────────┐
         │                               │                               │
         ▼                               ▼                               ▼
┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
│ 步骤1: 加载数据  │            │ 步骤1b: 数据预检  │            │ 步骤2: 生成信号  │
│ load_index_data()│           │ precheck.py      │            │ generate_signals()│
│ 读取CSV指数数据  │            │ 检查MongoDB完整性 │            │ 6层过滤→df_factor │
└────────┬────────┘            └────────┬────────┘            └────────┬────────┘
         │                              │                              │
         │                    ┌─────────▼─────────┐                   │
         │                    │ 发现volume=0?      │                   │
         │                    │ → auto_fix_volume()│                   │
         │                    └─────────┬─────────┘                   │
         │                              │                              │
         │                              │              ┌───────────────▼──────────┐
         │                              │              │ 步骤3: 启动PandaAI回测    │
         │                              │              │ run_backtest()            │
         │                              │              │ → strategy_shell.py       │
         │                              │              │   → PandaAI start()       │
         │                              │              │   → MongoDB写入结果       │
         │                              │              └───────────┬──────────────┘
         │                              │                          │
         │                              │              ┌───────────▼──────────────┐
         │                              │              │ 步骤4: 读取结果           │
         │                              │              │ read_result(bt_id)       │
         │                              │              │ 查询MongoDB获取指标       │
         │                              │              └───────────┬──────────────┘
         │                              │                          │
         │                              │              ┌───────────▼──────────────┐
         │                              │              │ 步骤5: 保存结果           │
         │                              │              │ result_baseline.csv      │
         │                              │              │ backtest_summary.csv     │
         │                              │              └──────────────────────────┘
```

### 3.2 文件职责清单

| 文件 | 职责 | 输入 | 输出 | 被谁调用 |
|------|------|------|------|----------|
| `params.py` | 参数配置中心 | 无 | 配置常量 | 所有模块 import |
| `signal_generator.py` | 信号生成 | CSV指数数据 | df_factor CSV | `run_backtest.py` |
| `strategy_shell.py` | 策略壳代码+引擎接口 | df_factor | backtest_id | `run_backtest.py` |
| `precheck.py` | 数据预检 | 标的列表+日期范围 | 错误列表 | `run_backtest.py` |
| `update_data.py` | 数据更新 | adata ETF数据 | CSV+MongoDB更新 | 手动执行 |
| `run_backtest.py` | 编排器(一键启动) | 命令行参数 | 回测结果CSV | 用户/AI执行 |

### 3.3 核心调用关系（代码级）

```
run_backtest.py
  ├── from params import GROWTH_SYMBOL, VALUE_SYMBOL, BACKTEST_CONFIG
  ├── from signal_generator import load_index_data, generate_signals, generate_signals_with_stoploss_variant
  │     └── load_index_data()           # → 读 c:\temp_v72_data\index_480080.csv
  │     └── generate_signals()          # → 6层过滤 → df_factor
  ├── from precheck import precheck_mongo_data, auto_fix_volume
  │     └── precheck_mongo_data()       # → 查 MongoDB index_daily_price + index_daily_quotation
  │     └── auto_fix_volume()           # → 修复 volume=0
  ├── from strategy_shell import ...
  │     └── run_backtest(df_factor)     # → PandaAI start() → MongoDB写入
  │     └── read_result(bt_id)          # → 查 MongoDB panda_back_test
  │     └── print_result()              # → 打印年化/回撤/Sharpe
  └── 保存 results/result_*.csv
```

---

## 4. 一键命令

### 4.1 回测（最常用）

```powershell
# 进入策略目录
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation

# 运行基线回测（包含：数据预检→信号生成→引擎回测→结果保存）
python run_backtest.py --variant baseline

# 运行止损变体
python run_backtest.py --variant dir_lock_20d
```

### 4.2 数据更新（延长回测到最新日期）

```powershell
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation

# 从adata获取ETF数据，缩放回填指数，更新CSV和MongoDB
python update_data.py
```

详见 [DATA_PIPELINE.md](DATA_PIPELINE.md)。

### 4.3 仅数据预检

```powershell
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation

# 检查MongoDB数据完整性（自动修复volume=0）
python precheck.py
```

### 4.4 仅生成信号（不回测）

```powershell
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation

python signal_generator.py
```

---

## 5. 关键设计决策

| 决策 | 原因 | 影响范围 |
|------|------|----------|
| 指数价格÷1000缩放 | 原始价格17000+，100股取整后100万资金无法建仓 | CSV + MongoDB 所有价格字段 |
| volume=999999999占位 | 引擎要求 volume>0 才能撮合订单，指数无真实成交量 | update_data.py 写入MongoDB时 |
| 0.5%安全边际 | 引擎用开盘价撮合，用收盘价计算满仓会资金不足 | strategy_shell.py target_shares计算 |
| T+1延迟执行 | before_trading(T)读df_factor[T-1]，handle_data(T)执行 | strategy_shell.py 信号读取逻辑 |
| 信号读CSV/引擎读MongoDB | 信号生成用CSV计算，引擎从MongoDB取行情 | update_data.py 必须同步两处 |

---

## 6. 常见问题排查

| 现象 | 根因 | 解决 |
|------|------|------|
| 回测年化极低(<5%) | MongoDB volume=0，订单全被拒 | `python precheck.py` 自动修复 |
| 策略不切换标的 | 同上，卖单也无法成交 | 同上 |
| 买入订单持续被拒 | 开盘价>收盘价，满仓计算资金不足 | strategy_shell.py 已加0.5%安全边际 |
| 信号正确但持仓不变 | MongoDB数据与CSV不一致 | `python update_data.py` 同步数据 |
| 回测区间不够新 | MongoDB数据未更新到最新 | `python update_data.py` 更新数据 |

---

## 7. 最新回测结果

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

---

## 8. 文档索引

| 文档 | 路径 | 用途 |
|------|------|------|
| **本文件** | `PROJECT_GUIDE.md` | 工程总纲，唯一入口 |
| 数据流水线 | `DATA_PIPELINE.md` | 数据更新全流程说明 |
| 策略说明 | `README.md` | 策略架构与参数说明 |
| 引擎回测流程 | `../../.ai/workflows/run_backtest.md` | PandaAI引擎通用使用指南 |
| 引擎文档 | `../../panda_quantflow/docs/` | 引擎架构/配置/策略开发指南 |
