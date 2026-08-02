"""B1-木星 成长价值轮动策略 — 信号生成器

生成 PandaAI 引擎所需的 df_factor（date/symbol/signal/weight）。

策略架构（6层过滤）:
  第1层 DC  — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 B2  — 价值动量过滤：价值方向但价值动量为负 → 切回成长
  第5层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第6层 E5  — 止损：持仓标的20日跌幅>9% → 降仓至17%，冷却8天

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
    BASELINE_PARAMS, E5_PARAMS, INDEX_DATA_DIR,
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


def generate_signals(g_close, v_close, params=None, e5_params=None):
    """生成 B1-木星策略信号

    Args:
        g_close: 成长100收盘价 Series
        v_close: 价值100收盘价 Series
        params: 核心参数 dict（默认使用 BASELINE_PARAMS）
        e5_params: E5止损参数 dict（默认使用 E5_PARAMS）

    Returns:
        df_factor: DataFrame，列为 date/symbol/signal/weight
    """
    p = {**BASELINE_PARAMS, **(params or {})}
    ep = {**E5_PARAMS, **(e5_params or {})}

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

    # 价值动量（短周期/长周期）
    v_mom_s = v_close.pct_change(p['ms'])
    v_mom_l = v_close.pct_change(p['ml'])

    # N日跌幅（用于E5止损，lookback来自参数）
    e5_lookback = ep.get('lookback', 20)
    g_dd20 = g_close / g_close.shift(e5_lookback) - 1
    v_dd20 = v_close / v_close.shift(e5_lookback) - 1

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
    # 第4层: B2 价值动量过滤
    # 方向=价值 但 价值动量为负（短周期和长周期都≤0）→ 切回成长
    # ================================================================
    wrong_value = (dir_raw == 'BEAR') & (v_mom_s <= 0) & (v_mom_l <= 0)
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})
    dir_series[wrong_value] = 'growth'

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
    # 第6层: E5 止损机制
    # 持仓标的20日跌幅 > st(9%) → 降仓至 sw_deep(17%)，冷却 cd(8) 天
    # 冷却期内若再次触发则重置冷却期（e5_reset=True）
    # 冷却期结束后恢复正常仓位
    # ================================================================
    st_thresh = ep['st']
    cd_days = ep['cd']
    sw_deep = ep['sw_deep']
    e5_reset = ep['e5_reset']

    in_cooldown = False
    cooldown_count = 0
    current_e5_dir = None  # 记录E5触发时的方向

    for i in range(n):
        if pd.isna(dir_series.iloc[i]) or pd.isna(wt.iloc[i]):
            continue

        current_dir = dir_series.iloc[i]

        # 当前持仓标的的20日跌幅
        if current_dir == 'growth':
            dd20 = g_dd20.iloc[i]
        else:
            dd20 = v_dd20.iloc[i]

        if pd.isna(dd20):
            continue

        # 检查冷却期是否到期
        if in_cooldown:
            cooldown_count += 1
            if cooldown_count >= cd_days:
                in_cooldown = False
                cooldown_count = 0
                current_e5_dir = None

        # 检查是否触发E5止损
        if dd20 < -st_thresh:
            if in_cooldown and e5_reset:
                # 冷却期内再次触发 → 重置冷却期
                cooldown_count = 0
            elif not in_cooldown:
                # 首次触发 → 进入冷却期
                in_cooldown = True
                cooldown_count = 0
                current_e5_dir = current_dir

            # 冷却期内仓位降至 sw_deep
            wt.iloc[i] = sw_deep
        elif in_cooldown:
            # 冷却期内但未触发 → 维持降仓
            wt.iloc[i] = sw_deep
        # else: 正常持仓，wt保持不变

    return _signals_to_df_factor(dir_series, wt)


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


def generate_signals_with_stoploss_variant(g_close, v_close, variant='baseline',
                                           params=None, e5_params=None):
    """生成带有不同止损变体的信号

    Args:
        variant: 止损变体名称
            - 'baseline': 原版E5（降仓17%+冷却8天）
            - 'strict_9pct': 严格9%止损（完全清仓+20日均线恢复）
            - 'dir_lock_20d': 9%止损+方向锁定20天（可换反方向）
        params: 核心参数
        e5_params: E5参数

    Returns:
        df_factor: DataFrame
    """
    if variant == 'baseline':
        return generate_signals(g_close, v_close, params, e5_params)

    p = {**BASELINE_PARAMS, **(params or {})}

    # 先生成基线信号（前5层），然后替换第6层
    # 临时禁用E5以获取前5层结果
    temp_e5 = {**E5_PARAMS, 'st': 999}  # 设极大阈值使E5不触发
    # 使用内部函数重新计算（复用 generate_signals 的前5层逻辑）
    # 这里通过生成基线信号后替换第6层来实现

    # 实际实现：直接调用 generate_signals 获取基线，然后重新计算权重
    # 更简洁的方式是重构，但为保持代码清晰，这里直接实现变体

    # 计算全局因子
    ratio = g_close / v_close
    ratio_ma20 = ratio.rolling(20).mean()
    ratio_dev = ratio / ratio_ma20 - 1
    ratio_dev_std20 = ratio_dev.rolling(20).std()
    t = ratio_dev / ratio_dev_std20
    slope = (ratio_ma20 - ratio_ma20.shift(5)) / ratio_ma20.shift(5)
    v_mom_s = v_close.pct_change(p['ms'])
    v_mom_l = v_close.pct_change(p['ml'])

    n = len(t)
    index = t.index

    # 第1层 DC
    raw_dir = (t > 0).map({True: 'BULL', False: 'BEAR'})
    mask = np.ones(n, dtype=bool)
    for k in range(1, p['dc']):
        mask = mask & (raw_dir.values == np.roll(raw_dir.values, k))
        mask[:k] = False
    confirmed_dir = raw_dir.where(pd.Series(mask, index=index), np.nan)

    # 第2层 DCD
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

    wt = pd.Series(1.0, index=index)

    # 第3层 弱信号
    weak_slope = slope.abs() < p['slope_thresh']
    weak_t = t.abs() < p['rt']
    is_weak = weak_t & weak_slope
    wt[is_weak] = 0.0

    # 第4层 B2
    wrong_value = (dir_raw == 'BEAR') & (v_mom_s <= 0) & (v_mom_l <= 0)
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})
    dir_series[wrong_value] = 'growth'

    # 第5层 BIAS
    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # 第6层：根据变体实现不同止损
    st_thresh = 0.09  # 所有变体统一9%阈值

    if variant == 'strict_9pct':
        # 严格9%止损：完全清仓，价格站上20日均线后恢复
        ma20_g = g_close.rolling(20).mean()
        ma20_v = v_close.rolling(20).mean()
        in_stop = False
        hwm = None

        for i in range(n):
            if pd.isna(dir_series.iloc[i]) or pd.isna(wt.iloc[i]):
                continue
            current_dir = dir_series.iloc[i]
            price = g_close.iloc[i] if current_dir == 'growth' else v_close.iloc[i]
            ma = ma20_g.iloc[i] if current_dir == 'growth' else ma20_v.iloc[i]

            if pd.isna(price) or pd.isna(ma):
                continue

            if in_stop:
                # 等待价格站上20日均线恢复
                if price > ma and not is_weak.iloc[i]:
                    in_stop = False
                    hwm = price
                else:
                    wt.iloc[i] = 0.0
            else:
                if hwm is None or price > hwm:
                    hwm = price
                drawdown = price / hwm - 1 if hwm > 0 else 0
                if drawdown < -st_thresh:
                    in_stop = True
                    wt.iloc[i] = 0.0
                    hwm = None

    elif variant == 'dir_lock_20d':
        # 9%止损+方向锁定20天：触发方向锁定20天不可买入，可换反方向
        cooldown_days = 20
        locked_dir = None
        cooldown_count = 0
        hwm = None

        for i in range(n):
            if pd.isna(dir_series.iloc[i]) or pd.isna(wt.iloc[i]):
                continue
            current_dir = dir_series.iloc[i]
            price = g_close.iloc[i] if current_dir == 'growth' else v_close.iloc[i]
            if pd.isna(price):
                continue

            # 检查方向锁定是否到期
            if locked_dir is not None:
                cooldown_count += 1
                if cooldown_count >= cooldown_days:
                    locked_dir = None
                    cooldown_count = 0

            if locked_dir is not None and current_dir == locked_dir:
                # 当前方向被锁定 → 检查能否换到反方向
                other_dir = 'value' if locked_dir == 'growth' else 'growth'
                if not is_weak.iloc[i]:
                    if other_dir == 'growth' and g_bias.iloc[i] <= p['bias_high']:
                        wt.iloc[i] = 1.0
                        dir_series.iloc[i] = 'growth'
                        hwm = g_close.iloc[i]
                    elif other_dir == 'value' and v_bias.iloc[i] <= p['bias_high']:
                        wt.iloc[i] = 1.0
                        dir_series.iloc[i] = 'value'
                        hwm = v_close.iloc[i]
                    else:
                        wt.iloc[i] = 0.0
                else:
                    wt.iloc[i] = 0.0
            else:
                if hwm is None or price > hwm:
                    hwm = price
                drawdown = price / hwm - 1 if hwm > 0 else 0
                if drawdown < -st_thresh:
                    locked_dir = current_dir
                    cooldown_count = 0
                    wt.iloc[i] = 0.0
                    hwm = None

    return _signals_to_df_factor(dir_series, wt)


if __name__ == '__main__':
    # 快速验证：生成基线信号并统计
    print("[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ {g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    print("\n[2] 生成基线B1信号...")
    df_factor = generate_signals(g_close, v_close)
    print(f"  df_factor: {len(df_factor)} rows")
    print(f"  日期范围: {df_factor['date'].min()} ~ {df_factor['date'].max()}")

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  价值:{value_days}天({value_days/total*100:.1f}%)  空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 保存
    out_dir = Path(__file__).parent / 'df_factors'
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / 'df_factor_baseline.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")
