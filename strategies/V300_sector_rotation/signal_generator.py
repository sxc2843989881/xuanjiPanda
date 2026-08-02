"""V300-极光 行业ETF轮动策略 — 信号生成器

★ 纯分数驱动：1-10分买入区，85分以上卖出区，无其他条件。

核心逻辑：
1. 每日为每只ETF计算0-100综合评分（动量50%+均值回归25%+趋势偏离25%）
2. 月频调仓日：按评分升序排列，评分最低的Top 3入选
3. 1-10分：买入区（恐慌/低估），85分以上：卖出区（贪婪/过热）
4. 缓冲机制：掉出Top 3才换仓
5. 同一板块最多选1只，分散化

输出格式：
  df_factor: DataFrame[date, symbol, signal, weight, score]
"""
import pandas as pd
import numpy as np
from pathlib import Path

from params import (
    INDUSTRY_ETF_POOL, INDUSTRY_GROUPS,
    CORE_PARAMS,
    BACKTEST_CONFIG, DATA_CACHE_DIR,
)
from factor_engine import FactorEngine


# ===== 数据加载 =====
def _get_mongo_symbol(code):
    """根据ETF代码确定MongoDB中的symbol格式"""
    if code.startswith('159'):
        return f'{code}.SZ'
    return f'{code}.SH'


def adjust_prices_forward(df):
    """前复权处理：消除除权除息导致的价格跳变

    用 pre_close 计算每天的真实调整因子 f[t] = pre_close[t] / close[t-1]
    正常日 f[t] = 1.0，除权日 f[t] < 1.0
    前复权价格 = 原始价格 × 未来所有调整因子的累积乘积

    前复权后的日收益率 = close[t] / pre_close[t] - 1（真实日收益率含分红）
    除权跳变被消除，价格序列连续。

    Args:
        df: DataFrame, 必须含 close, pre_close 列

    Returns:
        DataFrame, 前复权后的数据
    """
    df = df.copy()
    # 计算每天的调整因子
    f = df['pre_close'] / df['close'].shift(1)
    f = f.fillna(1.0)

    # 累积调整因子（从最旧到最新）
    cum_from_start = f.cumprod()
    total = cum_from_start.iloc[-1]
    # cum_adj[t] = 所有未来调整因子的乘积
    # 最新日期: cum_adj[last] = 1.0
    cum_adj = total / cum_from_start

    # 前复权价格
    for col in ['open', 'high', 'low', 'close']:
        if col in df.columns:
            df[col] = df[col] * cum_adj

    # 更新 pre_close 使其与调整后的 close 一致
    # 调整后的 pre_close[t] = 前复权 close[t-1]
    df['pre_close'] = df['close'].shift(1).fillna(df['close'].iloc[0])

    return df


def _fetch_from_mongodb(code, symbol, start_date, end_date):
    """从MongoDB获取ETF日线数据（含前复权处理）"""
    import pymongo
    MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
    MONGO_DB = "panda"
    COLLECTION = "etf_daily_quotation_v2"

    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    coll = db[COLLECTION]

    cursor = coll.find(
        {'symbol': symbol},
        {'trade_date': 1, 'open': 1, 'high': 1, 'low': 1, 'close': 1,
         'volume': 1, 'pre_close': 1, 'amount': 1, '_id': 0}
    ).sort('trade_date', 1)

    records = []
    for doc in cursor:
        td = doc['trade_date']
        if start_date <= td <= end_date:
            def _safe_float(v):
                if v is None:
                    return 0.0
                return float(v)
            records.append({
                'date': td,
                'open': _safe_float(doc.get('open')),
                'high': _safe_float(doc.get('high')),
                'low': _safe_float(doc.get('low')),
                'close': _safe_float(doc.get('close')),
                'volume': _safe_float(doc.get('volume')),
                'pre_close': _safe_float(doc.get('pre_close')),
            })

    client.close()

    if not records:
        return None

    df = pd.DataFrame(records)
    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
    df = df.set_index('date').sort_index()
    df['volume'] = df['volume'].clip(lower=1)

    # 前复权处理：消除除权除息跳变
    df = adjust_prices_forward(df)

    return df


