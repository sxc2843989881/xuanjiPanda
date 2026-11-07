"""B4V9 成长价值轮动策略 — 每日信号生成器（B4V8基线 + AW窗口35）
本脚本完全自包含，可直接在 GitHub Actions 上运行。
依赖: pip install requests pandas numpy matplotlib

B4V9 = B4V8 默认配置 + 唯一参数改动 aw_flip_window: 20 → 35
有效策略层级 = B4V4(6层) + 第10层 AW反打脸保护(slow_dc) + 第11层 RCD轮动减仓(rot_z)
  - 第10层 AW: 近35日T符号翻转>=5次判为震荡态, 震荡态内换向确认门槛 5天→7天,
    削减低质量"来回切"换向; fw=35 比 fw=20 判定更严格, 仅在真震荡时加严
  - 第11层 RCD: 持有腿波动率标准化急跌(<=-1.6σ)且相对另一腿超额<=-4%且另一腿上涨
    (资金轮动型急跌)时, 仓位减半; 持有腿止跌回升或方向切换后回补
官方引擎(截至2026-10-13): 年化45.77% / Calmar2.08 / 最大回撤-22.03%
  (vs B4V4口径 fw20: 45.48% / 1.88 / -24.18%)

继承自B4V4：
  - 信号与收益计算与官方引擎同源：使用成长100(480080)/价值100(480081)指数，
    数据来自 cnindex.com.cn 官网（同 update_data_latest.py 数据源）
  - 收盘价撮合（matching_type=0）：读昨日信号，今日收盘价成交
  - DC容差机制（dc_tolerance=0.09）：连续dc天中允许1天反向但|T|<=0.09
  - DCD从6天优化至5天
  - L5超级牛市锁满全程（移除深度回撤提前解锁）
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
import matplotlib.font_manager as fm

# 设置中文字体：动态探测可用CJK字体（Windows/Linux CI 通用），找不到则回退 DejaVu Sans
_CJK_CANDIDATES = [
    'SimHei', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC',
    'Noto Sans SC', 'Source Han Sans SC', 'WenQuanYi Micro Hei',
]
_CJK_FONT = None
if 'plt' in globals():
    _available = {f.name for f in fm.fontManager.ttflist}
    for _name in _CJK_CANDIDATES:
        if _name in _available:
            _CJK_FONT = _name
            break
if _CJK_FONT:
    plt.rcParams['font.sans-serif'] = [_CJK_FONT, 'DejaVu Sans']
    print(f'  🎨 中文字体: {_CJK_FONT}')
else:
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
    print('  ⚠️ 未找到中文字体，图表中文可能显示为方块')
plt.rcParams['axes.unicode_minus'] = False

warnings.filterwarnings('ignore')

# ========== B4V9策略配置 ==========
# 信号与收益计算统一使用官方引擎同源指数：成长100(480080)/价值100(480081)
CODE_G = '480080'     # 成长100指数（策略信号基准）
CODE_V = '480081'     # 价值100指数（策略信号基准）
NAME_G = '成长100指数'
NAME_V = '价值100指数'
# 实盘交易用的ETF（仅用于交易建议展示，收益计算基于指数）
GROWTH_ETF = '159259'
VALUE_ETF = '159263'
GROWTH_ETF_NAME = '易方达成长ETF'
VALUE_ETF_NAME = '易方达价值ETF'
FETCH_START = '2012-01-01'   # 拉取起始日期（覆盖官方引擎2013起全量回测）

# B4V9策略核心参数（与 strategies/growth_value_rotation_b4v9/params.py BASELINE_PARAMS 一致）
B4_PARAMS = dict(
    dc=5,                # 方向确认天数
    dc_tolerance=0.09,   # DC容差：连续dc天中允许1天反向但|T|<=0.09，仍算一致
    dcd=5,               # 方向冷却天数
    rt=1.3,              # T弱信号阈值
    slope_thresh=0.002,  # 弱斜率阈值
    bias_high=0.19,      # BIAS超买阈值
    bias_mode='clear',
    # 第5层：牛市止损 — 斐波那契阶梯解锁
    l5_lookback_window=65,      # 滚动窗口找牛市起点Low
    l5_min_rise=0.50,           # 最低触发涨幅：仅保留超级牛市，涨幅>50%才触发
    l5_big_bull_thresh=0.50,    # 超级牛市阈值：涨幅>50%用严格阶梯
    # 第8层：黑天鹅保护
    black_swan_g_thresh=0.06,
    black_swan_v_thresh=0.03,
    black_swan_lookback=5,
    momentum_period=10,
    # 第10层：反打脸保护 AW（slow_dc）—— B4V9 唯一改动：aw_flip_window 20→35
    aw_mode='slow_dc',            # 生产固定 slow_dc；'none' 可关闭回退 B4V4 行为
    aw_flip_window=35,           # 震荡态检测窗口：近35日T符号翻转次数（B4V4口径为20）
    aw_flip_thresh=5,            # 翻转次数阈值：>=5 判为震荡态
    aw_anchor_window=60,         # 慢锚均线窗口（预热不足的早期区间不判震荡）
    aw_dc_strict=7,              # 震荡态内新方向需连续确认7天（=dc+2）
    aw_t_gate=1.0,               # tgate 模式预留（生产不走该模式）
    # 第11层：单腿急跌·轮动减仓 RCD（rot_z 波动率标准化）
    dr_mode='rot_z',             # 'none' 可关闭；rot_z=波动率标准化触发
    dr_drop=0.06,                # rot 模式：持有腿5日跌幅阈值
    dr_z=1.6,                    # rot_z：持有腿5日跌幅 <= -1.6σ 触发
    dr_spread=0.04,              # 相对另一腿5日超额 <= -4%
    dr_other_up=True,            # 要求另一腿5日上涨（资金轮动型，规避普跌反弹踏空）
    dr_scale=0.5,                # 触发后仓位减半
    dr_rec=0.0,                  # 持有腿5日收益回正即恢复满仓
    dr_rec_win=5,                # 恢复判定窗口：5日
    dr_deep=False,               # 深度档默认关闭
    dr_drop20=0.12,
    dr_spread20=0.05,
    dr_deep_scale=0.0,
)


# ========== 数据获取（本地缓存，cnindex官网同源指数）==========
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

def _fetch_index_raw(code, name, start_date):
    """从 cnindex.com.cn 官网拉取指数日线数据（与官方引擎同源）

    API说明: 快速走势 / 日频数据
      https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat

    Returns:
        DataFrame[date, open, close] 或 None
    """
    import requests
    MAX_RETRIES = 5
    BASE_DELAY = 3
    end = datetime.now().strftime('%Y-%m-%d')
    url = "https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://www.cnindex.com.cn/",
    }

    for attempt in range(MAX_RETRIES):
        try:
            params = {
                "indexCode": code,
                "startDate": start_date,
                "endDate": end,
                "frequency": "day",
            }
            r = requests.get(url, params=params, headers=headers, timeout=30)
            data = r.json()
            if data.get("code") != 200:
                raise ValueError(f"API返回code={data.get('code')}")
            raw = data.get("data", {})
            rows = raw.get("data", [])
            if not rows:
                raise ValueError("数据为空")
            columns = raw.get("item", [])
            required = {'timestamp', 'close'}
            missing = required - set(columns)
            if missing:
                raise ValueError(f"官方接口字段缺失: {missing}, 实际: {columns}")
            df = pd.DataFrame(rows, columns=columns)
            df["date"] = pd.to_datetime(df["timestamp"])
            for c in ['open', 'high', 'low', 'close']:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.sort_values("date").reset_index(drop=True)
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            return df[['date', 'open', 'close']]

        except Exception as e:
            print(f'  ⚠️ {name}({code}): 第{attempt+1}次尝试失败 - {e}')

        if attempt < MAX_RETRIES - 1:
            delay = BASE_DELAY * (2 ** attempt)
            print(f'  🔄 等待 {delay}s 后重试...')
            time.sleep(delay)

    return None


def fetch_index_with_cache(code, name, start_date=FETCH_START):
    """从本地缓存加载指数数据，仅拉取最新数据

    工作流程：
      1. 检查本地 data/index_{code}.csv 是否存在
      2. 存在 → 加载缓存，获取最后日期，仅拉取该日期之后的数据
      3. 不存在 → 首次拉取 start_date 以来全部数据
      4. 合并后保存回缓存，返回收盘价 Series
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    cache_path = os.path.join(DATA_DIR, f'index_{code}.csv')

    # 尝试加载缓存
    last_date = None
    cached_df = None
    if os.path.exists(cache_path):
        try:
            cached_df = pd.read_csv(cache_path)
            cached_df['date'] = pd.to_datetime(cached_df['date'])
            cached_df = cached_df.sort_values('date').reset_index(drop=True)
            last_date = cached_df['date'].iloc[-1]
            print(f'  📂 本地缓存 {name}({code}): 已有 {len(cached_df)} 天，截至 {last_date.date()}')
        except Exception as e:
            print(f'  ⚠️ 缓存读取失败({e})，重新拉取')
            cached_df = None
            last_date = None

    # 决定拉取起始日期
    if last_date is None:
        fetch_start = start_date
        print(f'  📡 首次拉取 {name}({code})，从 {fetch_start} 起...')
    else:
        # 从缓存最后日期前5天开始拉取，确保重叠覆盖（防止数据修正）
        fetch_start = (last_date - timedelta(days=5)).strftime('%Y-%m-%d')
        print(f'  📡 增量拉取 {name}({code})，从 {fetch_start} 起（缓存截至 {last_date.date()}）...')

    # 拉取新数据
    new_df = _fetch_index_raw(code, name, fetch_start)
    if new_df is None:
        if cached_df is not None:
            # cnindex失败：尝试 adata ETF 兜底回补缺口
            print(f'  ⚠️ cnindex官网拉取失败，尝试 adata ETF 兜底...')
            filled = _etf_fallback_fill(cached_df, code, name)
            if filled is not None:
                DATA_SOURCE_LOG.append({
                    'code': code, 'name': name, 'source': 'adata_etf_fallback',
                    'last_date': str(filled.index[-1].date()), 'cnindex': 'failed'})
                return filled
            DATA_SOURCE_LOG.append({
                'code': code, 'name': name, 'source': 'stale_cache',
                'last_date': str(cached_df['date'].iloc[-1].date()), 'cnindex': 'failed'})
            print(f'  ⚠️ 无ETF兜底可用，使用缓存数据')
            return cached_df.set_index('date')['close']
        raise ValueError(f'{name}: cnindex官网无数据')

    # 合并缓存与新数据
    if cached_df is not None:
        existing_dates = set(cached_df['date'])
        new_df = new_df[~new_df['date'].isin(existing_dates)].copy()
        if len(new_df) == 0:
            print(f'  ✅ {name}({code}): 缓存已是最新，无需更新')
            DATA_SOURCE_LOG.append({
                'code': code, 'name': name, 'source': 'cnindex',
                'last_date': str(cached_df['date'].iloc[-1].date()), 'cnindex': 'ok'})
            return cached_df.set_index('date')['close']
        # 拼接并去重（保留最后一条）
        combined = pd.concat([cached_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset='date', keep='last').reset_index(drop=True)
        added = len(new_df)
    else:
        combined = new_df
        added = len(combined)

    # 保存缓存
    combined.to_csv(cache_path, index=False, encoding='utf-8')
    print(f'  💾 缓存已更新: +{added} 天，共 {len(combined)} 天 '
          f'({combined["date"].iloc[0].date()} ~ {combined["date"].iloc[-1].date()})')
    DATA_SOURCE_LOG.append({
        'code': code, 'name': name, 'source': 'cnindex',
        'last_date': str(combined["date"].iloc[-1].date()), 'cnindex': 'ok'})

    return combined.set_index('date')['close']


# ========== 数据源兜底：cnindex失败时用 adata ETF 缩放回填补最新缺口 ==========
# cnindex官网(hq.cnindex.com.cn)可能对境外/GitHub runner 无法访问(拉取失败)，
# 此时用 adata 拉对应 ETF(159259/159263) 日线(能拿到当天)，按缩放比回补指数最新缺口。
DATA_SOURCE_LOG = []  # 记录每次运行各指数的数据来源，写入 signal.json 供排查

CODE_ETF_MAP = {CODE_G: GROWTH_ETF, CODE_V: VALUE_ETF}


def _fetch_etf_close(etf_code, start_date):
    """用 adata 拉取 ETF 日线收盘价(能拿到当天)，返回 Series[date]=close 或 None"""
    try:
        import adata
    except ImportError:
        print('    ⚠️ adata 未安装，无法用ETF兜底')
        return None
    try:
        df = adata.fund.market.get_market_etf(
            fund_code=etf_code, start_date=start_date, k_type=1)
        if df is None or len(df) == 0:
            print(f'    ⚠️ adata ETF {etf_code} 返回空')
            return None
        df = df.copy()
        dc = 'trade_date' if 'trade_date' in df.columns else 'date'
        df[dc] = pd.to_datetime(df[dc])
        s = df.set_index(dc)['close'].astype(float).sort_index()
        s = s[~s.index.duplicated(keep='last')]
        print(f'    ✅ adata ETF {etf_code}: {s.index[0].date()} ~ {s.index[-1].date()} ({len(s)}天)')
        return s
    except Exception as e:
        print(f'    ⚠️ adata ETF {etf_code} 拉取失败: {e}')
        return None


def _etf_fallback_fill(cached_df, code, name):
    """cnindex 官网拉取失败时，用 adata 对应ETF按缩放比回补指数缺失的最新交易日。
    不写回缓存(兜底为近似值)，仅本次运行时融合，等官网恢复后仍以官网真值为准。"""
    etf_code = CODE_ETF_MAP.get(code)
    if not etf_code:
        return None
    last_idx_date = cached_df['date'].iloc[-1]
    etf_start = (last_idx_date - timedelta(days=5)).strftime('%Y-%m-%d')
    print(f'    🔄 cnindex失败，改用 adata ETF {etf_code} 兜底(自{etf_start}起)...')
    etf_close = _fetch_etf_close(etf_code, etf_start)
    if etf_close is None:
        return None
    ovl = cached_df['date'].isin(etf_close.index)
    if not ovl.any():
        print('    ⚠️ 指数缓存与ETF无重叠日，无法计算缩放比')
        return None
    last_ovl = cached_df.loc[ovl].iloc[-1]
    idx_close = float(last_ovl['close'])
    etf_close_at = float(etf_close.loc[last_ovl['date']])
    if etf_close_at <= 0:
        print('    ⚠️ 重叠日ETF收盘<=0，无法缩放')
        return None
    scale = idx_close / etf_close_at
    print(f'    ✅ 缩放比 = 指数{idx_close:.4f} / ETF{etf_close_at:.4f} = {scale:.4f}')
    new_idx = etf_close[etf_close.index > last_idx_date]
    if len(new_idx) == 0:
        print('    ✅ ETF无新增交易日，缓存已是最新')
        return cached_df.set_index('date')['close']
    added = pd.DataFrame({
        'date': new_idx.index,
        'open': (new_idx * scale).values,
        'close': (new_idx * scale).values,
    })
    combined = pd.concat([cached_df, added], ignore_index=True)
    combined = (combined.drop_duplicates(subset='date', keep='last')
                .sort_values('date').reset_index(drop=True))
    print(f'    💾 ETF兜底回填: +{len(added)}天，更新至 {combined["date"].iloc[-1].date()}')
    return combined.set_index('date')['close']


# ========== B4V9 辅助函数 ==========
def _calc_low_peak_rise(g_close, lookback_window):
    """预计算每个交易日的Low、Peak、peak_rise_pct、current_rise_pct、rise_days

    滚动窗口内找最低价Low，再从Low到当前找最高价Peak。

    Args:
        g_close: 收盘价Series
        lookback_window: 滚动窗口大小

    Returns:
        low_series: 每个交易日的Low
        peak_series: 每个交易日从Low到当前的最高价
        peak_rise_pct_series: 涨幅 = Peak/Low - 1（用于区分大小牛市）
        current_rise_pct_series: 涨幅 = 当前价/Low - 1（用于触发条件）
        rise_days_series: 从Low到当前的天数
    """
    n = len(g_close)
    index = g_close.index
    low_series = pd.Series(index=index, dtype=float)
    peak_series = pd.Series(index=index, dtype=float)
    peak_rise_pct_series = pd.Series(index=index, dtype=float)
    current_rise_pct_series = pd.Series(index=index, dtype=float)
    rise_days_series = pd.Series(index=index, dtype=int)

    for i in range(n):
        start = max(0, i - lookback_window + 1)
        window = g_close.iloc[start:i + 1]
        low_val = window.min()
        low_idx = window.idxmin()
        current_val = g_close.iloc[i]
        peak_val = g_close.loc[low_idx:g_close.index[i]].max()
        # 交易日数（从Low到当前的数据行数-1）
        rise_days = len(g_close.loc[low_idx:g_close.index[i]]) - 1

        low_series.iloc[i] = low_val
        peak_series.iloc[i] = peak_val
        peak_rise_pct_series.iloc[i] = peak_val / low_val - 1
        current_rise_pct_series.iloc[i] = current_val / low_val - 1
        rise_days_series.iloc[i] = rise_days

    return low_series, peak_series, peak_rise_pct_series, current_rise_pct_series, rise_days_series


# ========== B4V9 信号计算（B4V4 6层 + 第10层AW + 第11层RCD） ==========
def compute_signals(g_close, v_close, params):
    """B4V9 信号计算（B4V8默认基线 + aw_flip_window=35）

    有效层级：
    第1层 DC   — 方向确认：T值方向需连续 dc 天一致才确认（容差允许1天反向|T|<=dc_tolerance）
    第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
    第10层 AW  — 反打脸保护：近35日T翻转>=5次的震荡态内，换向确认 5天→7天
    第3层 弱信号 — T弱+斜率弱 → 空仓
    第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
    第5层 牛市止损 — 超级牛市锁满全程; 新高试错或时间到期解锁
    第8层 黑天鹅保护 — 固定10日动量解锁
    第11层 RCD — 轮动型单腿急跌(波动率标准化<=-1.6σ)时仓位减半，止跌回补
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

    # ===== L1: 方向确认 (DC) — 固定周期 + 容差 =====
    # T>0 → growth, T<0 → value
    # 需连续 dc 天方向一致才确认
    # 容差(dc_tolerance): 连续 dc 天中若仅1天反向但 |T| <= dc_tolerance，仍视为一致
    dc = params.get('dc', 5)
    dc_tolerance = params.get('dc_tolerance', 0.0)
    raw_dir = (t > 0).map({True: 'growth', False: 'value'})
    mask = np.ones(n, dtype=bool)
    for i in range(n):
        if dc > 1:
            start = max(0, i - dc + 1)
            window = raw_dir.iloc[start:i + 1]
            window_t = t.iloc[start:i + 1]
            if len(window) < dc:
                mask[i] = False
                continue
            main_dir = window.iloc[-1]  # 以最后一天方向为主方向
            opp_mask = window != main_dir
            opp_count = opp_mask.sum()
            if opp_count == 0:
                mask[i] = True
            elif opp_count == 1 and dc_tolerance > 0:
                opp_idx = window[opp_mask].index[0]
                if abs(window_t.loc[opp_idx]) <= dc_tolerance:
                    mask[i] = True
                else:
                    mask[i] = False
            else:
                mask[i] = False
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

    # ===== L10: 反打脸保护 (AW — Anti-Whipsaw) =====
    # 震荡态检测: 近 aw_flip_window 日 T 符号翻转次数 >= aw_flip_thresh → 震荡态。
    # slow_dc 动作: 震荡态内换向需新方向连续 aw_dc_strict(=7) 天确认（正常 dc=5），
    # 非震荡态行为完全不变。慢均线预热不足的早期区间不判震荡。
    # 与 strategies/growth_value_rotation_b4v9/signal_generator_dct.py 同源（标签 growth/value）。
    aw_mode = params.get('aw_mode', 'none')
    aw_whipsaw_flag = np.zeros(n, dtype=bool)
    if aw_mode != 'none':
        _sgn = np.sign(t)
        _flip = ((_sgn != _sgn.shift(1)) & (_sgn != 0) & (_sgn.shift(1) != 0)).astype(float)
        flip_w = _flip.rolling(params.get('aw_flip_window', 20)).sum()
        _anchor_ma = ratio.rolling(params.get('aw_anchor_window', 60)).mean()
        whipsaw = (flip_w >= params.get('aw_flip_thresh', 5)) & _anchor_ma.notna()
        aw_whipsaw_flag = whipsaw.fillna(False).values
        dc_strict = params.get('aw_dc_strict', 7)
        _raw_arr = raw_dir.values

        aw_dir = dir_raw.copy()
        prev = aw_dir.iloc[0]
        for i in range(1, n):
            cand = dir_raw.iloc[i]
            if whipsaw.iloc[i]:
                if cand == prev:
                    aw_dir.iloc[i] = cand
                else:
                    allow = False
                    if aw_mode == 'slow_dc':
                        k = int(dc_strict)
                        s = max(0, i - k + 1)
                        allow = (_raw_arr[s:i + 1] == cand).all()
                    aw_dir.iloc[i] = cand if allow else prev
            else:
                aw_dir.iloc[i] = cand
            prev = aw_dir.iloc[i]
        dir_raw = aw_dir

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

    # ===== L5: 牛市止损 — 斐波那契阶梯解锁 =====
    lookback_window = params.get('l5_lookback_window', 65)
    min_rise = params.get('l5_min_rise', 0.50)
    big_bull_thresh = params.get('l5_big_bull_thresh', 0.50)

    # 预计算每个交易日的Low、Peak、peak_rise_pct、current_rise_pct、rise_days
    g_low_series, g_peak_series, g_peak_rise_pct, g_current_rise_pct, g_rise_days = \
        _calc_low_peak_rise(g_close, lookback_window)

    # 5日动量
    g_mom_5d = g_close / g_close.shift(5) - 1

    # 触发条件：持有成长 + 当前价从Low涨幅>min_rise + 5日动量转负
    growth_high_mom = (
        (dir_series == 'growth')
        & (g_current_rise_pct > min_rise)
        & (g_mom_5d < 0)
    )

    # L5锁定标记(用于文案区分"L5牛市止损锁定"与"弱信号空仓")
    l5_lock_flag = np.zeros(n, dtype=bool)

    # 顺序追踪成长锁定状态
    growth_locked_until = -1       # 锁定的结束索引（绝对index）
    growth_lock_start = -1         # 触发锁定的起始索引
    growth_trigger_peak = None     # 触发时的Peak价（新高解锁基准）
    growth_trigger_low = None      # 触发时的Low价
    growth_trigger_peak_rise_pct = None  # 触发时的Peak/Low涨幅（区分大小牛市阶梯）
    growth_base_lock = 0           # 基准锁定天数

    for i in range(n):
        current_price = g_close.iloc[i]

        # ===== 检查解锁 =====
        if i < growth_locked_until and growth_trigger_peak is not None:
            # 路径A: 新高解锁（试错机制）
            if current_price > growth_trigger_peak:
                growth_locked_until = -1
                growth_lock_start = -1
                growth_trigger_peak = None
                growth_trigger_low = None
                growth_trigger_peak_rise_pct = None
                growth_base_lock = 0

            else:
                # 路径B: 斐波那契阶梯解锁
                peak = growth_trigger_peak
                low = growth_trigger_low
                peak_rise_pct = growth_trigger_peak_rise_pct
                base_lock = growth_base_lock

                # 回撤深度 = (Peak - Current) / (Peak - Low)
                drawdown = (peak - current_price) / (peak - low) if peak != low else 0.0

                if peak_rise_pct > big_bull_thresh:
                    # === 超级大牛市(peak_rise_pct > 50%) ===
                    # 解锁仅两条路径: 新高试错(路径A)或时间到期。
                    # 原"回撤>=0.782减半"与"回撤>=0.9立即解锁"已删除:
                    #   深度回撤后的反弹快而不持久, 动量/DC确认时已处高位;
                    #   官方引擎证实锁满全程年化+0.78pct/Sharpe+0.038/回撤持平。
                    pass
                else:
                    # === 普通小牛市(30% < peak_rise_pct ≤ 50%) ===
                    if drawdown >= 0.618:
                        # 回撤>0.618 → 完全解锁
                        growth_locked_until = -1
                        growth_lock_start = -1
                        growth_trigger_peak = None
                        growth_trigger_low = None
                        growth_trigger_peak_rise_pct = None
                        growth_base_lock = 0
                    elif drawdown >= 0.382:
                        # 回撤>0.382 → 缩短一半（从触发日算起）
                        new_lock_end = growth_lock_start + int(base_lock * 0.50)
                        if new_lock_end < growth_locked_until:
                            growth_locked_until = new_lock_end

        # ===== 锁定中：方向为成长时强制空仓 =====
        if i < growth_locked_until:
            if dir_series.iloc[i] == 'growth':
                wt.iloc[i] = 0.0
                l5_lock_flag[i] = True

        # ===== 检查是否触发止损（仅当有仓位时才触发）=====
        if growth_high_mom.iloc[i] and wt.iloc[i] > 0.001:
            growth_trigger_peak = float(g_peak_series.iloc[i])
            growth_trigger_low = float(g_low_series.iloc[i])
            growth_trigger_peak_rise_pct = float(g_peak_rise_pct.iloc[i])
            rise_days = int(g_rise_days.iloc[i])
            growth_base_lock = min(rise_days, lookback_window)
            growth_lock_start = i
            growth_locked_until = i + growth_base_lock
            wt.iloc[i] = 0.0  # 当天清仓

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

    # ===== L11: 单腿急跌·轮动减仓 (RCD — Rotation-Confirmed De-risk) =====
    # 仅"资金轮动型"急跌减仓: 持有腿波动率标准化急跌 + 相对另一腿显著走弱 + 另一腿上涨。
    # 普跌型(两腿同跌)不干预, 交给L8清仓与V型反弹, 避免反弹中踏空。
    # 动作: 触发后 wt *= dr_scale(半仓); 持有腿止跌回升(dr_rec)或方向切换后回补。
    # 与 strategies/growth_value_rotation_b4v9/signal_generator_dct.py 同源。
    dr_mode = params.get('dr_mode', 'none')
    rcd_flag = np.zeros(n, dtype=bool)
    if dr_mode != 'none':
        dr_drop = params.get('dr_drop', 0.06)
        dr_z = params.get('dr_z', 1.5)
        dr_spread = params.get('dr_spread', 0.04)
        dr_other_up = params.get('dr_other_up', True)
        dr_scale = params.get('dr_scale', 0.5)
        dr_rec = params.get('dr_rec', 0.0)
        dr_rec_win = params.get('dr_rec_win', 5)
        dr_deep = params.get('dr_deep', False)
        dr_drop20 = params.get('dr_drop20', 0.12)
        dr_spread20 = params.get('dr_spread20', 0.05)
        dr_deep_scale = params.get('dr_deep_scale', 0.0)

        g_r5_dr = g_close / g_close.shift(5) - 1
        v_r5_dr = v_close / v_close.shift(5) - 1
        g_r20_dr = g_close / g_close.shift(20) - 1
        v_r20_dr = v_close / v_close.shift(20) - 1
        g_s20_dr = g_close.pct_change().rolling(20).std()
        v_s20_dr = v_close.pct_change().rolling(20).std()

        is_g = (dir_series == 'growth').values
        held_r5 = np.where(is_g, g_r5_dr.values, v_r5_dr.values)
        other_r5 = np.where(is_g, v_r5_dr.values, g_r5_dr.values)
        held_r20 = np.where(is_g, g_r20_dr.values, v_r20_dr.values)
        other_r20 = np.where(is_g, v_r20_dr.values, g_r20_dr.values)
        held_s20 = np.where(is_g, g_s20_dr.values, v_s20_dr.values)
        spread5 = held_r5 - other_r5
        spread20 = held_r20 - other_r20
        held_rec = held_r20 if dr_rec_win >= 20 else held_r5
        # 波动率标准化跌幅: 持有腿5日收益 / (20日已实现波动 × √5)
        z5 = held_r5 / (held_s20 * np.sqrt(5))

        if dr_mode == 'rot_z':
            drop_ok = z5 <= -dr_z
        else:
            drop_ok = held_r5 <= -dr_drop
        spread_ok = spread5 <= -dr_spread
        rot_ok = (other_r5 > 0) if dr_other_up else np.ones(n, dtype=bool)
        trig = np.nan_to_num(drop_ok & spread_ok & rot_ok).astype(bool)

        derisk = False
        prev_dir_dr = None
        for i in range(n):
            if dir_series.iloc[i] != prev_dir_dr:
                derisk = False
                prev_dir_dr = dir_series.iloc[i]
            if trig[i]:
                derisk = True
            elif derisk and held_rec[i] >= dr_rec:
                derisk = False
            if derisk and wt.iloc[i] > 0.001:
                sc = dr_scale
                if dr_deep and held_r20[i] <= -dr_drop20 and spread20[i] <= -dr_spread20:
                    sc = dr_deep_scale
                wt.iloc[i] = wt.iloc[i] * sc
                rcd_flag[i] = True

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
    df['l5_lock'] = l5_lock_flag
    df['aw_whipsaw'] = aw_whipsaw_flag
    df['rcd_derisk'] = rcd_flag
    return df


# ========== P&L 净值计算 ==========
def calc_nav(df, g_close, v_close):
    """计算策略模拟净值曲线（初始1万元，含交易成本）— T+1 收盘价成交

    执行口径：信号在 T 日收盘确认 → 于 T+1 日收盘价成交 → 自 T+2 日起按新仓位计收益。
    因此第 i 个交易日实际持有的仓位 = df.iloc[i-2]（由 T-1 日收盘成交确立），
    当日收益 = 收盘→收盘涨跌幅 × 该仓位；调仓成本在成交发生的当日计扣。
    """
    da = g_close.reindex(df.index).ffill()
    db = v_close.reindex(df.index).ffill()
    daily_g_ret = da.pct_change()
    daily_v_ret = db.pct_change()
    TRADE_COST = 0.0001    # 万1佣金
    IMPACT_COST = 0.0005   # 5bp冲击成本
    TOTAL_COST = TRADE_COST + IMPACT_COST  # 单边合计6bp
    INIT_CAP = 10000

    nav = pd.Series(INIT_CAP, index=df.index)
    strat_ret = pd.Series(0.0, index=df.index)
    for i in range(2, len(df)):
        ps = df.iloc[i - 2]                          # 当日实际持有的仓位
        prev_ps = df.iloc[i - 3] if i - 3 >= 0 else df.iloc[i - 2]  # 成交前原仓位
        cost = 0.0
        # 调仓判定：方向变化或仓位变化超过1%，扣双边成本（成交发生于收盘）
        if prev_ps['dir'] != ps['dir'] or abs(prev_ps['wt'] - ps['wt']) > 0.01:
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
PLOT_TRADING_DAYS = 126   # 附图展示窗口：约6个月的交易日（数据仍全量计算，仅附图展示近6个月）

def make_chart(df, rt=1.3, plot_days=PLOT_TRADING_DAYS):
    """3子图：GV Ratio+MA20 → T值 → 仓位（仅展示最近 plot_days 个交易日，默认约6个月）"""
    fig, axes = plt.subplots(3, 1, figsize=(12, 7),
                             gridspec_kw={'height_ratios': [1.6, 1.15, 1]})

    # 只取最近 plot_days 个交易日展示，计算仍基于全量历史
    plot_df = df.tail(plot_days)
    plot_dates = plot_df.index
    dates = plot_dates  # 仅展示最近 plot_days 个交易日

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
    ax.axhline(rt, color='#E74C3C', ls='--', lw=0.8, label=f'+rt={rt}')
    ax.axhline(-rt, color='#2ECC71', ls='--', lw=0.8, label=f'-rt={rt}')
    ax.fill_between(dates, rt, -rt, alpha=0.05, color='gray')
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
def make_html(signal, df, chart_b64, trades=None, params=None, nav_data=None, init_cap=10000):
    """生成手机友好的信号页面 HTML（含交互式净值走势图）"""
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

    version_info = (f'B4V9(指数同源·AW反打脸fw{p.get("aw_flip_window", 35)}+RCD轮动减仓) · '
                    f'成长100/价值100 · '
                    f'DC={p["dc"]} 容差={p.get("dc_tolerance", 0)} DCD={p["dcd"]} '
                    f'动量={p["momentum_period"]}日 窗口={p.get("l5_lookback_window", 65)}日')
    t60_mean = df['T'].tail(60).abs().mean()

    # --- 调仓提示（信号层：对比前一交易日信号，目标是否变化）---
    sig_prev = df[['dir', 'wt']].shift(1)
    actions = []
    for i in range(len(df)):
        cur = df[['dir', 'wt']].iloc[i]
        prev = sig_prev.iloc[i]
        if pd.isna(prev['wt']) or prev['dir'] != cur['dir'] or abs(prev['wt'] - cur['wt']) > 0.01:
            if cur['wt'] == 0:
                target = '卖出/空仓'
            elif cur['dir'] == 'growth':
                target = f'买入成长 {cur["wt"]*100:.0f}%'
            else:
                target = f'买入价值 {cur["wt"]*100:.0f}%'
            actions.append('⚠ ' + target)
        else:
            actions.append('持有')
    df = df.assign(action=actions)

    # --- 持仓明细（实际持仓线路：信号 T 收盘确认 → T+1 收盘成交 → T+2 起生效）---
    HOLD_LAG = 2   # 当日实际持仓 = 信号滞后2天
    eff = df[['dir', 'wt']].shift(HOLD_LAG).copy()
    eff['dir'] = eff['dir'].fillna('cash')
    eff['wt'] = eff['wt'].fillna(0.0)
    _idx = df.index
    segs = []
    s_i, s_d, s_w = 0, eff['dir'].iloc[0], eff['wt'].iloc[0]
    for i in range(1, len(eff)):
        _d, _w = eff['dir'].iloc[i], eff['wt'].iloc[i]
        if _d != s_d or abs(_w - s_w) > 0.01:
            segs.append((s_i, i - 1, s_d, s_w))
            s_i, s_d, s_w = i, _d, _w
    segs.append((s_i, len(eff) - 1, s_d, s_w))
    pos_rows = []
    for _a, _b, _d, _w in segs:
        sig_date = f'{_idx[max(0, _a - HOLD_LAG)].strftime("%m-%d")}(信号)' if _a >= HOLD_LAG else '—(起点)'
        exec_date = f'{_idx[max(0, _a - 1)].strftime("%m-%d")}收盘' if _a >= 1 else '—'
        start_date = _idx[_a].strftime('%m-%d')
        end_date = _idx[_b].strftime('%m-%d')
        if _w == 0:
            label = '空仓'
        elif _d == 'growth':
            label = f'成长 {_w*100:.0f}%'
        else:
            label = f'价值 {_w*100:.0f}%'
        pos_rows.append({
            'sig': sig_date, 'exec': exec_date,
            'start': start_date, 'end': end_date,
            'label': label, 'dir': _d, 'wt': _w,
            'cur': '当前' if _b == len(eff) - 1 else '',
        })
    pos_rows = pos_rows[::-1][:10]

    # 最近10天表格
    last10 = df.tail(10)[['ratio', 'ma20', 'T', 'slope', 'dir', 'wt', 'action']].copy()
    last10.index = [d.strftime('%m-%d') for d in last10.index]
    last10['wt_label'] = last10['wt'].apply(
        lambda w: '空仓' if w == 0 else f'{w * 100:.0f}%')

    # 序列化NAV数据供Chart.js使用
    nav_json = json.dumps(nav_data) if nav_data else '[]'

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>B4V9 · 每日信号 — Panda AI</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js"></script>
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

  .chart-card {{ background: white; border-radius: 16px; padding: 16px;
    margin-bottom: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  .chart-card .title {{ font-size: 13px; font-weight: 600; margin-bottom: 10px;
    color: #7f8c8d; }}
  .chart-card .chart-container {{ position: relative; width: 100%; height: 350px; }}
  .chart-toolbar {{ display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; align-items: center; }}
  .chart-toolbar button {{ padding: 6px 14px; border: 1px solid #ddd; border-radius: 8px;
    background: white; font-size: 12px; cursor: pointer; color: #555;
    transition: all 0.15s; }}
  .chart-toolbar button:hover {{ background: #f0f0f0; border-color: #bbb; }}
  .chart-toolbar button.active {{ background: #e8f0fe; border-color: #1A73E8; color: #1A73E8; }}
  .chart-toolbar .hint {{ font-size: 11px; color: #999; margin-left: auto; align-self: center; }}
  .navigator-container {{ position: relative; width: 100%; height: 60px; margin-top: 4px; }}
  .range-info {{ font-size: 11px; color: #888; text-align: center; margin-top: 4px; }}

  .grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 12px; }}
  .stat-card {{ background: white; border-radius: 12px; padding: 14px 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }}
  .stat-card .num {{ font-size: 26px; font-weight: 700; color: #2c3e50; }}
  .stat-card .lbl {{ font-size: 11px; color: #95a5a6; margin-top: 2px; }}

  .advice {{ border-radius: 16px; padding: 14px 18px; margin-bottom: 12px; color: #fff;
    box-shadow: 0 6px 22px rgba(0,0,0,0.20); position: relative; overflow: hidden; }}
  .advice::before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 6px;
    background: rgba(255,255,255,0.35); }}
  .advice .adv-title {{ font-size: 12px; opacity: .9; font-weight: 700; letter-spacing: 2px; }}
  .advice .adv-text {{ font-size: 24px; font-weight: 800; margin-top: 6px; line-height: 1.35; }}
  .advice .adv-sub {{ font-size: 12px; opacity: .85; margin-top: 6px; }}
  .advice.cash {{ background: linear-gradient(135deg, #546E7A, #37474F); }}
  .advice.growth {{ background: linear-gradient(135deg, #F6A821, #E8890C); }}
  .advice.value {{ background: linear-gradient(135deg, #43A047, #2E7D32); }}

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
    .grid {{ grid-template-columns: 1fr 1fr 1fr; }}
    .header h1 {{ font-size: 15px; }}
    body {{ padding: 8px; }}
    .advice .adv-text {{ font-size: 19px; }}
    .chart-card .chart-container {{ height: 260px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>⚡ B4V9 · Panda AI</h1>
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
        <div class="row"><span class="label">信号基准</span>
          <span class="value">{latest.get('basis', latest['target'])}</span></div>
        <div class="row"><span class="label">实盘ETF</span>
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
          <span class="value">B4V9 (AW反打脸fw35 + RCD轮动减仓 + L5锁满全程 + DC容差)</span></div>
      </div>
    </div>
  </div>

  <!-- 交互式净值走势图 -->
  <div class="chart-card">
    <div class="title">📈 策略模拟净值走势</div>
    <div class="chart-toolbar">
      <button onclick="setRange('1m')" id="btn-1m">1月</button>
      <button onclick="setRange('3m')" id="btn-3m">3月</button>
      <button onclick="setRange('ytd')" id="btn-ytd">年初至今</button>
      <button onclick="setRange('all')" id="btn-all" class="active">全部</button>
      <span class="hint">🖱 滚轮放大 · 拖拽平移</span>
    </div>
    <div class="chart-container">
      <canvas id="navChart"></canvas>
    </div>
    <div class="navigator-container">
      <canvas id="navMiniChart"></canvas>
    </div>
    <div class="range-info" id="rangeInfo"></div>
  </div>

  <!-- 实盘净值走势（从 2026-08-04 实盘日起，本金10万） -->
  <div class="chart-card">
    <div class="title">🔵 实盘净值走势（2026-08-04 起 · 本金¥100,000）</div>
    <div class="chart-toolbar">
      <button onclick="liveSetRange('1m')" id="lbtn-1m">1月</button>
      <button onclick="liveSetRange('3m')" id="lbtn-3m">3月</button>
      <button onclick="liveSetRange('ytd')" id="lbtn-ytd">年初至今</button>
      <button onclick="liveSetRange('all')" id="lbtn-all" class="active">全部</button>
    </div>
    <div class="chart-container" style="height: 320px;">
      <canvas id="liveChart"></canvas>
    </div>
  </div>

  <div class="charts">
    <img src="data:image/png;base64,{chart_b64}" alt="B4V9信号图表">
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
      <div class="num" style="color:{'#E74C3C' if latest.get('pnl_pct',0) >= 0 else '#2ECC71'};font-size:20px">
        {latest.get('nav', 0):.0f}</div>
      <div class="lbl">模拟净值(¥) 累计{latest.get('pnl_pct', 0):+.2f}%</div>
      <div class="lbl" style="color:{'#E74C3C' if latest.get('ann_ret',0) >= 0 else '#2ECC71'};font-weight:700">
        年化{latest.get('ann_ret', 0)*100:+.2f}%</div>
    </div>
    <div class="stat-card">
      <div class="num" style="font-size:22px">{latest.get('latest_date','')}</div>
      <div class="lbl">最新交易日</div>
    </div>
  </div>

  <div class="advice {'cash' if latest['wt']==0 else latest['dir']}">
    <div class="adv-title">📢 今日信号 · 明日待操作</div>
    <div class="adv-text">{'🔒 L5牛市止损锁定' if latest['wt']==0 and latest.get('l5_lock') else ('⚠️ 空仓观望' if latest['wt']==0 else ('🚀 做多成长' if latest['dir']=='growth' else '🛡 做多价值'))}</div>
    <div class="adv-sub">{latest['date'].strftime('%m-%d')} 收盘确认 · 明日(下一交易日)收盘调仓：{latest['advice']}</div>
  </div>

  <div class="table-card">
    <div class="title">📋 最近10天信号明细<span style="font-size:11px;color:#95a5a6;font-weight:400"> · 信号当日收盘确认，下一交易日收盘调仓</span></div>
    <table>
      <tr><th>信号日</th><th>比价</th><th>T值</th><th>方向</th><th>目标仓位</th><th>调仓提示</th></tr>
      {''.join(f'<tr><td>{d}</td><td>{r["ratio"]:.3f}</td>'
        f'<td>{r["T"]:+.2f}</td>'
        f'<td><span class="tag tag-{r["dir"]}">{"成长" if r["dir"]=="growth" else "价值" if r["dir"]=="value" else "空仓"}</span></td>'
        f'<td>{r["wt_label"]}</td>'
        f'<td><span style="font-size:11px;color:{"#C0392B" if r["action"].startswith("⚠") else "#7f8c8d"}">{r["action"]}</span></td></tr>'
        for d, r in last10.iterrows())}
    </table>
  </div>

  <div class="table-card">
    <div class="title">📋 持仓明细（最近10段，最新在前）<span style="font-size:11px;color:#95a5a6;font-weight:400"> · 信号产生 → T+1收盘成交 → T+2起生效</span></div>
    <table>
      <tr><th>持仓</th><th>信号产生</th><th>收盘调仓</th><th>持仓区间</th><th></th></tr>
      {''.join(f'<tr><td><span class="tag tag-{p["dir"]}">{p["label"]}</span></td>'
        f'<td>{p["sig"]}</td><td>{p["exec"]}</td>'
        f'<td>{p["start"]} → {p["end"]}</td>'
        f'<td><span style="color:#C0392B;font-size:11px;font-weight:600">{p["cur"]}</span></td></tr>'
        for p in pos_rows)}
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
    B4V9 · 成长价值轮动策略（L5锁满全程 · AW反打脸 · RCD轮动减仓 · 指数同源）<br>
    信号基准: 成长100(480080)/价值100(480081) · 收益按指数收盘价计<br>
    实盘通道: 易方达成长ETF(159259)/易方达价值ETF(159263)<br>
    参数: DC={p["dc"]} 容差={p.get("dc_tolerance", 0)} DCD={p["dcd"]} rt={p["rt"]} BIAS={p["bias_high"]} 动量={p["momentum_period"]}日<br>
    第10层 AW: 反打脸保护(近{p.get("aw_flip_window", 35)}日T翻转≥{p.get("aw_flip_thresh", 5)}次判震荡, 确认{p["dc"]}→{p.get("aw_dc_strict", 7)}天) · 第11层 RCD: 轮动型急跌减仓至{p.get("dr_scale", 0.5)*100:.0f}%<br>
    第5层: 牛市止损(超级牛市锁满全程; 新高试错或时间到期解锁) · 第8层: 黑天鹅保护(固定10日动量解锁)<br>
    数据来源: cnindex.com.cn · 自动生成于 {beijing_now().strftime('%Y-%m-%d %H:%M')}<br>
    ⚠️ 仅供参考，不构成投资建议
  </div>
</div>

<script>
// 注册缩放插件
Chart.register(ChartZoom);

const navData = {nav_json};
const baseVal = {init_cap};
const liveDate = '2026-08-04';  // 实盘开始日：此日起线条为实盘(蓝)，此前为模拟盘(红)
const totalPoints = navData.length;

// 数据准备：用 x/y 对象格式，适配 linear x轴
const dataPoints = navData.map((d, i) => ({{ x: i, y: d.value }}));
const baseLine = navData.map((d, i) => ({{ x: i, y: baseVal }}));
const labels = navData.map(d => d.date);
const values = navData.map(d => d.value);

// Y轴自动适配可见数据
function getYRange(startIdx, endIdx) {{
  const visible = values.slice(startIdx, endIdx + 1);
  const min = Math.min(...visible);
  const max = Math.max(...visible);
  const pad = (max - min) * 0.08 || 100;
  return {{ min: Math.floor((min - pad) / 100) * 100, max: Math.ceil((max + pad) / 100) * 100 }};
}}

// 主图
const mainCtx = document.getElementById('navChart').getContext('2d');

const mainChart = new Chart(mainCtx, {{
  type: 'line',
  data: {{
    datasets: [{{
      label: '策略净值',
      data: dataPoints,
      borderColor: '#E74C3C',
      backgroundColor: function(ctx) {{
        const chart = ctx.chart;
        const {{ctx: c, chartArea}} = chart;
        if (!chartArea) return 'rgba(231,76,60,0.15)';
        const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
        g.addColorStop(0, 'rgba(231,76,60,0.25)');
        g.addColorStop(1, 'rgba(231,76,60,0.01)');
        return g;
      }},
      fill: true,
      tension: 0.1,
      pointRadius: 0,
      borderWidth: 2,
      parsing: false,
    }}, {{
      label: '初始资金',
      data: baseLine,
      borderColor: 'rgba(0,0,0,0.25)',
      borderDash: [4,4],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
      parsing: false,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    animation: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: 'rgba(0,0,0,0.8)',
        titleFont: {{ size: 12 }},
        bodyFont: {{ size: 13 }},
        padding: 10,
        cornerRadius: 8,
        callbacks: {{
          title: function(items) {{
            const idx = Math.round(items[0].parsed.x);
            return labels[idx] || '';
          }},
          label: function(ctx) {{
            if (ctx.datasetIndex === 0) {{
              const val = ctx.parsed.y;
              const pnl = ((val / baseVal) - 1) * 100;
              return `净值: ¥${{val.toFixed(0)}}  (${{pnl>=0?'+':''}}${{pnl.toFixed(2)}}%)`;
            }}
            return `¥${{ctx.parsed.y.toFixed(0)}}`;
          }}
        }}
      }},
      zoom: {{
        pan: {{ enabled: true, mode: 'x', threshold: 5 }},
        zoom: {{
          wheel: {{ enabled: true }},
          pinch: {{ enabled: true }},
          mode: 'x',
        }},
        limits: {{
          x: {{ minRange: 5, min: 0, max: totalPoints - 1 }}
        }}
      }}
    }},
    scales: {{
      x: {{
        type: 'linear',
        min: 0,
        max: totalPoints - 1,
        ticks: {{
          maxTicksLimit: 10,
          font: {{ size: 11 }},
          callback: function(val) {{
            const i = Math.round(val);
            if (i >= 0 && i < labels.length) return labels[i].slice(5);
            return '';
          }}
        }},
        grid: {{ display: false }},
      }},
      y: {{
        ticks: {{ font: {{ size: 11 }}, callback: v => '¥' + v.toFixed(0) }},
        grid: {{ color: 'rgba(0,0,0,0.06)' }},
      }}
    }},
  }},
  plugins: [ChartZoom]
}});

// 选区状态（迷你导航图选区遮罩 + 手柄；须在 miniChart 构造前定义，插件经配置传入）
let selStart = 0, selEnd = totalPoints - 1;
const selectionPlugin = {{
  id: 'selectionOverlay',
  afterDraw(chart) {{
    const {{ctx, chartArea, scales}} = chart;
    if (!chartArea) return;
    const xScale = scales.x;
    const left = xScale.getPixelForValue(selStart);
    const right = xScale.getPixelForValue(selEnd);
    ctx.save();
    ctx.fillStyle = 'rgba(0,0,0,0.18)';
    ctx.fillRect(chartArea.left, chartArea.top, left - chartArea.left, chartArea.bottom - chartArea.top);
    ctx.fillRect(right, chartArea.top, chartArea.right - right, chartArea.bottom - chartArea.top);
    ctx.strokeStyle = '#E74C3C';
    ctx.lineWidth = 2;
    ctx.strokeRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
    ctx.fillStyle = '#E74C3C';
    const hw = 4;
    ctx.fillRect(left - hw/2, chartArea.top, hw, chartArea.bottom - chartArea.top);
    ctx.fillRect(right - hw/2, chartArea.top, hw, chartArea.bottom - chartArea.top);
    ctx.restore();
  }}
}};

// 迷你导航图
const miniCtx = document.getElementById('navMiniChart').getContext('2d');
const miniChart = new Chart(miniCtx, {{
  type: 'line',
  data: {{
    datasets: [{{
      data: dataPoints,
      borderColor: '#ccc',
      backgroundColor: 'rgba(231,76,60,0.1)',
      fill: true,
      tension: 0,
      pointRadius: 0,
      borderWidth: 1,
      parsing: false,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: false }} }},
    scales: {{
      x: {{ type: 'linear', display: false, min: 0, max: totalPoints - 1 }},
      y: {{ display: false }}
    }},
    elements: {{ point: {{ radius: 0 }} }}
  }},
  plugins: [selectionPlugin]
}});

// ===== 实盘净值走势图（从 2026-08-04 实盘日起，起始本金10万，蓝线）=====
const liveIdx0 = navData.findIndex(d => d.date >= liveDate);  // 实盘起始数据索引(8/4)
const liveBaseNav = navData[liveIdx0].value;  // 8/4 原模拟净值，作为折算比例基准
const liveBase = 100000;                       // 实盘起始本金 ¥100,000
const liveData = navData.slice(liveIdx0)
  .map((d, i) => ({{ x: i, y: d.value / liveBaseNav * liveBase }}));
const liveRet = liveData.length ? (liveData[liveData.length - 1].y / liveBase - 1) * 100 : 0;
// 注意：liveData 经 map 后只保留 x/y，丢失 date；日期标签须从原始 navData 取
const liveLabels = navData.slice(liveIdx0).map(d => d.date);
const liveLen = liveData.length;

const liveCtx = document.getElementById('liveChart').getContext('2d');
const liveChart = new Chart(liveCtx, {{
  type: 'line',
  data: {{
    datasets: [{{
      label: '实盘净值',
      data: liveData,
      borderColor: '#3498DB',
      backgroundColor: function(ctx) {{
        const chart = ctx.chart;
        const {{ctx: c, chartArea}} = chart;
        if (!chartArea) return 'rgba(52,152,219,0.15)';
        const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
        g.addColorStop(0, 'rgba(52,152,219,0.25)');
        g.addColorStop(1, 'rgba(52,152,219,0.01)');
        return g;
      }},
      fill: true,
      tension: 0.1,
      pointRadius: 0,
      borderWidth: 2,
      parsing: false,
    }}, {{
      label: '实盘起始',
      data: liveData.map((p) => ({{ x: p.x, y: liveBase }})),
      borderColor: 'rgba(0,0,0,0.25)',
      borderDash: [4, 4],
      pointRadius: 0,
      borderWidth: 1,
      parsing: false,
    }}],
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    scales: {{
      x: {{
        type: 'linear',
        min: 0,
        max: liveLen - 1,
        ticks: {{
          color: '#888',
          maxTicksLimit: 8,
          callback: function(val, i) {{
            const idx = Math.round(val);
            const lab = liveLabels[idx];
            return lab ? lab.slice(5) : '';
          }},
        }},
        grid: {{ display: false }},
      }},
      y: {{
        ticks: {{ color: '#888' }},
        grid: {{ color: 'rgba(0,0,0,0.05)' }},
      }},
    }},
    plugins: {{
      legend: {{ display: true, position: 'top', labels: {{ color: '#555' }} }},
      tooltip: {{
        callbacks: {{
          title: function(items) {{
            const idx = Math.round(items[0].parsed.x);
            return liveLabels[idx] || '';
          }},
          label: function(item) {{
            return '净值: ¥' + Number(item.parsed.y).toLocaleString();
          }},
        }},
      }},
    }},
  }}
}});

// 实盘图范围切换（1月/3月/年初/全部）
function liveSetRange(rang) {{
  document.querySelectorAll('[id^="lbtn-"]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('lbtn-' + rang);
  if (btn) btn.classList.add('active');
  let x0 = 0, x1 = liveLen - 1;
  if (rang === '1m') x0 = Math.max(0, liveLen - 22);
  else if (rang === '3m') x0 = Math.max(0, liveLen - 66);
  else if (rang === 'ytd') x0 = 0;  // 实盘始于8/4，年初≈全部
  liveChart.options.scales.x.min = x0;
  liveChart.options.scales.x.max = x1;
  liveChart.update('none');
}}

// 更新主图显示范围
function applyRange(startIdx, endIdx, clearBtns) {{
  startIdx = Math.max(0, Math.floor(startIdx));
  endIdx = Math.min(totalPoints - 1, Math.ceil(endIdx));
  if (endIdx - startIdx < 5) endIdx = Math.min(totalPoints - 1, startIdx + 5);
  selStart = startIdx;
  selEnd = endIdx;

  const yr = getYRange(startIdx, endIdx);
  mainChart.options.scales.x.min = startIdx;
  mainChart.options.scales.x.max = endIdx;
  mainChart.options.scales.y.min = yr.min;
  mainChart.options.scales.y.max = yr.max;
  mainChart.update('none');
  miniChart.update('none');

  const sd = labels[startIdx], ed = labels[endIdx];
  const sv = values[startIdx], ev = values[endIdx];
  const pnl = ((ev / sv) - 1) * 100;
  document.getElementById('rangeInfo').textContent =
    `${{sd}} → ${{ed}}  |  区间收益: ${{pnl>=0?'+':''}}${{pnl.toFixed(2)}}%  |  ¥${{sv.toFixed(0)}} → ¥${{ev.toFixed(0)}}`;

  if (clearBtns) {{
    document.querySelectorAll('.chart-toolbar button').forEach(b => b.classList.remove('active'));
  }}
}}

// 快捷范围
function setRange(mode) {{
  document.querySelectorAll('.chart-toolbar button').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + mode).classList.add('active');

  let start = 0;
  const end = totalPoints - 1;
  if (mode === '1m') {{
    start = Math.max(0, end - 22);
  }} else if (mode === '3m') {{
    start = Math.max(0, end - 66);
  }} else if (mode === 'ytd') {{
    const ty = labels[end].slice(0, 4);
    const ys = labels.findIndex(d => d.startsWith(ty + '-01'));
    start = ys >= 0 ? ys : 0;
  }}
  applyRange(start, end, false);
}}

// 迷你图拖拽
let isDragging = false, dragMode = null, dragStartIdx = 0, dragSelS = 0, dragSelE = 0;
const miniCanvas = miniCtx.canvas;

function idxFromEvent(e) {{
  const rect = miniCanvas.getBoundingClientRect();
  const x = (e.clientX ?? e.touches[0].clientX) - rect.left;
  const ca = miniChart.chartArea;
  if (!ca) return null;
  const ratio = (x - ca.left) / (ca.right - ca.left);
  return Math.max(0, Math.min(totalPoints - 1, Math.round(ratio * (totalPoints - 1))));
}}

miniCanvas.addEventListener('mousedown', function(e) {{
  const idx = idxFromEvent(e);
  if (idx === null) return;
  const hs = Math.max(5, Math.round((selEnd - selStart) * 0.03));
  if (Math.abs(idx - selStart) <= hs) {{
    isDragging = true; dragMode = 'left';
  }} else if (Math.abs(idx - selEnd) <= hs) {{
    isDragging = true; dragMode = 'right';
  }} else if (idx > selStart && idx < selEnd) {{
    isDragging = true; dragMode = 'pan';
    dragStartIdx = idx; dragSelS = selStart; dragSelE = selEnd;
  }} else {{
    const span = selEnd - selStart;
    let ns = idx - Math.floor(span / 2);
    applyRange(ns, ns + span, true);
    isDragging = true; dragMode = 'pan';
    dragStartIdx = idx; dragSelS = Math.max(0, ns); dragSelE = Math.min(totalPoints-1, ns + span);
    return;
  }}
  miniCanvas.style.cursor = 'grabbing';
}});

document.addEventListener('mousemove', function(e) {{
  if (!isDragging) return;
  const idx = idxFromEvent(e);
  if (idx === null) return;
  if (dragMode === 'left') {{
    applyRange(idx, selEnd, true);
  }} else if (dragMode === 'right') {{
    applyRange(selStart, idx, true);
  }} else if (dragMode === 'pan') {{
    const dx = idx - dragStartIdx;
    let ns = dragSelS + dx, ne = dragSelE + dx;
    if (ns < 0) {{ ns = 0; ne = dragSelE - dragSelS; }}
    if (ne > totalPoints - 1) {{ ne = totalPoints - 1; ns = ne - (dragSelE - dragSelS); }}
    applyRange(ns, ne, true);
  }}
}});

document.addEventListener('mouseup', function() {{
  isDragging = false; dragMode = null;
  miniCanvas.style.cursor = 'crosshair';
}});
miniCanvas.style.cursor = 'crosshair';

// 滚轮缩放后同步选区和Y轴
mainChart.options.plugins.zoom.zoom.onZoomComplete = function({{chart}}) {{
  const ns = Math.round(chart.scales.x.min);
  const ne = Math.round(chart.scales.x.max);
  selStart = ns; selEnd = ne;
  const yr = getYRange(ns, ne);
  chart.options.scales.y.min = yr.min;
  chart.options.scales.y.max = yr.max;
  chart.update('none');
  miniChart.update('none');
  const sd = labels[ns], ed = labels[ne];
  const sv = values[ns], ev = values[ne];
  const pnl = ((ev / sv) - 1) * 100;
  document.getElementById('rangeInfo').textContent =
    `${{sd}} → ${{ed}}  |  区间收益: ${{pnl>=0?'+':''}}${{pnl.toFixed(2)}}%  |  ¥${{sv.toFixed(0)}} → ¥${{ev.toFixed(0)}}`;
  document.querySelectorAll('.chart-toolbar button').forEach(b => b.classList.remove('active'));
}};

// 初始化
applyRange(0, totalPoints - 1, false);
document.getElementById('btn-all').classList.add('active');
</script>
</body>
</html>'''


# ========== 主流程 ==========
def main():
    parser = argparse.ArgumentParser(description='B4V9 每日信号生成器 — Panda AI')
    parser.add_argument('--out-dir', type=str, default=None,
                        help='输出目录，默认 signal_page_b4/docs/')
    args = parser.parse_args()

    print('=' * 50)
    print('  ⚡ B4V9 (AW反打脸 + RCD轮动减仓) 每日信号生成器')
    print(f'  {beijing_now().strftime("%Y-%m-%d %H:%M")}')
    print(f'  策略版本: B4V9 (B4V8默认基线 + aw_flip_window 20→35)')
    print(f'  参数: DC={B4_PARAMS["dc"]} DCD={B4_PARAMS["dcd"]} '
          f'rt={B4_PARAMS["rt"]} 动量={B4_PARAMS["momentum_period"]}日 '
          f'AW={B4_PARAMS.get("aw_mode")}/fw{B4_PARAMS.get("aw_flip_window")} '
          f'RCD={B4_PARAMS.get("dr_mode")}/z{B4_PARAMS.get("dr_z")}')
    print('=' * 50)

    # 1. 获取数据（cnindex官网指数，本地缓存，每天仅增量拉取）
    print('\n📡 获取指数行情（cnindex官网）...')
    g = fetch_index_with_cache(CODE_G, NAME_G)
    v = fetch_index_with_cache(CODE_V, NAME_V)
    common = g.index.intersection(v.index)
    g, v = g[common], v[common]
    print(f'  共同交易日: {len(g)}天 ({g.index[0].date()} ~ {g.index[-1].date()})')

    # 2. 计算信号（B4V9：B4V4 6层 + 第10层AW + 第11层RCD）
    print('\n🧮 计算信号 (B4V9 AW+RCD 逻辑)...')
    df = compute_signals(g, v, B4_PARAMS)

    # 2.5 计算P&L净值（与官方引擎同源：基于指数收盘价）
    print('\n💰 计算策略模拟净值...')
    nav, INIT_CAP = calc_nav(df, g, v)
    current_nav = nav.iloc[-1]
    pnl_pct = (current_nav / INIT_CAP - 1) * 100
    years = max((df.index[-1] - df.index[0]).days / 365.25, 0.1)
    ann_ret = (current_nav / INIT_CAP) ** (1 / years) - 1 if current_nav > 0 else 0.0
    print(f'  初始资金: {INIT_CAP}  →  当前: {current_nav:.2f}  ({pnl_pct:+.2f}%)'
          f'  年化: {ann_ret * 100:.2f}%  ({years:.1f}年)')

    # 3. 最新信号
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    dir_now = last['dir'] if pd.notna(last['dir']) else 'cash'
    dir_prev = prev['dir'] if pd.notna(prev['dir']) else 'cash'
    wt_now = last['wt']
    wt_prev = prev['wt']

    # 信号基于指数，交易建议给出对应的实盘ETF
    if dir_now == 'growth':
        basis_name = f'{NAME_G}({CODE_G})'
        target = f'{NAME_G}({CODE_G}) · {GROWTH_ETF_NAME}({GROWTH_ETF})'
        etf_follow = f'{GROWTH_ETF_NAME}({GROWTH_ETF})'
    elif dir_now == 'value':
        basis_name = f'{NAME_V}({CODE_V})'
        target = f'{NAME_V}({CODE_V}) · {VALUE_ETF_NAME}({VALUE_ETF})'
        etf_follow = f'{VALUE_ETF_NAME}({VALUE_ETF})'
    else:
        basis_name = '空仓'
        target = '空仓'
        etf_follow = '空仓'

    if wt_now == 0:
        if df['l5_lock'].iloc[-1]:
            advice = 'L5牛市止损锁定中 — 超级牛市见顶后的锁定期内禁止回买成长，仅允许持有价值或现金，锁定到期或创新高后自动解锁'
        else:
            advice = '空仓观望 — 多空信号不明确，等待趋势明朗'
    elif dir_now != dir_prev:
        old_etf = f'{GROWTH_ETF_NAME}({GROWTH_ETF})' if dir_prev == 'growth' else (
            f'{VALUE_ETF_NAME}({VALUE_ETF})' if dir_prev == 'value' else '空仓')
        advice = f'切换：卖出 {old_etf}，买入 {etf_follow}，仓位 {wt_now * 100:.0f}%'
    elif wt_now < wt_prev:
        advice = f'降仓：{etf_follow} 从 {wt_prev * 100:.0f}% 降低至 {wt_now * 100:.0f}%'
    else:
        hold_text = '买入' if wt_now >= 1.0 else '持有'
        advice = f'{hold_text} {etf_follow}，仓位 {wt_now * 100:.0f}%'

    # 4. 调仓记录
    trades = []
    dir_map = {'growth': NAME_G, 'value': NAME_V, 'cash': '空仓'}
    for i in range(1, len(df)):
        ps = df.iloc[i - 1]
        cs = df.iloc[i]
        if ps['dir'] != cs['dir'] or abs(ps['wt'] - cs['wt']) > 0.01:
            action = '切换' if ps['dir'] != cs['dir'] else '调仓'
            # 信号在T日收盘后确认，T+1日执行调仓
            trade_date = df.index[i + 1] if i + 1 < len(df) else df.index[i]
            trades.append({
                'date': trade_date.strftime('%Y-%m-%d'),
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
        'l5_lock': bool(last.get('l5_lock', False)),
        'target': target,
        'basis': basis_name,
        'advice': advice,
        'nav': current_nav,
        'pnl_pct': pnl_pct,
        'ann_ret': ann_ret,
        'init_cap': INIT_CAP,
        'latest_date': df.index[-1].strftime('%m-%d'),
    }

    print(f'\n  📊 最新信号: {latest["dir"]}  仓位: {latest["wt"] * 100:.0f}%  '
          f'T={last["T"]:+.3f}  NAV={current_nav:.0f}')

    # 5. 生成图表（含净值曲线）
    print('\n📈 生成信号图表（含净值曲线）...')
    chart_b64 = make_chart(df, B4_PARAMS['rt'])

    # 序列化NAV数据供交互式净值走势图（展示全量历史）
    nav_list = [{'date': d.strftime('%Y-%m-%d'), 'value': float(v)} for d, v in nav.items()]

    # 6. 生成HTML
    print('\n📄 生成HTML信号页面...')
    html = make_html(latest, df, chart_b64, trades, B4_PARAMS,
                     nav_data=nav_list, init_cap=INIT_CAP)

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
        'strategy': 'B4V9 (AW反打脸fw35 + RCD轮动减仓 + L5锁满全程 · 指数同源)',
        'params': B4_PARAMS,
        'latest_signal': {
            'date': df.index[-1].strftime('%Y-%m-%d'),
            'direction': dir_now,
            'position_pct': float(wt_now * 100),
            'basis_index': basis_name,
            'target_etf': target,
            'T_value': float(last['T']),
            'slope': float(last['slope']),
            'deviation_pct': float(last['dev'] * 100),
            'l5_lock': bool(last.get('l5_lock', False)),
            'nav': float(current_nav),
            'pnl_pct': float(pnl_pct),
            'ann_ret': float(ann_ret),
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
        'data_source': DATA_SOURCE_LOG,
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
