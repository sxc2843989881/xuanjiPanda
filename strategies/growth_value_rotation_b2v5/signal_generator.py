"""B2v5 纯T值三者轮动策略 — 信号生成器（V1 max）

核心逻辑:
  以国债ETF为基准，计算成长相对国债的T_g和价值相对国债的T_v。
  选择T值最高的标的作为持仓方向：
  - T_g > T_v AND T_g > exit_thresh → 做多成长
  - T_v > T_g AND T_v > exit_thresh → 做多价值
  - 两者T值均低于exit_thresh → 持有国债（国债相对最强）
  
  无需止损：当市场暴跌时，成长/价值相对国债的T值自然下降，
  跌破阈值后自然切换到国债。
"""
import pandas as pd
import numpy as np
from pathlib import Path

from params import (
    GROWTH_SYMBOL, VALUE_SYMBOL, BOND_SYMBOL,
    ROTATION_PARAMS, INDEX_DATA_DIR,
)


def load_index_data(data_dir=None):
    """加载三个标的日线数据"""
    data_dir = Path(data_dir or INDEX_DATA_DIR)
    g = pd.read_csv(data_dir / 'index_480080.csv')
    v = pd.read_csv(data_dir / 'index_480081.csv')
    b = pd.read_csv(data_dir / 'index_511010.csv')

    for d in (g, v, b):
        d['date'] = pd.to_datetime(d['date'])
        d['close'] = pd.to_numeric(d['close'], errors='coerce')

    g_close = g.set_index('date')['close'].astype(float).sort_index().dropna()
    v_close = v.set_index('date')['close'].astype(float).sort_index().dropna()
    b_close = b.set_index('date')['close'].astype(float).sort_index().dropna()

    common = g_close.index.intersection(v_close.index).intersection(b_close.index)
    return g_close.loc[common].sort_index(), v_close.loc[common].sort_index(), b_close.loc[common].sort_index()


def _calc_t_value(asset_close, ref_close, lookback):
    ratio = asset_close / ref_close
    ratio_ma = ratio.rolling(lookback).mean()
    ratio_dev = ratio / ratio_ma - 1
    ratio_dev_std = ratio_dev.rolling(lookback).std()
    return ratio_dev / ratio_dev_std


def generate_signals(g_close, v_close, b_close, params=None):
    """生成 B2v5 纯T值三者轮动信号（V1 max方案）

    选择T值最大的标的，只有两者T值都低于退出阈值时才切到国债。
    """
    p = {**ROTATION_PARAMS, **(params or {})}

    lookback = p['lookback']
    entry_thresh = p['entry_thresh']
    exit_thresh = p['exit_thresh']
    switch_margin = p['switch_margin']
    dc = p['dc']
    dcd = p['dcd']
    bias_high = p['bias_high']
    ms = p['ms']

    n = len(g_close)
    index = g_close.index

    # 成长/价值相对国债的T值
    T_g = _calc_t_value(g_close, b_close, lookback)
    T_v = _calc_t_value(v_close, b_close, lookback)

    v_mom_s = v_close.pct_change(ms)
    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1

    # ================================================================
    # 状态机：选T值最大的标的
    # ================================================================
    raw_target = pd.Series('bond', index=index, dtype=object)
    state = 'bond'

    for i in range(n):
        tg = T_g.iloc[i]
        tv = T_v.iloc[i]

        if pd.isna(tg) or pd.isna(tv):
            raw_target.iloc[i] = state
            continue

        if state == 'bond':
            if tg > entry_thresh and tg >= tv:
                state = 'growth'
            elif tv > entry_thresh and tv > tg:
                state = 'value'
        elif state == 'growth':
            if tg < exit_thresh:
                state = 'bond'
            elif tv > tg + switch_margin and tv > entry_thresh:
                state = 'value'
        elif state == 'value':
            if tv < exit_thresh:
                state = 'bond'
            elif tg > tv + switch_margin and tg > entry_thresh:
                state = 'growth'

        raw_target.iloc[i] = state

    # ================================================================
    # DC + DCD
    # ================================================================
    confirmed = raw_target.copy()
    last_switch = -dcd - 1
    prev = 'bond'

    for i in range(1, n):
        curr_raw = raw_target.iloc[i]

        if curr_raw != prev:
            consistent = True
            for k in range(1, dc):
                if i - k < 0 or raw_target.iloc[i - k] != curr_raw:
                    consistent = False
                    break
            if consistent and (i - last_switch >= dcd):
                last_switch = i
                prev = curr_raw
            confirmed.iloc[i] = prev
        else:
            confirmed.iloc[i] = prev

    confirmed.iloc[0] = 'bond' if pd.isna(confirmed.iloc[0]) else confirmed.iloc[0]

    # ================================================================
    # B2价值动量
    # ================================================================
    asset = confirmed.copy()
    prev_held = 'bond' if pd.isna(asset.iloc[0]) else asset.iloc[0]
    waiting_for_value = False

    for i in range(1, n):
        curr = asset.iloc[i]

        if curr == 'value' and prev_held != 'value' and not waiting_for_value:
            if pd.isna(v_mom_s.iloc[i]) or v_mom_s.iloc[i] < 0:
                asset.iloc[i] = 'bond'
                waiting_for_value = True
            else:
                prev_held = 'value'
        elif waiting_for_value:
            if curr == 'value':
                if pd.notna(v_mom_s.iloc[i]) and v_mom_s.iloc[i] >= 0:
                    asset.iloc[i] = 'value'
                    prev_held = 'value'
                    waiting_for_value = False
                else:
                    asset.iloc[i] = 'bond'
            else:
                waiting_for_value = False
                prev_held = curr
        else:
            if curr != prev_held:
                prev_held = curr

    # ================================================================
    # BIAS
    # ================================================================
    extreme_g = (asset == 'growth') & (g_bias > bias_high)
    extreme_v = (asset == 'value') & (v_bias > bias_high)
    asset[extreme_g | extreme_v] = 'bond'

    return _signals_to_df_factor(asset)


def _signals_to_df_factor(asset):
    """将资产序列转换为 df_factor 格式"""
    records = []
    dates = asset.index
    for i in range(len(dates)):
        d = dates[i]
        a = asset.iloc[i]
        date_str = d.strftime('%Y%m%d')

        if pd.isna(a):
            a = 'bond'

        if a == 'growth':
            records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                            'signal': 'buy', 'weight': 1.0})
            records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
            records.append({'date': date_str, 'symbol': BOND_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
        elif a == 'value':
            records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                            'signal': 'buy', 'weight': 1.0})
            records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
            records.append({'date': date_str, 'symbol': BOND_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
        else:
            records.append({'date': date_str, 'symbol': BOND_SYMBOL,
                            'signal': 'buy', 'weight': 1.0})
            records.append({'date': date_str, 'symbol': GROWTH_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})
            records.append({'date': date_str, 'symbol': VALUE_SYMBOL,
                            'signal': 'sell', 'weight': 0.0})

    df = pd.DataFrame(records)
    df['date'] = df['date'].astype(str)
    return df


if __name__ == '__main__':
    print("[1] 加载指数数据...")
    g_close, v_close, b_close = load_index_data()

    print("\n[2] 生成 B2v5 V1 max 纯T值轮动信号...")
    df_factor = generate_signals(g_close, v_close, b_close)

    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    bond_days = (buy_rows['symbol'] == BOND_SYMBOL).sum()
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"国债:{bond_days}天({bond_days/total*100:.1f}%)")

    out_dir = Path(__file__).parent / 'df_factors'
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / 'df_factor_b2v5_rotation.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n保存至: {out_path}")