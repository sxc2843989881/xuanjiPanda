"""B1-木星 (X14 v2.0-lite) 每日信号生成器 — Panda AI
本脚本完全自包含，可直接在 GitHub Actions 上运行。
依赖: pip install baostock pandas numpy matplotlib akshare
"""
import os, base64, io, json, warnings, argparse, time
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now():
    return datetime.now(BEIJING_TZ)

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
warnings.filterwarnings('ignore')

# ========== 配置 ==========
CODE_G = '159259'   # 易方达成长ETF
CODE_V = '159263'   # 易方达价值ETF
NAME_G = '易方达成长ETF'
NAME_V = '易方达价值ETF'
FETCH_DAYS = 500    # 拉取天数（需400+天确保有足够数据）
MA_PERIOD = 20      # 均线周期

# X14 v2.0-lite 参数（冻结参数，经消融验证）
PARAMS = dict(
    dc=5,               # 方向确认天数（原版B1）
    dcd=4,              # 方向冷却天数（v2.0: 6→4，-0.1% Calmar）
    rt=1.3,             # T弱信号阈值
    slope_thresh=0.002, # 弱斜率阈值
    st=0.09,            # E5止损阈值（20日跌幅>9%）
    cd=8,               # E5冷却天数
    ms=10, ml=20,       # B2价值动量窗口
    bias_high=0.19,     # BIAS超买阈值
    bias_mode='clear',  # BIAS模式: clear(清仓)
    sw_mid=0.17,        # E5中档降仓比例
    sw_deep=0.17,       # E5深跌降仓比例
    dual_momentum=False,# Dual Momentum（默认关闭）
    e5_reset=True,      # E5冷却期内再次触发完全重置(原版行为)
    rapid_decline=False,# 急跌加速判断（默认关闭）
)


# ========== 数据获取 ==========
def fetch_baostock(code, name, days=500):
    """从 baostock 获取 ETF 日线数据（带重试机制）"""
    import baostock as bs

    MAX_RETRIES = 5
    BASE_DELAY = 3

    for attempt in range(MAX_RETRIES):
        try:
            bs.login()
            end = datetime.now().strftime('%Y-%m-%d')
            start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            rs = bs.query_history_k_data_plus(
                f'sz.{code}', 'date,open,close',
                start_date=start, end_date=end,
                frequency='d', adjustflag='2')
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            bs.logout()

            if rows:
                df = pd.DataFrame(rows, columns=['date', 'open', 'close'])
                df = df[df['close'] != '']
                for c in ['open', 'close']:
                    df[c] = df[c].astype(float)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                print(f'  ✅ {name}({code}): {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)}天)')
                return df['close']

            print(f'  ⚠️ {name}({code}): 第{attempt+1}次尝试返回空数据')

        except Exception as e:
            print(f'  ⚠️ {name}({code}): 第{attempt+1}次尝试失败 - {e}')
            try:
                bs.logout()
            except:
                pass

        if attempt < MAX_RETRIES - 1:
            delay = BASE_DELAY * (2 ** attempt)
            print(f'  🔄 等待 {delay}s 后重试...')
            time.sleep(delay)

    return fetch_akshare(code, name, days)


def fetch_akshare(code, name, days=500):
    """备用：从 akshare 获取 ETF 日线数据"""
    try:
        import akshare as ak

        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                 start_date=start, end_date=end, adjust="qfq")
        if df.empty:
            raise ValueError(f'{name} akshare: 无数据')

        df = df[['日期', '开盘', '收盘']].rename(columns={
            '日期': 'date', '开盘': 'open', '收盘': 'close'
        })
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        print(f'  ✅ {name}({code}) [akshare备用]: {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)}天)')
        return df['close']

    except Exception as e:
        print(f'  ❌ {name}({code}) akshare备用也失败: {e}')
        raise ValueError(f'{name}: baostock和akshare均无数据')


