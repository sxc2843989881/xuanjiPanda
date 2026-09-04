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
├── growth_value_rotation_b4v3/     # B4V3 成长价值轮动策略（基线版本，不再修改）
│   ├── params.py                  # 参数配置
│   ├── signal_generator_dct.py     # 信号生成器（6层 · 收盘价撮合+DC容差+DCD）
│   ├── strategy_shell.py          # PandaAI策略壳代码 + 回测函数
│   ├── run_backtest.py            # 一键回测脚本
│   └── results/                   # 回测结果（45.73%/Sharpe1.806/-24.24%）
├── growth_value_rotation_b4v4/     # B4V4 成长价值轮动策略（L5锁满全程版，现行生产版本）
│   ├── params.py                  # 参数配置
│   ├── signal_generator_dct.py     # 信号生成器（B4V3 + L5超级牛市锁满全程）
│   ├── strategy_shell.py          # PandaAI策略壳代码 + 回测函数
│   ├── run_backtest.py            # 一键回测脚本
│   ├── test_l5_*.py               # L5解锁消融研究脚本
│   └── results/                   # 回测结果（46.51%/Sharpe1.844/-24.24%）
├── growth_value_rotation_b5u_932304/  # B5U-932304 杠杆策略
│   ├── params.py                  # 参数配置
│   ├── signal_generator.py        # 信号生成器（B4V2+U层比价动量）
│   ├── strategy_shell.py          # 策略壳代码（含杠杆支持）
│   ├── strategy_shell_leveraged.py # 硬编码2x杠杆策略壳
│   ├── run_backtest.py            # 一键回测脚本
│   ├── run_daily.py               # 每日执行脚本
│   ├── df_factors/                # 生成的df_factor CSV文件
│   └── results/                   # 回测结果（含杠杆分析）
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
