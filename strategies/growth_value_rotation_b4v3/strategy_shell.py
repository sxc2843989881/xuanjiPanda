"""B4V3 成长价值轮动策略 — PandaAI 策略壳代码

PandaAI 引擎的策略代码以字符串形式传入，实现 3 个核心函数：
  - initialize:    初始化
  - before_trading: 每日开盘前读取昨日信号（T+1延迟）
  - handle_data:    每日交易执行调仓

本模块提供策略代码字符串和回测启动函数。

关键设计:
  1. T+1延迟: before_trading(T)读取df_factor[T-1]，handle_data(T)执行
  2. delta取整: 对交易股数差取整（100股为单位），避免误差累积
  3. 行情订阅: before_trading中必须sub_stock_symbol
  4. print绕过: 引擎拦截print，用sys.__stdout__.write绕过
  5. 因子索引: 预建{date:{symbol:(signal,weight)}}索引，避免daily全表扫描
"""
import os
import sys
import time
import pymongo
import pandas as pd
from pathlib import Path
from bson import ObjectId
from dotenv import load_dotenv

from params import BACKTEST_CONFIG, GROWTH_SYMBOL, VALUE_SYMBOL


# ===== PandaAI 策略代码（通用壳） =====
# 此字符串传入 PandaAI 引擎的 start() 函数的 code 参数
# 适用于任何基于 df_factor 的双标的轮动策略
STRATEGY_CODE = '''
from panda_backtest.api.api import *
from panda_backtest.api.stock_api import *
import sys as _sys
import bisect as _bisect

ACCOUNT_ID = "8888"

def _stdout(msg):
    _sys.__stdout__.write(msg + "\\n")
    _sys.__stdout__.flush()

def initialize(context):
    context.account = ACCOUNT_ID
    context._day_count = 0
    context._factor_index = None
    context._sorted_dates = None

def _get_factor_index(context):
    if context._factor_index is None:
        idx = {}
        dates_set = set()
        for _, row in context.df_factor.iterrows():
            d = str(row["date"])
            if d not in idx:
                idx[d] = {}
            idx[d][row["symbol"]] = (row["signal"], float(row["weight"]))
            dates_set.add(d)
        context._factor_index = idx
        context._sorted_dates = sorted(dates_set)
    return context._factor_index

def _get_prev_date(context, today):
    if context._sorted_dates is None:
        _get_factor_index(context)
    pos = _bisect.bisect_left(context._sorted_dates, today)
    if pos > 0:
        return context._sorted_dates[pos - 1]
    return None

def before_trading(context):
    today = str(context.now)
    fi = _get_factor_index(context)
    prev_date = _get_prev_date(context, today)
    prev_signals = fi.get(prev_date, {}) if prev_date else {}
    context.target_symbol = None
    context.target_weight = 0.0
    for sym, (sig, wt) in prev_signals.items():
        if sig == "buy" and wt > 0:
            context.target_symbol = sym
            context.target_weight = wt
            break
    # 空仓清仓时也需要行情数据来执行卖出，因此始终订阅两个标的
    context.sub_stock_symbol(["480080.SZ", "480081.SZ"], sub_type=0)
    context.sub_stock_symbol(["480080.SZ", "480081.SZ"], sub_type=1)
    if today >= "20260701" and today <= "20260731":
        _stdout(f"[BT] date={today}, prev_date={prev_date}, target={context.target_symbol}, wt={context.target_weight}")

def handle_data(context, bar_dict):
    stock_account = context.run_info.stock_account
    today = str(context.now)
    if context.target_symbol is None and context.target_weight == 0.0:
        positions = context.stock_account_dict[context.account].positions.to_dataframe()
        if not positions.empty and positions["positions"].sum() != 0:
            for _, row in positions.iterrows():
                if row["positions"] != 0:
                    order_shares(stock_account, row["symbol"], -int(row["positions"]))
        return
    if context.target_symbol is None:
        return
    bar = bar_dict[context.target_symbol]
    if bar is None or bar.last is None or bar.last <= 0:
        if today >= "20260701" and today <= "20260731":
            _stdout(f"[HD] date={today}, target={context.target_symbol}, bar=None or invalid, SKIP")
        return
    if today >= "20260701" and today <= "20260731":
        tv = context.stock_account_dict[context.account].total_value
        positions = context.stock_account_dict[context.account].positions.to_dataframe()
        pos_str = ""
        if not positions.empty:
            for _, row in positions.iterrows():
                if row["positions"] != 0:
                    pos_str += f"{row['symbol']}:{int(row['positions'])} "
        _stdout(f"[HD] date={today}, target={context.target_symbol}, bar.last={bar.last:.2f}, tv={tv:.0f}, pos=[{pos_str}]")
    total_value = context.stock_account_dict[context.account].total_value
    target_amount = total_value * context.target_weight
    positions = context.stock_account_dict[context.account].positions.to_dataframe()
    for _, row in positions.iterrows():
        sym = row["symbol"]
        pos = int(row["positions"])
        if sym != context.target_symbol and pos != 0:
            order_shares(stock_account, sym, -pos)
    # 开盘价撮合模式下，bar.last即为开盘价（引擎通过change_last_field设置）
    # 计算和撮合使用同一价格，留少量安全边际仅覆盖手续费+滑点
    safe_price = bar.last * 1.002
    target_shares = int(target_amount / safe_price)
    current_pos = 0
    if not positions.empty:
        cur = positions[positions["symbol"] == context.target_symbol]
        if not cur.empty:
            current_pos = int(cur.iloc[0]["positions"])
    delta = target_shares - current_pos
    if abs(delta) >= 100:
        delta = int(delta / 100) * 100
        if abs(delta) >= 100:
            order_shares(stock_account, context.target_symbol, delta)
            if today >= "20260701" and today <= "20260731":
                _stdout(f"[HD] date={today}, ORDER {context.target_symbol} delta={delta}")

def after_trading(context):
    context._day_count += 1
    if context._day_count % 100 == 0:
        tv = context.stock_account_dict[context.account].total_value
        _stdout(f"[PROGRESS] day={context._day_count}, date={context.now}, total_value={tv:.2f}")
'''


