"""B4V4 成长价值轮动策略 — 回测执行（L5锁满全程版）"""
import sys
sys.path.insert(0, r'c:\XuanJLH\pandaai\panda_quantflow\src')
import pandas as pd
import numpy as np
from params import GROWTH_SYMBOL, VALUE_SYMBOL, BASELINE_PARAMS, BACKTEST_CONFIG
from signal_generator_dct import load_index_data, generate_signals
from strategy_shell import run_backtest, read_result, print_result

print("=" * 60)
print("  B4V4 成长价值轮动策略（L5锁满全程）— 全量回测")
print("=" * 60)

g_close, v_close = load_index_data()
df = generate_signals(g_close, v_close, BASELINE_PARAMS)
print(f"信号数: {len(df)}")

bt, _ = run_backtest(df)
r = read_result(bt)

print_result("B4V4 L5锁满全程", r)