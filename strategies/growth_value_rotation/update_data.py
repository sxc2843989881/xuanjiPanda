"""
ETF数据等比例缩放回填指数数据

原理:
  指数(480080)有完整历史但截止较早（7月24日CSV / 7月1日MongoDB）
  ETF(159259)上市晚但可通过adata获取到最新（7月29日）
  
  在重叠日计算 指数收盘/ETF收盘 = 缩放比
  ETF上市前的数据用指数原值
  ETF上市后新增的数据用 ETF收盘 × 缩放比 回填

输出:
  更新后的 index_480080.csv / index_480081.csv
  同时写入 MongoDB index_daily_price 集合
"""
import os
import sys
import adata
import pandas as pd
import pymongo
from pathlib import Path
from datetime import datetime, timedelta

INDEX_DIR = Path(r'c:\temp_v72_data')
MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"

# 指数 → ETF 映射
INDEX_ETF_MAP = {
    '480080': {  # 成长100
        'index_file': 'index_480080.csv',
        'index_symbol': '480080.SZ',
        'etf_code': '159259',
        'etf_name': '易方达成长ETF',
    },
    '480081': {  # 价值100
        'index_file': 'index_480081.csv',
        'index_symbol': '480081.SZ',
        'etf_code': '159263',
        'etf_name': '易方达价值ETF',
    },
}


PRICE_COLS = ['open', 'high', 'low', 'close']


def fetch_etf_data(etf_code, start_date=None):
    """从adata获取ETF日线数据（含完整OHLC）"""
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

    print(f"  从adata获取ETF {etf_code} (从 {start_date})...")
    df = adata.fund.market.get_market_etf(
        fund_code=etf_code,
        start_date=start_date,
        k_type=1
    )

    if df is None or len(df) == 0:
        print(f"  ❌ ETF {etf_code} 数据为空")
        return None

    # 统一列名
    df = df.rename(columns={c: c.lower() for c in df.columns})
    if 'trade_date' in df.columns:
        df = df.rename(columns={'trade_date': 'date'})
    elif 'trade_time' in df.columns:
        df = df.rename(columns={'trade_time': 'date'})

    df['date'] = pd.to_datetime(df['date'])
    for col in PRICE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    keep = ['date'] + [c for c in PRICE_COLS if c in df.columns]
    df = df[keep].dropna().sort_values('date').reset_index(drop=True)

    print(f"  ✅ ETF {etf_code}: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()} ({len(df)}天)")
    return df


def load_index_csv(csv_path):
    """加载现有指数CSV数据（含完整OHLC）"""
    df = pd.read_csv(csv_path)
    df['date'] = pd.to_datetime(df['date'])
    for col in PRICE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    keep = ['date'] + [c for c in PRICE_COLS if c in df.columns]
    df = df[keep].dropna().sort_values('date').reset_index(drop=True)
    return df


def clean_bad_ohlc(df):
    """清除 open==high==low==close 的错误回填行（保留真实数据）"""
    if len(df) == 0:
        return df
    bad_mask = (df['open'] == df['high']) & (df['high'] == df['low']) & (df['low'] == df['close'])
    bad_count = bad_mask.sum()
    if bad_count > 0:
        print(f"  发现 {bad_count} 行OHLC全相同的错误数据，将清除:")
        for _, row in df[bad_mask].iterrows():
            print(f"    {row['date'].date()} open={row['open']:.4f} close={row['close']:.4f}")
        df = df[~bad_mask].reset_index(drop=True)
    return df


