"""B3v1-火星净值止损版 成长价值轮动策略 — 回测启动脚本

B3v1 = B3 + 净值止损机制

净值止损触发条件：
  - 净值60日收益>30% 且 20日收益>20%
  - 净值从最高点回撤>9%
  → 清仓，进入锁定

恢复逻辑：
  - MACD恢复: 跟踪成长100和价值100的MACD柱状态
  - 两者皆为绿柱(负值)时 → 继续锁定，绝不再入场
  - 至少一个转红柱(正值)时 → 止跌企稳，解锁恢复正常信号
"""
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime

from params import GROWTH_SYMBOL, VALUE_SYMBOL, BACKTEST_CONFIG
from signal_generator import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result
from precheck import precheck_mongo_data, auto_fix_volume


def main():
    script_dir = Path(__file__).parent
    out_dir = script_dir / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)
    df_dir = script_dir / 'df_factors'
    df_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print("  B3v1-火星净值止损版 — PandaAI 引擎回测")
    print("  B3 + 净值止损(回撤9%触发清仓，MACD柱状态决定冷却期)")
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
    print(f"\n[2] 生成B3v1信号（B3+净值止损）...")
    df_factor = generate_signals(g_close, v_close)

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"\n  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 保存 df_factor
    csv_path = df_dir / 'df_factor_b3v1.csv'
    df_factor.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
    print(f"  df_factor已保存: {csv_path.name}")

    # 3. 启动回测
    print(f"\n[3] 启动PandaAI回测...")
    print(f"  区间: {BACKTEST_CONFIG['start_date']} ~ {BACKTEST_CONFIG['end_date']}")

    bt_id, elapsed = run_backtest(df_factor)
    print(f"  回测完成, 耗时={elapsed:.1f}s, backtest_id={bt_id}")

    # 4. 读取结果
    print(f"\n[4] 读取回测结果...")
    result = read_result(bt_id)
    if result is None:
        print("  ⚠️ MongoDB无结果，请检查回测是否成功")
        return

    print_result('B3v1', result)

    # 5. 保存结果
    result['label'] = 'B3v1'
    result['backtest_id'] = bt_id
    result['elapsed'] = elapsed
    result['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    result_path = out_dir / 'result_b3v1.csv'
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

    # 6. 对比
    print(f"\n[5] 对比:")
    print(f"  B3(无止损):    年化=40.25%  回撤=-50.02%  Sharpe=1.391")
    print(f"  B1(原版):      年化=42.62%  回撤=-22.77%  Sharpe=1.604")
    print(f"  B3v1(净值止损): 年化={result['annual_ret']*100:.2f}%  "
          f"回撤={result['max_dd']*100:.2f}%  Sharpe={result['sharpe']:.3f}")


if __name__ == '__main__':
    main()