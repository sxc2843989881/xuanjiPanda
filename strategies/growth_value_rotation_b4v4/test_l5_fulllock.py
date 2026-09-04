"""L5超级牛市锁满全程(移除0.782减半解锁) 官方引擎确认性回测
预期 = l5_halve_engine.csv中'none'行: 年化46.51%/回撤-24.24%/Sharpe1.844
(信号与该消融行逐条一致, 引擎确定性)
"""
import os, time
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v4')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result

g, v = load_index_data()
df = generate_signals(g, v)  # 默认=锁满全程
bt_id, _ = run_backtest(df)
r = None
for _ in range(70):
    r = read_result(bt_id)
    if r:
        break
    time.sleep(4)
print_result('L5锁满全程(新生产配置)', r)
