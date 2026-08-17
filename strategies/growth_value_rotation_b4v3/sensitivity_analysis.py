"""B4V3 敏感性分析：变开始年份 / 变结束年份 — 直接调用回测引擎（无子进程）"""
import sys
import os
import time
import pandas as pd
import numpy as np
from pathlib import Path

SCRIPT_DIR = Path(r'c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v3')
sys.path.insert(0, str(SCRIPT_DIR))

from params import GROWTH_SYMBOL, VALUE_SYMBOL, BACKTEST_CONFIG
from signal_generator import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result
from precheck import precheck_mongo_data, auto_fix_volume


def make_date_str(year, month=1, day=1):
    return f"{year:04d}{month:02d}{day:02d}"


def run_one(df_factor, start_date, end_date, label):
    """运行单次回测"""
    # 截断df_factor
    df = df_factor.copy()
    df['date'] = df['date'].astype(str)
    df = df[(df['date'] >= start_date) & (df['date'] <= end_date)].copy()
    if len(df) == 0:
        return None

    config = {**BACKTEST_CONFIG, 'start_date': start_date, 'end_date': end_date}
    try:
        bt_id, elapsed = run_backtest(df, config=config)
        result = read_result(bt_id)
        if result:
            result['label'] = label
            result['start_date'] = start_date
            result['end_date'] = end_date
            result['elapsed'] = elapsed
            return result
    except Exception as e:
        print(f"    [ERROR] {e}")
    return None


def main():
    print("=" * 70)
    print("  B4V3 敏感性分析：变开始年份 / 变结束年份")
    print("=" * 70)

    # 1. 加载数据并生成信号
    print("\n[1] 加载指数数据并生成信号...")
    g_close, v_close = load_index_data()
    print(f"  成长100: {g_close.index[0].strftime('%Y-%m-%d')} ~ "
          f"{g_close.index[-1].strftime('%Y-%m-%d')} ({len(g_close)}天)")

    df_factor = generate_signals(g_close, v_close)
    print(f"  df_factor: {len(df_factor)}行")

    # 2. 数据预检（一次即可）
    print("\n[2] MongoDB数据预检...")
    symbols = [GROWTH_SYMBOL, VALUE_SYMBOL]
    errors = precheck_mongo_data(symbols, BACKTEST_CONFIG['start_date'], BACKTEST_CONFIG['end_date'])
    if errors:
        auto_fix_volume(symbols)
        errors = precheck_mongo_data(symbols, BACKTEST_CONFIG['start_date'], BACKTEST_CONFIG['end_date'])
        if errors:
            print("  ❌ 预检失败，退出")
            return
    print("  ✅ 预检通过")

    # 3. 构建任务列表
    tasks = []
    # 测试1：变开始年份
    end_fixed = "20260730"
    for sy in range(2013, 2026):
        tasks.append((make_date_str(sy, 1, 1), end_fixed, f"Start{sy}"))
    # 测试2：变结束年份
    start_fixed = "20130101"
    for ey in range(2014, 2027):
        ed = make_date_str(ey, 7, 30) if ey < 2026 else "20260730"
        tasks.append((start_fixed, ed, f"End{ey}"))

    total = len(tasks)
    print(f"\n  共 {total} 个回测任务，串行执行")

    # 4. 串行执行
    results_start = []
    results_end = []
    t0 = time.time()

    for i, (sd, ed, lb) in enumerate(tasks, 1):
        print(f"\n  [{i}/{total}] {lb}  {sd}~{ed}  ", end='', flush=True)
        bt_t0 = time.time()
        r = run_one(df_factor, sd, ed, lb)
        bt_elapsed = time.time() - bt_t0
        if r:
            calmar = r['annual_ret'] / abs(r['max_dd']) if r['max_dd'] != 0 else 0
            print(f"年化={r['annual_ret']*100:.2f}%  回撤={r['max_dd']*100:.2f}%  "
                  f"Sharpe={r['sharpe']:.3f}  Calmar={calmar:.3f}  ({bt_elapsed:.0f}s)")
            if lb.startswith('Start'):
                results_start.append(r)
            else:
                results_end.append(r)
        else:
            print(f"失败 ({bt_elapsed:.0f}s)")

    total_elapsed = time.time() - t0
    print(f"\n  总耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分钟)")

    # 5. 汇总结果
    print("\n" + "=" * 70)
    print("  汇总结果")
    print("=" * 70)

    print("\n  【测试1】变开始年份（固定结束=2026-07-30）")
    print(f"  {'开始年份':>8}  {'年化收益':>10}  {'最大回撤':>10}  {'Sharpe':>10}  {'Calmar':>10}  {'年数':>6}")
    print(f"  {'-'*56}")
    for r in results_start:
        calmar = r['annual_ret'] / abs(r['max_dd']) if r['max_dd'] != 0 else 0
        years = int(r['end_date'][:4]) - int(r['start_date'][:4])
        print(f"  {r['start_date'][:4]:>8}  {r['annual_ret']*100:>9.2f}%  {r['max_dd']*100:>9.2f}%  "
              f"{r['sharpe']:>10.3f}  {calmar:>10.3f}  {years:>6}")

    print("\n  【测试2】变结束年份（固定开始=2013-01-01）")
    print(f"  {'结束年份':>8}  {'年化收益':>10}  {'最大回撤':>10}  {'Sharpe':>10}  {'Calmar':>10}  {'年数':>6}")
    print(f"  {'-'*56}")
    for r in results_end:
        calmar = r['annual_ret'] / abs(r['max_dd']) if r['max_dd'] != 0 else 0
        years = int(r['end_date'][:4]) - int(r['start_date'][:4])
        print(f"  {r['end_date'][:4]:>8}  {r['annual_ret']*100:>9.2f}%  {r['max_dd']*100:>9.2f}%  "
              f"{r['sharpe']:>10.3f}  {calmar:>10.3f}  {years:>6}")

    # 6. 保存CSV
    out_dir = SCRIPT_DIR / 'results'
    out_dir.mkdir(exist_ok=True)
    if results_start:
        pd.DataFrame(results_start).to_csv(out_dir / 'sensitivity_start_year.csv', index=False, encoding='utf-8-sig')
        print(f"\n  ✅ 变开始年份结果已保存: {out_dir / 'sensitivity_start_year.csv'}")
    if results_end:
        pd.DataFrame(results_end).to_csv(out_dir / 'sensitivity_end_year.csv', index=False, encoding='utf-8-sig')
        print(f"  ✅ 变结束年份结果已保存: {out_dir / 'sensitivity_end_year.csv'}")


if __name__ == '__main__':
    main()