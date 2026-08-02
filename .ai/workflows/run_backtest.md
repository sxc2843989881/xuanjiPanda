# 使用 PandaAI 引擎回测策略 — 完整流程

> 本文档记录将任意策略接入 PandaAI 回测引擎的完整步骤，从信号生成到结果读取。
> 以 B1 木星（成长价值轮动）策略为例，但流程适用于任何策略。

## 概览

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ ① 信号生成       │ ──→ │ ② 编写策略代码    │ ──→ │ ③ 启动引擎回测   │
│ 计算 df_factor   │     │ initialize /      │     │ main_workflow    │
│ 保存为 CSV       │     │ before_trading /  │     │ _stock.start()   │
│                  │     │ handle_data       │     │                 │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          ↓
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ ⑥ 分析对比       │ ←── │ ⑤ 导出结果 CSV   │ ←── │ ④ 读取回测结果   │
│ 参数扫描 /       │     │ pandaai_nav.csv  │     │ MongoDB 查询     │
│ 策略对比         │     │ pandaai_trades   │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

---

## 核心原则

**新策略只需要换一份 `df_factor` CSV 文件**，策略代码（`initialize` / `before_trading` / `handle_data`）是通用壳，基本不用改。

```
┌──────────────────────────────────────────────────────┐
│ 策略代码（通用壳，不改）                                │
│  initialize → before_trading → handle_data            │
│  职责：读取 df_factor 信号，执行调仓                     │
├──────────────────────────────────────────────────────┤
│ df_factor（唯一变量）                                   │
│  date / symbol / signal / weight                      │
│  职责：承载策略逻辑，决定买什么、买多少                     │
└──────────────────────────────────────────────────────┘
```

**你的工作流：新策略逻辑 → 生成新的 df_factor.csv → 喂给引擎 → 读 MongoDB 结果。**

---

## 步骤 ①：信号生成（df_factor）

### 1.1 信号格式

PandaAI 引擎通过 `df_factor` DataFrame 接收外部信号，**必须**包含以下 4 列：

| 列名 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `date` | str | 信号日期（YYYYMMDD） | `"20130104"` |
| `symbol` | str | 标的代码 | `"480080.SZ"` |
| `signal` | str | 买卖信号 | `"buy"` 或 `"sell"` |
| `weight` | float | 仓位权重（0.0~1.0） | `1.0` |

**关键规则：**
- 每个交易日每个标的各占一行
- `signal="buy"` 且 `weight>0` 表示该标的为目标持仓方向
- 信号日期必须覆盖回测区间（含预热期）
- 双标的轮动策略每天有 2 行（一个 buy、一个 sell）

### 1.2 信号生成示例（B1 木星策略）

```python
# 在外部计算信号（可用 baostock/akshare 拉数据）
import pandas as pd
import numpy as np

# 获取成长和价值指数收盘价
g_close = fetch_data("480080.SZ")  # 成长100
v_close = fetch_data("480081.SZ")  # 价值100

# 计算比价 T 值
ratio = g_close / v_close
ma20 = ratio.rolling(20).mean()
t = (ratio / ma20 - 1) / (ratio / ma20 - 1).rolling(20).std()

# 生成信号：T>0 做多成长，T<0 做多价值
records = []
for date in t.index:
    if pd.notna(t[date]):
        if t[date] > 0:
            records.append({"date": date.strftime("%Y%m%d"), "symbol": "480080.SZ", "signal": "buy", "weight": 1.0})
            records.append({"date": date.strftime("%Y%m%d"), "symbol": "480081.SZ", "signal": "sell", "weight": 0.0})
        else:
            records.append({"date": date.strftime("%Y%m%d"), "symbol": "480080.SZ", "signal": "sell", "weight": 0.0})
            records.append({"date": date.strftime("%Y%m%d"), "symbol": "480081.SZ", "signal": "buy", "weight": 1.0})

df_factor = pd.DataFrame(records)
df_factor.to_csv("b1_jupiter_factor.csv", index=False)
```

### 1.3 信号保存

保存为 CSV 后，在回测脚本中加载：

```python
factor_path = Path("b1_jupiter_factor.csv")
df_factor = pd.read_csv(str(factor_path))
df_factor['date'] = df_factor['date'].astype(str)  # 确保日期为字符串
```

---

## 步骤 ②：编写策略代码

策略代码以**字符串**形式传入引擎，需实现 3 个核心函数。

### 2.1 策略模板（双标的轮动策略）

