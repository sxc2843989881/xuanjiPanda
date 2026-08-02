"""B4-动量止损版 成长价值轮动策略 — 每日信号生成器
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

warnings.filterwarnings('ignore')

# ========== B4策略配置 ==========
CODE_G = '159259'   # 易方达成长ETF
CODE_V = '159263'   # 易方达价值ETF
NAME_G = '易方达成长ETF'
NAME_V = '易方达价值ETF'
FETCH_DAYS = 500    # 拉取天数

# B4策略核心参数（与backtest params.py BASELINE_PARAMS 一致）
B4_PARAMS = dict(
    dc=5,                # 方向确认天数
    dcd=6,               # 方向冷却天数
    rt=1.3,              # T弱信号阈值
    slope_thresh=0.002,  # 弱斜率阈值
    bias_high=0.19,      # BIAS超买阈值
    bias_mode='clear',
    # 第5层：牛市高点止损
    mom_60d_period=60,
    mom_3m_thresh=0.50,
    mom_5d_lock=60,
    # 第8层：黑天鹅保护
    black_swan_g_thresh=0.06,
    black_swan_v_thresh=0.03,
    black_swan_lookback=5,
    momentum_period=10,
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


# ========== B4策略 6层信号计算 ==========
def compute_signals(g_close, v_close, params):
    """B4-动量止损版 6层信号计算

    B4 = B3 + 第5层(牛市高点止损·新高可解锁) + 第8层(黑天鹅保护·固定10日动量解锁)

    第1层 DC   — 方向确认：T值方向需连续 dc 天一致才确认
    第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
    第3层 弱信号 — T弱+斜率弱 → 空仓
    第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
    第5层 牛市高点止损 — 持有成长时，60日动量>50%+5日动量转负 → 清仓成长，锁定60天
    第8层 黑天鹅保护 — 5日内成长跌>6%且价值跌>3% → 全部空仓，固定10日动量解锁
    """
    # 比价 = 成长/价值
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()
    t = ratio_dev / ratio_dev_std20
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)

    n = len(t)
    index = t.index

    # ===== L1: 方向确认 (DC) =====
    dc = params.get('dc', 5)
    raw_dir = (t > 0).map({True: 'growth', False: 'value'})
    mask = np.ones(n, dtype=bool)
    for i in range(n):
        if dc > 1:
            for k in range(1, dc):
                if i < k or raw_dir.iloc[i] != raw_dir.iloc[i - k]:
                    mask[i] = False
                    break
    confirmed_dir = raw_dir.where(pd.Series(mask, index=index), np.nan)

    # ===== L2: 方向冷却 (DCD) =====
    dcd = params.get('dcd', 6)
    new_dir = confirmed_dir.ffill().fillna('growth')
    last_switch = -dcd - 1
    prev = new_dir.iloc[0]
    for i in range(1, n):
        curr = confirmed_dir.iloc[i]
        if pd.isna(curr):
            continue
        if curr != prev:
            if i - last_switch >= dcd:
                last_switch = i
                prev = curr
            new_dir.iloc[i] = prev
        else:
            new_dir.iloc[i] = prev
    dir_raw = new_dir.ffill().fillna('growth')
    dir_series = dir_raw.map({'growth': 'growth', 'value': 'value'})

    # 权重初始为1.0
    wt = pd.Series(1.0, index=index)

    # ===== L3: 弱信号空仓 =====
    weak_slope = slope.abs() < params.get('slope_thresh', 0.002)
    weak_t = t.abs() < params.get('rt', 1.3)
    is_weak = weak_t & weak_slope
    wt[is_weak] = 0.0

    # ===== L4: BIAS超买保护 =====
    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > params.get('bias_high', 0.19))
    extreme_v = (dir_series == 'value') & (v_bias > params.get('bias_high', 0.19))
    wt[extreme_g | extreme_v] = 0.0

    # ===== L5: 牛市高点止损 =====
    g_mom_60d = g_close / g_close.shift(params.get('mom_60d_period', 60)) - 1
    g_mom_5d = g_close / g_close.shift(5) - 1
    g_high_60d = g_close.rolling(params.get('mom_60d_period', 60)).max()
    growth_high_mom = (
        (dir_series == 'growth')
        & (g_mom_60d > params.get('mom_3m_thresh', 0.50))
        & (g_mom_5d < 0)
    )
    growth_locked_until = -1
    growth_trigger_peak = None
    for i in range(n):
        if i < growth_locked_until and growth_trigger_peak is not None:
            if g_close.iloc[i] > growth_trigger_peak:
                growth_locked_until = -1
                growth_trigger_peak = None
        if i < growth_locked_until:
            if dir_series.iloc[i] == 'growth':
                wt.iloc[i] = 0.0
        if growth_high_mom.iloc[i] and wt.iloc[i] > 0.001:
            growth_trigger_peak = g_high_60d.iloc[i]
            growth_locked_until = i + params.get('mom_5d_lock', 60)
            wt.iloc[i] = 0.0

    # ===== L8: 黑天鹅保护 =====
    g_ret_5d = g_close / g_close.shift(params.get('black_swan_lookback', 5)) - 1
    v_ret_5d = v_close / v_close.shift(params.get('black_swan_lookback', 5)) - 1
    momentum_p = params.get('momentum_period', 10)
    g_mom_fixed = g_close / g_close.shift(momentum_p) - 1
    v_mom_fixed = v_close / v_close.shift(momentum_p) - 1
    black_swan_trigger = (
        (g_ret_5d < -params.get('black_swan_g_thresh', 0.06))
        & (v_ret_5d < -params.get('black_swan_v_thresh', 0.03))
    )
    growth_locked = False
    value_locked = False
    for i in range(n):
        if growth_locked and g_mom_fixed.iloc[i] > 0:
            growth_locked = False
        if value_locked and v_mom_fixed.iloc[i] > 0:
            value_locked = False
        if dir_series.iloc[i] == 'growth' and growth_locked:
            wt.iloc[i] = 0.0
        if dir_series.iloc[i] == 'value' and value_locked:
            wt.iloc[i] = 0.0
        if black_swan_trigger.iloc[i] and wt.iloc[i] > 0.001:
            growth_locked = True
            value_locked = True
            wt.iloc[i] = 0.0

    # 构建明细 DataFrame
    df = pd.DataFrame(index=index)
    df['ratio'] = ratio
    df['ma20'] = ratio_ma20
    df['dev'] = ratio_dev
    df['T'] = t
    df['slope'] = slope
    df['dir'] = dir_series
    df['wt'] = wt
    df['g_bias'] = g_bias
    df['v_bias'] = v_bias
    df['g_mom_5d'] = g_mom_5d
    df['v_mom_5d'] = v_close.pct_change(5)
    df['g_dd20'] = g_close.pct_change(20)
    df['v_dd20'] = v_close.pct_change(20)
    df['is_weak'] = is_weak
    return df





# ========== 图表生成 ==========
def make_chart(df):
    """3子图：比价+MA20 → T值 → 仓位（仅信号，不回测P&L）"""
    fig, axes = plt.subplots(3, 1, figsize=(12, 8),
                             gridspec_kw={'height_ratios': [2, 1, 1]})
    dates = df.index[-120:]

    ax = axes[0]
    ax.plot(dates, df.loc[dates, 'ratio'], color='#333333', lw=1.5, label='GV Ratio')
    ax.plot(dates, df.loc[dates, 'ma20'], color='#E74C3C', ls='--', lw=1, label='MA20')
    ax.axhline(1.0, color='gray', ls=':', alpha=0.4)
    ax.set_ylabel('GV Ratio', fontsize=9)
    ax.legend(fontsize=8, loc='upper left')
    ax.grid(alpha=0.2)

    dir_colors = {'growth': '#F1C40F', 'value': '#2ECC71', 'cash': '#95A5A6'}
    last_dir = None
    start_d = None
    for d in dates:
        nd = df.loc[d, 'dir']
        if nd != last_dir:
            if last_dir is not None and start_d is not None:
                ax.axvspan(start_d, d, alpha=0.08, color=dir_colors.get(last_dir, '#CCC'))
            start_d = d
            last_dir = nd
    if last_dir is not None and start_d is not None:
        ax.axvspan(start_d, dates[-1], alpha=0.08, color=dir_colors.get(last_dir, '#CCC'))

    ax = axes[1]
    ax.plot(dates, df.loc[dates, 'T'], color='#3498DB', lw=1.5, label='T(z-score)')
    ax.axhline(0, color='gray', ls='-', lw=0.5)
    ax.axhline(1.3, color='#E74C3C', ls='--', lw=0.8, label='+rt=1.3')
    ax.axhline(-1.3, color='#2ECC71', ls='--', lw=0.8, label='-rt=1.3')
    ax.fill_between(dates, 1.3, -1.3, alpha=0.05, color='gray')
    ax.set_ylabel('T (z-score)', fontsize=9)
    ax.legend(fontsize=8, loc='upper left')
    ax.grid(alpha=0.2)

    ax = axes[2]
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
def make_html(signal, df, chart_b64, trades=None, params=None):
    """生成手机友好的信号页面 HTML"""
    p = params or B4_PARAMS
    latest = signal
    pos_cn = {'growth': '📈 成长', 'value': '📉 价值', 'cash': '⏸ 空仓'}
    pos_bg = {'growth': 'linear-gradient(135deg, #F1C40F, #F39C12)',
              'value': 'linear-gradient(135deg, #2ECC71, #27AE60)',
              'cash': 'linear-gradient(135deg, #95A5A6, #7F8C8D)'}

    n = min(30, len(df))
    recent = df.tail(n)
    g_days = (recent['dir'] == 'growth').sum()
    v_days = (recent['dir'] == 'value').sum()
    c_days = n - g_days - v_days

    version_info = f'B4 · DC={p["dc"]} DCD={p["dcd"]} 动量={p["momentum_period"]}日'
    t60_mean = df['T'].tail(60).abs().mean()

    # 最近10天表格
    last10 = df.tail(10)[['ratio', 'ma20', 'T', 'slope', 'dir', 'wt']].copy()
    last10.index = [d.strftime('%m-%d') for d in last10.index]
    last10['wt_label'] = last10['wt'].apply(
        lambda w: '空仓' if w == 0 else f'{w * 100:.0f}%')

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>B4-动量止损 · 每日信号 — Panda AI</title>
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

  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }}
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

  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
  .tag-growth {{ background: #FEF3CD; color: #856404; }}
  .tag-value {{ background: #D4EDDA; color: #155724; }}
  .tag-cash {{ background: #E2E3E5; color: #383D41; }}

  .footer {{ text-align: center; padding: 16px; color: #95a5a6; font-size: 11px;
    line-height: 1.8; }}

  @media (max-width: 600px) {{
    .signal-main {{ flex-direction: column; align-items: stretch; }}
    .signal-badge {{ width: 100%; }}
    .grid {{ grid-template-columns: 1fr 1fr; }}
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
      <h1>⚡ B4-动量止损 · Panda AI</h1>
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
          <span class="value">B4-动量止损 (6层)</span></div>
      </div>
    </div>
  </div>

  <div class="charts">
    <img src="data:image/png;base64,{chart_b64}" alt="B4-动量止损信号图表">
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
        f'<td><span class="tag tag-{r["dir"]}">{"成长" if r["dir"]=="growth" else "价值" if r["dir"]=="value" else "空仓"}</span></td>'
        f'<td>{r["wt_label"]}</td></tr>'
        for d, r in last10.iterrows())}
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
    B4-动量止损版 · 成长价值ETF轮动策略<br>
    参数: DC={p["dc"]} DCD={p["dcd"]} rt={p["rt"]} BIAS={p["bias_high"]} 动量={p["momentum_period"]}日<br>
    第5层: 牛市高点止损(新高可解锁) · 第8层: 黑天鹅保护(固定10日动量解锁)<br>
    数据来源: baostock · 自动生成于 {beijing_now().strftime('%Y-%m-%d %H:%M')}<br>
    ⚠️ 仅供参考，不构成投资建议
  </div>
</div>
</body>
</html>'''


# ========== 主流程 ==========
def main():
    parser = argparse.ArgumentParser(description='B4-动量止损 每日信号生成器 — Panda AI')
    parser.add_argument('--out-dir', type=str, default=None,
                        help='输出目录，默认 signal_page_b4/docs/')
    args = parser.parse_args()

    print('=' * 50)
    print('  ⚡ B4-动量止损版 (6层) 每日信号生成器')
    print(f'  {beijing_now().strftime("%Y-%m-%d %H:%M")}')
    print(f'  策略版本: B4 (B3 + 第5层 + 第8层·固定10日动量)')
    print(f'  参数: DC={B4_PARAMS["dc"]} DCD={B4_PARAMS["dcd"]} '
          f'rt={B4_PARAMS["rt"]} 动量={B4_PARAMS["momentum_period"]}日')
    print('=' * 50)

    # 1. 获取数据
    print('\n📡 获取行情数据...')
    g = fetch_baostock(CODE_G, NAME_G, FETCH_DAYS)
    time.sleep(2)
    v = fetch_baostock(CODE_V, NAME_V, FETCH_DAYS)
    common = g.index.intersection(v.index)
    g, v = g[common], v[common]
    print(f'  共同交易日: {len(g)}天')

    # 2. 计算信号（B4 6层逻辑）
    print('\n🧮 计算信号 (B4 6层逻辑)...')
    df = compute_signals(g, v, B4_PARAMS)

    # 3. 最新信号
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    dir_now = last['dir'] if pd.notna(last['dir']) else 'cash'
    dir_prev = prev['dir'] if pd.notna(prev['dir']) else 'cash'
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

    # 4. 调仓记录
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

    latest = {
        'date': df.index[-1],
        'dir': dir_now,
        'wt': wt_now,
        'T': last['T'],
        'slope': last['slope'],
        'dev_pct': last['dev'] * 100,
        'target': target,
        'advice': advice,
        'latest_date': df.index[-1].strftime('%m-%d'),
    }

    print(f'\n  📊 最新信号: {latest["dir"]}  仓位: {latest["wt"] * 100:.0f}%  '
          f'T={last["T"]:+.3f}')

    # 5. 生成图表（仅信号，不回测P&L）
    print('\n📈 生成信号图表...')
    chart_b64 = make_chart(df)

    # 6. 生成HTML
    print('\n📄 生成HTML信号页面...')
    html = make_html(latest, df, chart_b64, trades, B4_PARAMS)

    # 7. 输出
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out_dir if args.out_dir else os.path.join(script_dir, 'docs')
    os.makedirs(out_dir, exist_ok=True)

    out_html = os.path.join(out_dir, 'index.html')
    with open(out_html, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  ✅ HTML已保存: {out_html}')

    # JSON信号输出
    signal_json = {
        'generated_at': beijing_now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy': 'B4-动量止损版 (6层)',
        'params': B4_PARAMS,
        'latest_signal': {
            'date': df.index[-1].strftime('%Y-%m-%d'),
            'direction': dir_now,
            'position_pct': float(wt_now * 100),
            'target_etf': target,
            'T_value': float(last['T']),
            'slope': float(last['slope']),
            'deviation_pct': float(last['dev'] * 100),
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