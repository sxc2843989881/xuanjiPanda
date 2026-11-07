"""统一重建 MongoDB index_daily_price 行情(全量, /1000 量纲)

背景:
  引擎库历史惯例为「指数点/1000」(如 2026-10-13 成长=11.4757, 对应指数 11475.7371)。
  - update_data.py 旧逻辑写入 Mongo 的价格量纲与历史不一致(指数点 vs /1000),
    造成 2024-10-29 / 2026-10-14 两处量纲跳变;
  - 本脚本以 CSV 为唯一真值, 全量重建两个标的全部交易日:
      * 有真实 OHLC 的日期(>=2024-10-29): O/H/L/C = CSV 值 /1000
      * 只有收盘价的历史日期: O=H=L=C = close/1000
  重建后与昨日扫描所用序列(截至10-13)逐值一致, 并一致延伸到 11-06。
"""
import pandas as pd
import pymongo

MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
INDEX_DIR = r'c:\temp_v72_data'
SCALE = 1000.0
MAP = {'480080.SZ': 'index_480080.csv', '480081.SZ': 'index_480081.csv'}

client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
coll = client['panda']['index_daily_price']

for sym, fname in MAP.items():
    df = pd.read_csv(fr'{INDEX_DIR}\{fname}')
    df['date'] = pd.to_datetime(df['date'])
    for col in ('open', 'high', 'low', 'close'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['close'] = df['close'].ffill()
    df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)

    # 先清空该标的, 再全量写入
    coll.delete_many({'symbol': sym})

    records = []
    for _, row in df.iterrows():
        ds = row['date'].strftime('%Y%m%d')
        c = float(row['close']) / SCALE
        o = float(row['open']) / SCALE if pd.notna(row['open']) else c
        h = float(row['high']) / SCALE if pd.notna(row['high']) else c
        low = float(row['low']) / SCALE if pd.notna(row['low']) else c
        records.append({
            'symbol': sym,
            'date': ds,
            'trade_date': int(ds),
            'open': o, 'high': h, 'low': low, 'close': c, 'last': c,
            'volume': 999999999,
            'amount': 0,
            'limit_up': 999999,
            'limit_down': 0,
        })
    coll.insert_many(records)

    chk = list(coll.find({'symbol': sym,
                          'date': {'$in': ['20241028', '20241029', '20261013', '20261106']}},
                         {'_id': 0, 'date': 1, 'close': 1}).sort('date', 1))
    print(f"{sym}: 重建 {len(records)} 条 ({df['date'].iloc[0].date()}~{df['date'].iloc[-1].date()})")
    for d in chk:
        print('   ', d['date'], 'close=', round(d['close'], 4))

client.close()
print('重建完成')