```python
strategy_code = '''
from panda_backtest.api.api import *
from panda_backtest.api.stock_api import *
import numpy as np
import sys as _sys
import bisect as _bisect

GROWTH_SYMBOL = "480080.SZ"   # 标的A
VALUE_SYMBOL = "480081.SZ"    # 标的B
ACCOUNT_ID = "8888"

# ===== 绕过引擎 print 拦截，直接输出到控制台 =====
def _stdout(msg):
    _sys.__stdout__.write(msg + "\\n")
    _sys.__stdout__.flush()

# ===== 1. 初始化 =====
def initialize(context):
    context.account = ACCOUNT_ID
    context._day_count = 0
    context._factor_index = None    # 延迟构建因子索引
    context._sorted_dates = None
    _stdout("[INIT] 策略初始化完成")

# ===== 2. 构建因子索引（避免每天全表扫描） =====
def _get_factor_index(context):
    if context._factor_index is None:
        idx = {}
        dates_set = set()
        for _, row in context.df_factor.iterrows():
            d = str(row["date"])
            if d not in idx:
                idx[d] = {}
            idx[d][row["symbol"]] = (row["signal"], float(row["weight"]))
            dates_set.add(d)
        context._factor_index = idx
        context._sorted_dates = sorted(dates_set)
    return context._factor_index

# ===== 3. 获取前一交易日（实现 T+1 延迟执行） =====
def _get_prev_date(context, today):
    if context._sorted_dates is None:
        _get_factor_index(context)
    pos = _bisect.bisect_left(context._sorted_dates, today)
    if pos > 0:
        return context._sorted_dates[pos - 1]
    return None

# ===== 4. 每日开盘前：读取昨日信号 =====
def before_trading(context):
    today = str(context.now)
    fi = _get_factor_index(context)
    prev_date = _get_prev_date(context, today)
    prev_signals = fi.get(prev_date, {}) if prev_date else {}

    context.target_symbol = None
    context.target_weight = 0.0
    for sym, (sig, wt) in prev_signals.items():
        if sig == "buy" and wt > 0:
            context.target_symbol = sym
            context.target_weight = wt
            break

    # 订阅目标标的行情（必须）
    if context.target_symbol:
        context.sub_stock_symbol([context.target_symbol], sub_type=1)

# ===== 5. 每日交易：执行调仓 =====
def handle_data(context, bar_dict):
    stock_account = context.run_info.stock_account

    # 空仓信号 → 清空所有持仓
    if context.target_symbol is None and context.target_weight == 0.0:
        positions = context.stock_account_dict[context.account].positions.to_dataframe()
        if not positions.empty and positions["positions"].sum() != 0:
            for _, row in positions.iterrows():
                if row["positions"] != 0:
                    order_shares(stock_account, row["symbol"], -int(row["positions"]))
        return

    if context.target_symbol is None:
        return

    bar = bar_dict[context.target_symbol]
    if bar is None or bar.last is None or bar.last <= 0:
        return

    total_value = context.stock_account_dict[context.account].total_value
    target_amount = total_value * context.target_weight
    positions = context.stock_account_dict[context.account].positions.to_dataframe()

    # 卖出不在目标中的持仓
    for _, row in positions.iterrows():
        sym = row["symbol"]
        pos = int(row["positions"])
        if sym != context.target_symbol and pos != 0:
            order_shares(stock_account, sym, -pos)

    # 计算买卖股数差
    target_shares = int(target_amount / bar.last)
    current_pos = 0
    if not positions.empty:
        cur = positions[positions["symbol"] == context.target_symbol]
        if not cur.empty:
            current_pos = int(cur.iloc[0]["positions"])

    delta = target_shares - current_pos
    # 对 delta 取整（100 股为单位），避免对 target_shares 取整导致误差累积
    if abs(delta) >= 100:
        delta = int(delta / 100) * 100
        if abs(delta) >= 100:
            order_shares(stock_account, context.target_symbol, delta)

# ===== 6. 每日收盘后：进度日志 =====
def after_trading(context):
    context._day_count += 1
    if context._day_count % 200 == 0:
        tv = context.stock_account_dict[context.account].total_value
        _stdout(f"[PROGRESS] day={context._day_count}, date={context.now}, total_value={tv:.2f}")
'''
```

### 2.2 关键设计要点

| 要点 | 说明 |
|------|------|
| **T+1 延迟执行** | `before_trading` 读取 `T-1` 日信号，`handle_data` 在 T 日执行 |
| **delta 取整** | 对交易股数差（delta）取整而非对目标股数取整，避免小仓位时误差累积 |
| **行情订阅** | `before_trading` 中必须 `sub_stock_symbol`，否则 `bar_dict` 中无该标的行情 |
| **print 重定向** | 引擎拦截 `print()` 输出到 SRLogger，用 `sys.__stdout__.write()` 绕过 |
| **因子索引** | 在 `initialize` 中预建 `{date: {symbol: (signal, weight)}}` 索引，避免 daily 全表扫描 |

