"""B4V3 成长价值轮动策略 — 信号生成器

B4V3 = B4V2 + 收盘价撮合 + DC容差 + DCD优化

改进点：
  - 收盘价撮合（matching_type=0）：读昨日信号，今日收盘价成交
  - DC容差机制（dc_tolerance=0.09）：连续dc天中允许1天反向但|T|<=0.09
  - DCD从6天优化至5天

共6层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致才确认
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第5层 牛市止损 — 斐波那契阶梯解锁
  第8层 黑天鹅保护 — 固定10日动量解锁
"""
import pandas as pd
import numpy as np
from pathlib import Path

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

    # ---- 权重初始为 1.0 ----
    wt = pd.Series(1.0, index=index)

    # ================================================================
    # 第3层: T+斜率双重确认 → 空仓
    # |T|<rt 且 |斜率|<slope_thresh → 无趋势，空仓
    # ================================================================
    weak_slope = slope.abs() < p['slope_thresh']
    weak_t = t.abs() < p['rt']
    is_weak = weak_t & weak_slope
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
    # 超级牛市(rise_pct>50%): 0.782→锁定减半, 0.9→立即解锁
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
                peak_rise_pct = growth_trigger_peak_rise_pct  # 触发时的Peak/Low涨幅
                base_lock = growth_base_lock

                # 回撤深度 = (Peak - Current) / (Peak - Low)
                drawdown = (peak - current_price) / (peak - low) if peak != low else 0.0

                if peak_rise_pct > big_bull_thresh:
                    # === 超级大牛市(peak_rise_pct > 50%) ===
                    if drawdown >= 0.9:
                        # 回撤>=0.9 → 立即完全解锁
                        growth_locked_until = -1
                        growth_lock_start = -1
                        growth_trigger_peak = None
                        growth_trigger_low = None
                        growth_trigger_peak_rise_pct = None
                        growth_base_lock = 0
                    elif drawdown >= 0.782:
                        # 回撤>=0.782 → 锁定时间减半（从触发日算起）
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
        y10s = y10.reindex(index).ffill()
        chg5 = y10s.diff(5) * 100
        if rel_z is not None:
            roll = chg5.rolling(250).std()
            z = chg5 / roll
            preset = pd.Series('none', index=index)
            preset[z > rel_z] = 'value'     # 相对跳升→价值
            preset[z < -rel_z] = 'growth'   # 相对跳降→成长
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
                    if dir_series.iloc[i] != override and wt.iloc[i] > 0.001:
                        dir_series.iloc[i] = override
                remain -= 1

    return _signals_to_df_factor(dir_series, wt)


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