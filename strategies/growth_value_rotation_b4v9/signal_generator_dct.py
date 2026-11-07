"""B4V9 成长价值轮动策略 — 信号生成器（B4V8 默认基线 + aw_flip_window 20→35）

B4V9 = B4V8 默认配置 + 唯一参数改动 aw_flip_window: 20 → 35（AW 震荡识别窗口加长）。
  信号逻辑与 B4V8 逐行一致，唯一差异是震荡态检测窗口由近20日改为近35日，
  震荡判定更严格：只有真正高频打脸的市场才触发 slow_dc 加严（dc=5→7）。
  官方引擎: 年化 45.77% / 最大回撤 -22.03% / Calmar 2.08（vs fw20 的 45.48%/-24.18%/1.88）。

B4V8 = B4V7 + 第14层「震荡不明朗·半仓」(CHOP)【默认关闭】
  - 新增第14层 CHOP: 当翻转次数/效率比/比价偏离波动三指标同时处于危险分位
    (动量不可用·震荡不明朗)时, 将总仓位缩放到 chop_scale(默认0.5);
    不改方向、不改持仓腿。chop_mode='none' 时与 B4V7 逐字节一致(零改动)。

B4V5 = B4V4 + 第10层「反打脸保护」(AW)

B4V5 改进点（相对B4V4）：
  - 新增第10层 AW: 当T值高频上下翻转(震荡态)时提高换向确认门槛, 削减"来回切"
    的低质量换向。默认 slow_dc: 震荡态内确认天数 dc→dc+2。
    (官方引擎: 45.31%/Sharpe1.792/回撤-24.18% vs B4V4的44.18%/1.745/-24.24%;
     2026-08~10打脸窗口 -16.3%→-13.2%; 方向锚定类变体则降收益, 已弃用)
  - aw_mode='none' 时与 B4V4 逐字节一致(零改动)。

B4V4 = B4V3 + L5超级牛市锁满全程（移除深度回撤提前解锁）

改进点（相对B4V3）：
  - L5超级牛市(涨幅>50%)触发锁定后锁满全程, 移除"回撤>=0.782减半"提前解锁。
    依据: 深度回撤后的反弹快而不持久, 动量/DC确认时已处高位
    (实证: 2026-06-26触发L5, 旧版07-30回撤0.872减半→8/10解锁→8/18
    买回成长后跌9.4%; 官方引擎: 锁满全程年化46.51%/Sharpe1.844 vs 减半45.73%/1.806)
  - 移除"回撤>=0.9立即解锁"(B4V3即已删除: 13年从未触发的死代码)

继承自B4V3：
  - 收盘价撮合（matching_type=0）：读昨日信号，今日收盘价成交
  - DC容差机制（dc_tolerance=0.09）：连续dc天中允许1天反向但|T|<=0.09
  - DCD从6天优化至5天

共7层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致才确认
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第5层 牛市止损 — 超级牛市锁满全程; 新高试错或时间到期解锁
  第8层 黑天鹅保护 — 固定10日动量解锁
  第10层 反打脸保护(AW) — 震荡态(高T翻转频率)下抑制逆慢锚换向

B4V5 = B4V4 + 第10层反打脸保护(AW)。aw_mode 默认 'none' 时行为与 B4V4 完全一致。
"""
import pandas as pd
import numpy as np
from pathlib import Path

# L5事件日志(仅当params传l5_debug=True时填充; 研究诊断用)
L5_DEBUG_LOG = []

from params import (
    GROWTH_SYMBOL, VALUE_SYMBOL,
    BASELINE_PARAMS, INDEX_DATA_DIR,
)


def load_index_data(data_dir=None):
    """加载成长100和价值100指数日线数据

    Args:
        data_dir: 数据目录，默认使用 params.INDEX_DATA_DIR

    Returns:
        g_close: 成长100收盘价 Series（index=date）
        v_close: 价值100收盘价 Series（index=date）
    """
    data_dir = Path(data_dir or INDEX_DATA_DIR)
    g = pd.read_csv(data_dir / 'index_480080.csv')
    v = pd.read_csv(data_dir / 'index_480081.csv')

    for d in (g, v):
        d['date'] = pd.to_datetime(d['date'])
        d['close'] = pd.to_numeric(d['close'], errors='coerce')

    g_close = g.set_index('date')['close'].astype(float).sort_index().dropna()
    v_close = v.set_index('date')['close'].astype(float).sort_index().dropna()

    # 取两个指数共同交易日
    common = g_close.index.intersection(v_close.index)
    return g_close.loc[common].sort_index(), v_close.loc[common].sort_index()


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
        # 交易日数（从Low到当前的数据行数-1，而非日历天数）
        rise_days = len(g_close.loc[low_idx:g_close.index[i]]) - 1

        low_series.iloc[i] = low_val
        peak_series.iloc[i] = peak_val
        peak_rise_pct_series.iloc[i] = peak_val / low_val - 1
        current_rise_pct_series.iloc[i] = current_val / low_val - 1
        rise_days_series.iloc[i] = rise_days

    return low_series, peak_series, peak_rise_pct_series, current_rise_pct_series, rise_days_series


