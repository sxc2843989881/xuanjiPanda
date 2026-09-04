"""用 PandaAI 官方引擎跑 双侧提前确认(both) + 反悔阀 消融

用户思路: 概率检测两个方向都开(成长/价值都提前), 靠排错机制兜底。
"""
import time, os
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3')
import pandas as pd
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result

g_close, v_close = load_index_data()

CASES = [
    ('基线(esc关闭)',          {}),
    ('both day3-T0.6 无反悔阀', {'esc_day': 3, 'esc_t': 0.6, 'esc_side': 'both'}),
    ('both day3-T0.6 反悔阀2天', {'esc_day': 3, 'esc_t': 0.6, 'esc_side': 'both', 'esc_revert_window': 2}),
    ('both day3-T0.6 反悔阀3天', {'esc_day': 3, 'esc_t': 0.6, 'esc_side': 'both', 'esc_revert_window': 3}),
    ('both day3-T0.8 反悔阀2天', {'esc_day': 3, 'esc_t': 0.8, 'esc_side': 'both', 'esc_revert_window': 2}),
]

results = {}
for label, params in CASES:
    print(f"\n{'='*52}\n回测: {label} {params}\n{'='*52}", flush=True)
    df = generate_signals(g_close, v_close, params)
    print(f"  信号行数={len(df)} buy={len(df[df['signal']=='buy'])}", flush=True)
    bt_id, elapsed = run_backtest(df)
    print(f"  回测ID={bt_id} 耗时={elapsed:.0f}s", flush=True)
    r = None
    for _ in range(60):
        r = read_result(bt_id)
        if r is not None:
            break
        time.sleep(5)
    print_result(label, r)
    if r:
        results[label] = {**r, 'bt_id': bt_id}

print("\n" + "=" * 64)
print("双侧提前确认+反悔阀 消融汇总 (PandaAI 官方引擎, 2013-2026)")
print("=" * 64)
if '基线(esc关闭)' in results:
    b = results['基线(esc关闭)']
    rows = []
    for label, r in results.items():
        d_a = (r['annual_ret'] - b['annual_ret']) * 100
        d_d = (r['max_dd'] - b['max_dd']) * 100
        d_s = r['sharpe'] - b['sharpe']
        print(f"  {label:<24} 年化{r['annual_ret']*100:>7.2f}% ({d_a:+.2f}pct)  "
              f"回撤{r['max_dd']*100:>7.2f}% ({d_d:+.2f}pct)  Sharpe{r['sharpe']:.3f} ({d_s:+.3f})")
        rows.append(dict(case=label, ann=r['annual_ret']*100, dd=r['max_dd']*100,
                         sharpe=r['sharpe'], d_ann=d_a, d_dd=d_d, d_sharpe=d_s))
    out = r'c:\XuanJLH\pandaai\signal_page_b4\results\esc_both_revert_engine.csv'
    pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
    print(f"\n已存 {out}")
