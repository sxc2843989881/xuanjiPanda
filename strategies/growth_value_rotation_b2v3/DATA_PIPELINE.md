# B1-木星 数据流水线文档

> **本文档说明所有数据的来源、流转路径、更新方法和调用链。**
> 回到工程总纲: [PROJECT_GUIDE.md](PROJECT_GUIDE.md)

---

## 1. 数据全景

本策略涉及三类数据，分布在三个位置：

```
┌─────────────────────────────────────────────────────────────────┐
│                        数据来源                                  │
│                                                                 │
│  ┌─────────────┐        ┌─────────────┐        ┌─────────────┐ │
│  │  adata 库   │        │ CSV 指数文件 │        │  MongoDB    │ │
│  │  (网络获取)  │        │ (本地存储)   │        │ (引擎读取)  │ │
│  │             │        │             │        │             │ │
│  │ ETF日线数据  │        │ 指数日线数据 │        │ 指数日线行情 │ │
│  │ 159259/263  │        │ 480080/081  │        │ 480080/081  │ │
│  └──────┬──────┘        └──────┬──────┘        └──────┬──────┘ │
│         │                      │                      │        │
│         │    update_data.py    │                      │        │
│         │ ◄──────────────────► │ ◄──────────────────► │        │
│         │     缩放回填+同步     │     行情写入同步       │        │
└─────────┼──────────────────────┼──────────────────────┼────────┘
          │                      │                      │
          ▼                      ▼                      ▼
   ┌──────────────────────────────────────────────────────────┐
   │                    使用方                                 │
   │                                                          │
   │  signal_generator.py  ← 读 CSV 计算信号                  │
   │  PandaAI 引擎         ← 读 MongoDB 撮合订单              │
   │  precheck.py          ← 查 MongoDB 检查完整性             │
   └──────────────────────────────────────────────────────────┘
```

### 1.1 三类数据详解

| 数据类型 | 存储位置 | 用途 | 读取者 |
|----------|----------|------|--------|
| ETF日线 | adata库(网络) | 缩放回填指数的最新数据 | `update_data.py` |
| 指数CSV | `c:\temp_v72_data\index_480080.csv` / `index_480081.csv` | 信号计算的数据源 | `signal_generator.py` |
| MongoDB行情 | `panda.index_daily_price` + `index_daily_quotation` | PandaAI引擎撮合订单的行情源 | PandaAI引擎、`precheck.py` |

### 1.2 为什么有两个数据位置？

| 位置 | 原因 |
|------|------|
| CSV文件 | 信号生成器(`signal_generator.py`)直接读CSV，计算速度快，不依赖MongoDB |
| MongoDB | PandaAI引擎从MongoDB读取行情数据撮合订单，引擎设计如此 |

**核心约束: CSV 和 MongoDB 必须保持同步，否则信号和回测会不一致。**

---

## 2. 目录与文件位置

### 2.1 数据文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| 成长100指数CSV | `c:\temp_v72_data\index_480080.csv` | 价格已÷1000缩放 |
| 价值100指数CSV | `c:\temp_v72_data\index_480081.csv` | 价格已÷1000缩放 |
| CSV缩放前备份 | `c:\temp_v72_data\index_480080.csv.bak_*` | 自动备份 |
| MongoDB集合1 | `panda.index_daily_price` | 引擎主要行情源 |
| MongoDB集合2 | `panda.index_daily_quotation` | 引擎辅助行情源 |

### 2.2 代码文件位置

| 文件 | 路径 | 职责 |
|------|------|------|
| 数据更新脚本 | `strategies/growth_value_rotation/update_data.py` | ETF缩放回填+CSV/MongoDB同步 |
| 数据预检脚本 | `strategies/growth_value_rotation/precheck.py` | 回测前检查MongoDB完整性 |
| 信号生成器 | `strategies/growth_value_rotation/signal_generator.py` | 从CSV读取数据计算信号 |
| 参数配置 | `strategies/growth_value_rotation/params.py` | 数据路径等配置 |

### 2.3 MongoDB连接信息

```
URI:    mongodb://panda:panda@127.0.0.1:27018/admin
数据库:  panda
集合:    index_daily_price, index_daily_quotation
```