def merge_index_etf(index_df, etf_df):
    """ETF数据等比例缩放回填指数（含完整OHLC）

    1. 清除指数中OHLC全相同的错误回填行
    2. 找到最后一个重叠日
    3. 计算缩放比 = 指数收盘 / ETF收盘
    4. 重叠日之前用指数原值
    5. 重叠日之后用 ETF OHLC × 缩放比
    """
    # 先清除错误数据
    index_df = clean_bad_ohlc(index_df)

    # 合并找重叠日（用close计算缩放比）
    merged = pd.merge(index_df[['date', 'close']], etf_df[['date', 'close']],
                      on='date', suffixes=('_idx', '_etf'))

    if len(merged) == 0:
        print("  ❌ 无重叠日，无法计算缩放比")
        return None

    # 最后一个重叠日
    last_overlap = merged.iloc[-1]
    scale_ratio = last_overlap['close_idx'] / last_overlap['close_etf']
    overlap_date = last_overlap['date']

    print(f"  缩放基准日: {overlap_date.date()}")
    print(f"  指数收盘: {last_overlap['close_idx']:.4f}")
    print(f"  ETF收盘: {last_overlap['close_etf']:.4f}")
    print(f"  缩放比: {scale_ratio:.4f}")

    # 指数原值（到重叠日为止），保留所有价格列
    idx_part = index_df[index_df['date'] <= overlap_date].copy()

    # ETF缩放部分（重叠日之后），对所有价格列缩放
    etf_part = etf_df[etf_df['date'] > overlap_date].copy()
    for col in PRICE_COLS:
        if col in etf_part.columns:
            etf_part[col] = etf_part[col] * scale_ratio
    etf_part = etf_part[['date'] + [c for c in PRICE_COLS if c in etf_part.columns]]

    # 统一列
    all_cols = ['date'] + [c for c in PRICE_COLS if c in idx_part.columns or c in etf_part.columns]
    idx_part = idx_part[[c for c in all_cols if c in idx_part.columns]]
    etf_part = etf_part[[c for c in all_cols if c in etf_part.columns]]

    # 拼接
    result = pd.concat([idx_part, etf_part], ignore_index=True)
    result = result.sort_values('date').reset_index(drop=True)

    # 去重（保留最后一条）
    result = result.drop_duplicates(subset='date', keep='last').reset_index(drop=True)

    new_rows = len(etf_part)
    print(f"  回填新增: {new_rows}天")
    if new_rows > 0:
        print(f"  更新后范围: {result['date'].iloc[0].date()} ~ {result['date'].iloc[-1].date()} ({len(result)}天)")
        # 打印新增行的OHLC验证
        print(f"  新增行OHLC预览:")
        for _, row in etf_part.iterrows():
            print(f"    {row['date'].date()} O={row['open']:.4f} H={row['high']:.4f} L={row['low']:.4f} C={row['close']:.4f}")

    return result, scale_ratio, overlap_date


def update_csv(csv_path, merged_df, original_df):
    """更新CSV文件（保留原始列结构，使用真实OHLC）"""
    # 读取原始文件获取完整列结构
    orig = pd.read_csv(csv_path)
    orig['date'] = pd.to_datetime(orig['date'])

    # 清除错误行（OHLC全相同）
    if all(c in orig.columns for c in PRICE_COLS):
        bad_mask = (orig['open'] == orig['high']) & (orig['high'] == orig['low']) & (orig['low'] == orig['close'])
        bad_count = bad_mask.sum()
        if bad_count > 0:
            print(f"  清除CSV中 {bad_count} 行OHLC全相同的错误数据")
            orig = orig[~bad_mask].reset_index(drop=True)

    # 找出新增的日期
    new_dates = set(merged_df['date']) - set(orig['date'])
    if not new_dates:
        print("  CSV无需更新（无新日期）")
        return 0

    new_rows = merged_df[merged_df['date'].isin(new_dates)].copy()

    # 为新行填充其他列
    last_orig = orig.iloc[-1]
    new_records = []
    for _, row in new_rows.iterrows():
        new_rec = last_orig.to_dict()
        new_rec['date'] = row['date']
        # 使用真实OHLC
        for col in PRICE_COLS:
            if col in row.index and col in orig.columns:
                new_rec[col] = row[col]
        if 'timestamp' in orig.columns:
            new_rec['timestamp'] = row['date'].strftime('%Y-%m-%d')
        if 'current' in orig.columns:
            new_rec['current'] = row['close']
        new_records.append(new_rec)

    new_df = pd.DataFrame(new_records)
    combined = pd.concat([orig, new_df], ignore_index=True)
    combined['date'] = combined['date'].dt.strftime('%Y-%m-%d')
    combined.to_csv(str(csv_path), index=False, encoding='utf-8')
    print(f"  ✅ CSV已更新: +{len(new_records)}行 → {csv_path.name}")
    return len(new_records)


