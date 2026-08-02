"""B1-木星 改进计划回测引擎
支持6个独立改进模块 + 组合模式
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import baostock as bs
import time, json, warnings
warnings.filterwarnings('ignore')

# ====================================================================
# 1. 数据层
# ====================================================================
CODE_G = '159259'; CODE_V = '159263'

def load_data():
    bs.login()
    def fetch(code):
        rs = bs.query_history_k_data_plus(f'sz.{code}', 'date,close',
            start_date='2025-01-01', end_date=datetime.now().strftime('%Y-%m-%d'),
            frequency='d', adjustflag='2')
        rows = []
        while rs.next(): rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=['date','close'])
        df = df[df['close']!='']; df['close']=df['close'].astype(float)
        df['date']=pd.to_datetime(df['date']); df=df.set_index('date').sort_index()
        return df['close']
    g_etf = fetch(CODE_G); v_etf = fetch(CODE_V)
    bs.logout()

    g_idx = pd.read_csv(r'c:\XuanJLH\Qbot\custom\marketdata\cn\index\480080.csv',
                        parse_dates=['date'], index_col='date')['close'].sort_index()
    v_idx = pd.read_csv(r'c:\XuanJLH\Qbot\custom\strategies\etf_rotation\data\cache\idx_local_480081.csv',
                        parse_dates=['date'], index_col='date')['close'].sort_index()

    def merge(a, b):
        a.index=pd.to_datetime(a.index); b.index=pd.to_datetime(b.index)
        s=float(a.loc[b.index[0]])/float(b.loc[b.index[0]])
        return pd.concat([a[a.index<b.index[0]], b*s])[~pd.concat([a[a.index<b.index[0]], b*s]).index.duplicated(keep='last')].sort_index()

    g=merge(g_idx, g_etf); v=merge(v_idx, v_etf)
    all_d=g.index.union(v.index).sort_values()
    g=g.reindex(all_d).ffill(); v=v.reindex(all_d).ffill()
    return g, v

# ====================================================================
# 2. 基础回测引擎
# ====================================================================
BASE_PARAMS = dict(dc=5, dcd=4, rt=1.3, slope_thresh=0.002, st=0.09, cd=8,
                   ms=10, ml=20, bias_high=0.19, bias_mode='clear',
                   sw_mid=0.17, sw_deep=0.17, e5_reset=True)

def run_backtest(g, v, params, improvements=None):
    """通用回测引擎
    improvements: dict 格式, 如 {'cal_tilt':True, 't_adaptive':True}
    """
    if improvements is None: improvements = {}
    p = {**BASE_PARAMS, **params}

    ratio = g / v
    ma20 = ratio.rolling(20).mean()
    dev = ratio / ma20 - 1
    t_raw = dev / dev.rolling(20).std()
    slope = ma20.diff(5) / ma20.shift(5)
    g_bias = g / g.rolling(20).mean() - 1
    v_bias = v / v.rolling(20).mean() - 1
    g_dd20 = g.pct_change(20)
    v_dd20 = v.pct_change(20)
    v_mom_s = v.pct_change(p['ms'])
    v_mom_l = v.pct_change(p['ml'])

    n = len(t_raw)
    month = t_raw.index.month

    # ---- 环境感知器 ----
    env = {}

    # 改进1: 季节性偏置
    if improvements.get('cal_tilt'):
        # H1偏成长: T+0.3; H2偏价值: T-0.3
        season_bias = pd.Series(0.3, index=t_raw.index)
        season_bias[month > 6] = -0.3
        t = t_raw + season_bias
        env['season_bias'] = season_bias
    else:
        t = t_raw

    # 改进4: 波动率环境 (影响dc/dcd)
    dc = p['dc']; dcd = p['dcd']
    if improvements.get('vol_regime'):
        vol_20 = t_raw.rolling(20).std()
        vol_ma = vol_20.rolling(60).mean()
        high_vol = vol_20 > vol_ma * 1.5
        low_vol = vol_20 < vol_ma * 0.7
        env['high_vol'] = high_vol
        env['low_vol'] = low_vol

    # 改进3: 加速度提前 (影响dc)
    if improvements.get('accel'):
        dt_5 = t_raw.diff(5)
        dt_std = dt_5.rolling(60).std()
        accel = dt_5.abs() > dt_std * 2  # 加速状态
        decel = (t_raw.abs() < 1.0) & (dt_5.abs() < 0.2)  # 减速状态
        env['accel'] = accel
        env['decel'] = decel

    # ---- 方向确认 (DC) ----
    raw_dir = (t > 0).map({True: 'growth', False: 'value'})
    dir_dc = raw_dir.copy()

    for i in range(n):
        # 动态dc
        dc_eff = dc
        if improvements.get('vol_regime'):
            if i > 60 and high_vol.iloc[i]: dc_eff = dc + 2
            if i > 60 and low_vol.iloc[i]: dc_eff = max(3, dc - 1)
        if improvements.get('accel'):
            if i > 60 and accel.iloc[i]: dc_eff = max(3, dc_eff - 2)
            if i > 60 and decel.iloc[i]: dc_eff = min(10, dc_eff + 1)

        if i < dc_eff:
            dir_dc.iloc[i] = raw_dir.iloc[0] if i == 0 else dir_dc.iloc[i-1]
            continue
        window = raw_dir.iloc[i-dc_eff+1:i+1]
        if (window == 'growth').all() or (window == 'value').all():
            dir_dc.iloc[i] = window.iloc[-1]
        else:
            dir_dc.iloc[i] = dir_dc.iloc[i-1] if i > 0 else raw_dir.iloc[0]

    # ---- 方向冷却 (DCD) ----
    dir_dcd = dir_dc.copy()
    last_switch = -dcd - 1
    for i in range(n):
        # 动态dcd
        dcd_eff = dcd
        if improvements.get('vol_regime') and i > 60:
            if high_vol.iloc[i]: dcd_eff = dcd + 2
            if low_vol.iloc[i]: dcd_eff = max(2, dcd - 1)

        cur = dir_dc.iloc[i]
        prev = dir_dcd.iloc[i-1] if i > 0 else cur
        if cur != prev and i - last_switch >= dcd_eff:
            last_switch = i
            dir_dcd.iloc[i] = cur
        else:
            dir_dcd.iloc[i] = prev

    # ---- L3: 弱信号空仓 ----
    is_weak = (t.abs() < p['rt']) & (slope.abs() < p['slope_thresh'])
    wt = pd.Series(1.0, index=t.index)
    wt[is_weak] = 0.0

    # ---- L4: B2价值动量 ----
    # 改进5: 季节校准
    b2_threshold = 0
    if improvements.get('b2_season'):
        # 对每个时间点分别判断
        b2_threshold_series = pd.Series(0, index=t.index)
        b2_threshold_series[month > 6] = -0.02  # H2: 需要价值动量<-2%才反手
    else:
        b2_threshold_series = pd.Series(0, index=t.index)

    wrong_value = (dir_dcd == 'value') & (v_mom_s <= b2_threshold_series) & (v_mom_l <= b2_threshold_series)
    dir_b2 = dir_dcd.copy()
    dir_b2[wrong_value] = 'growth'
    dir_s = dir_b2

    # ---- L5: BIAS超买保护 ----
    if p.get('bias_mode', 'clear') == 'clear':
        eg = (dir_s == 'growth') & (g_bias > p['bias_high'])
        ev = (dir_s == 'value') & (v_bias > p['bias_high'])
        wt[eg | ev] = 0.0

    # ---- L6: E5止损 ----
    st = p['st']
    # 改进6: 年度周期风控
    if improvements.get('cycle_risk'):
        t_250ma = t_raw.rolling(250).mean()
        cycle_dev = t_250ma.abs()
        st_adj = pd.Series(st, index=t.index)
        high_cycle = cycle_dev > 0.5
        st_adj[high_cycle] = st * 0.8
        env['high_cycle'] = high_cycle
    else:
        st_adj = pd.Series(st, index=t.index)

    cd = p['cd']
    e5_g = (dir_s == 'growth') & (g_dd20 < -st_adj)
    e5_v = (dir_s == 'value') & (v_dd20 < -st_adj)
    e5_trig = e5_g | e5_v
    gs_d = (dir_s == 'growth') & (g_dd20 < -0.14)
    vs_d = (dir_s == 'value') & (v_dd20 < -0.14)

    in_cd, cc = False, 0
    for i in range(n):
        if pd.isna(wt.iloc[i]): continue
        if e5_trig.iloc[i] and not in_cd and wt.iloc[i] > 0:
            in_cd = True; cc = 0
            sw = p['sw_deep'] if (gs_d.iloc[i] or vs_d.iloc[i]) else p['sw_mid']
            wt.iloc[i] *= sw
        elif in_cd:
            cc += 1
            if cc >= cd:
                if e5_trig.iloc[i]:
                    cc = cd - 3
                    sw = p['sw_deep'] if (gs_d.iloc[i] or vs_d.iloc[i]) else p['sw_mid']
                    wt.iloc[i] *= sw
                else:
                    in_cd = False
                    wt.iloc[i] = 0.0 if is_weak.iloc[i] else 1.0
            else:
                if wt.iloc[i] > 0: wt.iloc[i] = p['sw_deep']

    # ---- 改进2: T值强度自适应仓位 ----
    if improvements.get('t_adaptive'):
        # 仅对"满仓"信号做自适应: T强时+仓, T边缘时-仓
        # E5冷却/L3空仓/L5超买的低仓位不进一步干扰
        base_full_position = (wt >= 1.0)
        confidence = pd.Series(np.minimum(1.0, t_raw.abs() / 2.0), index=t.index)
        wt[base_full_position] = wt[base_full_position] * (0.5 + 0.5 * confidence[base_full_position])
        env['confidence'] = confidence

    # ---- P&L计算 ----
    da = g.reindex(t.index).ffill()
    db = v.reindex(t.index).ffill()
    dgr, dvr = da.pct_change(), db.pct_change()
    TC, IC = 0.0001, 0.0005; TOTAL = TC + IC; CAP = 10000

    nav = pd.Series(CAP, index=t.index)
    strat_ret = pd.Series(0.0, index=t.index)
    for i in range(1, n):
        ps, cs = wt.iloc[i-1], wt.iloc[i]
        cost = TOTAL * 2 if (dir_s.iloc[i-1] != dir_s.iloc[i] or abs(wt.iloc[i-1] - wt.iloc[i]) > 0.01) else 0.0
        if ps > 0 and dir_s.iloc[i-1] == 'growth':
            strat_ret.iloc[i] = dgr.iloc[i] * ps - cost
        elif ps > 0 and dir_s.iloc[i-1] == 'value':
            strat_ret.iloc[i] = dvr.iloc[i] * ps - cost
        else:
            strat_ret.iloc[i] = -cost
        nav.iloc[i] = nav.iloc[i-1] * (1 + strat_ret.iloc[i])

    ret = nav.iloc[-1] / CAP - 1
    n_days = len(nav)
    ann = (1 + ret) ** (252 / n_days) - 1 if n_days > 0 else 0
    dd = 1 - (nav / nav.cummax()).min()
    daily_r = nav.pct_change().dropna()
    sharpe = daily_r.mean() / daily_r.std() * np.sqrt(252) if daily_r.std() > 0 else 0
    calmar = ann / dd if dd > 0 else 0

    switches = sum(1 for i in range(1, n) if dir_s.iloc[i] != dir_s.iloc[i-1] or abs(wt.iloc[i] - wt.iloc[i-1]) > 0.01)
    cash_days = (wt == 0).sum()

    return dict(
        ret=ann, dd=dd, sharpe=sharpe, calmar=calmar,
        nav=nav.iloc[-1], switches=switches, cash_days=int(cash_days),
        total_days=n, switches_cost=switches * TOTAL * 2 * 10000,
        e5_count=int(e5_trig.sum()),
    )


# ====================================================================
# 3. 运行所有回测
# ====================================================================
print("加载数据...")
g, v = load_data()

TESTS = [
    ('🧪 基线 x14_base (原版副本)', {}),
    ('🔬 改进1: 季节性偏置', {'cal_tilt': True}),
    ('🔬 改进2: T值自适应仓位', {'t_adaptive': True}),
    ('🔬 改进3: 加速度提前', {'accel': True}),
    ('🔬 改进4: 波动率自适应', {'vol_regime': True}),
    ('🔬 改进5: B2季节校准', {'b2_season': True}),
    ('🔬 改进6: 年度周期风控', {'cycle_risk': True}),
    ('🚀 全部改进组合', {'cal_tilt': True, 't_adaptive': True, 'accel': True,
                          'vol_regime': True, 'b2_season': True, 'cycle_risk': True}),
]

results = []
for name, imps in TESTS:
    r = run_backtest(g, v, {}, imps)
    r['name'] = name
    results.append(r)
    print(f"  {name}: 年化={r['ret']*100:.2f}%  DD={r['dd']*100:.2f}%  Sharpe={r['sharpe']:.3f}  Calmar={r['calmar']:.3f}  终值={r['nav']:.0f}  切换={r['switches']}")

# ===== 结果对比 =====
print("\n" + "=" * 110)
print("  B1-木星 6项改进回测对比")
print(f"  数据: {g.index[0].date()} ~ {g.index[-1].date()}  指数回填({(g.index < datetime(2026,1,5)).sum()}天)+ETF实值({(g.index >= datetime(2026,1,5)).sum()}天)")
print("=" * 110)
print(f"{'策略':<30} {'年化':>8} {'最大回撤':>10} {'Sharpe':>8} {'Calmar':>8} {'终值':>10} {'切换':>6} {'空仓':>6} {'E5次':>6}")
print(f"{'─'*30} {'─'*8} {'─'*10} {'─'*8} {'─'*8} {'─'*10} {'─'*6} {'─'*6} {'─'*6}")

for r in results:
    sign = '★' if '基线' in r['name'] else ('✅' if '组合' in r['name'] else ' ')
    dd_str = f"{r['dd']*100:.2f}%"
    print(f"{sign} {r['name']:<28} {r['ret']*100:>7.2f}% {dd_str:>10} {r['sharpe']:>8.3f} {r['calmar']:>8.3f} {r['nav']:>9.0f} {r['switches']:>5} {r['cash_days']:>5} {r['e5_count']:>5}")

# 对比基线
base = results[0]
print(f"\n{'─'*110}")
print(f"{'相对于基线的增量':>30} {'Δ年化':>8} {'ΔDD':>10} {'ΔSharpe':>8} {'ΔCalmar':>8} {'Δ终值':>10}")
print(f"{'─'*30} {'─'*8} {'─'*10} {'─'*8} {'─'*8} {'─'*10}")
for r in results[1:]:
    da_ret = r['ret'] - base['ret']
    da_dd = r['dd'] - base['dd']
    da_sharpe = r['sharpe'] - base['sharpe']
    da_calmar = r['calmar'] - base['calmar']
    da_nav = (r['nav'] / base['nav'] - 1) * 100
    print(f"{r['name']:<30} {da_ret*100:>+7.2f}% {da_dd*100:>+9.2f}% {da_sharpe:>+8.3f} {da_calmar:>+8.3f} {da_nav:>+9.2f}%")

# 导出
out = {'tests': TESTS, 'results': results}
with open(r'c:\XuanJLH\PandaAI\research\b1_improvements\backtest_results.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2, default=str)
print(f"\n✅ 结果已导出: backtest_results.json")