# ========== X14 v2.0-lite 信号计算 ==========
def compute_signals(g_close, v_close, params):
    """X14 v2.0-lite 信号计算（与 backtest_x14_engine.py build_core 一致）

    6层逻辑:
    L1: 方向确认 (DC) — T值连续dc天同号才确认方向
    L2: 方向冷却 (DCD) — 切换后锁定dcd天
    L3: T+斜率双重确认 — |T|<rt 且 |斜率|<slope_thresh 时空仓
    L4: B2价值动量 — 价值方向自身动量下跌则转成长
    L5: BIAS超买保护 — BIAS>19%时空仓(bias_mode='clear')
    L6: E5止损 — 20日跌幅>9%触发止损+冷却
    """
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(MA_PERIOD).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    t = ratio_dev / ratio_dev.rolling(MA_PERIOD).std()
    slope = ratio_ma20.diff(5) / ratio_ma20.shift(5)
    g_bias = g_close / g_close.rolling(MA_PERIOD).mean() - 1
    v_bias = v_close / v_close.rolling(MA_PERIOD).mean() - 1
    g_dd20 = g_close.pct_change(20)
    v_dd20 = v_close.pct_change(20)
    v_mom_short = v_close.pct_change(params['ms'])
    v_mom_long = v_close.pct_change(params['ml'])

    n = len(t)

    # ===== L1: 方向确认 (DC) =====
    raw_dir = (t > 0).map({True: 'growth', False: 'value'})
    dir_dc = raw_dir.copy()
    for i in range(params['dc'], n):
        window = raw_dir.iloc[i - params['dc'] + 1:i + 1]
        if (window == 'growth').all() or (window == 'value').all():
            dir_dc.iloc[i] = window.iloc[-1]
        else:
            dir_dc.iloc[i] = dir_dc.iloc[i - 1] if i > 0 else raw_dir.iloc[0]

    # ===== L2: 方向冷却 (DCD) =====
    dcd = params['dcd']
    dir_dcd = dir_dc.copy()
    last_switch = -dcd - 1
    for i in range(n):
        cur = dir_dc.iloc[i]
        prev = dir_dcd.iloc[i - 1] if i > 0 else cur
        if cur != prev and i - last_switch >= dcd:
            last_switch = i
            dir_dcd.iloc[i] = cur
        else:
            dir_dcd.iloc[i] = prev

    # ===== L3: 弱信号空仓 =====
    is_weak = (t.abs() < params['rt']) & (slope.abs() < params['slope_thresh'])
    wt = pd.Series(1.0, index=t.index)
    wt[is_weak] = 0.0

    # ===== L4: B2价值动量 =====
    wrong_value = (dir_dcd == 'value') & (v_mom_short <= 0) & (v_mom_long <= 0)
    dir_b2 = dir_dcd.copy()
    dir_b2[wrong_value] = 'growth'
    dir_s = dir_b2

    # ===== L5: BIAS超买保护 =====
    if params.get('bias_mode', 'clear') == 'clear':
        extreme_g = (dir_s == 'growth') & (g_bias > params['bias_high'])
        extreme_v = (dir_s == 'value') & (v_bias > params['bias_high'])
        wt[extreme_g | extreme_v] = 0.0

    # ===== L6: E5止损 =====
    st = params['st']
    cd = params['cd']
    sw_mid = params.get('sw_mid', 0.17)
    sw_deep = params.get('sw_deep', 0.17)
    e5_reset = params.get('e5_reset', False)

    e5_g = (dir_s == 'growth') & (g_dd20 < -st)
    e5_v = (dir_s == 'value') & (v_dd20 < -st)
    e5_trig = e5_g | e5_v

    # E5深跌判断
    gs_deep = (dir_s == 'growth') & (g_dd20 < -0.14)
    vs_deep = (dir_s == 'value') & (v_dd20 < -0.14)
    e5_deep = gs_deep | vs_deep

    in_cooldown = False
    cooldown_count = 0
    for i in range(n):
        idx = t.index[i]
        if pd.isna(wt.iloc[i]) or pd.isna(dir_s.iloc[i]):
            continue

        if e5_trig.iloc[i] and not in_cooldown and wt.iloc[i] > 0:
            in_cooldown = True
            cooldown_count = 0
            sw_use = sw_deep if e5_deep.iloc[i] else sw_mid
            wt.iloc[i] *= sw_use

        elif in_cooldown:
            cooldown_count += 1
            if cooldown_count >= cd:
                if e5_trig.iloc[i]:
                    if e5_reset:
                        cooldown_count = 0
                    else:
                        cooldown_count = cd - 3
                    sw_use = sw_deep if e5_deep.iloc[i] else sw_mid
                    wt.iloc[i] *= sw_use
                else:
                    in_cooldown = False
                    if is_weak.iloc[i]:
                        wt.iloc[i] = 0.0
                    else:
                        wt.iloc[i] = 1.0
            else:
                if wt.iloc[i] > 0:
                    wt.iloc[i] = sw_deep

    # 构建明细 DataFrame
    df = pd.DataFrame(index=t.index)
    df['ratio'] = ratio
    df['ma20'] = ratio_ma20
    df['dev'] = ratio_dev
    df['T'] = t
    df['slope'] = slope
    df['dir'] = dir_s
    df['wt'] = wt
    df['g_bias'] = g_bias
    df['v_bias'] = v_bias
    df['g_dd20'] = g_dd20
    df['v_dd20'] = v_dd20
    df['dir_dcd'] = dir_dcd
    df['is_weak'] = is_weak
    return df