def _fetch_from_adata(code, start_date, end_date):
    """从adata获取ETF日线数据（备用方案）"""
    try:
        import adata
        s = f'{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}'
        e = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}'
        df = adata.fund.market.get_market_etf(
            fund_code=code,
            start_date=s,
            k_type=1
        )
        if df is None or len(df) == 0:
            return None

        date_col = 'trade_date' if 'trade_date' in df.columns else 'trade_time'
        df = df.rename(columns={
            date_col: 'date',
            'open': 'open', 'high': 'high', 'low': 'low',
            'close': 'close', 'volume': 'volume',
        })
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df = df[['open', 'high', 'low', 'close', 'volume']]
        df['volume'] = df['volume'].fillna(0).clip(lower=1)
        return df
    except Exception as e:
        print(f"    adata获取{code}失败: {e}")
        return None


def load_etf_data(cache_dir=None):
    """从MongoDB加载ETF数据，缓存到CSV加速后续加载"""
    cache_dir = Path(cache_dir or DATA_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)

    price_dict = {}
    start_dt = BACKTEST_CONFIG['start_date']
    end_dt = BACKTEST_CONFIG['end_date']

    for code, name in INDUSTRY_ETF_POOL.items():
        csv_path = cache_dir / f'{code}.csv'

        if csv_path.exists():
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if (df.index.min() <= pd.Timestamp(start_dt) and
                df.index.max() >= pd.Timestamp(end_dt)):
                if 'close' in df.columns:
                    price_dict[code] = df['close'].astype(float).sort_index().dropna()
                    continue
            print(f"  {code}({name}) 缓存数据不完整，重新加载...")

        print(f"  加载 {code}({name}) 从MongoDB...")
        symbol = _get_mongo_symbol(code)
        df = _fetch_from_mongodb(code, symbol, start_dt, end_dt)

        if df is None or len(df) < 30:
            print(f"    MongoDB数据不足，尝试adata...")
            df = _fetch_from_adata(code, start_dt, end_dt)

        if df is None or len(df) == 0:
            print(f"    ❌ 无法获取{code}({name})数据")
            continue

        df.to_csv(csv_path)
        print(f"    ✅ {len(df)}条, {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")

        if 'close' in df.columns:
            price_dict[code] = df['close'].astype(float).sort_index().dropna()

    return price_dict


def get_common_dates(price_dict):
    """获取所有ETF的共同交易日"""
    common = None
    for code, series in price_dict.items():
        if common is None:
            common = set(series.index)
        else:
            common = common.intersection(set(series.index))
    return sorted(list(common))


