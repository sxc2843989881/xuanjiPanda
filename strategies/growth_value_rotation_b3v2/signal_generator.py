"""B3v2-火星MACD双绿空仓版 成长价值轮动策略 — 信号生成器

B3v2 = B3 + 第5层(MACD双绿柱空仓)

共5层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
  第5层 MACD双绿 — 成长和价值的MACD柱均为负值 → 空仓
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


def generate_signals(g_close, v_close, params=None):
    """生成 B3v2-火星MACD双绿空仓版策略信号

    B3v2 = B3 + 第5层(MACD双绿柱空仓)

    共5层：
      第1层 DC   — 方向确认：T值方向需连续 dc 天一致
      第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
      第3层 弱信号 — T弱+斜率弱 → 空仓
      第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓
      第5层 MACD双绿 — 成长和价值的MACD柱均为负值 → 空仓

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
    # 第4层: 方向映射 + BIAS 超买保护
    # BULL → growth, BEAR → value
    # 持仓标的偏离MA20 > bias_high → 清仓
    #
    # 注意：B3去掉了B2价值动量过滤层，所以方向直接映射
    # ================================================================
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ================================================================
    # 第5层: MACD双绿柱空仓（B3v2新增）
    # 成长和价值的MACD柱均为负值（绿柱）→ 市场全面下跌，空仓
    # ================================================================
    g_ema12 = g_close.ewm(span=12, adjust=False).mean()
    g_ema26 = g_close.ewm(span=26, adjust=False).mean()
    g_macd_line = g_ema12 - g_ema26
    g_macd_signal = g_macd_line.ewm(span=9, adjust=False).mean()
    g_macd_hist = g_macd_line - g_macd_signal  # >0: 红柱, <0: 绿柱

    v_ema12 = v_close.ewm(span=12, adjust=False).mean()
    v_ema26 = v_close.ewm(span=26, adjust=False).mean()
    v_macd_line = v_ema12 - v_ema26
    v_macd_signal = v_macd_line.ewm(span=9, adjust=False).mean()
    v_macd_hist = v_macd_line - v_macd_signal  # >0: 红柱, <0: 绿柱

    # 双绿柱：两者MACD均为负值 → 空仓
    both_green = (g_macd_hist < 0) & (v_macd_hist < 0)
    wt[both_green] = 0.0

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


if __name__ == '__main__':
    # 快速验证：生成信号并统计
    print("[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ {g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    print("\n[2] 生成B3信号...")
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
    out_path = out_dir / 'df_factor_b3.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")