# ========== P&L 计算 ==========
def calc_nav(df, g_close, v_close):
    """计算策略净值曲线"""
    da = g_close.reindex(df.index).ffill()
    db = v_close.reindex(df.index).ffill()
    daily_g_ret = da.pct_change()
    daily_v_ret = db.pct_change()
    TRADE_COST = 0.0001
    IMPACT_COST = 0.0005
    TOTAL_COST = TRADE_COST + IMPACT_COST
    INIT_CAP = 10000

    nav = pd.Series(INIT_CAP, index=df.index)
    strat_ret = pd.Series(0.0, index=df.index)
    for i in range(1, len(df)):
        ps = df.iloc[i - 1]
        cs = df.iloc[i]
        cost = 0.0
        if ps['dir'] != cs['dir'] or abs(ps['wt'] - cs['wt']) > 0.01:
            cost = TOTAL_COST * 2
        if ps['wt'] > 0 and ps['dir'] == 'growth':
            strat_ret.iloc[i] = daily_g_ret.iloc[i] * ps['wt'] - cost
        elif ps['wt'] > 0 and ps['dir'] == 'value':
            strat_ret.iloc[i] = daily_v_ret.iloc[i] * ps['wt'] - cost
        else:
            strat_ret.iloc[i] = -cost
        nav.iloc[i] = nav.iloc[i - 1] * (1 + strat_ret.iloc[i])
    return nav, INIT_CAP


# ========== 图表生成 ==========
def make_chart(df, rt=1.3, nav=None):
    """4子图：P&L → 比价+MA20 → T值 → 仓位"""
    n_rows = 4 if nav is not None else 3
    ratios = [1.5, 2, 1, 1] if nav is not None else [2, 1, 1]
    fig, axes = plt.subplots(n_rows, 1, figsize=(12, 8),
                             gridspec_kw={'height_ratios': ratios})

    if nav is not None:
        ax = axes[0]
        nav_show = nav.tail(30)
        init_val = nav.iloc[-len(nav_show):].iloc[0] if len(nav) > 30 else nav.iloc[0]
        ax.plot(nav_show.index, nav_show.values, color='#E74C3C', lw=2)
        ax.fill_between(nav_show.index, init_val, nav_show.values,
                        color='#E74C3C', alpha=0.15)
        ax.axhline(init_val, color='gray', ls=':', lw=0.8)
        ax.annotate(f'{nav_show.iloc[-1]:.0f}',
                    xy=(nav_show.index[-1], nav_show.iloc[-1]),
                    fontsize=11, fontweight='bold', color='#E74C3C',
                    va='bottom', ha='left')
        pnl_pct = (nav_show.iloc[-1] / init_val - 1) * 100
        ax.set_title(f'P&L (10,000 start)  Now {nav_show.iloc[-1]:.0f}  ({pnl_pct:+.2f}%)',
                     fontsize=11, fontweight='bold')
        ax.set_ylabel('P&L (CNY)', fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=6)
        ax.grid(alpha=0.2)

    ax_next = 1 if nav is not None else 0
    dates = df.index[-120:]

    ax = axes[ax_next]
    ax.plot(dates, df.loc[dates, 'ratio'], color='#333333', lw=1.5, label='GV Ratio')
    ax.plot(dates, df.loc[dates, 'ma20'], color='#E74C3C', ls='--', lw=1, label='MA20')
    ax.axhline(1.0, color='gray', ls=':', alpha=0.4)
    ax.set_ylabel('GV Ratio', fontsize=9)
    ax.legend(fontsize=8, loc='upper left')
    ax.grid(alpha=0.2)

    dir_colors = {'growth': '#F1C40F', 'value': '#2ECC71', 'cash': '#95A5A6'}
    last_dir = None
    start = None
    for d in dates:
        nd = df.loc[d, 'dir']
        if nd != last_dir:
            if last_dir is not None and start is not None:
                ax.axvspan(start, d, alpha=0.08, color=dir_colors.get(last_dir, '#CCC'))
            start = d
            last_dir = nd
    if last_dir is not None and start is not None:
        ax.axvspan(start, dates[-1], alpha=0.08, color=dir_colors.get(last_dir, '#CCC'))

    ax = axes[ax_next + 1]
    ax.plot(dates, df.loc[dates, 'T'], color='#3498DB', lw=1.5, label='T(z-score)')
    ax.axhline(0, color='gray', ls='-', lw=0.5)
    ax.axhline(rt, color='#E74C3C', ls='--', lw=0.8, label=f'+rt={rt}')
    ax.axhline(-rt, color='#2ECC71', ls='--', lw=0.8, label=f'-rt={rt}')
    ax.fill_between(dates, rt, -rt, alpha=0.05, color='gray')
    ax.set_ylabel('T (z-score)', fontsize=9)
    ax.legend(fontsize=8, loc='upper left')
    ax.grid(alpha=0.2)

    ax = axes[ax_next + 2]
    ax.fill_between(dates, 0, df.loc[dates, 'wt'], color='#9B59B6', alpha=0.4, step='mid')
    ax.plot(dates, df.loc[dates, 'wt'], color='#9B59B6', lw=1, drawstyle='steps-post')
    ax.set_ylabel('Position', fontsize=9)
    ax.set_ylim(-0.05, 1.05)
    ax.set_xlabel('Date', fontsize=9)
    ax.grid(alpha=0.2)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ========== HTML 生成 ==========