def run_backtest(df_factor, config=None, strategy_code=None):
    """启动 PandaAI 引擎回测

    Args:
        df_factor: 信号 DataFrame（date/symbol/signal/weight）
        config: 回测配置 dict（默认使用 BACKTEST_CONFIG）
        strategy_code: 策略代码字符串（默认使用 STRATEGY_CODE）

    Returns:
        backtest_id: 回测ID（用于查询MongoDB结果）
        elapsed: 回测耗时（秒）
    """
    cfg = {**BACKTEST_CONFIG, **(config or {})}

    # 环境准备
    os.chdir(r'c:\XuanJLH\pandaai\panda_quantflow')
    sys.path.insert(0, r'c:\XuanJLH\pandaai\panda_quantflow\src')
    load_dotenv(r'c:\XuanJLH\pandaai\panda_quantflow\.env')

    from panda_backtest.main_workflow_stock import start, get_backtest_id

    # 确保 date 列为字符串
    df_factor = df_factor.copy()
    df_factor['date'] = df_factor['date'].astype(str)

    # 截断到安全边界（避免基准数据缺失导致引擎崩溃）
    if 'date' in df_factor.columns:
        df_factor = df_factor[df_factor['date'] <= cfg['end_date']].copy()

    bt_id = get_backtest_id()
    t0 = time.time()

    result_id = start(
        back_test_id=bt_id,
        code=strategy_code or STRATEGY_CODE,
        start_date=cfg['start_date'],
        end_date=cfg['end_date'],
        start_capital=cfg['start_capital'],
        standard_symbol=cfg['standard_symbol'],
        commission_rate=cfg['commission_rate'],
        account_id=cfg['account_id'],
        df_factor=df_factor,
        frequency=cfg['frequency'],
        matching_type=cfg.get('matching_type', 1),  # 默认1=开盘价撮合
    )

    elapsed = time.time() - t0
    return bt_id, elapsed


def read_result(bt_id):
    """从 MongoDB 读取回测结果

    Args:
        bt_id: 回测ID

    Returns:
        dict: 包含年化收益、最大回撤、夏普等指标
    """
    try:
        client = pymongo.MongoClient(
            "mongodb://panda:panda@127.0.0.1:27018/admin",
            serverSelectionTimeoutMS=5000
        )
        db = client['panda']
        r = db['panda_back_test'].find_one({'_id': ObjectId(bt_id)})
        if r is None:
            return None
        return {
            'annual_ret': r.get('back_profit_year', 0),
            'max_dd': r.get('max_drawdown', 0),
            'sharpe': r.get('sharpe', 0),
            'sortino': r.get('sortino', 0),
            'volatility': r.get('volatility', 0),
            'alpha': r.get('alpha', 0),
            'beta': r.get('beta', 0),
            'total_return': r.get('back_profit', 0),
            'benchmark_return': r.get('benchmark_profit', 0),
        }
    except Exception as e:
        print(f"  MongoDB读取失败: {e}")
        return None


def print_result(label, result):
    """格式化打印回测结果

    Args:
        label: 策略标签
        result: read_result 返回的 dict
    """
    if result is None:
        print(f"  {label}: 无结果")
        return

    calmar = result['annual_ret'] / abs(result['max_dd']) if result['max_dd'] != 0 else 0
    print(f"  {label}:")
    print(f"    年化={result['annual_ret']*100:.2f}%  回撤={result['max_dd']*100:.2f}%  "
          f"Sharpe={result['sharpe']:.3f}  Calmar={calmar:.3f}")
    print(f"    基准年化={result['benchmark_return']*100:.2f}%  "
          f"Alpha={result['alpha']*100:.2f}%  Beta={result['beta']:.3f}")