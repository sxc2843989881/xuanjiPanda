"""V300-极光 行业ETF轮动策略 — 一键回测启动脚本

一键运行：
  python run_backtest.py

流程：
  1. 加载或下载ETF数据（akshare）
  2. 生成0-100评分信号
  3. 本地回测引擎模拟
  4. 输出结果 + 保存文件

V300策略整合来源：
  - 文档: 0-100评分系统 + 弱势区买入/强势区卖出
  - ETF-Mega7: 多周期风险调整动量 + 成交量因子
  - Mean Reversion Study: BIAS乖离率 + RSI + 布林带
  - DAO-Quant: 趋势质量R² + 缓冲机制
  - B2v6: 跟踪止损 + 冷却期
"""
import argparse
import sys
import time
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

from params import (
    INDUSTRY_ETF_POOL, BACKTEST_CONFIG, DATA_CACHE_DIR,
)
from signal_generator import load_etf_data, generate_signals, print_statistics
from backtest_engine import BacktestEngine


def main():
    parser = argparse.ArgumentParser(description='V300-极光 行业ETF轮动策略回测')
    parser.add_argument('--skip-download', action='store_true',
                        help='跳过下载，使用缓存数据')
    parser.add_argument('--output-dir', default=None,
                        help='结果输出目录')
    args = parser.parse_args()

    # 输出目录
    script_dir = Path(__file__).parent
    out_dir = Path(args.output_dir) if args.output_dir else script_dir / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)
    df_dir = script_dir / 'df_factors'
    df_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print("  V300-极光 行业ETF轮动策略 — 本地回测")
    print("=" * 70)
    print(f"\n  整合来源:")
    print(f"    📄 文档策略: 0-100评分系统 + 弱势区/强势区")
    print(f"    📊 ETF-Mega7: 多周期风险调整动量 + 成交量因子")
    print(f"    📈 Mean Reversion: BIAS乖离率 + RSI + 布林带")
    print(f"    🔬 DAO-Quant: 趋势质量R² + 缓冲机制")
    print(f"    🛡️  B2v6: 跟踪止损 + 冷却期")

    # ---- 1. 加载数据 ----
    print(f"\n{'='*70}")
    print("[1] 加载ETF数据...")
    print(f"  ETF池: {len(INDUSTRY_ETF_POOL)}只")
    for code, name in INDUSTRY_ETF_POOL.items():
        print(f"    {code} - {name}")

    t0 = time.time()
    price_dict = load_etf_data()
    elapsed_dl = time.time() - t0
    print(f"\n  成功加载 {len(price_dict)}/{len(INDUSTRY_ETF_POOL)} 只ETF")
    print(f"  耗时: {elapsed_dl:.1f}s")

    if len(price_dict) < 3:
        print("\n  ❌ 有效ETF数量不足，无法回测")
        sys.exit(1)

    # ---- 2. 生成信号 ----
    print(f"\n{'='*70}")
    print("[2] 生成V300评分信号...")
    t0 = time.time()
    df_factor, score_detail = generate_signals(price_dict)
    elapsed_sig = time.time() - t0
    print(f"  耗时: {elapsed_sig:.1f}s")

    if df_factor is None or len(df_factor) == 0:
        print("\n  ❌ 信号生成失败，无法回测")
        sys.exit(1)

    # 统计
    print_statistics(df_factor, price_dict)

    # 保存信号
    csv_name = f"df_factor_v300.csv"
    csv_path = df_dir / csv_name
    df_factor.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
    print(f"\n  信号已保存: {csv_path.name}")

    # ---- 3. 启动回测 ----
    print(f"\n{'='*70}")
    print("[3] 执行本地回测...")
    print(f"  区间: {BACKTEST_CONFIG['start_date']} ~ {BACKTEST_CONFIG['end_date']}")
    print(f"  初始资金: {BACKTEST_CONFIG['start_capital']:,}")
    print(f"  佣金: {BACKTEST_CONFIG['commission_rate']*10000:.1f}‱ + 滑点: {BACKTEST_CONFIG['slippage']*100:.1f}%")

    t0 = time.time()
    engine = BacktestEngine(price_dict)
    result, nav, details = engine.run(df_factor)
    elapsed_bt = time.time() - t0

    if result is None:
        print("  ❌ 回测无结果")
        return

    print(f"\n  [结果] 耗时={elapsed_bt:.1f}s")
    print("-" * 70)
    engine.print_result('V300-极光', result)

    # ---- 4. 保存结果 ----
    print(f"\n{'='*70}")
    print("[4] 保存结果...")

    # 结果指标
    result['label'] = 'V300'
    result['elapsed'] = elapsed_bt
    result['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    result['etf_count'] = len(price_dict)
    result['signal_days'] = df_factor['date'].nunique()

    result_path = out_dir / 'result_v300.csv'
    pd.DataFrame([result]).to_csv(str(result_path), index=False, encoding='utf-8-sig')
    print(f"  结果已保存: {result_path}")

    # 净值曲线
    if nav is not None:
        nav_df = pd.DataFrame({'nav': nav})
        nav_path = out_dir / 'nav_v300.csv'
        nav_df.to_csv(nav_path, encoding='utf-8-sig')
        print(f"  净值已保存: {nav_path}")

    # 汇总
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

    # ---- 5. 生成可视化（可选） ----
    if nav is not None:
        try:
            _generate_chart(nav, details, out_dir)
        except Exception as e:
            print(f"  图表生成跳过: {e}")

    print(f"\n{'='*70}")
    print("  ✅ 回测完成!")
    print(f"  V300策略: 年化={result['annual_ret']*100:.2f}%  "
          f"回撤={result['max_dd']*100:.2f}%  "
          f"Sharpe={result['sharpe']:.3f}")
    print(f"  结果文件: {out_dir}")
    print(f"{'='*70}")


def _generate_chart(nav, details, out_dir):
    """生成净值曲线图"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    # 设置中文字体
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'WenQuanYi Micro Hei', 'SimHei', 'Microsoft YaHei']
    plt.rcParams['axes.unicode_minus'] = False

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), gridspec_kw={'height_ratios': [3, 1, 1]})

    # 净值曲线
    ax1 = axes[0]
    nav.plot(ax=ax1, color='#2196F3', linewidth=1.5, label='V300策略净值')
    ax1.set_title('V300-极光 行业ETF轮动策略 — 净值曲线', fontsize=14, fontweight='bold')
    ax1.set_ylabel('资产净值 (元)')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)

    # 回撤曲线
    ax2 = axes[1]
    rolling_max = nav.cummax()
    drawdown = (nav - rolling_max) / rolling_max * 100
    drawdown.plot(ax=ax2, color='#F44336', linewidth=1, label='回撤%')
    ax2.fill_between(drawdown.index, 0, drawdown.values, color='#F44336', alpha=0.3)
    ax2.set_ylabel('回撤 (%)')
    ax2.legend(loc='lower left')
    ax2.grid(True, alpha=0.3)

    # 持仓仓位
    ax3 = axes[2]
    positions = details.get('positions', pd.DataFrame())
    if positions is not None and len(positions.columns) > 0:
        # 绘制持仓权重堆叠图
        non_zero = positions.loc[:, (positions.sum() > 0)]
        if len(non_zero.columns) > 0:
            non_zero.plot.area(ax=ax3, alpha=0.7, linewidth=0)
            ax3.set_ylabel('持仓权重')
            ax3.legend(loc='upper right', fontsize=8)
            ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    chart_path = out_dir / 'v300_chart.png'
    fig.savefig(chart_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"  图表已保存: {chart_path}")


if __name__ == '__main__':
    main()