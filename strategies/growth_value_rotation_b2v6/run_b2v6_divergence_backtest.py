"""
B2v6底背离版本 — PandaAI回测

步骤:
1. 使用analyze_b2v6_divergence.py生成带底背离恢复的df_factor
2. 使用PandaAI引擎运行回测
3. 读取结果并保存
"""
import sys
import os
import pandas as pd
from pathlib import Path
from datetime import datetime

# 加载分析脚本中的信号生成函数
sys.path.insert(0, r'c:\XuanJLH\pandaai\panda_quantflow\src')
from analyze_b2v6_divergence import generate_signals_with_analysis, load_index_data, print_stop_analysis
from strategy_shell import run_backtest, read_result, print_result
from params import BACKTEST_CONFIG, GROWTH_SYMBOL, VALUE_SYMBOL
from precheck import precheck_mongo_data, auto_fix_volume


def main():
    print("=" * 70)
    print("  B2v6 底背离版本 — PandaAI 回测")
    print("=" * 70)

    script_dir = Path(__file__).parent
    out_dir = script_dir / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)

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

    # 2. 生成信号（带底背离恢复）
    print(f"\n[2] 生成B2v6底背离版本信号...")
    df_factor, stop_df, recovery_df, divergence_df = generate_signals_with_analysis(g_close, v_close)

    # 统计持仓分布
    buy_rows = df_factor[df_factor['signal'] == 'buy']
    growth_days = (buy_rows['symbol'] == GROWTH_SYMBOL).sum()
    value_days = (buy_rows['symbol'] == VALUE_SYMBOL).sum()
    cash_days = len(buy_rows[buy_rows['weight'] <= 0.001])
    total = len(buy_rows)
    print(f"  成长:{growth_days}天({growth_days/total*100:.1f}%)  "
          f"价值:{value_days}天({value_days/total*100:.1f}%)  "
          f"空仓:{cash_days}天({cash_days/total*100:.1f}%)")

    # 打印止损分析
    print_stop_analysis(stop_df, recovery_df, divergence_df)

    # 保存df_factor
    df_dir = script_dir / 'df_factors'
    df_dir.mkdir(exist_ok=True)
    df_factor.to_csv(str(df_dir / 'df_factor_b2v6_divergence.csv'), index=False, encoding='utf-8-sig')
    print(f"  df_factor已保存")

    # 3. 启动PandaAI回测
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

    print_result('B2v6_底背离', result)

    # 5. 保存结果
    result['label'] = 'B2v6_底背离'
    result['backtest_id'] = bt_id
    result['elapsed'] = elapsed
    result['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    result_path = out_dir / f"result_b2v6_divergence.csv"
    pd.DataFrame([result]).to_csv(str(result_path), index=False, encoding='utf-8-sig')
    print(f"\n  结果已保存: {result_path}")

    # 6. 对比E5基线
    print(f"\n[5] 与E5基线对比:")
    e5_result = {'annual_ret': 0.42615275610960457, 'max_dd': -0.22765572774371734, 'sharpe': 1.6040193933308324}
    print(f"  E5基线(20天/9%):   年化=42.62%  回撤=-22.77%  Sharpe=1.604")
    print(f"  B2v6底背离版:       年化={result['annual_ret']*100:.2f}%  "
          f"回撤={result['max_dd']*100:.2f}%  Sharpe={result['sharpe']:.3f}")

    # 保存止损分析日志
    log_dir = Path(r'c:\Users\28439\.trae-cn\work\6a60e0ea5437f897f4891301')
    if not stop_df.empty:
        stop_df.to_csv(str(log_dir / 'stop_logs.csv'), index=False, encoding='utf-8-sig')
    if not recovery_df.empty:
        recovery_df.to_csv(str(log_dir / 'recovery_logs.csv'), index=False, encoding='utf-8-sig')
    if not divergence_df.empty:
        divergence_df.to_csv(str(log_dir / 'divergence_logs.csv'), index=False, encoding='utf-8-sig')


if __name__ == '__main__':
    main()