---

## 3. 数据更新完整流程

### 3.1 何时需要更新数据？

- 回测结束日期需要延长到最新交易日时
- MongoDB数据与CSV不一致时
- 回测前数据预检发现问题时

### 3.2 一键更新命令

```powershell
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python update_data.py
```

### 3.3 更新调用链（代码级）

```
update_data.py ::main()
│
├── 对每个指数(480080, 480081):
│   │
│   ├── [1] load_index_csv()
│   │       读取 c:\temp_v72_data\index_480080.csv
│   │       返回 DataFrame(date, close)
│   │
│   ├── [2] fetch_etf_data()
│   │       调用 adata.fund.market.get_market_etf()
│   │       获取ETF(159259/159263)最近60天日线
│   │       返回 DataFrame(date, close)
│   │
│   ├── [3] merge_index_etf()
│   │       找最后一个重叠日
│   │       计算缩放比 = 指数收盘 / ETF收盘
│   │       重叠日前: 用指数原值
│   │       重叠日后: 用 ETF收盘 × 缩放比
│   │       返回合并后的完整DataFrame
│   │
│   ├── [4] update_csv()
│   │       找出CSV中没有的新日期
│   │       用前一行填充open/high/low等列
│   │       追加写入CSV文件
│   │
│   └── [5] update_mongodb()
│           查询MongoDB已有最新日期
│           补全之后所有缺失日期
│           每条记录包含:
│             - symbol, date, open/high/low/close
│             - volume=999999999 (占位值)
│             - limit_up=999999, limit_down=0
│           批量插入MongoDB
│
└── 输出: CSV新增行数 + MongoDB新增行数
```

### 3.4 ETF等比例缩放原理

```
指数(480080): 完整历史(2012年~) 但截止较早
ETF(159259):  上市晚 但可通过adata获取到最新

重叠日: 指数和ETF都有数据的日期
缩放比 = 指数收盘 / ETF收盘  (在最后一个重叠日计算)

回填方法:
  重叠日及之前 → 用指数原值（保持历史连续性）
  重叠日之后   → 用 ETF收盘 × 缩放比（等比例延伸）

实际缩放比(2026年7月):
  成长: 480080 / 159259 ≈ 10139.22
  价值: 480081 / 159263 ≈ 7177.84
```

### 3.5 价格缩放说明（÷1000）

```
原始指数价格: ~17000 (成长100在2026年)
÷1000后:      ~17

原因:
  PandaAI引擎以100股为单位撮合
  100万资金 ÷ 17000/股 = 58股 → 取整100股 = 0股 → 无法建仓
  100万资金 ÷ 17/股 = 58823股 → 取整100股 = 58800股 → 正常建仓

影响范围:
  CSV文件: 所有价格列(open/high/low/close)均已÷1000
  MongoDB: 所有价格字段均已÷1000

注意:
  缩放是一次性操作，已在所有历史数据上执行完毕
  update_data.py 回填的新数据自然也是缩放后的值
  （因为缩放比是基于已缩放的指数价格计算的）
```

---

## 4. 数据预检流程

### 4.1 何时运行预检？

- **每次回测前自动运行**（已集成到 `run_backtest.py` 步骤1b）
- 也可手动运行: `python precheck.py`

### 4.2 预检调用链

```
run_backtest.py
│
├── [步骤1b] precheck_mongo_data()
│   │
│   │  对每个集合(index_daily_price, index_daily_quotation):
│   │    对每个标的(480080.SZ, 480081.SZ):
│   │      ✓ 检查数据是否存在
│   │      ✓ 检查日期范围是否覆盖回测区间
│   │      ✓ 检查 volume<=0 的记录（核心检查项）
│   │      ✓ 检查 close<=0 的记录
│   │      ✓ 检查回测区间内数据完整性
│   │      ✓ 检查关键字段(open/high/low/close/volume)是否存在
│   │      ✓ 检查涨跌停价合理性
│   │
│   └── 返回 errors 列表（空=通过）
│
├── 如果有错误:
│   │
│   ├── auto_fix_volume()
│   │     将 volume<=0 的记录更新为 999999999
│   │
│   └── 重新预检
│       ├── 通过 → 继续回测
│       └── 仍失败 → 终止，提示手动检查
│
└── 预检通过 → 进入步骤2(信号生成)
```