---

## 步骤 ③：启动引擎回测

### 3.1 完整回测脚本

```python
import os, sys
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# ===== 环境准备 =====
os.chdir(r'c:\XuanJLH\pandaai\panda_quantflow')
sys.path.insert(0, r'c:\XuanJLH\pandaai\panda_quantflow\src')
load_dotenv(r'c:\XuanJLH\pandaai\panda_quantflow\.env')

from panda_backtest.main_workflow_stock import start, get_backtest_id

# ===== 加载信号 =====
df_factor = pd.read_csv("b1_jupiter_factor.csv")
df_factor['date'] = df_factor['date'].astype(str)

# ===== 策略代码（见步骤②） =====
strategy_code = '''...'''  # 上面的完整策略代码

# ===== 运行回测 =====
back_test_id = start(
    back_test_id=get_backtest_id(),
    code=strategy_code,
    start_date="20130101",          # 回测开始日期
    end_date="20260730",            # 回测结束日期
    start_capital=1000000,          # 初始资金 100 万
    standard_symbol="480080.SZ",    # 基准指数
    commission_rate=1,              # 费率档位 1（万一免五）
    account_id="8888",
    df_factor=df_factor,
    frequency="1d",
    # 注意: matching_type 和 slippage 不是 start() 的有效参数
    # 引擎会报 TypeError，请勿传入
)

print(f"回测完成! back_test_id: {back_test_id}")
```

### 3.2 关键参数速查

| 参数 | 类型 | 必填 | 说明 | 建议值 |
|------|------|------|------|--------|
| `start_date` | str | 是 | 开始日期（YYYYMMDD） | `"20130101"` |
| `end_date` | str | 是 | 结束日期 | `"20260730"` |
| `start_capital` | int | 是 | 初始资金 | `1000000` |
| `standard_symbol` | str | 是 | 基准指数代码 | `"480080.SZ"` |
| `commission_rate` | int | 是 | 费率档位 | `1`（ETF 万一免五） |
| `frequency` | str | 是 | K线频率 | `"1d"` |
| `df_factor` | DataFrame | 否 | 外部信号 | 见步骤① |

---

## 步骤 ④：读取回测结果

回测结果存储在 MongoDB 中，通过 `back_test_id` 查询：

```python
import pymongo
from bson import ObjectId

client = pymongo.MongoClient("mongodb://panda:panda@127.0.0.1:27018/admin")
db = client['panda']

# 查询回测汇总
result = db['panda_back_test'].find_one({'_id': ObjectId(back_test_id)})

cagr = result['back_profit_year'] * 100      # 年化收益率 %
max_dd = result['max_drawdown'] * 100         # 最大回撤 %
sharpe = result['sharpe']                     # 夏普比率
sortino = result['sortino']                   # 索提诺比率
volatility = result['volatility'] * 100       # 年化波动率 %
alpha = result['alpha'] * 100                 # Alpha %
beta = result['beta']                         # Beta
total_return = result['back_profit'] * 100    # 总收益率 %

print(f"CAGR: {cagr:.2f}%  MaxDD: {max_dd:.2f}%  Sharpe: {sharpe:.3f}")
```

### MongoDB 主要集合

| 集合 | 内容 | 用途 |
|------|------|------|
| `panda_back_test` | 回测汇总（CAGR/MaxDD/Sharpe 等） | 核心指标 |
| `panda_backtest_profit` | 每日收益曲线 | 净值分析 |
| `panda_backtest_trade` | 逐笔交易记录 | 交易分析 |
| `panda_backtest_account` | 账户明细 | 资金曲线 |
| `panda_backtest_position` | 持仓记录 | 持仓分析 |

---

## 步骤 ⑤：导出结果 CSV

将回测结果导出为 CSV 便于后续分析：

```python
# 导出净值
profits = list(db['panda_backtest_profit'].find(
    {'back_test_id': ObjectId(back_test_id)}
).sort('date', 1))
nav_df = pd.DataFrame(profits)
nav_df.to_csv("pandaai_nav.csv", index=False)

# 导出交易记录
trades = list(db['panda_backtest_trade'].find(
    {'back_test_id': ObjectId(back_test_id)}
).sort('trade_time', 1))
trades_df = pd.DataFrame(trades)
trades_df.to_csv("pandaai_trades.csv", index=False)
```

---

## 步骤 ⑥：分析对比