def make_html(signal, df, chart_b64, trades=None):
    """生成手机友好的信号页面 HTML"""
    latest = signal
    pos_cn = {'growth': '📈 成长', 'value': '📉 价值', 'cash': '⏸ 空仓'}
    pos_color = {'growth': '#F1C40F', 'value': '#2ECC71', 'cash': '#95A5A6'}
    pos_bg = {'growth': 'linear-gradient(135deg, #F1C40F, #F39C12)',
              'value': 'linear-gradient(135deg, #2ECC71, #27AE60)',
              'cash': 'linear-gradient(135deg, #95A5A6, #7F8C8D)'}

    n = min(30, len(df))
    recent = df.tail(n)
    g_days = (recent['dir'] == 'growth').sum()
    v_days = (recent['dir'] == 'value').sum()
    c_days = (recent['dir'] == 'cash').sum()

    version_info = f'X14 v2.0-lite · DC={PARAMS["dc"]} DCD={PARAMS["dcd"]}'

    t60_mean = df['T'].tail(60).abs().mean()

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>B1-木星 · 每日信号 — Panda AI</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f6fa; color: #2c3e50; padding: 12px; }}
  .container {{ max-width: 800px; margin: 0 auto; }}

  .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e, #0f3460);
    color: white; padding: 20px; border-radius: 16px; margin-bottom: 12px;
    display: flex; justify-content: space-between; align-items: center; }}
  .header h1 {{ font-size: 18px; font-weight: 700; letter-spacing: 0.5px; }}
  .header .sub {{ font-size: 11px; opacity: 0.7; margin-top: 2px; }}
  .header .date {{ font-size: 12px; opacity: 0.8; text-align: right; }}

  .signal-card {{ background: white; border-radius: 16px; padding: 20px;
    margin-bottom: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  .signal-main {{ display: flex; align-items: center; gap: 20px; }}
  .signal-badge {{ font-size: 28px; font-weight: 700; padding: 14px 20px;
    border-radius: 12px; color: white; min-width: 130px; text-align: center;
    background: {pos_bg[latest['dir']]}; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
  .signal-meta {{ flex: 1; min-width: 0; }}
  .signal-meta .row {{ display: flex; justify-content: space-between;
    padding: 5px 0; font-size: 13px; border-bottom: 1px solid #f0f0f0; }}
  .signal-meta .row:last-child {{ border: none; }}
  .signal-meta .label {{ color: #7f8c8d; }}
  .signal-meta .value {{ font-weight: 600; }}

  .charts {{ margin-bottom: 12px; }}
  .charts img {{ width: 100%; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}

  .grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 12px; }}
  .stat-card {{ background: white; border-radius: 12px; padding: 14px 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }}
  .stat-card .num {{ font-size: 26px; font-weight: 700; color: #2c3e50; }}
  .stat-card .lbl {{ font-size: 11px; color: #95a5a6; margin-top: 2px; }}

  .advice-card {{ background: white; border-radius: 16px; padding: 18px 20px;
    margin-bottom: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  .advice-card .title {{ font-size: 13px; font-weight: 600; margin-bottom: 6px;
    color: #7f8c8d; }}
  .advice-card .text {{ font-size: 18px; line-height: 1.6; font-weight: 500;
    color: #2c3e50; }}

  .table-card {{ background: white; border-radius: 16px; padding: 16px;
    margin-bottom: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    overflow-x: auto; }}
  .table-card .title {{ font-size: 13px; font-weight: 600; margin-bottom: 10px;
    color: #7f8c8d; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 500px; }}
  th, td {{ padding: 7px 8px; text-align: center; border-bottom: 1px solid #eee;
    white-space: nowrap; }}
  th {{ background: #f8f9fa; font-weight: 600; color: #7f8c8d; font-size: 11px; }}
  tr:hover {{ background: #f8f9fa; }}

  .dir-growth {{ color: #F39C12; font-weight: 600; }}
  .dir-value {{ color: #27AE60; font-weight: 600; }}
  .dir-cash {{ color: #95A5A6; font-weight: 600; }}

  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
  .tag-growth {{ background: #FEF3CD; color: #856404; }}
  .tag-value {{ background: #D4EDDA; color: #155724; }}
  .tag-cash {{ background: #E2E3E5; color: #383D41; }}

  .footer {{ text-align: center; padding: 16px; color: #95a5a6; font-size: 11px;
    line-height: 1.8; }}

  @media (max-width: 600px) {{
    .signal-main {{ flex-direction: column; align-items: stretch; }}
    .signal-badge {{ width: 100%; }}
    .grid {{ grid-template-columns: 1fr 1fr 1fr; }}
    .header h1 {{ font-size: 15px; }}
    body {{ padding: 8px; }}
    .advice-card .text {{ font-size: 16px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>🌲 B1-木星 · Panda AI</h1>
      <div class="sub">{version_info}</div>
    </div>
    <div class="date">{beijing_now().strftime('%Y-%m-%d<br>%H:%M')}</div>
  </div>

  <div class="signal-card">
    <div class="signal-main">
      <div class="signal-badge">{pos_cn[latest['dir']]}</div>
      <div class="signal-meta">
        <div class="row"><span class="label">仓位</span>
          <span class="value">{'空仓' if latest['wt']==0 else f'{latest["wt"]*100:.0f}%'}</span></div>
        <div class="row"><span class="label">持仓标的</span>
          <span class="value">{latest['target']}</span></div>
        <div class="row"><span class="label">数据截至</span>
          <span class="value">{latest['date'].strftime('%Y-%m-%d')}</span></div>
        <div class="row"><span class="label">T值 (z-score)</span>
          <span class="value">{latest['T']:+.3f}</span></div>
        <div class="row"><span class="label">SLOPE (5日斜率)</span>
          <span class="value">{latest['slope']:+.6f}</span></div>
        <div class="row"><span class="label">比价偏离MA20</span>
          <span class="value">{latest['dev_pct']:+.2f}%</span></div>
        <div class="row"><span class="label">策略版本</span>
          <span class="value">X14 v2.0-lite</span></div>
      </div>
    </div>
  </div>

  <div class="charts">
    <img src="data:image/png;base64,{chart_b64}" alt="B1-木星信号图表">
  </div>

  <div class="grid">
    <div class="stat-card">
      <div class="num">{g_days}</div>
      <div class="lbl">最近{n}天 · 成长</div>
    </div>
    <div class="stat-card">
      <div class="num">{v_days}</div>
      <div class="lbl">最近{n}天 · 价值</div>
    </div>
    <div class="stat-card">
      <div class="num">{c_days}</div>
      <div class="lbl">最近{n}天 · 空仓</div>
    </div>
    <div class="stat-card">
      <div class="num">{t60_mean:.2f}</div>
      <div class="lbl">T均值(60天)</div>
    </div>
    <div class="stat-card">
      <div class="num" style="color:{'#E74C3C' if latest.get('pnl_pct',0) >= 0 else '#2ECC71'}">
        {latest.get('nav', 0):.0f}</div>
      <div class="lbl">账户净值(¥){latest.get('pnl_pct', 0):+.2f}%</div>
    </div>
    <div class="stat-card">
      <div class="num" style="font-size:22px">{latest.get('latest_date','')}</div>
      <div class="lbl">最新交易日</div>
    </div>
  </div>

  <div class="advice-card">
    <div class="title">💰 交易建议</div>
    <div class="text">{latest['advice']}</div>
  </div>

  <div class="table-card">
    <div class="title">📋 最近10天信号明细</div>
    <table>
      <tr><th>日期</th><th>比价</th><th>MA20</th><th>T值</th><th>斜率</th><th>方向</th><th>仓位</th></tr>
      {''.join(f'<tr><td>{d}</td><td>{r["ratio"]:.3f}</td><td>{r["ma20"]:.3f}</td>'
        f'<td>{r["T"]:+.2f}</td><td>{r["slope"]:+.4f}</td>'
        f'<td><span class="tag tag-{r["dir"]}">{pos_cn[r["dir"]]}</span></td>'
        f'<td>{r["wt_label"]}</td></tr>'
        for d, r in latest['last10'].iterrows())}
    </table>
  </div>

  <div class="table-card">
    <div class="title">📋 调仓记录（最近20笔，最新在前）</div>
    <table>
      <tr><th>日期</th><th>操作</th><th>从</th><th>到</th></tr>
      {''.join(f'<tr><td>{t["date"]}</td><td><span class="tag" style="background:#E8F0FE;color:#1A73E8">{t["action"]}</span></td>'
        f'<td>{t["from"]}</td><td>{t["to"]}</td></tr>'
        for t in (trades or [])[-20:][::-1])}
      {'' if trades else '<tr><td colspan="4" style="color:#95a5a6;padding:12px">暂无调仓记录</td></tr>'}
    </table>
  </div>

  <div class="footer">
    B1-木星 (X14 v2.0-lite) · 成長價值ETF輪動策略<br>
    参数: DC={PARAMS["dc"]} DCD={PARAMS["dcd"]} T={PARAMS["rt"]} E5={PARAMS["st"]} BIAS={PARAMS["bias_high"]}<br>
    数据来源: baostock · 自动生成于 {beijing_now().strftime('%Y-%m-%d %H:%M')}<br>
    ⚠️ 仅供参考，不构成投资建议
  </div>
</div>
</body>
</html>'''


# ========== 主流程 ==========
def main():
    parser = argparse.ArgumentParser(description='B1-木星 (X14) 每日信号生成器 — Panda AI')
    parser.add_argument('--out-dir', type=str, default=None,
                        help='输出目录，默认 signal_page/docs/')
    args = parser.parse_args()

    print('=' * 50)
    print('  🌲 B1-木星 (X14 v2.0-lite) 每日信号生成器')
    print(f'  {beijing_now().strftime("%Y-%m-%d %H:%M")}')
    print(f'  策略版本: X14 v2.0-lite')
    print(f'  参数: DC={PARAMS["dc"]} DCD={PARAMS["dcd"]} T={PARAMS["rt"]} '
          f'E5={PARAMS["st"]} BIAS={PARAMS["bias_high"]}')
    print('=' * 50)

    # 1. 获取数据
    print('\n📡 获取行情数据...')
    g = fetch_baostock(CODE_G, NAME_G, FETCH_DAYS)
    time.sleep(2)
    v = fetch_baostock(CODE_V, NAME_V, FETCH_DAYS)
    common = g.index.intersection(v.index)
    g, v = g[common], v[common]
    print(f'  共同交易日: {len(g)}天')

    # 2. 计算信号
    print('\n🧮 计算信号 (X14 v2.0-lite 6层逻辑)...')
    df = compute_signals(g, v, PARAMS)

    # 3. 计算P&L
    print('\n💰 计算策略净值...')
    nav, INIT_CAP = calc_nav(df, g, v)
    current_nav = nav.iloc[-1]
    pnl_pct = (current_nav / INIT_CAP - 1) * 100
    print(f'  初始资金: {INIT_CAP}  →  当前: {current_nav:.2f}  ({pnl_pct:+.2f}%)')

    # 4. 最新信号
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    dir_now = last['dir']
    dir_prev = prev['dir']
    wt_now = last['wt']
    wt_prev = prev['wt']

    target = f'{NAME_G}({CODE_G})' if dir_now == 'growth' else (
        f'{NAME_V}({CODE_V})' if dir_now == 'value' else '空仓')

    if wt_now == 0:
        advice = '空仓观望 — 多空信号不明确，等待趋势明朗'
    elif dir_now != dir_prev:
        old_target = f'{NAME_G}({CODE_G})' if dir_prev == 'growth' else f'{NAME_V}({CODE_V})'
        advice = f'切换：卖出 {old_target}，买入 {target}，仓位 {wt_now * 100:.0f}%'
    elif wt_now < wt_prev:
        advice = f'降仓：{target} 从 {wt_prev * 100:.0f}% 降低至 {wt_now * 100:.0f}%'
    else:
        hold_text = '买入' if wt_now >= 1.0 else '持有'
        advice = f'{hold_text} {target}，仓位 {wt_now * 100:.0f}%'

    # 5. 调仓记录
    trades = []
    dir_map = {'growth': NAME_G, 'value': NAME_V, 'cash': '空仓'}
    for i in range(1, len(df)):
        ps = df.iloc[i - 1]
        cs = df.iloc[i]
        if ps['dir'] != cs['dir'] or abs(ps['wt'] - cs['wt']) > 0.01:
            action = '切换' if ps['dir'] != cs['dir'] else '调仓'
            trades.append({
                'date': df.index[i].strftime('%Y-%m-%d'),
                'from': f"{dir_map.get(ps['dir'], ps['dir'])} {ps['wt'] * 100:.0f}%",
                'to': f"{dir_map.get(cs['dir'], cs['dir'])} {cs['wt'] * 100:.0f}%",
                'action': action,
            })

    # 最近10天表格
    last10 = df.tail(10)[['ratio', 'ma20', 'T', 'slope', 'dir', 'wt']].copy()
    last10.index = [d.strftime('%m-%d') for d in last10.index]
    last10['wt_label'] = last10['wt'].apply(
        lambda w: '空仓' if w == 0 else f'{w * 100:.0f}%')

    latest = {
        'date': df.index[-1],
        'dir': dir_now,
        'wt': wt_now,
        'T': last['T'],
        'slope': last['slope'],
        'dev_pct': last['dev'] * 100,
        'target': target,
        'advice': advice,
        'nav': current_nav,
        'pnl_pct': pnl_pct,
        'init_cap': INIT_CAP,
        'latest_date': df.index[-1].strftime('%m-%d'),
        'last10': last10,
    }

    print(f'\n  📊 最新信号: {latest["dir"]}  仓位: {latest["wt"] * 100:.0f}%  '
          f'T={last["T"]:+.3f}  NAV={current_nav:.0f}')

    # 6. 生成图表
    print('\n📈 生成图表...')
    chart_b64 = make_chart(df, PARAMS['rt'], nav)

    # 7. 生成HTML
    print('\n📄 生成HTML信号页面...')
    html = make_html(latest, df, chart_b64, trades)

    # 8. 输出
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir if args.out_dir else os.path.join(script_dir, 'docs')
    os.makedirs(out_dir, exist_ok=True)

    # HTML输出
    out_html = os.path.join(out_dir, 'index.html')
    with open(out_html, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  ✅ HTML已保存: {out_html}')

    # JSON信号输出（结构化数据，方便程序读取）
    signal_json = {
        'generated_at': beijing_now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy': 'B1-木星 (X14 v2.0-lite)',
        'params': PARAMS,
        'latest_signal': {
            'date': df.index[-1].strftime('%Y-%m-%d'),
            'direction': dir_now,
            'position_pct': float(wt_now * 100),
            'target_etf': target,
            'T_value': float(last['T']),
            'slope': float(last['slope']),
            'deviation_pct': float(last['dev'] * 100),
            'nav': float(current_nav),
            'pnl_pct': float(pnl_pct),
            'advice': advice,
        },
        'recent_signals': [
            {
                'date': d.strftime('%Y-%m-%d'),
                'direction': r['dir'],
                'position_pct': float(r['wt'] * 100),
                'T': float(r['T']),
            }
            for d, r in df.tail(10).iterrows()
        ],
        'trades_count': len(trades),
    }

    out_json = os.path.join(out_dir, 'signal.json')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(signal_json, f, ensure_ascii=False, indent=2)
    print(f'  ✅ JSON已保存: {out_json}')

    print(f'\n✅ 完成! 共 {len(trades)} 笔调仓记录')
    print('=' * 50)


if __name__ == '__main__':
    main()