# ===== 信号生成核心 =====
def generate_signals(price_dict, params=None):
    """V300-极光 每日信号生成器 — 纯评分驱动（评分<30买入）

    ★ 核心规则：
    1. 每日评分，评分<30的ETF买入候选，选评分最低的买入1份
    2. 持仓ETF评分>70：全仓卖出（过热/止盈）
    3. 持仓ETF评分<30（已持仓）：全仓卖出（趋势转弱）
    4. 持仓ETF评分>50：减仓一半（生命周期内仅一次）

    Args:
        price_dict: {code: Series(close)}
        params: 核心参数（默认CORE_PARAMS）

    Returns:
        df_factor: DataFrame[date, symbol, signal, weight, score]
        score_detail: dict, 各ETF评分详情
    """
    p = {**CORE_PARAMS, **(params or {})}

    engine = FactorEngine()
    common_dates = get_common_dates(price_dict)

    print(f"  共同交易日: {len(common_dates)}天")
    print(f"  日期范围: {common_dates[0].strftime('%Y-%m-%d')} ~ {common_dates[-1].strftime('%Y-%m-%d')}")

    # ---- 计算每只ETF的每日评分 ----
    all_scores = {}
    for code in price_dict:
        price = price_dict[code]
        common_idx = price.index.intersection(common_dates)
        price = price.loc[common_idx]
        result = engine.compute_composite_score(price)
        all_scores[code] = result['composite']

    # 构建评分矩阵
    score_matrix = pd.DataFrame({
        code: scores for code, scores in all_scores.items()
    }, index=common_dates)

    # ---- 计算每只ETF的MACD看涨信号 ----
    all_divergence = {}
    for code in price_dict:
        price = price_dict[code]
        common_idx = price.index.intersection(common_dates)
        price = price.loc[common_idx]
        div = engine.detect_macd_bullish_divergence(price)
        all_divergence[code] = div

    # MACD看涨信号矩阵
    divergence_matrix = pd.DataFrame({
        code: div for code, div in all_divergence.items()
    }, index=common_dates)

    # 过去3天内有MACD看涨信号即可
    recent_div = divergence_matrix.rolling(window=3, min_periods=1).max().fillna(0).astype(bool)

    # ---- 行业分组映射 ----
    code_to_group = {}
    for group, codes in INDUSTRY_GROUPS.items():
        for code in codes:
            code_to_group[code] = group

    # ---- 初始化：10份资金 ----
    total_units = p['total_units']
    cash_units = total_units
    positions = {}  # code -> {units, entry_price, entry_date, half_reduced}

    records = []

    for date in common_dates:
        if date not in score_matrix.index:
            continue

        row = score_matrix.loc[date]
        valid_scores = row.dropna()
        if len(valid_scores) == 0:
            continue

        # ============================================================
        # 1. 检查持仓的卖出条件
        # ============================================================
        codes_to_remove = []
        for code in sorted(positions.keys()):
            score = valid_scores.get(code, 50)
            if pd.isna(score):
                continue
            info = positions[code]

            # 1.1 评分 > 70：全仓卖出（过热/止盈）
            if score > p['sell_full_zone']:
                cash_units += info['units']
                records.append({
                    'date': date, 'symbol': code,
                    'signal': 'sell_full', 'weight': 0.0, 'score': score,
                    'note': f'评分{score:.1f}>70止盈'
                })
                codes_to_remove.append(code)
                continue

            # 1.2 评分 < 30：全仓卖出（趋势转弱/止损）
            if score < 30:
                cash_units += info['units']
                records.append({
                    'date': date, 'symbol': code,
                    'signal': 'sell_full', 'weight': 0.0, 'score': score,
                    'note': f'评分{score:.1f}<30趋势转弱卖出'
                })
                codes_to_remove.append(code)
                continue

            # 1.3 评分 > 50 且未减仓：减仓一半（生命周期仅一次）
            if score > p['reduce_zone'] and not info['half_reduced']:
                sell_units = info['units'] / 2
                info['units'] -= sell_units
                cash_units += sell_units
                info['half_reduced'] = True
                records.append({
                    'date': date, 'symbol': code,
                    'signal': 'reduce', 'weight': info['units'] / total_units, 'score': score,
                    'note': f'评分{score:.1f}>50减半'
                })
                continue

            # 1.4 关闭硬止损（由评分<30代替）
            holding_days = (date - info['entry_date']).days
            if holding_days > p['stop_loss_days']:
                current_price = price_dict[code].get(date, None)
                if current_price is not None and not pd.isna(current_price):
                    drawdown = current_price / info['entry_price'] - 1
                    if drawdown < -p['stop_loss_pct']:
                        cash_units += info['units']
                        records.append({
                            'date': date, 'symbol': code,
                            'signal': 'sell_stop', 'weight': 0.0, 'score': score,
                            'note': f'持有{holding_days}天跌{drawdown*100:.1f}%止损'
                        })
                        codes_to_remove.append(code)
                        continue

        # 统一移除已卖出的
        for code in codes_to_remove:
            del positions[code]

        # ============================================================
        # 2. 检查买入条件
        # ============================================================
        if cash_units >= 1:
            # 取评分 < 30（买入区）且不在持仓中的ETF
            buy_candidates = valid_scores[
                (valid_scores < 30) &
                (~valid_scores.index.isin(positions.keys()))
            ]
            if len(buy_candidates) > 0:
                # 按评分升序排列，选最低分的（最恐慌/低估）
                sorted_codes = buy_candidates.sort_values().index
                for best_code in sorted_codes:
                    # 行业分散化
                    best_group = code_to_group.get(best_code)
                    same_group_held = False
                    if best_group:
                        for held_code in positions:
                            if code_to_group.get(held_code) == best_group:
                                same_group_held = True
                                break
                    if same_group_held:
                        continue
                    best_score = valid_scores[best_code]
                    entry_price = price_dict[best_code].get(date, None)
                    if entry_price is not None and not pd.isna(entry_price):
                        positions[best_code] = {
                            'units': 1,
                            'entry_price': entry_price,
                            'entry_date': date,
                            'half_reduced': False,
                        }
                        cash_units -= 1
                        records.append({
                            'date': date, 'symbol': best_code,
                            'signal': 'buy', 'weight': 1.0 / total_units, 'score': best_score,
                            'note': f'评分{best_score:.1f}<30买入1份'
                        })
                        break  # 每天只买1份

        # ============================================================
        # 3. 记录每日持仓状态
        # ============================================================
        used_units = total_units - cash_units
        if used_units > 0:
            for code, info in positions.items():
                score = valid_scores.get(code, 50)
                records.append({
                    'date': date, 'symbol': code,
                    'signal': 'hold', 'weight': info['units'] / total_units, 'score': score,
                    'note': f'持仓{info["units"]:.1f}份'
                })
        else:
            records.append({
                'date': date, 'symbol': 'BOND',
                'signal': 'hold', 'weight': 1.0, 'score': 0,
                'note': '空仓'
            })

    # ---- 构建df_factor ----
    df_factor = pd.DataFrame(records)
    if len(df_factor) > 0:
        df_factor['date'] = df_factor['date'].astype(str)
        df_factor = df_factor.sort_values(['date', 'symbol']).reset_index(drop=True)

    return df_factor, {'scores': all_scores, 'score_matrix': score_matrix}


