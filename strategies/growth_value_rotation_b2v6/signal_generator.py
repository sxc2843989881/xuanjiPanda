"""B2v6-木星 成长价值轮动策略 — 信号生成器

生成 PandaAI 引擎所需的 df_factor（date/symbol/signal/weight）。

策略架构（6层过滤）:
  第1层 DC  — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 B2v6 — 价值动量过滤（修复版）：价值动量负→空仓，不反手买成长
  第5层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第6层 TS  — 最高点跟踪止损 + 冷却期 + 信号变化恢复

B2v6与B2v3的核心差异:
  1. 第4层修复：当成长跌+价值跌时，direction=value(空仓)，不再反手标记为growth
  2. 第6层替换：E5(20日固定窗口跌幅) → 最高点跟踪止损 + 冷却期 + 信号变化恢复
     - 触发止损后直接清仓（sw_deep=0），不留残仓
     - 进入冷却期（默认10天），冷却期内保持空仓
     - 冷却期结束后，等待DC信号方向发生变化再入场
     - 不再使用MACD底背离条件，简化恢复逻辑
     - 仓位从空仓变持仓时重置HWM，防止假止损

使用方式:
  from signal_generator import generate_signals, load_index_data
  g_close, v_close = load_index_data()
  df_factor = generate_signals(g_close, v_close)
  df_factor.to_csv("df_factor.csv", index=False)
"""
import pandas as pd
import numpy as np
from pathlib import Path

from params import (
    GROWTH_SYMBOL, VALUE_SYMBOL,
    BASELINE_PARAMS, TRAILING_STOP_PARAMS, NAV_STOP, INDEX_DATA_DIR,
)


# ===== MACD 辅助函数 =====
def _calc_macd(price_series):
    """计算MACD柱值：2*(DIF-DEA)"""
    ema12 = price_series.ewm(span=12, adjust=False).mean()
    ema26 = price_series.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return 2 * (dif - dea)


def _has_bottom_divergence(price, macd, lookback=20):
    """判断过去lookback天内是否出现MACD底背离

    MACD底背离定义：价格创出新低（或更低），但MACD柱值不再新低
     → 下跌动能衰竭，市场即将企稳反弹

    Args:
        price: 价格序列（至少10个数据点）
        macd: MACD柱值序列（与price等长）
        lookback: 检测窗口（默认20天）

    Returns:
        bool: 是否出现底背离
    """
    r = min(lookback, len(price) - 1)
    if r < 10:
        return False
    p = price[-r:]
    m = macd[-r:]
    # 找局部低点
    local_mins = []
    for j in range(2, len(p) - 1):
        if p.iloc[j] < p.iloc[j - 1] and p.iloc[j] < p.iloc[j + 1]:
            local_mins.append((j, p.iloc[j], m.iloc[j]))
    if len(local_mins) < 2:
        return False
    # 底背离：最近的低点价格更低，但MACD更高
    return local_mins[-1][1] < local_mins[-2][1] and local_mins[-1][2] > local_mins[-2][2]


def load_index_data(data_dir=None):
    """加载成长100和价值100指数日线数据"""
    data_dir = Path(data_dir or INDEX_DATA_DIR)
    g = pd.read_csv(data_dir / 'index_480080.csv')
    v = pd.read_csv(data_dir / 'index_480081.csv')

    for d in (g, v):
        d['date'] = pd.to_datetime(d['date'])
        d['close'] = pd.to_numeric(d['close'], errors='coerce')

    g_close = g.set_index('date')['close'].astype(float).sort_index().dropna()
    v_close = v.set_index('date')['close'].astype(float).sort_index().dropna()

    common = g_close.index.intersection(v_close.index)
    return g_close.loc[common].sort_index(), v_close.loc[common].sort_index()


