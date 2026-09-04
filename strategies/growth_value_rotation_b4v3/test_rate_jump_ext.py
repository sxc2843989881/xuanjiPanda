"""第9层利率跳变 扩展研究: 未测维度全扫 — PandaAI官方引擎

已测(2026/8/26归档): rel_z{2.5,3,3.5}×w{5,10,15,20}×mode{dir,scale,scale_dir}+爆发抑制(全负)
最优: rel3σ dir w10 = +0.91pct
本轮新维度:
  1. 方向不对称: 仅跳升(→价值) / 仅跳降(→成长)
  2. T门控: 仅|T|<1.3或<0.8(T不确定区)时跳变领先
  3. T同向确认: 仅T已指向预设方向时领先
  4. 变化窗: chg=3 / chg=10
  5. σ回看期: 120 / 500
"""
import os, time, pandas as pd
os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result

g, v = load_index_data()
bond = pd.read_csv(r'c:\XuanJLH\pandaai\signal_page_growth\data\bond_yield.csv',
                   parse_dates=['date']).set_index('date').sort_index()
y10 = bond['y10'].dropna().reindex(g.index).ffill().reindex(g.index).rename('y10')

def R(**kw):
    d = {'y10': y10, 'rate_jump_mode': 'dir', 'rate_jump_rel_z': 3, 'rate_jump_lock': 10}
    d.update(kw)
    return d

cases = [
    (None,                      '基线(无跳变)'),
    (R(),                       'rel3σ w10 复验(参照+0.91)'),
    (R(rate_jump_sides='up'),   '仅跳升→价值'),
    (R(rate_jump_sides='down'), '仅跳降→成长'),
    (R(rate_jump_tgate=1.3),    'T门控<1.3'),
    (R(rate_jump_tgate=0.8),    'T门控<0.8'),
    (R(rate_jump_t_agree=True), 'T同向确认'),
    (R(rate_jump_chg=3),        'chg窗=3日'),
    (R(rate_jump_chg=10),       'chg窗=10日'),
    (R(rate_jump_sigma_lb=120), 'σ回看=120'),
    (R(rate_jump_sigma_lb=500), 'σ回看=500'),
]

results = {}
for params, label in cases:
    print(f"\n{'='*52}\n{label}  {params if params else ''}\n{'='*52}", flush=True)
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
    else:
        print("  无结果", flush=True)

print("\n" + "=" * 72)
print("第9层利率跳变 扩展维度汇总 (PandaAI官方引擎, 20130101-20260824)")
print("=" * 72)
base = results.get('基线(无跳变)')
rows = []
for label, r in results.items():
    da = (r['annual_ret'] - base['annual_ret']) * 100 if base else 0
    ddd = (r['max_dd'] - base['max_dd']) * 100 if base else 0
    dsh = r['sharpe'] - base['sharpe'] if base else 0
    print(f"  {label:<24} 年化{r['annual_ret']*100:>7.2f}%({da:+.2f}pct) "
          f"回撤{r['max_dd']*100:>7.2f}%({ddd:+.2f}pct) Sharpe{r['sharpe']:.3f}({dsh:+.3f})")
    rows.append(dict(case=label, ann=r['annual_ret']*100, dd=r['max_dd']*100,
                     sharpe=r['sharpe'], d_ann=da, d_dd=ddd, d_sharpe=dsh))
out = r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3\results\rate_jump_ext_grid.csv'
pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
print(f"\n已存 {out}")
