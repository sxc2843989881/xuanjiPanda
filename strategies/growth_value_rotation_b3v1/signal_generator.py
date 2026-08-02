"""B3v1-火星净值止损版 成长价值轮动策略 — 信号生成器

B3v1 = B3 + 净值止损机制

净值止损触发条件：
  - 净值从最高点回撤>9% → 全额清仓，进入锁定期

恢复逻辑（锁定期内自动检测）：
  - MACD恢复: 跟踪成长100和价值100的MACD柱状态
  - 两者皆为绿柱(负值)时 → 市场仍在下跌，继续锁定
  - 至少一个转红柱(正值)时 → 止跌企稳，解锁恢复正常信号
"""
import pandas as pd
import numpy as np
from pathlib import Path

from params import (
    GROWTH_SYMBOL, VALUE_SYMBOL,
    BASELINE_PARAMS, NAV_STOP_PARAMS, INDEX_DATA_DIR,
)


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


def generate_signals(g_close, v_close, params=None, nav_stop_params=None):
    """生成 B3v1 策略信号（B3 + 净值止损）

    Args:
        g_close: 成长100收盘价 Series
        v_close: 价值100收盘价 Series
        params: 核心参数 dict（默认使用 BASELINE_PARAMS）
        nav_stop_params: 净值止损参数 dict（默认使用 NAV_STOP_PARAMS）

    Returns:
        df_factor: DataFrame，列为 date/symbol/signal/weight
    """
    p = {**BASELINE_PARAMS, **(params or {})}
    ns = {**NAV_STOP_PARAMS, **(nav_stop_params or {})}

    # ---- 全局因子计算 ----
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()
    t = ratio_dev / ratio_dev_std20
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)

    n = len(t)
    index = t.index

    # ================================================================
    # 第1层: 方向确认 (DC)
    # ================================================================
    raw_dir = (t > 0).map({True: 'BULL', False: 'BEAR'})
    mask = np.ones(n, dtype=bool)
    for k in range(1, p['dc']):
        mask = mask & (raw_dir.values == np.roll(raw_dir.values, k))
        mask[:k] = False
    confirmed_dir = raw_dir.where(pd.Series(mask, index=index), np.nan)

    # ================================================================
    # 第2层: 方向冷却 (DCD)
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

    # 基础方向（用于场景A的DC信号判断）
    base_dir = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    # ---- 权重初始为 1.0 ----
    wt = pd.Series(1.0, index=index)

    # ================================================================
    # 第3层: T+斜率双重确认 → 空仓
    # ================================================================
    weak_slope = slope.abs() < p['slope_thresh']
    weak_t = t.abs() < p['rt']
    is_weak = weak_t & weak_slope
    wt[is_weak] = 0.0

    # ================================================================
    # 第4层: 方向映射 + BIAS 超买保护
    # ================================================================
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ================================================================
    # 第5层: 净值止损机制（简化版）
    # 仅从高点回撤>9% → 清仓，锁定
    # 恢复: MACD柱状态决定冷却期，双绿柱时绝不再入场
    # ================================================================
    if ns.get('enabled', True):
        nav_dd_thresh = ns['nav_dd']         # 0.09

        # 每日收益率
        g_ret = g_close.pct_change()
        v_ret = v_close.pct_change()

        # 净值序列
        nav = pd.Series(1.0, index=index)
        nav_hwm = 1.0

        # 止损状态
        in_lockup = False
        locked_dir = None          # 触发止损时的方向
        lockup_date = None         # 触发止损的日期
        lockup_macd_tracker = {}   # MACD三重转色追踪器: {prev_state, transitions}

        # MACD计算 (12, 26, 9)
        g_ema12 = g_close.ewm(span=12, adjust=False).mean()
        g_ema26 = g_close.ewm(span=26, adjust=False).mean()
        g_macd_line = g_ema12 - g_ema26
        g_macd_signal = g_macd_line.ewm(span=9, adjust=False).mean()
        g_macd_hist = g_macd_line - g_macd_signal  # >0: 红柱(正), <0: 绿柱(负)

        v_ema12 = v_close.ewm(span=12, adjust=False).mean()
        v_ema26 = v_close.ewm(span=26, adjust=False).mean()
        v_macd_line = v_ema12 - v_ema26
        v_macd_signal = v_macd_line.ewm(span=9, adjust=False).mean()
        v_macd_hist = v_macd_line - v_macd_signal  # >0: 红柱(正), <0: 绿柱(负)

        # 日志收集
        trigger_logs = []
        recovery_logs = []

        for i in range(1, n):
            current_dir = dir_series.iloc[i]
            current_wt = wt.iloc[i]
            current_base = base_dir.iloc[i] if i < len(base_dir) else None
            date_str = str(index[i].date())

            if pd.isna(current_dir) or pd.isna(current_wt):
                continue

            # ----------------------------------------------------------
            # 1. 计算当日净值
            #    NAV[i] = NAV[i-1] * (1 + 昨日持仓的今日收益)
            # ----------------------------------------------------------
            if i >= 1:
                prev_dir = dir_series.iloc[i - 1]
                prev_wt = wt.iloc[i - 1]

                if pd.isna(prev_dir) or pd.isna(prev_wt) or prev_wt <= 0.001:
                    daily_ret = 0.0
                elif prev_dir == 'growth':
                    daily_ret = g_ret.iloc[i] if not pd.isna(g_ret.iloc[i]) else 0.0
                else:
                    daily_ret = v_ret.iloc[i] if not pd.isna(v_ret.iloc[i]) else 0.0

                nav.iloc[i] = nav.iloc[i - 1] * (1 + daily_ret)

            # 更新HWM
            if nav.iloc[i] > nav_hwm:
                nav_hwm = nav.iloc[i]

            # ----------------------------------------------------------
            # 2. 检查是否处于锁定状态
            # ----------------------------------------------------------
            if in_lockup:
                # ----------------------------------------------------------
                # MACD三重转色解锁: 绿→红→绿→红 三次转色后永久解锁
                # 锁定初期: 两者MACD绿柱(负值) → 禁止入场
                # 跟踪MACD状态(双绿=true, 其他=false), 每变化一次计一次转换
                # 累积3次转换(绿→红→绿→红)后永久解锁, 之后不再跟踪
                # ----------------------------------------------------------
                g_macd_green = (not pd.isna(g_macd_hist.iloc[i])) and (g_macd_hist.iloc[i] < 0)
                v_macd_green = (not pd.isna(v_macd_hist.iloc[i])) and (v_macd_hist.iloc[i] < 0)
                current_both_green = g_macd_green and v_macd_green

                # 确定MACD状态描述
                if g_macd_green and v_macd_green:
                    macd_state = "两者皆跌(双绿柱)"
                elif not g_macd_green and not v_macd_green:
                    macd_state = "两者皆涨(双红柱)"
                elif g_macd_green and not v_macd_green:
                    macd_state = "成长跌,价值涨(成长绿柱)"
                else:
                    macd_state = "价值跌,成长涨(价值绿柱)"

                # 初始化MACD转换追踪器（锁定第一天）
                if not lockup_macd_tracker:
                    lockup_macd_tracker['prev_state'] = current_both_green
                    lockup_macd_tracker['transitions'] = 0

                # 检测状态转换: 绿(true)↔非绿(false)
                if current_both_green != lockup_macd_tracker['prev_state']:
                    lockup_macd_tracker['transitions'] += 1
                    lockup_macd_tracker['prev_state'] = current_both_green
                    recovery_logs.append({
                        'date': date_str,
                        'locked_dir': locked_dir,
                        'macd_state': macd_state,
                        'g_macd_hist': f"{g_macd_hist.iloc[i]:.4f}",
                        'v_macd_hist': f"{v_macd_hist.iloc[i]:.4f}",
                        'reason': f"MACD转换#{lockup_macd_tracker['transitions']}: {macd_state}"
                    })

                # 需要3次转换: 绿→红(1) 红→绿(2) 绿→红(3) 后才永久解锁
                if lockup_macd_tracker['transitions'] >= 3:
                    recovery_logs.append({
                        'date': date_str,
                        'locked_dir': locked_dir,
                        'macd_state': macd_state,
                        'g_macd_hist': f"{g_macd_hist.iloc[i]:.4f}",
                        'v_macd_hist': f"{v_macd_hist.iloc[i]:.4f}",
                        'reason': f"MACD三重转色完成,永久解锁({macd_state})"
                    })

                    in_lockup = False
                    locked_dir = None
                    lockup_macd_tracker.clear()
                    nav_hwm = nav.iloc[i]  # 重置HWM为当前净值

                    # 恢复正常B3信号
                    if current_base is not None:
                        dir_series.iloc[i] = current_base
                        new_wt = 1.0
                        if is_weak.iloc[i]:
                            new_wt = 0.0
                        if current_base == 'growth' and g_bias.iloc[i] > p['bias_high']:
                            new_wt = 0.0
                        elif current_base == 'value' and v_bias.iloc[i] > p['bias_high']:
                            new_wt = 0.0
                        wt.iloc[i] = new_wt
                    else:
                        wt.iloc[i] = 0.0
                    continue
                else:
                    # 转换次数不足三次，继续锁定
                    wt.iloc[i] = 0.0
                    continue

            # ----------------------------------------------------------
            # 3. 正常状态：检查净值回撤>9%触发止损
            # ----------------------------------------------------------
            nav_dd_value = nav.iloc[i] / nav_hwm - 1

            if nav_dd_value < -nav_dd_thresh:
                # 触发净值止损 → 全额清仓，进入锁定
                # 记录触发时的MACD状态
                g_macd_green = (not pd.isna(g_macd_hist.iloc[i])) and (g_macd_hist.iloc[i] < 0)
                v_macd_green = (not pd.isna(v_macd_hist.iloc[i])) and (v_macd_hist.iloc[i] < 0)
                g_state = "绿柱" if g_macd_green else "红柱"
                v_state = "绿柱" if v_macd_green else "红柱"

                trigger_logs.append({
                    'date': date_str,
                    'dir': current_dir,
                    'nav_dd': f"{nav_dd_value*100:.1f}%",
                    'nav_hwm': f"{nav_hwm:.4f}",
                    'nav': f"{nav.iloc[i]:.4f}",
                    'g_macd': g_state,
                    'v_macd': v_state,
                    'reason': f"净值回撤{nav_dd_value*100:.1f}%>9% | 成长MACD={g_state}, 价值MACD={v_state}"
                })

                in_lockup = True
                locked_dir = current_dir
                lockup_date = date_str
                lockup_macd_tracker.clear()  # 重置MACD追踪器，准备重新计数

                # 清仓
                wt.iloc[i] = 0.0
                continue

    # ================================================================
    # 转换为df_factor
    # ================================================================
    df_factor = _signals_to_df_factor(dir_series, wt)

    # 打印日志
    _print_trigger_logs(trigger_logs, recovery_logs)

    return df_factor


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