def generate_signals(g_close, v_close, params=None):
    """生成 B4V3 策略信号

    DC容差: 连续 dc 天中，若仅1天反向但 |T| <= dc_tolerance，仍视为方向一致。
    用于忽略T值在零轴附近的微负噪声，避免因单日微负导致确认重新累积。

    共6层：
      第1层 DC   — 方向确认 + 容差
      第2层 DCD  — 方向冷却
      第3层 弱信号 — T弱+斜率弱 → 空仓
      第4层 BIAS — 超买保护
      第5层 牛市止损 — 斐波那契阶梯解锁
      第8层 黑天鹅保护 — 固定10日动量解锁

    Args:
        g_close: 成长100收盘价 Series
        v_close: 价值100收盘价 Series
        params: 核心参数 dict（默认使用 BASELINE_PARAMS）

    Returns:
        df_factor: DataFrame，列为 date/symbol/signal/weight
    """
    p = {**BASELINE_PARAMS, **(params or {})}

    # ---- 全局因子计算 ----
    # 比价 = 成长/价值，反映成长相对价值的强弱
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()

    # T值 = 比价偏离度的标准化（z-score）
    t = ratio_dev / ratio_dev_std20

    # 比价MA20斜率（5日变化率）
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)

    n = len(t)
    index = t.index

    # ================================================================
    # 第1层: 方向确认 (DC) - 固定周期 + 容差
    # T>0 → BULL(成长), T<0 → BEAR(价值)
    # 需连续 dc 天方向一致才确认
    # 容差: 连续 dc 天中若仅1天反向但 |T| <= dc_tolerance，仍视为一致
    # ================================================================
    dc = p.get('dc', 5)              # 固定方向确认天数
    dc_tolerance = p.get('dc_tolerance', 0.0)  # 容差阈值
    # 提前确认层(实验性, esc_day=0 关闭): 连续esc_day天同向且|T|>=esc_t时提前确认方向
    # 依据: 翻转第3天T<=-0.6时, DC=5窗口剩余2天翻正概率<12%, 深负有惯性
    esc_day = p.get('esc_day', 0)
    esc_t = p.get('esc_t', 0)
    esc_side = p.get('esc_side', 'both')
    # 快速反悔阀(esc_revert_window=0 关闭): 提前确认后若T在窗口内翻回原方向,
    # 立即撤销(不等DC重确认、不扣DCD), 把"切错被困5~28天"压缩到"错1~2天"
    esc_revert_window = p.get('esc_revert_window', 0)
    raw_dir = (t > 0).map({True: 'BULL', False: 'BEAR'})
    mask = np.ones(n, dtype=bool)
    esc_last = -1            # 最近一次提前确认的索引
    esc_last_dir = None      # 该次提前确认的方向
    revert_ok = set()        # 快速反悔生效的索引(绕过DCD)
    for i in range(n):
        if esc_day and i >= esc_day - 1:
            _wd = raw_dir.iloc[i - esc_day + 1: i + 1]
            if (_wd == _wd.iloc[-1]).all():
                _early = (
                    (_wd.iloc[-1] == 'BEAR' and t.iloc[i] <= -esc_t and esc_side in ('both', 'value')) or
                    (_wd.iloc[-1] == 'BULL' and t.iloc[i] >= esc_t and esc_side in ('both', 'growth'))
                )
                if _early:
                    mask[i] = True
                    esc_last = i
                    esc_last_dir = _wd.iloc[-1]
                    continue
        # 快速反悔: 提前确认后窗口内T翻向对立方向 → 立即确认反向(撤销提前切换)
        if (esc_revert_window and esc_last >= 0
                and esc_last < i <= esc_last + esc_revert_window
                and raw_dir.iloc[i] != esc_last_dir):
            mask[i] = True
            revert_ok.add(i)
            esc_last = -1  # 反悔后结束本次提前确认的监控
            continue
        if dc > 1:
            # 往前看 dc 天（含今天），判断是否方向一致（含容差）
            start = max(0, i - dc + 1)
            window = raw_dir.iloc[start:i + 1]
            window_t = t.iloc[start:i + 1]
            if len(window) < dc:
                mask[i] = False
                continue
            main_dir = window.iloc[-1]  # 以最后一天方向为主方向
            # 统计与主方向不一致的天数
            opp_mask = window != main_dir
            opp_count = opp_mask.sum()
            if opp_count == 0:
                mask[i] = True
            elif opp_count == 1 and dc_tolerance > 0:
                # 恰有1天反向，且其|T|<=容差 → 视为一致
                opp_idx = window[opp_mask].index[0]
                if abs(window_t.loc[opp_idx]) <= dc_tolerance:
                    mask[i] = True
                else:
                    mask[i] = False
            else:
                mask[i] = False
    confirmed_dir = raw_dir.where(pd.Series(mask, index=index), np.nan)

    # ================================================================
    # 第2层: 方向冷却 (DCD)
    # 换向后需等 dcd 天才允许再次换向
    # ================================================================
    new_dir = confirmed_dir.ffill().fillna('BULL')
    last_switch = -p['dcd'] - 1
    prev = new_dir.iloc[0]
    for i in range(1, n):
        curr = confirmed_dir.iloc[i]
        if pd.isna(curr):
            continue
        if curr != prev:
            # 快速反悔的换向绕过DCD冷却(撤销错误比坚持错误便宜)
            if i - last_switch >= p['dcd'] or i in revert_ok:
                last_switch = i
                prev = curr
            new_dir.iloc[i] = prev
        else:
            new_dir.iloc[i] = prev
    dir_raw = new_dir.ffill().fillna('BULL')

    # ================================================================
    # 第10层: 反打脸保护 (AW — Anti-Whipsaw)
    #
    # 问题: 当T值在零轴附近高频上下翻转时, DC=dc 的确认窗口恰与震荡半周期
    #       同量级, 每次换向都反向踩点(打脸)。2016/2020/2022/2023/2026 的
    #       共同特征: 突发利空→假突破+上下反转, 市场无持续趋势。
    # 机理: 震荡态下唯一仍稳定的信息是"慢趋势"(比价相对长均线的方向)。
    #       故在震荡态抑制"逆慢锚"的换向, 只允许顺慢锚方向切换, 从而避免
    #       被短周期噪声反复甩动, 同时不降低仓位(非空仓/非降仓方案)。
    #
    # 震荡态检测: flip20 = 近 aw_flip_window 日 T 符号翻转次数;
    #             flip20 >= aw_flip_thresh 判为震荡态。
    # 慢锚方向  : ratio 相对 aw_anchor_window 日慢均线的位置(上=成长/下=价值)。
    #
    # aw_mode:
    #   none        — 关闭(默认, 行为=B4V4基线, 零改动)
    #   anchor_hold — 震荡态内抑制逆慢锚换向(顺锚放行)
    #   anchor_hard — 震荡态内直接锚定慢锚方向
    #   slow_dc     — 震荡态内提高确认门槛(新方向需连续 aw_dc_strict 天)
    #   tgate       — 震荡态内仅当 |T|>=aw_t_gate 才放行换向
    #   slow_dc_graded  — 确认门槛随震荡强度分级加严
    #                     strict = clip(dc + aw_grad_base + floor((flip20-thresh)/aw_grad_step), 2, aw_grad_cap)
    #                     逻辑: 越震荡越需要长确认, 而非全期用同一个固定门槛
    #   freeze      — 震荡态内冻结不换向(保持前一方向), 震荡态结束后直接采纳原始方向
    #   slow_dc_asym — 非对称确认: 仅「价值→成长」(BEAR→BULL)加长确认门槛,
    #                  反方向(成长→价值)保持原 dc 门槛。
    #                  参数: aw_asym_dc(价值→成长门槛, 默认=aw_dc_strict),
    #                        aw_asym_dc_rev(成长→价值门槛, 默认=dc),
    #                        aw_asym_scope('whipsaw'=仅震荡态生效/ 'all'=全期生效)
    # ================================================================
    aw_mode = p.get('aw_mode', 'none')
    if aw_mode != 'none':
        _sgn = np.sign(t)
        _flip = ((_sgn != _sgn.shift(1)) & (_sgn != 0) & (_sgn.shift(1) != 0)).astype(float)
        flip20 = _flip.rolling(p.get('aw_flip_window', 20)).sum()
        _anchor_n = p.get('aw_anchor_window', 60)
        _anchor_ma = ratio.rolling(_anchor_n).mean()
        anchor_dir = pd.Series(
            np.where(ratio > _anchor_ma, 'growth', 'value'), index=index)
        # 慢均线预热不足的早期区间不判震荡(锚无意义)
        whipsaw = (flip20 >= p.get('aw_flip_thresh', 5)) & _anchor_ma.notna()
        dc_strict = p.get('aw_dc_strict', 7)
        t_gate = p.get('aw_t_gate', 1.0)
        _raw_arr = raw_dir.values

        # 分级确认门槛: 越震荡(flip20越高)所需连续确认天数越长
        _strict_arr = np.full(n, dc_strict, dtype=int)
        if aw_mode == 'slow_dc_graded':
            _step = p.get('aw_grad_step', 2)
            _lv = np.floor((flip20.fillna(0).values - p.get('aw_flip_thresh', 5)) / _step)
            _strict_arr = np.clip(p['dc'] + p.get('aw_grad_base', 2) + _lv,
                                  2, p.get('aw_grad_cap', 12)).astype(int)

        aw_dir = dir_raw.copy()
        prev = aw_dir.iloc[0]
        _asym_all = (aw_mode == 'slow_dc_asym'
                     and p.get('aw_asym_scope', 'whipsaw') == 'all')
        for i in range(1, n):
            cand = dir_raw.iloc[i]
            if aw_mode == 'slow_dc_asym' and (whipsaw.iloc[i] or _asym_all):
                # 非对称确认: 仅「价值→成长」(BEAR→BULL)加长门槛, 反方向保持原 dc
                if cand == prev:
                    aw_dir.iloc[i] = cand
                else:
                    if prev == 'BEAR' and cand == 'BULL':
                        k = int(p.get('aw_asym_dc', dc_strict))
                    else:
                        k = int(p.get('aw_asym_dc_rev', p['dc']))
                    s = max(0, i - k + 1)
                    allow = (_raw_arr[s:i + 1] == cand).all()
                    aw_dir.iloc[i] = cand if allow else prev
            elif whipsaw.iloc[i]:
                if aw_mode == 'anchor_hard':
                    aw_dir.iloc[i] = anchor_dir.iloc[i]
                elif aw_mode == 'freeze':
                    aw_dir.iloc[i] = prev
                elif cand == prev:
                    aw_dir.iloc[i] = cand
                else:
                    allow = False
                    if aw_mode == 'anchor_hold':
                        allow = (cand == anchor_dir.iloc[i])
                    elif aw_mode in ('slow_dc', 'slow_dc_graded'):
                        k = int(_strict_arr[i])
                        s = max(0, i - k + 1)
                        allow = (_raw_arr[s:i + 1] == cand).all()
                    elif aw_mode == 'tgate':
                        allow = abs(t.iloc[i]) >= t_gate
                    aw_dir.iloc[i] = cand if allow else prev
            else:
                aw_dir.iloc[i] = cand
            prev = aw_dir.iloc[i]
        dir_raw = aw_dir

    # ---- 权重初始为 1.0 ----
    wt = pd.Series(1.0, index=index)

    # ================================================================
    # 第3层: T+斜率双重确认 → 空仓
    # |T|<rt 且 |斜率|<slope_thresh → 无趋势，空仓
    # ================================================================
    weak_slope = slope.abs() < p['slope_thresh']
    weak_t = t.abs() < p['rt']
    is_weak = weak_t & weak_slope
    # weak_confirm: 弱信号空仓需连续N天确认(1=原单日触发)。
    # 依据: 1天/2天弱段空仓期间原方向平均+0.28%/+0.61%(纯噪声踏空),
    # >5天段-3.92%(真避险) → 空仓作为切换动作应与DC确认哲学对齐
    wc = p.get('weak_confirm', 1)
    if wc > 1:
        is_weak = is_weak & is_weak.astype(int).rolling(wc).sum().ge(wc)
    # L3 弱信号开关（消融实验用；默认开启）
    if p.get('enable_l3', True):
        wt[is_weak] = 0.0

    # ================================================================
    # 第4层: 方向映射 + BIAS 超买保护
    # BULL → growth, BEAR → value
    # 持仓标的偏离MA20 > bias_high → 清仓
    # ================================================================
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ================================================================
    # 第5层: 牛市止损 — 斐波那契阶梯解锁
    #
    # 双路径解锁：
    #   路径A-新高解锁: 价格突破触发时的Peak → 立即解锁（试错机制）
    #   路径B-斐波那契阶梯: 基于回撤深度阶梯式缩短锁定
    #
    # 对每个交易日，滚动65日窗口找到最低价Low，
    # 再从Low到当前找到最高价Peak，
    # 记录涨幅rise_pct和上涨天数rise_days。
    #
    # 触发条件：持有成长 + rise_pct > min_rise(50%) + 5日动量转负
    # 基准锁定 = min(rise_days, 65)
    #
    # 超级牛市(rise_pct>50%): B4V4锁满全程; 解锁仅靠新高试错或时间到期
    # 普通牛市(30%<rise_pct≤50%): 0.382→缩至一半, 0.618→完全解锁
    # ================================================================
    lookback_window = p.get('l5_lookback_window', 65)
    min_rise = p.get('l5_min_rise', 0.30)
    big_bull_thresh = p.get('l5_big_bull_thresh', 0.50)

    # 预计算每个交易日的Low、Peak、peak_rise_pct、current_rise_pct、rise_days
    # peak_rise_pct = Peak/Low - 1（用于区分大小牛市以选择解锁阶梯）
    # current_rise_pct = 当前价/Low - 1（用于触发条件）
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

    # 顺序追踪成长锁定状态
    growth_locked_until = -1       # 锁定的结束索引（绝对index）
    growth_lock_start = -1         # 触发锁定的起始索引
    growth_trigger_peak = None     # 触发时的Peak价（新高解锁基准）
    growth_trigger_low = None      # 触发时的Low价
    growth_trigger_peak_rise_pct = None  # 触发时的Peak/Low涨幅（区分大小牛市阶梯）
    growth_base_lock = 0           # 基准锁定天数
    g_lock_series = np.zeros(n, dtype=bool)  # 每日成长是否处于L5锁定(第13层RRS用)
    # l5_debug: 记录L5事件(触发/各路径解锁)到模块级L5_DEBUG_LOG供研究
    l5_dbg = p.get('l5_debug', False)
    if l5_dbg:
        L5_DEBUG_LOG.clear()

    def _dbg(evt, **kw):
        if l5_dbg:
            rec = dict(type=evt, date=str(index[kw['i']].date()), **{k: v for k, v in kw.items() if k != 'i'})
            L5_DEBUG_LOG.append(rec)

    # ---- 第12层 L5R: 到期趋势修复确认 所需的预计算 ----
    # 模式: none/gate/mom/ma (趋势修复: 收盘>MA & 动量>0) / dd (洗盘确认: 距牛市峰值回撤>=阈值)
    l5_repair = p.get('l5_repair_mode', 'none')
    _rep_ma = g_close.rolling(p.get('l5_repair_ma', 20)).mean() if l5_repair in ('gate', 'ma') else None
    _rep_mom = (g_close / g_close.shift(p.get('l5_repair_mom', 20)) - 1) if l5_repair in ('gate', 'mom') else None
    _rep_dd = p.get('l5_repair_dd', 0.20)

    for i in range(n):
        current_price = g_close.iloc[i]

        # ===== 第12层 L5R: 时间到期解锁需趋势修复/洗盘确认 =====
        # 到期日若确认条件不满足, 逐日滚动延长锁定(继续持另一腿/空仓), 直到满足。
        #  dd 模式动机: 2026-09-24 到期时成长"距峰值仅-3.3%且动量≈0"(顶部滞涨),
        #  而 2015-09-24 到期时成长"距峰值-27.6%(已深度洗盘后反弹)"——两者在
        #  MA/动量上几乎不可分, 唯有"距峰值回撤深度"可稳健区分。
        if (l5_repair != 'none' and growth_trigger_peak is not None
                and i == growth_locked_until):
            if l5_repair == 'dd':
                _lo = growth_lock_start if growth_lock_start >= 0 else 0
                _peak_run = float(g_close.iloc[_lo:i + 1].max())
                _dd_v = (_peak_run - current_price) / _peak_run if _peak_run > 0 else 0.0
                _ok = _dd_v >= _rep_dd
                _dbg('repair_check', i=i, dd=round(_dd_v, 3), ok=bool(_ok))
            else:
                _ma_v = _rep_ma.iloc[i]
                _mom_v = _rep_mom.iloc[i]
                _ma_ok = True if l5_repair == 'mom' else (not np.isnan(_ma_v) and current_price > _ma_v)
                _mom_ok = True if l5_repair == 'ma' else (not np.isnan(_mom_v) and _mom_v > 0)
                _ok = _ma_ok and _mom_ok
            if _ok:
                _dbg('unlock_repair', i=i,
                     days_held=i - growth_lock_start if growth_lock_start >= 0 else -1)
                growth_locked_until = -1
                growth_lock_start = -1
                growth_trigger_peak = None
                growth_trigger_low = None
                growth_trigger_peak_rise_pct = None
                growth_base_lock = 0
            else:
                growth_locked_until = i + 1   # 趋势未修复 → 继续锁定

        # ===== 检查解锁 =====
        if i < growth_locked_until and growth_trigger_peak is not None:
            # 路径A: 新高解锁（试错机制）
            if current_price > growth_trigger_peak:
                _dbg('unlock_newhigh', i=i,
                     days_held=i - growth_lock_start if growth_lock_start >= 0 else -1)
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
                peak_rise_pct = growth_trigger_peak_rise_pct  # 触发时的Peak/Low涨幅
                base_lock = growth_base_lock

                # 回撤深度 = (Peak - Current) / (Peak - Low)
                drawdown = (peak - current_price) / (peak - low) if peak != low else 0.0

                if peak_rise_pct > big_bull_thresh:
                    # === 超级大牛市(peak_rise_pct > 50%) — B4V4: 锁满全程 ===
                    # 解锁仅两条路径: 新高试错(路径A)或时间到期。
                    # 移除的提前解锁路径(官方引擎消融 l5_halve_engine.csv):
                    #   'halve'=0.782减半(B4V3基线): 45.73%/1.806, 提前解锁后
                    #     深度回撤反弹中追高(2026-08-18买回成长后跌9.4%)
                    #   'extend'=延长1.5倍: 44.13%, 过犹不及
                    #   锁满全程(本版默认): 46.51%/1.844, 回撤持平
                    halve_mode = p.get('l5_halve_mode', 'none')
                    if drawdown >= 0.782 and halve_mode == 'extend':
                        _dbg('unlock_extend', i=i, drawdown=round(float(drawdown), 3),
                             rise_pct=round(float(peak_rise_pct), 3))
                        new_lock_end = growth_lock_start + int(base_lock * 1.5)
                        if new_lock_end > growth_locked_until:
                            growth_locked_until = new_lock_end
                    elif drawdown >= 0.782 and halve_mode == 'halve':
                        _dbg('unlock_halve', i=i, drawdown=round(float(drawdown), 3),
                             rise_pct=round(float(peak_rise_pct), 3))
                        new_lock_end = growth_lock_start + int(base_lock * 0.50)
                        if new_lock_end < growth_locked_until:
                            growth_locked_until = new_lock_end
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
            _dbg('trigger', i=i, rise_pct=round(growth_trigger_peak_rise_pct, 3),
                 rise_days=rise_days, base_lock=growth_base_lock)
        g_lock_series[i] = (i < growth_locked_until)

    # ================================================================
    # 第8层: 黑天鹅保护 — 系统性风险（固定10日动量解锁）
    #
    # 逻辑：5日内成长跌>6% 且 价值跌>3% → 判断为黑天鹅事件
    #       触发后成长和价值分别独立锁定
    #       解锁条件：固定10日动量>0
    #
    #       相对于B3v5的动态波动率自适应动量(波动率映射5-30日周期)，
    #       B4采用固定10日动量解锁，在29个动量周期(8-90日)全面扫描中
    #       被验证为最优参数，各波动率环境下均表现稳健。
    # ================================================================
    g_ret_5d = g_close / g_close.shift(p['black_swan_lookback']) - 1  # 成长5日收益
    v_ret_5d = v_close / v_close.shift(p['black_swan_lookback']) - 1  # 价值5日收益

    # 固定动量周期（由参数配置，默认10日）
    momentum_p = p['momentum_period']

    # 预计算固定周期动量
    g_mom_fixed = g_close / g_close.shift(momentum_p) - 1
    v_mom_fixed = v_close / v_close.shift(momentum_p) - 1

    # 触发条件：5日内成长跌>6% 且 价值跌>3%（双跌确认黑天鹅）
    black_swan_trigger = (
        (g_ret_5d < -p['black_swan_g_thresh'])
        & (v_ret_5d < -p['black_swan_v_thresh'])
    )

    # 成长/价值独立锁定状态
    growth_locked = False
    value_locked = False
    bs_lock_series = np.zeros(n, dtype=bool)  # 每日是否处于黑天鹅锁定(第13层RRS用)
    for i in range(n):
        # 检查解锁：固定动量>0 → 解锁对应方向
        if growth_locked and g_mom_fixed.iloc[i] > 0:
            growth_locked = False
        if value_locked and v_mom_fixed.iloc[i] > 0:
            value_locked = False

        # 锁定方向不可持仓
        if dir_series.iloc[i] == 'growth' and growth_locked:
            wt.iloc[i] = 0.0
        if dir_series.iloc[i] == 'value' and value_locked:
            wt.iloc[i] = 0.0

        # 检查触发条件（有持仓时才触发）
        if black_swan_trigger.iloc[i] and wt.iloc[i] > 0.001:
            growth_locked = True
            value_locked = True
            wt.iloc[i] = 0.0  # 当天清仓
        bs_lock_series[i] = (growth_locked or value_locked)

    # ================================================================
    # 第9层: 利率跳变响应层 (用户观点: 只关注利率跳变日期)
    #
    # 10Y水平/连续斜率是滞后信号(反映已发生的市场); 只有"利率跳变事件"
    # 携带新的风险/风格信息。实证(2013-2026): Δ>20bp事件后20日, 预设方向
    #   (升→价值/降→成长)均收益+5.39% vs 反向+1.39% = +4.00pct领先奖励。
    #
    # rate_jump_mode 三种响应方式：
    #   none       — 不启用
    #   dir        — 方向领先: 跳变预设方向≠当前方向则切到预设方向(已测, 官方引擎无改善)
    #   scale      — 降仓避险: 跳变窗口内当前持仓权重×rate_jump_scale(风险来临降暴露)
    #   scale_dir  — 降仓+换向: 窗口内权重×scale, 且方向不一致时切到跳变方向
    # ================================================================
    y10 = p.get('y10', None)
    rj_mode = p.get('rate_jump_mode', 'none')
    if y10 is not None and rj_mode != 'none':
        jump_bp = p.get('rate_jump_bp', 20)      # 5日利率变化阈值(bp), 双向
        rel_z = p.get('rate_jump_rel_z', None)   # 相对跳变: 5日Δ÷250日常态σ>rel_z (贴合环境)
        burst_z = p.get('rate_jump_burst_z', None) # 爆发抑制: 若同日1日Δ÷σ>burst_z(极速爆发=风险反转)则抑制方向追随
        jump_lock = p.get('rate_jump_lock', 20)  # 响应窗口天数
        rj_scale = p.get('rate_jump_scale', 0.5) # 降仓缩放系数
        jump_sides = p.get('rate_jump_sides', 'both')  # both/up/down: 仅响应跳升或仅跳降(方向不对称研究)
        chg_n = p.get('rate_jump_chg', 5)        # 利率变化窗口天数
        sig_lb = p.get('rate_jump_sigma_lb', 250) # 常态σ回看期
        tgate = p.get('rate_jump_tgate', None)   # T门控: 仅|T|<tgate(T不确定区)时允许跳变领先
        t_agree = p.get('rate_jump_t_agree', False)  # 仅T已与预设同向时才领先(确认模式)
        y10s = y10.reindex(index).ffill()
        chg5 = y10s.diff(chg_n) * 100
        if rel_z is not None:
            roll = chg5.rolling(sig_lb).std()
            z = chg5 / roll
            preset = pd.Series('none', index=index)
            if jump_sides in ('both', 'up'):
                preset[z > rel_z] = 'value'     # 相对跳升→价值
            if jump_sides in ('both', 'down'):
                preset[z < -rel_z] = 'growth'   # 相对跳降→成长
            if jump_sides == 'up':
                evt = z > rel_z
            elif jump_sides == 'down':
                evt = z < -rel_z
            else:
                evt = (z.abs() > rel_z)
        else:
            preset = pd.Series('none', index=index)
            preset[chg5 > jump_bp] = 'value'    # 跳升→价值(价值跑赢)
            preset[chg5 < -jump_bp] = 'growth'  # 跳降→成长(成长跑赢)
            evt = (chg5.abs() > jump_bp)
        # 爆发掩码: 单日极速突变(1日Δ÷σ>burst_z)视为风险/反转, 抑制当次日方向追随
        burst = None
        if burst_z is not None:
            chg1 = y10s.diff(1) * 100
            roll1 = chg1.rolling(250).std()
            burst = (chg1 / roll1).abs() > burst_z
        remain = 0
        override = 'none'
        for i in range(n):
            if evt.iloc[i]:
                remain = jump_lock + 1
                # 爆发抑制: 触发当日为单日极速爆发则本次不切方向(避免追反转)
                if burst is not None and burst.iloc[i]:
                    override = 'none'
                else:
                    override = preset.iloc[i] if dir_series.iloc[i] != preset.iloc[i] else 'none'
            if remain > 0:
                # 降仓: 窗口内当前有持仓则缩权重
                if rj_mode in ('scale', 'scale_dir') and wt.iloc[i] > 0.001:
                    wt.iloc[i] = wt.iloc[i] * rj_scale
                # 换向: 窗口内若方向不一致且选择了换向模式
                if rj_mode == 'scale_dir' and override != 'none':
                    if dir_series.iloc[i] != override and wt.iloc[i] > 0.001:
                        dir_series.iloc[i] = override
                elif rj_mode == 'dir' and override != 'none':
                    # T门控: 跳变领先仅在T不确定区(|T|<tgate)生效, 强T区信任主信号
                    if tgate is not None and abs(t.iloc[i]) >= tgate:
                        pass
                    # T同向确认: 仅当T已指向预设方向时才领先(顺水推舟, 不硬顶)
                    elif t_agree and not (
                        (override == 'value' and t.iloc[i] < 0) or
                        (override == 'growth' and t.iloc[i] > 0)):
                        pass
                    elif dir_series.iloc[i] != override and wt.iloc[i] > 0.001:
                        dir_series.iloc[i] = override
                remain -= 1

    # ================================================================
    # 第11层: 单腿急跌·轮动减仓 (RCD — Rotation-Confirmed De-risk)
    #
    # 问题: 持有腿遭遇"突发单边急跌"时, 现有各层都不响应——
    #       L3弱信号要求|T|小且斜率弱(急跌时T强负, 不触发);
    #       L4 BIAS只保护超买(正偏离); L5只锁超级牛市;
    #       L8黑天鹅要求"两腿同跌"(单边急跌时另一腿不跌, 不触发);
    #       DC=5确认滞后, 等它换向时急跌已走完大半。
    #
    # 机理: 急跌可分两类(2013-2026, 71次单边急跌事件实证)——
    #   轮动型(另一腿上涨, n=24): 资金撤离持有腿 → 前瞻20日 -3.04%, 67%继续跑输;
    #   普跌型(两腿同跌, n=47): 系统性冲击 → 前瞻20日 +3.39%, 倾向V型反弹。
    #   故仅在"轮动型"下减仓; 普跌型交给L8(清仓)与自身反弹, 不干预。
    #
    # 触发(全部满足): ①持有腿急跌 ②相对另一腿显著走弱 ③另一腿上涨
    # 动作: 动态减仓(降至 dr_scale); 持有腿止跌回升(dr_rec)或方向切换后回补。
    # ================================================================
    dr_mode = p.get('dr_mode', 'none')
    if dr_mode != 'none':
        dr_drop = p.get('dr_drop', 0.06)
        dr_z = p.get('dr_z', 1.5)
        dr_spread = p.get('dr_spread', 0.04)
        dr_other_up = p.get('dr_other_up', True)
        dr_scale = p.get('dr_scale', 0.5)
        dr_rec = p.get('dr_rec', 0.0)
        dr_rec_win = p.get('dr_rec_win', 5)   # 恢复判定所用持有腿收益窗口(5=短/20=趋势修复)
        dr_deep = p.get('dr_deep', False)
        dr_drop20 = p.get('dr_drop20', 0.12)
        dr_spread20 = p.get('dr_spread20', 0.05)
        dr_deep_scale = p.get('dr_deep_scale', 0.0)

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
        # 恢复判定序列: 5日收益(短周期) 或 20日收益(趋势修复)
        held_rec = held_r20 if dr_rec_win >= 20 else held_r5
        # 波动率标准化跌幅: 以持有腿20日已实现波动为"常态"基准(自适应波动环境)
        z5 = held_r5 / (held_s20 * np.sqrt(5))

        if dr_mode == 'rot_z':
            drop_ok = z5 <= -dr_z
        else:
            drop_ok = held_r5 <= -dr_drop
        spread_ok = spread5 <= -dr_spread
        rot_ok = (other_r5 > 0) if dr_other_up else np.ones(n, dtype=bool)
        trig = drop_ok & spread_ok & rot_ok
        trig = np.nan_to_num(trig).astype(bool)

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

    # ================================================================
    # 第13层: 轮动型急跌·加速换向 (RRS — Rotation-Crash Rapid Switch)
    #
    # 动机: DC=5 确认滞后。轮动型急跌(持有腿5日跌≥6% 且 相对另一腿超额≤-4%
    #       且 另一腿上涨)时, 资金已明确撤离持有腿、流向另一腿; 等 DC 确认时
    #       急跌已走完大半。RRS 在触发当日立即把方向切到"另一腿", 并保持
    #       rrs_lock 天(期间覆盖 DC), 以承接另一腿的上行, 而非持现金或半仓。
    # 实证(2013-2026, 71次单边急跌): 轮动型前瞻20日 全样本 -3.04% /
    #       策略实际持有的13次 -7.47%(77%继续跑输) —— 看似换向有正期望。
    # 【官方引擎结论: 已证伪】5种配置(rot_z lock5/10/20、固定阈值、宽触发)
    #   全部劣于基线: 年化 46.59%→44.79~45.96%, Sharpe 1.855→1.792~1.831,
    #   回撤与 2026 急跌窗口(-0.6%)零改善。原因: "轮动"常在一两周内反转,
    #   换向后另一腿不再上涨, 反而锁定损失并踏空原持有腿的反弹
    #   (2025 rrs_fix10 61.2%→43.1%, 2015 163.6%→156.2%)。
    # 保留该层仅为留档, 默认 rrs_mode='none'(与B4V7基线逐字节一致)。
    # ================================================================
    rrs_mode = p.get('rrs_mode', 'none')
    if rrs_mode != 'none':
        rrs_drop = p.get('rrs_drop', 0.06)
        rrs_z = p.get('rrs_z', 1.5)
        rrs_spread = p.get('rrs_spread', 0.04)
        rrs_lock = p.get('rrs_lock', 10)
        rrs_other_up = p.get('rrs_other_up', True)
        _g5 = g_close / g_close.shift(5) - 1
        _v5 = v_close / v_close.shift(5) - 1
        _gs = g_close.pct_change().rolling(20).std()
        _vs = v_close.pct_change().rolling(20).std()
        _isg = (dir_series == 'growth').values
        _h5 = np.where(_isg, _g5.values, _v5.values)
        _o5 = np.where(_isg, _v5.values, _g5.values)
        _hs = np.where(_isg, _gs.values, _vs.values)
        _sp = _h5 - _o5
        if rrs_mode == 'rot_z':
            _drop = (_h5 / (_hs * np.sqrt(5))) <= -rrs_z
        else:
            _drop = _h5 <= -rrs_drop
        _rot = (_o5 > 0) if rrs_other_up else np.ones(n, dtype=bool)
        _trig = np.nan_to_num(_drop & (_sp <= -rrs_spread) & _rot).astype(bool)
        _remain = 0
        _tgt = 'none'
        for i in range(n):
            if _trig[i] and wt.iloc[i] > 0.001:
                _cur = dir_series.iloc[i]
                _cand = 'value' if _cur == 'growth' else 'growth'
                _locked = bool(bs_lock_series[i]) or (_cand == 'growth' and bool(g_lock_series[i]))
                if not _locked:
                    _tgt = _cand
                    _remain = rrs_lock
            if _remain > 0 and wt.iloc[i] > 0.001:
                dir_series.iloc[i] = _tgt
                _remain -= 1

    # ================================================================
    # 第14层: 震荡不明朗·半仓 (CHOP — Chop Half Position) —— B4V8 新增
    #
    # 动机: 在"动量不可用"的震荡态(方向确认即买在顶点)内, 无论切到哪条腿
    #       都没有方向优势, 只是被短周期噪声反复甩动。此时把总仓位降到半仓,
    #       以减少来回换手的损耗敞口。
    # 识别(全部非参数, 仅用截至当日的信息; 三个指标同时处于危险分位才触发):
    #   ①flip 高: 近 chop_flip_window 日 T 符号翻转次数 处于 expanding 高分位
    #   ②ER   低: 效率比 = |ratio_t-ratio_{t-n}| / Σ|Δratio| 处于 expanding 低分位
    #             (低=比价来回磨、无方向)
    #   ③rdstd高: 比价偏离度(ratio/MA20-1)的20日标准差 处于 expanding 高分位
    # 动作: 危险态内 wt *= chop_scale(默认0.5), 不改变方向、不改变持仓腿。
    #   expanding 分位只用历史数据, 无未来信息。
    #
    # 【依据】可行性检验(2013-2026):
    #   - 纯DC轮动(V401)内该综合态前瞻10/20/30日实际净值收益 -0.19%/-0.50%/-0.08%,
    #     负收益概率 46.2%/47.6%/45.7%(基准 39%), 持有腿前瞻20日 -0.83% —— 确为亏损态;
    #   - 但 B4V7(含AW)内同态前瞻20日仍为正(+0.98%), 说明 AW 已部分化解;
    #     半仓为等比例缩放(均值/标准差同比例), 夏普不变, 属"让收益换低回撤"。
    # chop_mode='none' 时与 B4V7 基线逐字节一致(零改动)。
    # ================================================================
    chop_mode = p.get('chop_mode', 'none')
    if chop_mode == 'chop':
        _fw = p.get('chop_flip_window', 20)
        _sgn_c = np.sign(t)
        _flip_c = ((_sgn_c != _sgn_c.shift(1)) & (_sgn_c != 0) & (_sgn_c.shift(1) != 0)).astype(float)
        _flipw = _flip_c.rolling(_fw).sum()
        _er = (ratio - ratio.shift(_fw)).abs() / ratio.diff().abs().rolling(_fw).sum()
        _rdstd = (ratio / ratio.rolling(20).mean() - 1).rolling(20).std()
        _minh = p.get('chop_min_hist', 250)
        _p_flip = _flipw.expanding(min_periods=_minh).rank(pct=True)
        _p_er = _er.expanding(min_periods=_minh).rank(pct=True)
        _p_rd = _rdstd.expanding(min_periods=_minh).rank(pct=True)
        chop = ((_p_flip >= p.get('chop_flip_q', 0.70))
                & (_p_er <= p.get('chop_er_q', 0.30))
                & (_p_rd >= p.get('chop_rdstd_q', 0.70)))
        chop = chop.fillna(False).values
        _csc = p.get('chop_scale', 0.5)
        for i in range(n):
            if chop[i] and wt.iloc[i] > 0.001:
                wt.iloc[i] = wt.iloc[i] * _csc

    df_out = _signals_to_df_factor(dir_series, wt)
    if p.get('_debug_dir'):
        return df_out, dir_series, wt
    return df_out