def generate_signals(g_close, v_close, params=None, ts_params=None):
    """生成 B2v6 策略信号

    Args:
        g_close: 成长100收盘价 Series
        v_close: 价值100收盘价 Series
        params: 核心参数 dict（默认使用 BASELINE_PARAMS）
        ts_params: 跟踪止损参数 dict（默认使用 TRAILING_STOP_PARAMS）

    Returns:
        df_factor: DataFrame，列为 date/symbol/signal/weight
    """
    p = {**BASELINE_PARAMS, **(params or {})}
    tp = {**TRAILING_STOP_PARAMS, **(ts_params or {})}

    # ---- 全局因子计算 ----
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()

    # T值 = 比价偏离度的标准化（z-score）
    t = ratio_dev / ratio_dev_std20

    # 比价MA20斜率（5日变化率）
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)

    # 价值动量（短周期/长周期）
    v_mom_s = v_close.pct_change(p['ms'])
    v_mom_l = v_close.pct_change(p['ml'])

    n = len(t)
    index = t.index

    # ================================================================
    # 第1层: 方向确认 (DC)
    # T>0 → BULL(成长), T<0 → BEAR(价值)
    # 需连续 dc 天方向一致才确认
    # ================================================================
    raw_dir = (t > 0).map({True: 'BULL', False: 'BEAR'})
    mask = np.ones(n, dtype=bool)
    for k in range(1, p['dc']):
        mask = mask & (raw_dir.values == np.roll(raw_dir.values, k))
        mask[:k] = False
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
            if i - last_switch >= p['dcd']:
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
    wt[is_weak] = 0.0

    # ================================================================
    # 第4层: B2v6 价值动量过滤（修复版）
    #
    # 【修复】原始B2v3中，当DC方向为value且价值动量负时，
    #  actual_dir被设为growth（反手买成长），造成方向标记错误。
    # 修复后：direction保持value，权重0，直接空仓等待。
    #
    # 逻辑:
    #   - DC方向=growth → 正常买入growth
    #   - DC方向从growth切到value，价值动量负 → 空仓(direction=value, wt=0)
    #   - 等待状态中价值动量转正 → 恢复value持仓
    #   - 等待状态中DC方向切回growth → 正常买入growth
    #   - 当前已持有value → B2不干预
    # ================================================================
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    actual_dir = dir_series.copy()
    actual_wt = wt.copy()
    prev_held = 'growth'
    waiting_for_value = False

    for i in range(n):
        dc_dir = dir_series.iloc[i]
        current_wt = wt.iloc[i]

        if current_wt <= 0.001:
            # 【修复】不更新prev_held，不重置waiting状态
            # 让prev_held保持上次实际持仓的方向，避免方向标记错误
            continue

        if dc_dir == 'growth':
            actual_dir.iloc[i] = 'growth'
            actual_wt.iloc[i] = 1.0
            prev_held = 'growth'
            waiting_for_value = False

        elif dc_dir == 'value':
            if prev_held == 'growth' and not waiting_for_value:
                # 第一次从growth切换到value
                if v_mom_s.iloc[i] < 0:
                    # 【修复】direction=value，不反手标记为growth
                    # 当成长跌+价值跌时，直接空仓，不反手买成长
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 0.0
                    waiting_for_value = True
                else:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 1.0
                    prev_held = 'value'
                    waiting_for_value = False

            elif waiting_for_value:
                # 等待状态：价值动量负→继续保持空仓，方向为value
                if v_mom_s.iloc[i] >= 0:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 1.0
                    prev_held = 'value'
                    waiting_for_value = False
                else:
                    # 【修复】direction=value，不反手标记为growth
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 0.0

            else:
                # prev_held == 'value'，正常持有value
                actual_dir.iloc[i] = 'value'
                actual_wt.iloc[i] = 1.0
                prev_held = 'value'
                waiting_for_value = False

    dir_series = actual_dir
    wt = actual_wt

    # ================================================================
    # 第5层: BIAS 超买保护
    # 持仓标的偏离MA20 > bias_high → 清仓
    # ================================================================
    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ================================================================
    # 第6层: 最高点跟踪止损 + 冷却期 + 信号变化恢复
    #
    # 触发止损后直接清仓（wt=0.0），进入冷却期。
    # 冷却期结束后，等待DC信号下一次变化再调仓。
    #
    # 设计逻辑：
    #   1. 跟踪持仓期间最高价（HWM），从最高点回撤>9%触发止损
    #   2. 止损后清仓，进入冷却期（默认10天），冷却期内保持空仓
    #   3. 冷却期结束 → 不立即入场，等待DC信号方向变化再调仓
    #   4. HWM管理：仅在方向改变时重置HWM，第3/4/5层清零时不重置
    #      防止第3/4/5层短暂清零导致HWM重置，避免止损无法触发
    # ================================================================
    if tp['enabled'] and not NAV_STOP['enabled']:
        trailing_st = tp['st']
        cool_days = tp.get('cool_days', 10)

        in_cooldown = False          # 止损触发后的冷却期
        cool_count = 0               # 冷却期计数
        stopped_dir = None           # 止损时的方向（用于判断信号变化）
        hwm = None
        last_dir = None              # 上一日方向，用于检测方向变化重置HWM
        base_dir = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

        for i in range(n):
            current_dir = dir_series.iloc[i]
            current_wt = wt.iloc[i]

            if pd.isna(current_dir) or pd.isna(current_wt):
                continue

            # 获取当前方向的价格
            if current_dir == 'growth':
                price = g_close.iloc[i]
            else:
                price = v_close.iloc[i]

            if pd.isna(price):
                continue

            # ----------------------------------------------------------
            # HWM初始化与方向变化重置
            # - 首次持仓时初始化HWM
            # - 方向改变时重置HWM（新持有周期开始）
            # - 关键：第3/4/5层清零时不重置HWM，避免止损无法触发
            # ----------------------------------------------------------
            if hwm is None and current_wt > 0.001:
                hwm = price
            elif last_dir is not None and current_dir != last_dir and current_wt > 0.001:
                # 方向改变 → 重置HWM（新方向的新持有周期）
                hwm = price

            # ----------------------------------------------------------
            # 冷却期：止损触发后清仓，等待冷却期结束
            # 冷却期结束后保持stopped_dir，不重置，让信号等待逻辑生效
            # ----------------------------------------------------------
            if in_cooldown:
                cool_count += 1
                wt.iloc[i] = 0.0

                if cool_count >= cool_days:
                    # 冷却期结束，解除冷却期状态
                    # 但stopped_dir保持原值，让"信号变化等待"逻辑接管
                    in_cooldown = False
                    cool_count = 0
                    # 不重置stopped_dir → 下一轮循环进入信号变化等待
                else:
                    continue  # 冷却期内保持空仓

            # ----------------------------------------------------------
            # 冷却期后：等待DC信号方向变化再入场
            # 如果止损时的方向与当前base_dir不同，说明信号已变化
            # ----------------------------------------------------------
            if stopped_dir is not None:
                current_base = base_dir.iloc[i] if i < len(base_dir) else None
                if current_base is not None and current_base != stopped_dir:
                    # 信号已变化 → 解除锁定，跟随新方向
                    stopped_dir = None
                    new_dir = current_base
                    dir_series.iloc[i] = new_dir
                    wt.iloc[i] = 1.0
                    if new_dir == 'growth':
                        hwm = g_close.iloc[i]
                    else:
                        hwm = v_close.iloc[i]
                    last_dir = new_dir
                    continue
                else:
                    # 信号未变化 → 保持空仓
                    wt.iloc[i] = 0.0
                    last_dir = current_dir
                    continue

            # ----------------------------------------------------------
            # 空仓时跳过止损检查
            # 注意：第3/4/5层清零时至此，保留HWM不重置
            # ----------------------------------------------------------
            if current_wt <= 0.001:
                last_dir = current_dir
                continue

            # ----------------------------------------------------------
            # 正常状态：更新最高点 + 检查回撤
            # ----------------------------------------------------------
            if hwm is None or price > hwm:
                hwm = price

            if hwm > 0:
                drawdown = price / hwm - 1
                if drawdown < -trailing_st:
                    # 触发止损 → 清仓 + 进入冷却期
                    in_cooldown = True
                    cool_count = 0
                    stopped_dir = current_dir  # 记录止损时的方向
                    wt.iloc[i] = 0.0
                    last_dir = current_dir
                    continue

            last_dir = current_dir

    # ================================================================
    # 第7层: 全局净值止损（预留，当前禁用）
    # ================================================================
    if NAV_STOP['enabled']:
        stop_pct = NAV_STOP['stop_pct']
        min_cool = NAV_STOP['min_cool']
        nav_prev = 1000000.0
        hwm_nav = 1000000.0
        stopped = False
        sed = None
        cool_cnt = 0
        base_dir = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

        for i in range(1, n):
            if wt.iloc[i] > 0.001 and pd.notna(dir_series.iloc[i]):
                d = dir_series.iloc[i]
                if d == 'growth':
                    nav_now = nav_prev * (1 + (g_close.iloc[i] / g_close.iloc[i - 1] - 1) * wt.iloc[i])
                else:
                    nav_now = nav_prev * (1 + (v_close.iloc[i] / v_close.iloc[i - 1] - 1) * wt.iloc[i])
            else:
                nav_now = nav_prev

            if nav_now > hwm_nav: hwm_nav = nav_now
            if stopped: cool_cnt += 1

            if not stopped:
                dd = (hwm_nav - nav_now) / hwm_nav
                if dd > stop_pct:
                    stopped = True
                    sed = dir_series.iloc[i] if pd.notna(dir_series.iloc[i]) else 'growth'
                    hwm_nav = nav_now
                    wt.iloc[i] = 0.0
                    cool_cnt = 0
            elif cool_cnt >= min_cool:
                recovered = False
                dc_t = base_dir.iloc[i]
                if sed == 'growth' and dc_t == 'value':
                    dir_series.iloc[i] = 'value'; wt.iloc[i] = 1.0; recovered = True
                elif sed == 'value' and dc_t == 'growth':
                    dir_series.iloc[i] = 'growth'; wt.iloc[i] = 1.0; recovered = True
                if recovered:
                    stopped = False; sed = None; cool_cnt = 0
                else:
                    wt.iloc[i] = 0.0
            elif stopped:
                wt.iloc[i] = 0.0

            nav_prev = nav_now

    return _signals_to_df_factor(dir_series, wt)


def _signals_to_df_factor(dir_series, wt):
    """将方向序列和权重序列转换为 df_factor 格式"""
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
    # 快速验证：生成基线信号并统计
    print("[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ {g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    print("\n[2] 生成B2v6信号...")
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
    out_path = out_dir / 'df_factor_b2v6.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")