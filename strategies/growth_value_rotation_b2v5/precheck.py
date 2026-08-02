"""B2v5 三档风险轮动策略 — 数据预检模块

检查三个标的的 MongoDB 数据完整性。
"""
import pymongo

MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
MONGO_DB = "panda"
CHECK_COLLECTIONS = ['index_daily_price', 'index_daily_quotation']
VOLUME_PLACEHOLDER = 999999999


def precheck_mongo_data(symbols, start_date, end_date, verbose=True):
    """回测前预检 MongoDB 数据"""
    errors = []
    warnings = []

    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    for coll_name in CHECK_COLLECTIONS:
        coll = db[coll_name]

        for symbol in symbols:
            if verbose:
                print(f"  [预检] {coll_name}/{symbol}...")

            total = coll.count_documents({'symbol': symbol})
            if total == 0:
                errors.append(f"{coll_name}/{symbol}: 无数据!")
                continue

            date_range = list(coll.find(
                {'symbol': symbol},
                {'date': 1, '_id': 0}
            ).sort('date', 1))

            if not date_range:
                errors.append(f"{coll_name}/{symbol}: 无日期数据!")
                continue

            earliest = date_range[0]['date']
            latest = date_range[-1]['date']

            if earliest > start_date:
                errors.append(f"{coll_name}/{symbol}: 最早日期 {earliest} 晚于回测开始 {start_date}")
            if latest < end_date:
                warnings.append(f"{coll_name}/{symbol}: 最新日期 {latest} 早于回测结束 {end_date}")

            zero_vol = coll.count_documents({
                'symbol': symbol,
                'volume': {'$lte': 0}
            })
            if zero_vol > 0:
                errors.append(
                    f"{coll_name}/{symbol}: 有 {zero_vol} 条 volume<=0 的记录！"
                )

            zero_close = coll.count_documents({
                'symbol': symbol,
                'close': {'$lte': 0}
            })
            if zero_close > 0:
                errors.append(
                    f"{coll_name}/{symbol}: 有 {zero_close} 条 close<=0 的记录！"
                )

            in_range = list(coll.find({
                'symbol': symbol,
                'date': {'$gte': start_date, '$lte': end_date}
            }).sort('date', 1))

            if not in_range:
                errors.append(f"{coll_name}/{symbol}: 回测区间内无数据!")
                continue

            required_fields = ['open', 'high', 'low', 'close', 'volume']
            missing_fields = []
            sample = in_range[0]
            for field in required_fields:
                if field not in sample or sample[field] is None:
                    missing_fields.append(field)
            if missing_fields:
                errors.append(f"{coll_name}/{symbol}: 缺少字段 {missing_fields}")

            if verbose:
                print(f"    总记录: {total}, 区间内: {len(in_range)}, "
                      f"范围: {earliest} ~ {latest}")
                if zero_vol > 0:
                    print(f"    ❌ volume<=0: {zero_vol}条")
                else:
                    print(f"    ✅ volume检查通过")
                if zero_close > 0:
                    print(f"    ❌ close<=0: {zero_close}条")
                else:
                    print(f"    ✅ close检查通过")

    client.close()

    if warnings and verbose:
        print("\n  ⚠️ 警告:")
        for w in warnings:
            print(f"    {w}")

    if errors and verbose:
        print("\n  ❌ 预检失败:")
        for e in errors:
            print(f"    {e}")
    elif verbose:
        print("  ✅ 预检通过!")

    return errors


def auto_fix_volume(symbols, verbose=True):
    """自动修复 volume=0 的记录"""
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    fixed_count = 0

    for coll_name in CHECK_COLLECTIONS:
        coll = db[coll_name]

        for symbol in symbols:
            for bad_val in [0, 0.0]:
                result = coll.update_many(
                    {'symbol': symbol, 'volume': bad_val},
                    {'$set': {'volume': VOLUME_PLACEHOLDER}}
                )
                if result.modified_count > 0 and verbose:
                    print(f"  [修复] {coll_name}/{symbol}: "
                          f"volume={bad_val} → {VOLUME_PLACEHOLDER} "
                          f"({result.modified_count}条)")
                fixed_count += result.modified_count

    client.close()

    if fixed_count > 0 and verbose:
        print(f"  ✅ 共修复 {fixed_count} 条记录")
    elif verbose:
        print(f"  ✅ 无需修复")

    return fixed_count
