"""L3弱信号空仓确认机制消融 — PandaAI官方引擎

逻辑: 空仓也是一次切换(往返12bp+踏空风险), 但当前只需1天信号触发,
与方向切换的DC=5确认哲学不对称。改为连续N天弱才空仓:
  统计依据(2013-2026, 120段): 1天段44个原方向平均+0.28%(踏空噪声),
  2天段22个+0.61%(踏空), >5天段31个-3.92%(真避险主力)。
  短段=噪声, 长段=避险 → 2天确认只砍噪声、保留避险。
非参数拟合: N=2/3为逻辑值(与DC哲学对齐), 非搜索最优。
"""
import os, time, pandas as pd
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result

g, v = load_index_data()

cases = [
    ({},                 '基线(单日触发)'),
    ({'weak_confirm': 2}, '弱信号2天确认'),
    ({'weak_confirm': 3}, '弱信号3天确认'),
]

results = {}
for params, label in cases:
    print(f"\n{'='*52}\n{label}  {params}\n{'='*52}", flush=True)
    df = generate_signals(g, v, params)
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
print("L3弱信号确认机制 消融汇总 (PandaAI官方引擎, 20130101-20260824)")
print("=" * 66)
base = results.get('基线(单日触发)')
rows = []
for label, r in results.items():
    da = (r['annual_ret'] - base['annual_ret']) * 100 if base else 0
    ddd = (r['max_dd'] - base['max_dd']) * 100 if base else 0
    dsh = r['sharpe'] - base['sharpe'] if base else 0
    print(f"  {label:<14} 年化{r['annual_ret']*100:>7.2f}%({da:+.2f}pct) "
          f"回撤{r['max_dd']*100:>7.2f}%({ddd:+.2f}pct) Sharpe{r['sharpe']:.3f}({dsh:+.3f})")
    rows.append(dict(case=label, ann=r['annual_ret']*100, dd=r['max_dd']*100,
                     sharpe=r['sharpe'], d_ann=da, d_dd=ddd, d_sharpe=dsh))
out = r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3\results\weak_confirm_engine.csv'
pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
print(f"\n已存 {out}")
