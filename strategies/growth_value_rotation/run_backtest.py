"""B1-木星 成长价值轮动策略 — 回测启动脚本

一键运行基线策略回测：
  python run_backtest.py                    # 基线B1
  python run_backtest.py --variant baseline # 基线B1（显式指定）
  python run_backtest.py --variant dir_lock_20d  # 9%止损+方向锁定20天

输出:
  - 回测结果（年化/回撤/Sharpe/Calmar）
  - df_factor CSV 保存至 df_factors/ 目录
  - 结果保存至 results/ 目录
"""
import argparse
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime

from params import GROWTH_SYMBOL, VALUE_SYMBOL, BACKTEST_CONFIG
from signal_generator import load_index_data, generate_signals, generate_signals_with_stoploss_variant
from strategy_shell import run_backtest, read_result, print_result
from precheck import precheck_mongo_data, auto_fix_volume


def main():
    parser = argparse.ArgumentParser(description='B1-木星 成长价值轮动策略回测')
    parser.add_argument('--variant', default='baseline',
                        choices=['baseline', 'strict_9pct', 'dir_lock_20d'],
                        help='止损变体（默认baseline）')
    parser.add_argument('--output-dir', default=None,
                        help='结果输出目录（默认 strategies/growth_value_rotation/results/）')
    args = parser.parse_args()

    # 输出目录
    script_dir = Path(__file__).parent
    out_dir = Path(args.output_dir) if args.output_dir else script_dir / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)

    # df_factors 目录
    df_dir = script_dir / 'df_factors'
    df_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print(f"  B1-木星 成长价值轮动策略 — PandaAI 引擎回测")
    print(f"  止损变体: {args.variant}")
    print("=" * 70)

    # 1. 加载数据
    print("\n[1] 加载指数数据...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ "
          f"{g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    # 1b. 数据预检
    print(f"\n[1b] MongoDB数据预检...")
    symbols = [GROWTH_SYMBOL, VALUE_SYMBOL]
    errors = precheck_mongo_data(symbols, BACKTEST_CONFIG['start_date'], BACKTEST_CONFIG['end_date'])
    if errors:
        print("\n  尝试自动修复 volume=0...")
        auto_fix_volume(symbols)
        print("\n  重新预检...")
        errors = precheck_mongo_data(symbols, BACKTEST_CONFIG['start_date'], BACKTEST_CONFIG['end_date'])
        if errors:
            print("\n  ❌ 预检仍失败，请手动检查数据!")
            sys.exit(1)
        print("  ✅ 修复后预检通过!")
    print("  ✅ 预检通过，继续回测")

    # 2. 生成信号
    print(f"\n[2] 生成信号: {args.variant}...")
    if args.variant == 'baseline':
        df_factor = generate_signals(g_close, v_close)
    else:
        df_factor = generate_signals_with_stoploss_variant(
            g_close, v_close, variant=args.variant
        )

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 保存 df_factor
    csv_name = f"df_factor_{args.variant}.csv"
    csv_path = df_dir / csv_name
    df_factor.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
    print(f"  df_factor已保存: {csv_path.name}")

    # 3. 启动回测
    print(f"\n[3] 启动PandaAI回测...")
    print(f"  区间: {BACKTEST_CONFIG['start_date']} ~ {BACKTEST_CONFIG['end_date']}")
    print(f"  初始资金: {BACKTEST_CONFIG['start_capital']:,}")
    print(f"  基准: {BACKTEST_CONFIG['standard_symbol']}")

    bt_id, elapsed = run_backtest(df_factor)
    print(f"  回测完成, 耗时={elapsed:.1f}s, backtest_id={bt_id}")

    # 4. 读取结果
    print(f"\n[4] 读取回测结果...")
    result = read_result(bt_id)
    if result is None:
        print("  ⚠️ MongoDB无结果，请检查回测是否成功")
        return

    print_result(args.variant, result)

    # 5. 保存结果
    result['label'] = args.variant
    result['backtest_id'] = bt_id
    result['elapsed'] = elapsed
    result['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    result_path = out_dir / f"result_{args.variant}.csv"
    pd.DataFrame([result]).to_csv(str(result_path), index=False, encoding='utf-8-sig')
    print(f"\n  结果已保存: {result_path}")

    # 追加到汇总文件
    summary_path = out_dir / 'backtest_summary.csv'
    if summary_path.exists():
        existing = pd.read_csv(str(summary_path))
        pd.concat([existing, pd.DataFrame([result])], ignore_index=True).to_csv(
            str(summary_path), index=False, encoding='utf-8-sig'
        )
    else:
        pd.DataFrame([result]).to_csv(
            str(summary_path), index=False, encoding='utf-8-sig'
        )
    print(f"  汇总已更新: {summary_path}")


if __name__ == '__main__':
    main()