def generate_signals_with_period(g_close, v_close, momentum_period, params=None):
    """生成指定动量周期的信号（用于参数敏感性分析）

    Args:
        g_close: 成长100收盘价 Series
        v_close: 价值100收盘价 Series
        momentum_period: L8解锁动量周期（日）
        params: 其他参数

    Returns:
        df_factor: DataFrame，列为 date/symbol/signal/weight
    """
    p = {**BASELINE_PARAMS, **(params or {}), 'momentum_period': momentum_period}
    return generate_signals(g_close, v_close, p)


def _signals_to_df_factor(dir_series, wt):
    """将方向序列和权重序列转换为 df_factor 格式

    Args:
        dir_series: 方向序列 ('growth'/'value')
        wt: 权重序列 (0.0~1.0)

    Returns:
        DataFrame: date/symbol/signal/weight
    """
    records = []
    dates = dir_series.index
    for i in range(len(dates)):
        d = dates[i]
        s = dir_series.iloc[i]
        w = float(wt.iloc[i])
        date_str = d.strftime('%Y%m%d')

        if pd.isna(s) or pd.isna(w):
            s, w = 'growth', 0.0

        if w > 0.001:
            if s == 'growth':
                records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                                'signal': 'buy', 'weight': round(w, 4)})
                records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                                'signal': 'sell', 'weight': 0.0})
            else:
                records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                                'signal': 'buy', 'weight': round(w, 4)})
                records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                                'signal': 'sell', 'weight': 0.0})
        else:
            records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
            records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})

    df = pd.DataFrame(records)
    df['date'] = df['date'].astype(str)
    return df


if __name__ == '__main__':
    # 快速验证：生成信号并统计
    print("[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ {g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    print("\n[2] 生成B4V3信号...")
    df_factor = generate_signals(g_close, v_close)
    print(f"  df_factor: {len(df_factor)} rows")
    print(f"  日期范围: {df_factor['date'].min()} ~ {df_factor['date'].max()}")

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 保存
    out_dir = Path(__file__).parent / 'df_factors'
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / 'df_factor_b4v3.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")