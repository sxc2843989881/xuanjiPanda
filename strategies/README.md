# PandaAI 策略开发目录

> 本目录是 PandaAI 平台所有策略代码的统一存放位置。
> 每个策略一个子目录，包含信号生成、策略壳代码、参数配置和回测脚本。

## 目录结构

```
pandaai/strategies/
├── README.md                      # 本文件
├── growth_value_rotation/         # B1-木星 成长价值轮动策略
│   ├── README.md                  # 策略文档
│   ├── params.py                  # 参数配置
│   ├── signal_generator.py        # 信号生成器（6层过滤 → df_factor）
│   ├── strategy_shell.py          # PandaAI策略壳代码 + 回测函数
│   ├── run_backtest.py            # 一键回测脚本
│   ├── df_factors/                # 生成的df_factor CSV文件
│   └── results/                   # 回测结果
```

## 新建策略

在 `strategies/` 下创建新子目录，参考 `growth_value_rotation/` 的文件结构：

1. `params.py` — 参数配置
2. `signal_generator.py` — 信号生成器，输出 df_factor（date/symbol/signal/weight）
3. `strategy_shell.py` — PandaAI 策略壳代码 + `run_backtest()` / `read_result()` 函数
4. `run_backtest.py` — 一键回测脚本
5. `README.md` — 策略文档

## 核心原则

- **新策略只需换一份 df_factor CSV**，策略壳代码是通用的
- 策略执行延迟1天：`before_trading(T)`读取`df_factor[T-1]`，`handle_data(T)`执行
- 使用后复权数据进行计算
- 回测结果保存为CSV，防止最优策略丢失
- 禁止通过调整参数拟合回测结果，策略设计基于逻辑推导