### 6.1 单策略参数扫描

修改信号生成参数，批量跑回测，保存对比结果：

```python
results = []
for e5_weight in [0.1, 0.2, 0.3, 0.5, 0.7, 1.0]:
    # 用不同参数生成 df_factor
    df_factor = generate_signals(e5_weight=e5_weight)
    bt_id = start(code=strategy_code, df_factor=df_factor, ...)
    result = db['panda_back_test'].find_one({'_id': ObjectId(bt_id)})
    results.append({
        'e5_weight': e5_weight,
        'CAGR': result['back_profit_year'] * 100,
        'MaxDD': result['max_drawdown'] * 100,
        'Sharpe': result['sharpe'],
    })

pd.DataFrame(results).to_csv("pandaai_e5_analysis.csv", index=False)
```

### 6.2 策略对比

比较不同策略的逐笔交易差异：

```python
# 加载两个策略的交易记录
trades_a = pd.read_csv("pandaai_trades_v1.csv")
trades_b = pd.read_csv("pandaai_trades_v2.csv")

# 对比方向不一致的交易
merged = pd.merge(trades_a, trades_b, on='date', suffixes=('_a', '_b'))
diff = merged[merged['symbol_a'] != merged['symbol_b']]
```

---

## 常见问题与解决方案

### Q1: 回测速度慢（15 分钟才跑 300 天）

**原因**：MongoDB 每日多次写入（每笔交易、每次持仓变化都写入）。

**解决**：修改引擎的 `reverse_event_process.py`，改为仅在回测结束时一次性写入结果。优化后 3275 天从数小时降至 15 秒。

### Q2: 订单被拒（status≠2）

**原因**：指数数据缺少 `volume`、`limit_up`、`limit_down` 字段，或 `volume=0`，导致交易所校验失败。

**解决**：在行情数据中补充这些字段，**volume 必须为非零值**：
```python
# 指数数据补充字段
df['volume'] = 999999999  # 指数无成交量，但必须>0，否则引擎拒绝所有订单
df['limit_up'] = 999999   # 指数无涨跌停，设极大值
df['limit_down'] = 0.001  # 指数无涨跌停，设极小值
```

> **警告**: `volume=0` 会导致 PandaAI 引擎认为该标的不可交易，拒绝所有买卖订单。
> 回测前建议运行 `precheck.py` 自动检测并修复 `volume<=0` 的记录。

### Q3: 基准指数数据缺失导致引擎崩溃

**原因**：`standard_symbol` 对应的指数在部分日期无数据。

**解决**：选择数据完整的指数作为基准（如 `480080.SZ` 成长100，覆盖 2012-2026 年）。

### Q4: 回测结果与原始引擎差异大

**常见原因及修复**：
1. **执行延迟不一致**：确认 `before_trading` 是否读取了 `T-1` 日信号
2. **费率设置过高**：ETF 无印花税，佣金万一免五，设置 `commission_rate=1`
3. **取整误差累积**：对 delta 取整而非对 target_shares 取整
4. **撮合价格不同**：`matching_type=1`（开盘价）vs `matching_type=0`（收盘价）

### Q5: 策略代码中的 print 看不到输出

**原因**：引擎拦截了 `print()`，重定向到 SRLogger（写入 MongoDB）。

**解决**：用 `sys.__stdout__.write()` 绕过：
```python
import sys as _sys
def _stdout(msg):
    _sys.__stdout__.write(msg + "\n")
    _sys.__stdout__.flush()
```

---

## 完整脚本参考

完整可运行的回测脚本参考：
- `c:\XuanJLH\pandaai\strategies\growth_value_rotation\run_backtest.py` — **策略回测启动脚本（推荐）**
- `c:\XuanJLH\pandaai\strategies\growth_value_rotation\signal_generator.py` — 信号生成器
- `c:\XuanJLH\pandaai\strategies\growth_value_rotation\strategy_shell.py` — 策略壳代码 + 回测函数
- `c:\XuanJLH\pandaai\strategies\growth_value_rotation\params.py` — 参数配置
- `c:\XuanJLH\pandaai\signal_page\generate.py` — 每日信号生成器（GitHub Actions 用）

> **策略开发目录**: 所有策略代码存放在 `pandaai/strategies/` 下，每个策略一个子目录。

## 引擎文档索引

| 文档 | 路径 |
|------|------|
| 策略开发指南 | `panda_quantflow/docs/STRATEGY_GUIDE.md` |
| 回测参数配置 | `panda_quantflow/docs/BACKTEST_CONFIG.md` |
| 引擎架构 | `panda_quantflow/docs/ARCHITECTURE.md` |