def _print_trigger_logs(trigger_logs, recovery_logs):
    """打印触发和恢复日志"""
    if trigger_logs:
        print(f"\n  ⚡ 净值止损触发记录 ({len(trigger_logs)}次):")
        print(f"  {'日期':<12} {'方向':<8} {'回撤':<10} {'成长MACD':<10} {'价值MACD':<10} {'触发原因':<50}")
        print(f"  {'─'*100}")
        for lg in trigger_logs:
            print(f"  {lg['date']:<12} {lg['dir']:<8} {lg['nav_dd']:<10} {lg['g_macd']:<10} {lg['v_macd']:<10} {lg['reason']:<50}")

    if recovery_logs:
        print(f"\n  ✅ MACD恢复记录 ({len(recovery_logs)}次):")
        print(f"  {'日期':<12} {'锁定方向':<10} {'MACD状态':<26} {'原因':<40}")
        print(f"  {'─'*88}")
        for lg in recovery_logs:
            print(f"  {lg['date']:<12} {lg['locked_dir']:<10} {lg['macd_state']:<26} {lg['reason']:<40}")


if __name__ == '__main__':
    # 快速验证
    print("[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ {g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    print("\n[2] 生成B3v1信号（B3+净值止损）...")
    df_factor = generate_signals(g_close, v_close)
    print(f"  df_factor: {len(df_factor)} rows")

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
    out_path = out_dir / 'df_factor_b3v1.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")