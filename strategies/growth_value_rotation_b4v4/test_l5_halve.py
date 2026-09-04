"""L5超级牛市0.782减半解锁 消融 — PandaAI官方引擎

背景: 2026-06-26触发L5(涨幅75.4%, 基准锁64天, 原锁至~9/28),
07-30回撤深度0.872触发减半→8/10提前解锁→8/18买回成长→9月起大跌。
用户观点: 回撤0.872=牛市已死亡, 跌回原点不该恢复入场资格, 减半解锁是错误方向。

l5_halve_mode:
  halve(现行): dd>=0.782 → 锁定减半
  none      : 超级牛市锁满全程(仅新高/时间解锁)
  extend    : dd>=0.782 → 锁定延长至1.5倍

注意: 该路径13年仅2026-06-26这一个事件生效(2015年3次触发未达0.782),
故此消融本质=对单一事件的行为修改, 需警惕过拟合; 但逻辑上
"跌回牛市起点"与"风险释放"的语义之争是原则性设计问题。
"""
import os, time
import pandas as pd

os.chdir(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v4')
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result

g, v = load_index_data()

# ===== 1) 信号层面: 2026-06-26事件的仓位时间线对比 =====
print('=' * 70)
print('2026-06-26 L5事件 三种模式的成长仓位时间线')
print('=' * 70)
sigs = {}
for mode in ['halve', 'none', 'extend']:
    d = generate_signals(g, v, {'l5_halve_mode': mode})
    x = d[d['symbol'] == 'growth'].copy()
    x['dint'] = x['date'].astype(str).astype(int)
    sigs[mode] = x.set_index('dint')

sub = sigs['halve'].loc[(sigs['halve'].index >= 20260805) & (sigs['halve'].index <= 20260910)]
for dt in sub.index:
    row = [f"{sigs[m].loc[dt, 'weight']:>4.1f}" for m in ['halve', 'none', 'extend']]
    mark = ' ←差异' if not (row[0] == row[1] == row[2]) else ''
    print(f'{dt}  halve={row[0]}  none={row[1]}  extend={row[2]}{mark}')

# 成长指数区间跌幅(买回后的伤害)
g_close = g
i_0818 = g.index.get_loc(pd.Timestamp('2026-08-18'))
last_i = len(g) - 1
print(f"\n成长指数 8/18收({g_close.iloc[i_0818]:.0f}) → 最新({g_close.iloc[last_i]:.0f}): "
      f"{(g_close.iloc[last_i]/g_close.iloc[i_0818]-1)*100:+.1f}%")

# ===== 2) 官方引擎回测 =====
print('\n' + '=' * 70)
print('官方引擎全历史回测 (20130101-20260824)')
print('=' * 70)
results = {}
for mode, label in [('halve', '基线:0.782减半(现行)'),
                    ('none', '超级牛市锁满全程'),
                    ('extend', '深度回撤延长1.5倍')]:
    print(f"\n--- {label} ---", flush=True)
    d = generate_signals(g, v, {'l5_halve_mode': mode})
    bt_id, _ = run_backtest(d)
    r = None
    for _ in range(70):
        r = read_result(bt_id)
        if r:
            break
        time.sleep(4)
    if r:
        results[label] = r
        print(f"  年化={r['annual_ret']*100:.2f}% 回撤={r['max_dd']*100:.2f}% Sharpe={r['sharpe']:.3f}", flush=True)

print('\n' + '=' * 70)
print('汇总')
print('=' * 70)
base = results.get('基线:0.782减半(现行)')
rows = []
for label, r in results.items():
    da = (r['annual_ret'] - base['annual_ret']) * 100 if base else 0
    ddd = (r['max_dd'] - base['max_dd']) * 100 if base else 0
    dsh = r['sharpe'] - base['sharpe'] if base else 0
    print(f"  {label:<20} 年化{r['annual_ret']*100:>7.2f}%({da:+.2f}pct) "
          f"回撤{r['max_dd']*100:>7.2f}%({ddd:+.2f}pct) Sharpe{r['sharpe']:.3f}({dsh:+.3f})")
    rows.append(dict(case=label, ann=r['annual_ret']*100, dd=r['max_dd']*100,
                     sharpe=r['sharpe'], d_ann=da, d_dd=ddd, d_sharpe=dsh))
out = r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v4\results\l5_halve_engine.csv'
pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
print(f'\n已存 {out}')
