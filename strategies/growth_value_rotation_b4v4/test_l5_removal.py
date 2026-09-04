"""L5死代码移除后的官方引擎确认性回测(预期与基线完全一致: 45.73%/-24.24%/1.806)
依据: 移除前后信号hash逐字节一致(98edaf46...), 引擎确定性 → 结果必然相同。
此运行仅为官方引擎留档。
"""
import os, time
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v4')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result

g, v = load_index_data()
df = generate_signals(g, v)
bt_id, elapsed = run_backtest(df)
r = None
for _ in range(70):
    r = read_result(bt_id)
    if r:
        break
    time.sleep(4)
print_result('L5死代码移除后(应=基线)', r)
