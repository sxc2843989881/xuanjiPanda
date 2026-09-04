# PandaAI AI 开发文档

> PandaAI 量化回测平台的 AI 协作文档，记录引擎使用方式、策略开发流程和回测工作流。

## 文档导航

| 文档 | 用途 |
|------|------|
| `../strategies/growth_value_rotation/PROJECT_GUIDE.md` | **★ 策略工程总纲** — 任何策略的入口文档，含目录结构、调用链、一键命令 |
| `../strategies/growth_value_rotation/DATA_PIPELINE.md` | **数据流水线** — 数据来源、更新流程、缩放原理、预检机制 |
| `workflows/run_backtest.md` | **引擎回测通用流程** — 从信号生成到结果读取的端到端指南 |
| `../panda_quantflow/docs/STRATEGY_GUIDE.md` | 策略开发指南（引擎内置） |
| `../panda_quantflow/docs/BACKTEST_CONFIG.md` | 回测参数配置（引擎内置） |
| `../panda_quantflow/docs/ARCHITECTURE.md` | 引擎架构文档（引擎内置） |
| `../strategies/` | **策略开发目录** — 所有策略代码存放于此 |
| `../strategies/growth_value_rotation/` | B1-木星 成长价值轮动策略 |
| `../strategies/growth_value_rotation_b4v3/` | **B4V3 成长价值轮动**（基线，45.73%/Sharpe1.806） |
| `../strategies/growth_value_rotation_b4v4/` | **B4V4 成长价值轮动（L5锁满全程版，现行生产）** — 年化46.51%/Sharpe1.844 |
| `../strategies/growth_value_rotation_b5u_932304/` | **B5U-932304 杠杆策略** — 年化96.77%（2x杠杆） |
| `../strategies/growth_value_rotation_b5u_932304_mtv/` | **V113 B5U-932304-MTV** — 动量趋势轮动 + L5保护（年化59.44%） |
| `../research/b1_improvements/` | B1 木星策略改进回测记录 |

## 策略开发目录

**所有策略代码存放在 `../strategies/` 目录下**，每个策略一个子目录：

```
pandaai/strategies/
├── growth_value_rotation/     # B1-木星 成长价值轮动策略
│   ├── PROJECT_GUIDE.md       # ★ 工程总纲（入口文档）
│   ├── DATA_PIPELINE.md       # 数据流水线文档
│   ├── README.md              # 策略说明文档
│   ├── params.py              # 参数配置
│   ├── signal_generator.py    # 信号生成器（6层过滤 → df_factor）
│   ├── strategy_shell.py      # PandaAI策略壳代码 + 回测函数
│   ├── precheck.py            # 数据预检模块
│   ├── update_data.py         # 数据更新模块（ETF缩放回填）
│   ├── run_backtest.py        # ★ 一键回测脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果
├── growth_value_rotation_b5u_932304/  # B5U-932304（年化96.77%）
│   ├── params.py              # 参数配置
│   ├── signal_generator.py    # B4V2+U层比价动量信号生成
│   ├── strategy_shell.py      # 策略壳代码（含杠杆支持）
│   ├── strategy_shell_leveraged.py # 2x杠杆策略壳
│   ├── run_backtest.py        # 一键回测
│   ├── run_daily.py           # 每日执行脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果（含杠杆分析）
├── growth_value_rotation_b5u_932304_mtv/  # V113 B5U-932304-MTV（年化59.44%）
│   ├── params.py              # 参数配置（3资产：932304/480080/480081）
│   ├── signal_generator_v113.py  # V113信号生成器（B5U+L5保护）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   ├── run_daily.py           # 每日执行脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果
```

新建策略时，在 `strategies/` 下创建新的子目录，参考 `growth_value_rotation/` 的文件结构，并编写对应的 `PROJECT_GUIDE.md`。

## 环境

- **Python 环境**: conda 环境 `pandaai`
- **MongoDB**: 默认连接 `127.0.0.1:27018`，认证 `panda/panda`
- **项目根目录**: `c:\XuanJLH\pandaai\panda_quantflow\`
- **指数数据目录**: `c:\temp_v72_data\`（CSV文件，价格已÷1000缩放）

## 启动命令

```powershell
# === 策略回测 ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python run_backtest.py --variant baseline   # 基线B1回测（含数据预检）
python run_backtest.py --variant dir_lock_20d  # 止损变体回测

# === B5U-932304 杠杆策略（年化96.77%） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b5u_932304
python run_backtest.py                      # 一键回测（含杠杆分析）
python run_daily.py                         # 每日信号生成

# === V113 B5U-932304-MTV（年化59.44%+L5保护） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b5u_932304_mtv
python signal_generator_v113.py             # 生成V113信号
python run_backtest.py                      # 一键引擎回测

# === 数据更新 ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python update_data.py                       # 更新ETF数据到最新日期
python precheck.py                          # 仅数据预检

# === 每日信号 ===
cd c:\XuanJLH\pandaai\panda_quantflow
python ..\signal_page\generate.py           # 生成 B1 每日信号
```
