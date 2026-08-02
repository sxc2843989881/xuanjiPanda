"""
B2v6 底背离版本 — 完整调仓分析脚本

恢复原始B2v6的三层恢复逻辑（场景A快速轮动+场景B底背离解锁），
同时保留HWM修复（仓位从空仓变持仓时重置HWM，防止假止损）。
输出详细调仓日志，逐条分析止损触发、底背离判定和执行。
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import sys
import os

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
    """判断过去lookback天内是否出现MACD底背离"""
    r = min(lookback, len(price) - 1)
    if r < 10:
        return False
    p = price[-r:]
    m = macd[-r:]
    local_mins = []
    for j in range(2, len(p) - 1):
        if p.iloc[j] < p.iloc[j - 1] and p.iloc[j] < p.iloc[j + 1]:
            local_mins.append((j, p.iloc[j], m.iloc[j]))
    if len(local_mins) < 2:
        return False
    return local_mins[-1][1] < local_mins[-2][1] and local_mins[-1][2] > local_mins[-2][2]


def _check_recover_with_divergence(idx, price, macd):
    """检查是否满足底背离恢复条件：站上10日均线 + MACD底背离"""
    if idx < 10:
        return False
    ma10 = price.iloc[idx - 9:idx + 1].mean()
    if price.iloc[idx] <= ma10:
        return False
    return _has_bottom_divergence(
        price.iloc[max(0, idx - 20):idx + 1],
        macd.iloc[max(0, idx - 20):idx + 1]
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


def generate_signals_with_analysis(g_close, v_close, params=None, ts_params=None):
    """
    生成B2v6信号（带底背离恢复），并输出详细调仓日志

    第6层设计：
      1. 跟踪持仓期间最高价（HWM），从最高点回撤>9%触发止损
      2. 止损后直接清仓（sw_deep=0）
      3. 三层恢复逻辑：
         场景A：DC信号切换到另一方 + 5日动量>2% → 直接轮动
         场景B：双方向均下跌 → 任一方向MACD底背离后解锁，等DC信号下次变化再入场
      4. HWM管理：仓位从空仓变持仓时重置HWM，第3/4/5层清零时不重置
    """
    p = {**BASELINE_PARAMS, **(params or {})}
    tp = {**TRAILING_STOP_PARAMS, **(ts_params or {})}

    # 计算全局因子
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()
    t = ratio_dev / ratio_dev_std20
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)
    v_mom_s = v_close.pct_change(p['ms'])
    v_mom_l = v_close.pct_change(p['ml'])

    # MACD计算
    g_macd = _calc_macd(g_close)
    v_macd = _calc_macd(v_close)

    n = len(t)
    index = t.index

    # ===== 第1层 DC =====
    raw_dir = (t > 0).map({True: 'BULL', False: 'BEAR'})
    mask = np.ones(n, dtype=bool)
    for k in range(1, p['dc']):
        mask = mask & (raw_dir.values == np.roll(raw_dir.values, k))
        mask[:k] = False
    confirmed_dir = raw_dir.where(pd.Series(mask, index=index), np.nan)

    # ===== 第2层 DCD =====
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

    # 权重初始为1.0
    wt = pd.Series(1.0, index=index)

    # ===== 第3层 弱信号 =====
    weak_slope = slope.abs() < p['slope_thresh']
    weak_t = t.abs() < p['rt']
    is_weak = weak_t & weak_slope
    wt[is_weak] = 0.0

    # ===== 第4层 B2v6 =====
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})
    actual_dir = dir_series.copy()
    actual_wt = wt.copy()
    prev_held = 'growth'
    waiting_for_value = False

    for i in range(n):
        dc_dir = dir_series.iloc[i]
        current_wt = wt.iloc[i]
        if current_wt <= 0.001:
            continue
        if dc_dir == 'growth':
            actual_dir.iloc[i] = 'growth'
            actual_wt.iloc[i] = 1.0
            prev_held = 'growth'
            waiting_for_value = False
        elif dc_dir == 'value':
            if prev_held == 'growth' and not waiting_for_value:
                if v_mom_s.iloc[i] < 0:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 0.0
                    waiting_for_value = True
                else:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 1.0
                    prev_held = 'value'
                    waiting_for_value = False
            elif waiting_for_value:
                if v_mom_s.iloc[i] >= 0:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 1.0
                    prev_held = 'value'
                    waiting_for_value = False
                else:
                    actual_dir.iloc[i] = 'value'
                    actual_wt.iloc[i] = 0.0
            else:
                actual_dir.iloc[i] = 'value'
                actual_wt.iloc[i] = 1.0
                prev_held = 'value'
                waiting_for_value = False

    dir_series = actual_dir
    wt = actual_wt

    # ===== 第5层 BIAS =====
    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ===== 第6层: 最高点跟踪止损 + 三层恢复逻辑 =====
    stop_logs = []
    recovery_logs = []
    divergence_logs = []

    if tp['enabled'] and not NAV_STOP['enabled']:
        trailing_st = tp['st']
        cool_days = tp.get('cool_days', 10)

        in_cooldown = False
        cool_count = 0
        stopped_dir = None
        hwm = None
        last_dir = None
        last_wt = 0.0
        base_dir = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

        # 底背离状态
        divergence_unlocked = False
        unlocked_date = None

        for i in range(n):
            current_dir = dir_series.iloc[i]
            current_wt = wt.iloc[i]
            date_str = str(index[i].date())

            if pd.isna(current_dir) or pd.isna(current_wt):
                continue

            if current_dir == 'growth':
                price = g_close.iloc[i]
            else:
                price = v_close.iloc[i]

            if pd.isna(price):
                continue

            # ----------------------------------------------------------
            # HWM初始化与重置
            # 关键修复：只在方向改变时重置HWM
            # 第3/4/5层清零后恢复时不重置（避免止损被回避）
            # ----------------------------------------------------------
            if hwm is None and current_wt > 0.001:
                hwm = price
                last_dir = current_dir
            elif last_dir is not None and current_dir != last_dir and current_wt > 0.001:
                # 方向改变 → 重置HWM（新方向的新持有周期）
                hwm = price
                last_dir = current_dir

            # ----------------------------------------------------------
            # 冷却期
            # ----------------------------------------------------------
            if in_cooldown:
                cool_count += 1
                wt.iloc[i] = 0.0
                if cool_count >= cool_days:
                    in_cooldown = False
                    cool_count = 0
                else:
                    continue

            # ----------------------------------------------------------
            # 冷却期后：三层恢复逻辑
            # ----------------------------------------------------------
            if stopped_dir is not None:
                current_base = base_dir.iloc[i] if i < len(base_dir) else None
                other_dir = 'value' if stopped_dir == 'growth' else 'growth'

                # 计算双方5日动量
                g_mom5 = g_close.iloc[i] / g_close.iloc[i - 5] - 1 if i >= 5 else 0
                v_mom5 = v_close.iloc[i] / v_close.iloc[i - 5] - 1 if i >= 5 else 0

                # 场景A: DC信号切换到另一方 + 5日动量>2%
                if current_base is not None and current_base != stopped_dir:
                    other_mom5 = g_mom5 if other_dir == 'growth' else v_mom5
                    if other_mom5 > 0.02:
                        recovery_logs.append({
                            'date': date_str, 'scene': 'A',
                            'stopped_dir': stopped_dir, 'new_dir': other_dir,
                            'dc_switch': f"{stopped_dir}→{other_dir}",
                            'mom5': f"{other_mom5*100:.1f}%",
                            'reason': f"DC切换到{other_dir}+5日动量{other_mom5*100:.1f}%>2%"
                        })
                        stopped_dir = None
                        dir_series.iloc[i] = other_dir
                        wt.iloc[i] = 1.0
                        hwm = v_close.iloc[i] if other_dir == 'value' else g_close.iloc[i]
                        last_dir = other_dir
                        continue

                # 场景B: 底背离解锁
                if i >= 20:
                    g_divergence = _check_recover_with_divergence(i, g_close, g_macd)
                    v_divergence = _check_recover_with_divergence(i, v_close, v_macd)

                    if g_divergence or v_divergence:
                        divergence_dir = 'growth' if g_divergence else 'value'
                        divergence_logs.append({
                            'date': date_str, 'divergence_dir': divergence_dir,
                            'g_divergence': g_divergence, 'v_divergence': v_divergence,
                            'stopped_dir': stopped_dir,
                            'g_price': f"{g_close.iloc[i]:.2f}", 'v_price': f"{v_close.iloc[i]:.2f}",
                        })

                        if not divergence_unlocked:
                            divergence_unlocked = True
                            unlocked_date = date_str
                            recovery_logs.append({
                                'date': date_str, 'scene': 'B_unlock',
                                'stopped_dir': stopped_dir,
                                'divergence': f"{divergence_dir}底背离",
                                'reason': f"底背离解锁，等待DC信号变化"
                            })
                            wt.iloc[i] = 0.0
                            continue

                # 场景B第二阶段：解锁后等待DC信号变化
                if divergence_unlocked:
                    if current_base is not None and current_base != stopped_dir:
                        recovery_logs.append({
                            'date': date_str, 'scene': 'B_enter',
                            'stopped_dir': stopped_dir, 'new_dir': current_base,
                            'dc_switch': f"{stopped_dir}→{current_base}",
                            'reason': f"底背离解锁后DC信号变化，跟随{current_base}"
                        })
                        stopped_dir = None
                        divergence_unlocked = False
                        unlocked_date = None
                        dir_series.iloc[i] = current_base
                        wt.iloc[i] = 1.0
                        hwm = g_close.iloc[i] if current_base == 'growth' else v_close.iloc[i]
                        last_dir = current_base
                        continue
                    else:
                        wt.iloc[i] = 0.0
                        last_dir = current_dir
                        continue

                # 无场景触发 → 保持空仓
                wt.iloc[i] = 0.0
                last_dir = current_dir
                continue

            # ----------------------------------------------------------
            # 空仓时跳过止损检查
            # ----------------------------------------------------------
            if current_wt <= 0.001:
                last_wt = 0.0
                continue

            # ----------------------------------------------------------
            # 正常状态：更新最高点 + 检查回撤
            # ----------------------------------------------------------
            if hwm is None or price > hwm:
                hwm = price

            if hwm > 0:
                drawdown = price / hwm - 1
                if drawdown < -trailing_st:
                    stop_logs.append({
                        'date': date_str, 'dir': current_dir,
                        'price': f"{price:.2f}", 'hwm': f"{hwm:.2f}",
                        'drawdown': f"{drawdown*100:.2f}%",
                        'trigger': f"从HWM({hwm:.2f})回撤{drawdown*100:.2f}% > {trailing_st*100:.0f}%"
                    })
                    in_cooldown = True
                    cool_count = 0
                    stopped_dir = current_dir
                    divergence_unlocked = False
                    unlocked_date = None
                    wt.iloc[i] = 0.0
                    last_wt = 0.0
                    continue

            last_wt = current_wt

    # 转换为df_factor
    records = []
    for i in range(len(index)):
        d = index[i]
        s = dir_series.iloc[i]
        w = float(wt.iloc[i])
        date_str = d.strftime('%Y%m%d')
        if pd.isna(s) or pd.isna(w):
            s, w = 'growth', 0.0
        if w > 0.001:
            if s == 'growth':
                records.append({'date': date_str, 'symbol': GROWTH_SYMBOL, 'signal': 'buy', 'weight': round(w, 4)})
                records.append({'date': date_str, 'symbol': VALUE_SYMBOL, 'signal': 'sell', 'weight': 0.0})
            else:
                records.append({'date': date_str, 'symbol': VALUE_SYMBOL, 'signal': 'buy', 'weight': round(w, 4)})
                records.append({'date': date_str, 'symbol': GROWTH_SYMBOL, 'signal': 'sell', 'weight': 0.0})
        else:
            records.append({'date': date_str, 'symbol': GROWTH_SYMBOL, 'signal': 'sell', 'weight': 0.0})
            records.append({'date': date_str, 'symbol': VALUE_SYMBOL, 'signal': 'sell', 'weight': 0.0})

    df_factor = pd.DataFrame(records)
    df_factor['date'] = df_factor['date'].astype(str)

    stop_df = pd.DataFrame(stop_logs) if stop_logs else pd.DataFrame()
    recovery_df = pd.DataFrame(recovery_logs) if recovery_logs else pd.DataFrame()
    divergence_df = pd.DataFrame(divergence_logs) if divergence_logs else pd.DataFrame()

    return df_factor, stop_df, recovery_df, divergence_df


def print_stop_analysis(stop_df, recovery_df, divergence_df):
    """打印止损分析报告"""
    print("\n" + "=" * 80)
    print("  B2v6 底背离版本 — 止损触发分析报告")
    print("=" * 80)

    if stop_df.empty:
        print("\n  ❌ 止损从未触发！请检查HWM管理逻辑")
        return

    print(f"\n  📊 止损触发次数: {len(stop_df)} 次")
    print(f"  📊 恢复事件次数: {len(recovery_df)} 次")
    print(f"  📊 底背离检测次数: {len(divergence_df)} 次")

    print("\n" + "-" * 80)
    print("  止损触发记录")
    print("-" * 80)
    for _, row in stop_df.iterrows():
        print(f"  [{row['date']}] {row['dir']:6s} 价格={row['price']:>8s}  HWM={row['hwm']:>8s}  "
              f"回撤={row['drawdown']:>7s}  → {row['trigger']}")

    if not recovery_df.empty:
        print("\n" + "-" * 80)
        print("  恢复事件记录")
        print("-" * 80)
        for _, row in recovery_df.iterrows():
            scene = row['scene']
            if scene == 'A':
                print(f"  [{row['date']}] 场景A(快速轮动): 止损方向={row['stopped_dir']} → "
                      f"新方向={row['new_dir']}  动量={row['mom5']}  {row['reason']}")
            elif scene == 'B_unlock':
                print(f"  [{row['date']}] 场景B(底背离解锁): 止损方向={row['stopped_dir']}  "
                      f"底背离={row['divergence']}  {row['reason']}")
            elif scene == 'B_enter':
                print(f"  [{row['date']}] 场景B(信号变化入场): 止损方向={row['stopped_dir']} → "
                      f"新方向={row['new_dir']}  {row['reason']}")

    if not divergence_df.empty:
        print("\n" + "-" * 80)
        print("  底背离检测详情")
        print("-" * 80)
        for _, row in divergence_df.iterrows():
            print(f"  [{row['date']}] 底背离方向={row['divergence_dir']:6s}  "
                  f"成长底背离={row['g_divergence']} 价值底背离={row['v_divergence']}  "
                  f"止损方向={row['stopped_dir']}  "
                  f"成长价={row['g_price']:>8s} 价值价={row['v_price']:>8s}")

    # 统计
    if not recovery_df.empty:
        scene_a = len(recovery_df[recovery_df['scene'] == 'A'])
        scene_b_unlock = len(recovery_df[recovery_df['scene'] == 'B_unlock'])
        scene_b_enter = len(recovery_df[recovery_df['scene'] == 'B_enter'])
        print(f"\n  📈 恢复方式统计：")
        print(f"     场景A(快速轮动): {scene_a} 次")
        print(f"     场景B(底背离解锁): {scene_b_unlock} 次")
        print(f"     场景B(信号变化入场): {scene_b_enter} 次")

    if not divergence_df.empty and not recovery_df.empty:
        unlocked_dates = recovery_df[recovery_df['scene'] == 'B_unlock']['date'].tolist()
        entered_dates = recovery_df[recovery_df['scene'] == 'B_enter']['date'].tolist()
        if len(unlocked_dates) == len(entered_dates):
            print(f"\n  ⏱ 底背离解锁到入场等待时间：")
            for u, e in zip(unlocked_dates, entered_dates):
                du = datetime.strptime(u, '%Y-%m-%d')
                de = datetime.strptime(e, '%Y-%m-%d')
                days = (de - du).days
                print(f"     {u} → {e}: {days}天")


def main():
    print("=" * 80)
    print("  B2v6 底背离版本 — 完整调仓分析")
    print("=" * 80)

    # 1. 加载数据
    print("\n[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ "
          f"{g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    # 2. 生成信号
    print("\n[2] 生成B2v6底背离版本信号...")
    df_factor, stop_df, recovery_df, divergence_df = generate_signals_with_analysis(g_close, v_close)

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 3. 保存信号
    out_dir = Path(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b2v6\df_factors')
    out_dir.mkdir(exist_ok=True)
    df_factor.to_csv(str(out_dir / 'df_factor_b2v6_divergence.csv'), index=False, encoding='utf-8-sig')
    print(f"  df_factor已保存")

    # 4. 打印分析报告
    print_stop_analysis(stop_df, recovery_df, divergence_df)

    # 5. 保存日志
    log_dir = Path(r'c:\Users\28439\.trae-cn\work\6a60e0ea5437f897f4891301')
    if not stop_df.empty:
        stop_df.to_csv(str(log_dir / 'stop_logs.csv'), index=False, encoding='utf-8-sig')
    if not recovery_df.empty:
        recovery_df.to_csv(str(log_dir / 'recovery_logs.csv'), index=False, encoding='utf-8-sig')
    if not divergence_df.empty:
        divergence_df.to_csv(str(log_dir / 'divergence_logs.csv'), index=False, encoding='utf-8-sig')
    print(f"\n  📁 日志已保存至 {log_dir}")

    return df_factor, stop_df, recovery_df, divergence_df


if __name__ == '__main__':
    main()