"""仅跳降(→成长)机制稳健性检验: 不同窗口下是否持续为正
非选参: 机制(跳降领先)已由主网格确立, 此处验证其对窗口参数不敏感
"""
import os, time, pandas as pd
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result

g, v = load_index_data()
bond = pd.read_csv(r'c:\XuanJLH\pandaai\signal_page_growth\data\bond_yield.csv',
                   parse_dates=['date']).set_index('date').sort_index()
y10 = bond['y10'].dropna().reindex(g.index).ffill().reindex(g.index).rename('y10')

cases = [
    (None, '基线'),
    ({'y10': y10, 'rate_jump_mode': 'dir', 'rate_jump_rel_z': 3, 'rate_jump_lock': 10,
      'rate_jump_sides': 'down'}, '仅跳降 w10(主结果)'),
    ({'y10': y10, 'rate_jump_mode': 'dir', 'rate_jump_rel_z': 3, 'rate_jump_lock': 5,
      'rate_jump_sides': 'down'}, '仅跳降 w5'),
    ({'y10': y10, 'rate_jump_mode': 'dir', 'rate_jump_rel_z': 3, 'rate_jump_lock': 15,
      'rate_jump_sides': 'down'}, '仅跳降 w15'),
    ({'y10': y10, 'rate_jump_mode': 'dir', 'rate_jump_rel_z': 3, 'rate_jump_lock': 20,
      'rate_jump_sides': 'down'}, '仅跳降 w20'),
]

results = {}
for params, label in cases:
    print(f"\n{'='*52}\n{label}\n{'='*52}", flush=True)
    df = generate_signals(g, v, params) if params else generate_signals(g, v)
    bt_id, _ = run_backtest(df)
    r = None
    for _ in range(70):
        r = read_result(bt_id)
        if r:
            break
        time.sleep(4)
    if r:
        results[label] = r
        print(f"  年化={r['annual_ret']*100:.2f}% 回撤={r['max_dd']*100:.2f}% Sharpe={r['sharpe']:.3f}", flush=True)

print("\n" + "=" * 66)
print("仅跳降机制 窗口稳健性 (PandaAI官方引擎)")
print("=" * 66)
base = results.get('基线')
for label, r in results.items():
    da = (r['annual_ret'] - base['annual_ret']) * 100 if base else 0
    ddd = (r['max_dd'] - base['max_dd']) * 100 if base else 0
    dsh = r['sharpe'] - base['sharpe'] if base else 0
    print(f"  {label:<16} 年化{r['annual_ret']*100:>7.2f}%({da:+.2f}pct) "
          f"回撤{r['max_dd']*100:>7.2f}%({ddd:+.2f}pct) Sharpe{r['sharpe']:.3f}({dsh:+.3f})")
