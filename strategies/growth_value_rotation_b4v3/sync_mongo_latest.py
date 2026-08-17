"""将新增交易日数据同步到MongoDB（复权价 = CSV点位/1000）"""
import pymongo
import pandas as pd

MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client['panda']

VOLUME_PLACEHOLDER = 999999999

def sync_symbol(csv_code, mongo_sym):
    """同步单个标的"""
    g = pd.read_csv(rf'c:\temp_v72_data\index_{csv_code}.csv')
    g['date'] = pd.to_datetime(g['date']).dt.strftime('%Y%m%d')

    for coll_name in ['index_daily_price', 'index_daily_quotation']:
        coll = db[coll_name]
        # 获取MongoDB已有日期
        existing = set(d['date'] for d in coll.find({'symbol': mongo_sym}, {'date': 1, '_id': 0}))
        # 找出需要新增的行
        new_rows = g[~g['date'].isin(existing)]
        print(f"  [{coll_name}] {mongo_sym}: 已有{len(existing)}条, 新增{len(new_rows)}条")

        inserted = 0
        for _, row in new_rows.iterrows():
            doc = {
                'symbol': mongo_sym,
                'date': row['date'],
                'trade_date': int(row['date']),
                'open': float(row['open']) / 1000.0,
                'high': float(row['high']) / 1000.0,
                'low': float(row['low']) / 1000.0,
                'close': float(row['close']) / 1000.0,
                'last': float(row['close']) / 1000.0,
                'amount': float(row['amount']) if pd.notna(row['amount']) else 0.0,
                'volume': VOLUME_PLACEHOLDER,
                'limit_up': 999999,
                'limit_down': 0,
            }
            coll.insert_one(doc)
            inserted += 1
        if inserted:
            print(f"    🔄 插入 {inserted} 条")
        else:
            print(f"    ✅ 无需更新")

print("同步新增交易日数据到 MongoDB...")
sync_symbol('480080', '480080.SZ')
sync_symbol('480081', '480081.SZ')
client.close()
print("\n同步完成")