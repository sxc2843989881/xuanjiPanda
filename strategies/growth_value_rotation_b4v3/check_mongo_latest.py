"""检查MongoDB中480080/480081的最新数据状态和文档结构"""
import pymongo
from pprint import pprint

MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client['panda']

for coll_name in ['index_daily_price', 'index_daily_quotation']:
    coll = db[coll_name]
    print(f"\n===== {coll_name} =====")
    for sym in ['480080.SZ', '480081.SZ']:
        latest = coll.find({'symbol': sym}).sort('date', -1).limit(2)
        print(f"\n[{sym}] 最新2条:")
        for doc in latest:
            print(f"  date={doc.get('date')}")
            pprint(dict(doc))
        print(f"  总记录数: {coll.count_documents({'symbol': sym})}")

client.close()