def print_statistics(df_factor, price_dict):
    """打印信号统计（每日信号格式）"""
    if df_factor is None or len(df_factor) == 0:
        print("  ⚠️ 无信号生成")
        return

    print(f"\n  信号总数: {len(df_factor)}")
    print(f"  日期范围: {df_factor['date'].min()} ~ {df_factor['date'].max()}")

    # 统计各类信号
    hold_records = df_factor[df_factor['signal'] == 'hold']
    hold_days = hold_records['date'].nunique()

    print(f"\n  持仓天数: {hold_days}天")

    # 交易事件统计
    buy_events = df_factor[df_factor['signal'] == 'buy']
    sell_events = df_factor[df_factor['signal'].isin(['sell_full', 'sell_stop'])]
    reduce_events = df_factor[df_factor['signal'] == 'reduce']
    stop_events = df_factor[df_factor['signal'] == 'sell_stop']

    print(f"  买入事件: {len(buy_events)}次")
    print(f"  卖出事件: {len(sell_events)}次 (其中止损: {len(stop_events)}次)")
    print(f"  减仓事件: {len(reduce_events)}次")

    # 空仓天数
    bond_days = len(df_factor[(df_factor['symbol'] == 'BOND') & (df_factor['signal'] == 'hold')])
    if bond_days > 0:
        total_days = df_factor['date'].nunique()
        print(f"  空仓(债券): {bond_days}天 ({bond_days/total_days*100:.1f}%)")

    # 各ETF持仓天数
    etf_holdings = hold_records[hold_records['symbol'] != 'BOND']
    if len(etf_holdings) > 0:
        holding_days = etf_holdings.groupby('symbol').size()
        avg_weight = etf_holdings.groupby('symbol')['weight'].mean()
        print(f"\n  各ETF持仓统计:")
        for code, days in holding_days.sort_values(ascending=False).items():
            name = INDUSTRY_ETF_POOL.get(code, code)
            avg_w = avg_weight[code] * 100
            print(f"    {code}({name}): {days}天, 平均权重{avg_w:.1f}%")

    # 买入评分统计
    if len(buy_events) > 0:
        print(f"\n  买入平均评分: {buy_events['score'].mean():.1f}")
        print(f"  买入评分范围: {buy_events['score'].min():.1f} ~ {buy_events['score'].max():.1f}")


if __name__ == '__main__':
    print("=" * 70)
    print("  V300-极光 行业ETF轮动策略 — 信号生成（纯分数驱动）")
    print("=" * 70)

    print("\n[1] 加载ETF数据...")
    price_dict = load_etf_data()
    print(f"  成功加载 {len(price_dict)} 只ETF数据")

    print("\n[2] 生成评分信号...")
    df_factor, score_detail = generate_signals(price_dict)

    print("\n[3] 信号统计:")
    print_statistics(df_factor, price_dict)

    # 保存
    script_dir = Path(__file__).parent
    df_dir = script_dir / 'df_factors'
    df_dir.mkdir(exist_ok=True)

    csv_path = df_dir / 'df_factor_v300.csv'
    if df_factor is not None and len(df_factor) > 0:
        df_factor.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"\n  信号已保存: {csv_path}")
    else:
        print(f"\n  ⚠️ 无信号生成，未保存CSV")