def update_mongodb(index_symbol, merged_df, original_last_date):
    """更新MongoDB index_daily_price集合（含真实OHLC）

    关键：MongoDB可能有比CSV更早的截止日期，需要补全所有缺失日期
    同时清除OHLC全相同的错误记录
    """
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client['panda']
    coll = db['index_daily_price']

    # 清除OHLC全相同的错误记录
    # 注意: 2024-10-29 之前的历史行只有收盘价(open=high=low=close 属正常),
    # 绝不能删; 仅清理真实OHLC时代(>=20241029)内的异常行。
    REAL_OHLC_START = '20241029'
    all_docs = list(coll.find({'symbol': index_symbol}).sort('date', 1))
    bad_ids = []
    for doc in all_docs:
        if str(doc.get('date', '')) < REAL_OHLC_START:
            continue
        o, h, l, c = doc.get('open', 0), doc.get('high', 0), doc.get('low', 0), doc.get('close', 0)
        if o == h == l == c and o > 0:
            bad_ids.append(doc['_id'])
    if bad_ids:
        print(f"  清除MongoDB中 {len(bad_ids)} 条OHLC全相同的错误记录")
        coll.delete_many({'_id': {'$in': bad_ids}})

    # 查询MongoDB中已有的最新日期
    latest = coll.find({'symbol': index_symbol}).sort('date', -1).limit(1)
    mongo_last_date = None
    for r in latest:
        mongo_last_date = r.get('date', '')

    print(f"  MongoDB当前最新: {mongo_last_date}")
    if not mongo_last_date:
        print("  ⚠️ MongoDB中无该指数数据，跳过")
        return 0

    # 以MongoDB最新日期为基准，补全之后所有缺失的日期
    mongo_last_date_fmt = pd.to_datetime(mongo_last_date, format='%Y%m%d')
    new_data = merged_df[merged_df['date'] > mongo_last_date_fmt]

    if len(new_data) == 0:
        print("  MongoDB无需更新（无新日期）")
        return 0

    print(f"  需补充: {new_data['date'].iloc[0].date()} ~ {new_data['date'].iloc[-1].date()} ({len(new_data)}天)")

    # 构造插入记录（使用真实OHLC）
    records = []
    for _, row in new_data.iterrows():
        date_str = row['date'].strftime('%Y%m%d')
        # 检查是否已存在
        if coll.count_documents({'symbol': index_symbol, 'date': date_str}) > 0:
            continue

        # Mongo 引擎库统一使用「指数点/1000」量纲(历史惯例, 全序列必须一致),
        # merged_df 为指数点量纲 → 写入前 /1000
        _s = 1000.0
        record = {
            'symbol': index_symbol,
            'date': date_str,
            'trade_date': int(date_str),
            'open': float(row.get('open', row['close'])) / _s,
            'high': float(row.get('high', row['close'])) / _s,
            'low': float(row.get('low', row['close'])) / _s,
            'close': float(row['close']) / _s,
            'last': float(row['close']) / _s,
            'volume': 999999999,  # 占位值，引擎要求 volume>0 才能成交订单
            'amount': 0,
            'limit_up': 999999,
            'limit_down': 0,
        }
        records.append(record)

    if records:
        coll.insert_many(records)
        print(f"  ✅ MongoDB已更新: +{len(records)}行 ({records[0]['date']} ~ {records[-1]['date']})")
    else:
        print("  MongoDB无新数据需插入")

    client.close()
    return len(records)


def main():
    print("=" * 70)
    print("  ETF数据等比例缩放回填指数数据")
    print("  成长100(480080) ← 成长ETF(159259)")
    print("  价值100(480081) ← 价值ETF(159263)")
    print("=" * 70)
    
    total_csv_updated = 0
    total_mongo_updated = 0
    
    for idx_code, info in INDEX_ETF_MAP.items():
        print(f"\n{'─' * 60}")
        print(f"处理 {idx_code} ({info['index_symbol']})")
        print(f"  ETF: {info['etf_code']} ({info['etf_name']})")
        print(f"{'─' * 60}")
        
        # 1. 加载现有指数CSV
        csv_path = INDEX_DIR / info['index_file']
        if not csv_path.exists():
            print(f"  ❌ 指数文件不存在: {csv_path}")
            continue
        
        index_df = load_index_csv(csv_path)
        index_last_date = index_df['date'].iloc[-1]
        print(f"  指数CSV: {index_df['date'].iloc[0].date()} ~ {index_last_date.date()} ({len(index_df)}天)")
        
        # 2. 获取ETF数据（从指数最后日期前60天开始，确保有重叠）
        etf_start = (index_last_date - timedelta(days=60)).strftime('%Y-%m-%d')
        etf_df = fetch_etf_data(info['etf_code'], etf_start)
        if etf_df is None:
            continue
        
        # 3. 缩放合并
        result = merge_index_etf(index_df, etf_df)
        if result is None:
            continue
        merged_df, scale_ratio, overlap_date = result
        
        # 4. 更新CSV
        print(f"\n  更新CSV...")
        csv_added = update_csv(csv_path, merged_df, index_df)
        total_csv_updated += csv_added
        
        # 5. 更新MongoDB
        print(f"\n  更新MongoDB...")
        mongo_added = update_mongodb(info['index_symbol'], merged_df, index_last_date)
        total_mongo_updated += mongo_added
    
    print(f"\n{'=' * 70}")
    print(f"  完成！CSV新增 {total_csv_updated} 行，MongoDB新增 {total_mongo_updated} 行")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    main()
