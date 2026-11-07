"""B4V9 成长价值轮动策略（B4V8基线 + aw_flip_window=35）— 全量回测 + 分年度/近期分析"""
import sys
import os

sys.path.insert(0, r'c:\XuanJLH\pandaai\panda_quantflow\src')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import pymongo

from params import BASELINE_PARAMS, BACKTEST_CONFIG, GROWTH_SYMBOL, VALUE_SYMBOL
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')
os.makedirs(OUT, exist_ok=True)


def log(*a):
    print(*a, flush=True)


log("=" * 64)
log("  B4V9 成长价值轮动策略（B4V8基线 + AW slow_dc aw_flip_window=35）— 全量回测")
log("=" * 64)

g_close, v_close = load_index_data()
df = generate_signals(g_close, v_close, BASELINE_PARAMS)
log(f"信号数: {len(df)}  aw_mode={BASELINE_PARAMS.get('aw_mode')}  "
    f"aw_flip_window={BASELINE_PARAMS.get('aw_flip_window')}  dr_mode={BASELINE_PARAMS.get('dr_mode')}")

bt, elapsed = run_backtest(df)
log(f"回测ID: {bt}  耗时 {elapsed:.1f}s")
r = read_result(bt)
print_result("B4V9", r)

client = pymongo.MongoClient("mongodb://panda:panda@127.0.0.1:27018/admin",
                             serverSelectionTimeoutMS=5000)
db = client['panda']
acc = pd.DataFrame(list(db['panda_backtest_account'].find(
    {'back_id': bt, 'type': 0}, {'_id': 0, 'gmt_create': 1, 'total_profit': 1})))
pos = pd.DataFrame(list(db['panda_backtest_position'].find(
    {'back_id': bt}, {'_id': 0, 'gmt_create': 1, 'contract_code': 1, 'position': 1})))
client.close()
acc['date'] = pd.to_datetime(acc['gmt_create'])
acc = acc.sort_values('date').drop_duplicates('date').reset_index(drop=True)
nav = acc.set_index('date')['total_profit'] / 10000.0
pos['date'] = pd.to_datetime(pos['gmt_create'])
pos = pos[pos['position'] != 0]
held = {d: ','.join(sorted(grp['contract_code'].unique())) for d, grp in pos.groupby('date')}
nav.to_csv(os.path.join(OUT, 'b4v9_nav.csv'), encoding='utf-8-sig')

navv = nav.values
dd = navv / np.maximum.accumulate(navv) - 1
i_mdd = int(np.argmin(dd))
i_peak = int(np.argmax(navv[:i_mdd + 1]))
log(f"\n净值: {nav.index[0].date()} ~ {nav.index[-1].date()}  "
    f"{nav.iloc[0]:.1f} -> {nav.iloc[-1]:.1f}（{nav.iloc[-1]/nav.iloc[0]-1:+.1%}）")
log(f"全期最大回撤: {dd.min()*100:.2f}%  峰 {nav.index[i_peak].date()}({navv[i_peak]:.1f}) "
    f"→ 谷 {nav.index[i_mdd].date()}({navv[i_mdd]:.1f})")

log("\n===== 分年度收益 =====")
for y in sorted(set(nav.index.year)):
    seg = nav[nav.index.year == y]
    if len(seg) < 2:
        continue
    ret = seg.iloc[-1] / seg.iloc[0] - 1
    seg_dd = (seg / seg.cummax() - 1).min()
    log(f"  {y}: {ret*100:+8.2f}%   年内最大回撤 {seg_dd*100:7.2f}%   "
        f"成长{sum('480080' in held.get(d,'') for d in seg.index)}天/"
        f"价值{sum('480081' in held.get(d,'') for d in seg.index)}天")

log("\n===== 近期表现 =====")


def win_stats(label, start):
    seg = nav.loc[start:]
    if len(seg) < 2:
        return
    ret = seg.iloc[-1] / seg.iloc[0] - 1
    seg_dd = (seg / seg.cummax() - 1).min()
    dd_day = (seg / seg.cummax() - 1).idxmin()
    n_days = (seg.index[-1] - seg.index[0]).days
    ann = (1 + ret) ** (365 / n_days) - 1 if n_days > 0 else np.nan
    log(f"  {label:<18} 收益 {ret*100:+8.2f}%   年化 {ann*100:+8.2f}%   "
        f"最大回撤 {seg_dd*100:7.2f}% ({dd_day.date()})   期末持仓 {held.get(seg.index[-1],'空仓')}")


win_stats("近1年", '2025-10-13')
win_stats("近2年", '2024-10-14')
win_stats("近3年", '2023-10-13')
win_stats("2026年内", '2026-01-01')
win_stats("2026-08-03起", '2026-08-03')
win_stats("2026-09-23起", '2026-09-23')