### 4.3 volume=0 问题详解

```
问题: 指数没有真实成交量，update_data.py写入volume=999999999
      但早期数据或手动导入的数据可能volume=0

影响: PandaAI引擎校验订单时，如果标的volume=0，
      会认为该标的不可交易，拒绝所有买卖订单

表现: 回测年化极低(<5%)，策略不切换标的，持仓始终不变

解决: precheck.py 自动检测并修复 volume=0 → 999999999
```

---

## 5. 完整数据生命周期

```
┌──────────────────────────────────────────────────────────────┐
│                    数据生命周期                                │
│                                                              │
│  [初始导入]  指数历史数据 → CSV → MongoDB                     │
│      │                                                       │
│      │  (一次性，已完成)                                      │
│      ▼                                                       │
│  [价格缩放]  所有价格 ÷1000 → CSV + MongoDB                   │
│      │                                                       │
│      │  (一次性，已完成)                                      │
│      ▼                                                       │
│  [日常更新]  adata ETF → 缩放回填 → CSV + MongoDB             │
│      │                                                       │
│      │  python update_data.py (按需执行)                      │
│      ▼                                                       │
│  [回测前预检]  检查MongoDB完整性 → 自动修复volume=0           │
│      │                                                       │
│      │  python run_backtest.py (自动执行步骤1b)               │
│      ▼                                                       │
│  [信号生成]  读CSV → 6层过滤 → df_factor                      │
│      │                                                       │
│      ▼                                                       │
│  [引擎回测]  读MongoDB行情 → 撮合订单 → 写结果到MongoDB       │
│      │                                                       │
│      ▼                                                       │
│  [结果读取]  查MongoDB → 年化/回撤/Sharpe → 保存CSV           │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. 常见数据问题与解决

| 问题 | 根因 | 检测方法 | 解决方法 |
|------|------|----------|----------|
| 回测年化极低 | MongoDB volume=0 | `python precheck.py` | 自动修复或手动设为999999999 |
| 策略不切换标的 | 同上，卖单被拒 | 查df_factor确认信号正确 | 修复volume后重跑 |
| 回测区间不够新 | MongoDB数据未更新 | `python precheck.py` 看日期范围 | `python update_data.py` |
| 信号与回测不一致 | CSV与MongoDB不同步 | 对比CSV和MongoDB最新日期 | `python update_data.py` 同步 |
| 买入订单被拒 | 开盘价>收盘价，资金不足 | 查引擎日志 | strategy_shell.py 已加0.5%安全边际 |
| MongoDB数据缺口 | 某些日期数据缺失 | precheck检查区间完整性 | update_data.py 补全 |

---

## 7. 验证数据一致性

手动检查CSV和MongoDB是否同步:

```powershell
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python -c "
import pandas as pd, pymongo
# CSV最新日期
df = pd.read_csv(r'c:\temp_v72_data\index_480080.csv')
print(f'CSV最新: {df.iloc[-1][\"date\"]}')
# MongoDB最新日期
client = pymongo.MongoClient('mongodb://panda:panda@127.0.0.1:27018/admin')
db = client['panda']
r = db['index_daily_price'].find({'symbol':'480080.SZ'}).sort('date',-1).limit(1)
for doc in r: print(f'MongoDB最新: {doc[\"date\"]}')
"
```

如果两个日期不一致，运行 `python update_data.py` 同步。

---

## 8. 新策略数据扩展指南

新增策略如果使用不同的标的，需要:

1. 在 `c:\temp_v72_data\` 放置对应的指数CSV文件
2. 在 `update_data.py` 的 `INDEX_ETF_MAP` 中添加新的指数-ETF映射
3. 在 `precheck.py` 的调用处添加新标的代码
4. 运行 `python update_data.py` 初始化数据
5. 运行 `python precheck.py` 验证数据完整性

详细参考: [PROJECT_GUIDE.md](PROJECT_GUIDE.md) 第2节"新建策略时"
