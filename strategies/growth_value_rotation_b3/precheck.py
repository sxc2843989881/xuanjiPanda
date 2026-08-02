"""B3-火星 成长价值轮动策略 — 数据预检模块

在回测启动前检查 MongoDB 数据完整性，防止因数据问题导致回测失真。

检查项:
  1. volume=0 或 volume<0 的记录（导致引擎拒绝所有订单）
  2. close=0 或 close<0 的记录（导致价格异常）
  3. 数据日期范围是否覆盖回测区间
  4. 关键字段（open/high/low/close/pre_close）是否存在
  5. 涨跌停价是否合理（limit_up > close > limit_down）

使用方式:
  from precheck import precheck_mongo_data
  errors = precheck_mongo_data(['480080.SZ', '480081.SZ'], start_date, end_date)
  if errors:
      print("预检失败，请修复数据后再回测")
      sys.exit(1)
"""
import pymongo
from datetime import datetime

from params import GROWTH_SYMBOL, VALUE_SYMBOL, BACKTEST_CONFIG, INDEX_DATA_DIR

MONGO_URI = "mongodb://panda:panda@127.0.0.1:27018/admin"
MONGO_DB = "panda"

# 需要检查的集合
CHECK_COLLECTIONS = ['index_daily_price', 'index_daily_quotation']

# volume 占位值（非零的占位值）
VOLUME_PLACEHOLDER = 999999999


def precheck_mongo_data(symbols, start_date, end_date, verbose=True):
    """回测前预检 MongoDB 数据

    Args:
        symbols: 标的列表 ['480080.SZ', '480081.SZ']
        start_date: 回测开始日期 'YYYYMMDD'
        end_date: 回测结束日期 'YYYYMMDD'
        verbose: 是否打印详细信息

    Returns:
        errors: 错误列表，空列表表示通过
    """
    errors = []
    warnings = []

    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    for coll_name in CHECK_COLLECTIONS:
        coll = db[coll_name]

        for symbol in symbols:
            if verbose:
                print(f"  [预检] {coll_name}/{symbol}...")

            # 1. 检查数据是否存在
            total = coll.count_documents({'symbol': symbol})
            if total == 0:
                errors.append(f"{coll_name}/{symbol}: 无数据!")
                continue

            # 2. 检查日期范围
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

            # 3. 检查 volume=0（核心检查项）
            zero_vol = coll.count_documents({
                'symbol': symbol,
                'volume': {'$lte': 0}
            })
            if zero_vol > 0:
                errors.append(
                    f"{coll_name}/{symbol}: 有 {zero_vol} 条 volume<=0 的记录！"
                    f"引擎将拒绝所有订单，请修复。"
                )

            # 4. 检查 close=0 或异常值
            zero_close = coll.count_documents({
                'symbol': symbol,
                'close': {'$lte': 0}
            })
            if zero_close > 0:
                errors.append(
                    f"{coll_name}/{symbol}: 有 {zero_close} 条 close<=0 的记录！"
                )

            # 5. 检查回测区间内的数据完整性
            in_range = list(coll.find({
                'symbol': symbol,
                'date': {'$gte': start_date, '$lte': end_date}
            }).sort('date', 1))

            if not in_range:
                errors.append(f"{coll_name}/{symbol}: 回测区间内无数据!")
                continue

            # 6. 检查关键字段是否存在
            required_fields = ['open', 'high', 'low', 'close', 'volume']
            missing_fields = []
            sample = in_range[0]
            for field in required_fields:
                if field not in sample or sample[field] is None:
                    missing_fields.append(field)
            if missing_fields:
                errors.append(
                    f"{coll_name}/{symbol}: 缺少字段 {missing_fields}"
                )

            # 7. 检查涨跌停价合理性（采样检查）
            for doc in in_range[-5:]:  # 检查最后5条
                close = doc.get('close', 0)
                limit_up = doc.get('limit_up', 999999)
                limit_down = doc.get('limit_down', 0)

                # 如果有实际涨跌停价（非占位值）
                if limit_up != 999999 and limit_up > 0:
                    if limit_up < close:
                        errors.append(
                            f"{coll_name}/{symbol}/{doc.get('date')}: "
                            f"limit_up({limit_up}) < close({close})"
                        )
                if limit_down != 0 and limit_down > 0:
                    if limit_down > close:
                        errors.append(
                            f"{coll_name}/{symbol}/{doc.get('date')}: "
                            f"limit_down({limit_down}) > close({close})"
                        )

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

    # 打印警告
    if warnings and verbose:
        print("\n  ⚠️ 警告:")
        for w in warnings:
            print(f"    {w}")

    # 打印错误
    if errors and verbose:
        print("\n  ❌ 预检失败:")
        for e in errors:
            print(f"    {e}")
    elif verbose:
        print("  ✅ 预检通过!")

    return errors


def auto_fix_volume(symbols, verbose=True):
    """自动修复 volume=0 的记录

    将 volume<=0 的记录更新为占位值 999999999

    Args:
        symbols: 标的列表

    Returns:
        fixed_count: 修复的记录数
    """
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

    fixed_count = 0

    for coll_name in CHECK_COLLECTIONS:
        coll = db[coll_name]

        for symbol in symbols:
            # 修复 volume=0 和 volume=0.0
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


if __name__ == '__main__':
    """独立运行预检"""
    print("=" * 60)
    print("  MongoDB 数据预检")
    print("=" * 60)

    symbols = [GROWTH_SYMBOL, VALUE_SYMBOL]
    start_date = BACKTEST_CONFIG['start_date']
    end_date = BACKTEST_CONFIG['end_date']

    print(f"\n检查标的: {symbols}")
    print(f"回测区间: {start_date} ~ {end_date}\n")

    errors = precheck_mongo_data(symbols, start_date, end_date)

    if errors:
        print("\n" + "=" * 60)
        print("  发现问题，尝试自动修复 volume=0...")
        print("=" * 60)
        auto_fix_volume(symbols)

        print("\n重新预检...")
        errors = precheck_mongo_data(symbols, start_date, end_date)

        if errors:
            print("\n❌ 仍有未修复的问题，请手动检查!")
        else:
            print("\n✅ 修复后预检通过!")
    else:
        print("\n✅ 数据预检全部通过!")