"""B3v4-成长动量止损版 成长价值轮动策略 — 信号生成器

B3v4 = B3 + 第5层(牛市高点止损·新高可解锁) + 第6层(熊市长期阴跌保护)
       + 第7层(价值回撤止损) + 第8层(黑天鹅保护)

共8层：
  第1层 DC   — 方向确认：T值方向需连续 dc 天一致
  第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
  第3层 弱信号 — T弱+斜率弱 → 空仓
  第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓

  B3v4新增：
    第5层 牛市高点止损 — 持有成长时，若60日动量>50%且5日动量转负
                        → 清仓成长，锁定60个交易日
                        → 若成长指数突破历史最高价，可提前解锁
    第6层 熊市阴跌保护 — 成长价格<90天均线 → 无法买入成长
    第7层 价值回撤止损 — 持有价值时，从持仓高点回撤>6%
                        → 清仓价值，锁定20个交易日
                        → 冷却期后恢复正常信号判断
    第8层 黑天鹅保护 — 5日内成长跌>8%且价值跌>5% → 全部空仓
                      等待价值20日动量>0时解除"""
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
    """生成 B3v4-成长动量止损版策略信号

    B3v4 = B3 + 第5层(牛市高点止损·新高可解锁) + 第6层(熊市长期阴跌保护)
           + 第7层(价值回撤止损) + 第8层(黑天鹅保护)

    共8层：
      第1层 DC   — 方向确认：T值方向需连续 dc 天一致
      第2层 DCD  — 方向冷却：换向后需等 dcd 天才允许再次换向
      第3层 弱信号 — T弱+斜率弱 → 空仓
      第4层 BIAS — 超买保护：偏离MA20>bias_high → 清仓

      B3v4新增：
      第5层 牛市高点止损 — 持有成长时，若60日动量>50%且5日动量转负
                           → 清仓成长，锁定60个交易日
                           → 若成长指数突破60日最高价，可提前解锁
      第6层 熊市阴跌保护 — 成长价格<90天均线 → 无法买入成长
      第7层 价值回撤止损 — 持有价值时，从持仓高点回撤>6%
                           → 清仓价值，锁定20个交易日
                           → 冷却期后恢复正常信号判断
      第8层 黑天鹅保护 — 5日内成长跌>8%且价值跌>5% → 全部空仓
                        等待价值20日动量>0时解除

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
    # ================================================================
    dir_series = dir_raw.map({'BULL': 'growth', 'BEAR': 'value'})

    g_bias = g_close / g_close.rolling(20).mean() - 1
    v_bias = v_close / v_close.rolling(20).mean() - 1
    extreme_g = (dir_series == 'growth') & (g_bias > p['bias_high'])
    extreme_v = (dir_series == 'value') & (v_bias > p['bias_high'])
    wt[extreme_g | extreme_v] = 0.0

    # ================================================================
    # 第5层: 牛市高点回撤止损 — 新高可解锁（B3v4新增/修改）
    #
    # 逻辑：持有成长方向时，若过去60日涨幅>50%，
    #       且最新5日动量转负 → 说明牛市顶部可能已到
    #       清仓成长，并锁定成长方向60个交易日不得买入
    #
    # 提前解锁条件：若成长指数价格突破触发前60日最高价，
    #              则提前结束锁定，允许重新买入成长
    #
    # 例如：指数从100跌到95触发止损（5日动量转负），
    #       触发前60日最高=100，后期涨到111突破100，则解锁
    #
    # 核心洞察：超级大回撤都发生在牛市顶点之后。
    #           在顶部区域，任何轮换都大概率亏钱。
    #           通过"60日涨幅过大+5日转弱"识别顶部，提前撤退。
    #           同时通过"新高可解锁"机制，避免在短暂回调后过早放行，
    #           又允许在真正的牛市中场休息后重新入场。
    # ================================================================
    g_mom_60d = g_close / g_close.shift(p['mom_60d_period']) - 1  # 60日动量
    g_mom_5d = g_close / g_close.shift(5) - 1                     # 5日动量
    g_high_60d = g_close.rolling(p['mom_60d_period']).max()             # 60日最高价（新高基准：触发前60日内的最高点）

    # 触发条件：持有成长 + 60日涨幅>50% + 5日动量转负
    growth_high_mom = (
        (dir_series == 'growth')
        & (g_mom_60d > p['mom_3m_thresh'])
        & (g_mom_5d < 0)
    )

    # 顺序追踪成长锁定状态
    growth_locked_until = -1
    growth_trigger_peak = None  # 触发时的历史最高价（新高基准）
    for i in range(n):
        # 检查新高解锁：若指数突破历史最高价，提前结束锁定
        if i < growth_locked_until and growth_trigger_peak is not None:
            if g_close.iloc[i] > growth_trigger_peak:
                growth_locked_until = -1
                growth_trigger_peak = None

        if i < growth_locked_until:
            # 锁定中：方向为成长时强制空仓
            if dir_series.iloc[i] == 'growth':
                wt.iloc[i] = 0.0

        # 检查是否触发止损（仅当当前有仓位时才触发）
        if growth_high_mom.iloc[i] and wt.iloc[i] > 0.001:
            growth_trigger_peak = g_high_60d.iloc[i]  # 记录60日最高价（新高基准）
            growth_locked_until = i + p['mom_5d_lock']  # 锁定60个交易日
            wt.iloc[i] = 0.0  # 当天清仓
            # 注意：锁定期间成长方向强制空仓，但价值方向不受影响

    # ================================================================
    # 第7层: 价值回撤止损 — 20天冷却期（B3v4新增）
    #
    # 逻辑：持有价值方向时，若价值价格从持仓期间高点回撤>6%，
    #       说明价值正在下跌，触发止损清仓
    #       清仓后设定20个交易日冷却期
    #       冷却期内不能买入价值方向
    #       冷却期结束后恢复正常信号判断
    #
    # 核心洞察：价值在极端熊市中同样会暴跌（如2015年股灾中价值跌-36%），
    #           第5层只锁定成长方向，不锁定价值方向。
    #           需要给价值方向也加上回撤保护机制。
    # ================================================================
    value_locked_until = -1
    value_holding_high = None  # 持仓期间价值最高价

    for i in range(n):
        # 冷却期：价值方向强制空仓
        if i < value_locked_until:
            if dir_series.iloc[i] == 'value':
                wt.iloc[i] = 0.0

        # 检查是否持有价值（方向为价值且权重>0）
        if dir_series.iloc[i] == 'value' and wt.iloc[i] > 0.001:
            # 新开仓或继续持有：更新持仓期间最高价
            if value_holding_high is None:
                value_holding_high = v_close.iloc[i]
            else:
                value_holding_high = max(value_holding_high, v_close.iloc[i])

            # 检查回撤是否超过阈值（从持仓高点回撤>6%）
            value_dd = v_close.iloc[i] / value_holding_high - 1
            if value_dd < -p['value_dd_thresh']:
                # 触发止损：清仓价值，设定冷却期
                value_locked_until = i + p['value_dd_cooldown']
                wt.iloc[i] = 0.0
                value_holding_high = None  # 重置持仓最高价
        else:
            # 不在持仓价值状态，重置持仓最高价
            value_holding_high = None

    # ================================================================
    # 第8层: 黑天鹅保护 — 系统性风险全仓空仓（B3v4新增）
    #
    # 逻辑：5日内成长跌>8% 且 价值跌>5% → 判断为黑天鹅事件
    #       触发后全部持仓清空（成长和价值都清仓）
    #       等待价值20日动量>0时解除锁定
    #
    # 核心洞察：2020年疫情、2015年股灾等系统性风险中，
    #           成长和价值同时暴跌，单方向保护机制不够。
    #           通过"双跌"条件识别系统性风险，提前撤退。
    #           用价值20日动量>0判断市场企稳，避免过早抄底。
    # ================================================================
    g_ret_5d = g_close / g_close.shift(p['black_swan_lookback']) - 1  # 成长5日收益
    v_ret_5d = v_close / v_close.shift(p['black_swan_lookback']) - 1  # 价值5日收益
    v_mom_20d = v_close / v_close.shift(p['black_swan_unlock_period']) - 1  # 价值20日动量

    # 触发条件：5日内成长跌>8% 且 价值跌>5%（双跌确认黑天鹅）
    black_swan_trigger = (
        (g_ret_5d < -p['black_swan_g_thresh'])
        & (v_ret_5d < -p['black_swan_v_thresh'])
    )

    # 顺序追踪黑天鹅锁定状态
    black_swan_locked = False
    for i in range(n):
        if black_swan_locked:
            # 检查解锁条件：价值20日动量>0 → 市场企稳
            if v_mom_20d.iloc[i] > 0:
                black_swan_locked = False
            else:
                # 锁定中：强制全部空仓（成长和价值都清仓）
                wt.iloc[i] = 0.0

        # 检查触发条件（有持仓时才触发）
        if black_swan_trigger.iloc[i] and wt.iloc[i] > 0.001:
            black_swan_locked = True
            wt.iloc[i] = 0.0  # 当天清仓
            # 注意：锁定期间成长和价值方向都强制空仓，
            #       直到价值20日动量>0才解除

    # ================================================================
    # 第6层: 熊市长期阴跌保护（B3v4新增）
    #
    # 逻辑：成长指数价格低于90天均线 → 市场处于长期下跌趋势
    #       此时买入成长风险极高，禁止买入成长方向
    #
    # 核心洞察：在熊市中，成长指数跌跌不休，抄底大概率被套
    #           通过90天均线过滤掉大部分下跌趋势
    # ================================================================
    g_ma90 = g_close.rolling(p['ma90_days']).mean()
    growth_below_ma90 = g_close < g_ma90
    wt[growth_below_ma90 & (dir_series == 'growth')] = 0.0

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

    print("\n[2] 生成B3v4信号...")
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
    out_path = out_dir / 'df_factor_b3v4.csv'
    df_factor.to_csv(str(out_path), index=False, encoding='utf-8-sig')
    print(f"\n  保存至: {out_path}")