"""V300-极光 行业ETF轮动策略 — 本地回测引擎

基于ETF-Mega7-Strategy的回测引擎设计，参考streametf的轮动回测逻辑，
实现本地回测（无需MongoDB/PandaAI引擎依赖）。

核心计算：
  1. 基于df_factor信号进行持仓模拟
  2. 考虑交易成本（佣金+滑点）
  3. 计算净值曲线和绩效指标
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

from params import BACKTEST_CONFIG, INDUSTRY_ETF_POOL


class BacktestEngine:
    """本地回测引擎"""

    def __init__(self, price_dict, config=None):
        """
        Args:
            price_dict: {code: Series(close)} 所有ETF价格数据
            config: 回测配置（默认BACKTEST_CONFIG）
        """
        self.price_dict = price_dict
        self.cfg = {**BACKTEST_CONFIG, **(config or {})}

    def run(self, df_factor):
        """执行回测

        Args:
            df_factor: DataFrame[date, symbol, signal, weight, score]

        Returns:
            result: dict, 包含回测指标
            nav: Series, 每日净值
            details: dict, 详细数据
        """
        if df_factor is None or len(df_factor) == 0:
            return None, None, None

        # ---- 数据准备 ----
        df = df_factor.copy()
        df['date'] = pd.to_datetime(df['date'])

        # 获取所有交易日
        all_dates = sorted(set(df['date'].unique()))
        for code, series in self.price_dict.items():
            all_dates = sorted(set(all_dates) | set(series.index))

        # 构建价格矩阵
        price_matrix = pd.DataFrame(index=all_dates)
        for code, series in self.price_dict.items():
            price_matrix[code] = series

        price_matrix = price_matrix.sort_index().ffill()

        # 筛选回测区间
        start = pd.to_datetime(self.cfg['start_date'])
        end = pd.to_datetime(self.cfg['end_date'])
        price_matrix = price_matrix[(price_matrix.index >= start) & (price_matrix.index <= end)]

        # ---- 构建持仓矩阵 ----
        # positions: DataFrame(index=date, columns=code, values=weight)
        # 初始化为NaN（表示"未设定"），与"明确设为0（空仓）"区分
        positions = pd.DataFrame(np.nan, index=price_matrix.index, columns=price_matrix.columns)

        # 按日期处理信号
        signal_dates = sorted(df['date'].unique())
        for signal_date in signal_dates:
            if signal_date < start or signal_date > end:
                continue
            day_signals = df[df['date'] == signal_date]
            is_bond = any(row['symbol'] == 'BOND' for _, row in day_signals.iterrows())

            if is_bond:
                # BOND信号：所有ETF权重设为0（明确空仓）
                if signal_date in positions.index:
                    positions.loc[signal_date] = 0.0
            else:
                for _, row in day_signals.iterrows():
                    sym = row['symbol']
                    if sym in positions.columns:
                        if signal_date in positions.index:
                            positions.loc[signal_date, sym] = row['weight']

        # 前向填充持仓：信号日之间的持仓保持不变
        # NaN → 前向填充（使用上一个信号日的持仓）
        # 0.0 → 明确空仓，不会被填充覆盖
        positions = positions.ffill().fillna(0)

        # ---- 计算每日收益率 ----
        returns = price_matrix.pct_change().fillna(0)

        # 组合每日收益率 = 持仓权重 × 各ETF收益率
        # 使用T+1延迟：今日信号 → 明日执行
        portfolio_returns = pd.Series(0.0, index=returns.index)
        for i in range(1, len(returns)):
            date = returns.index[i]
            prev_date = returns.index[i - 1]
            # 使用前一天的持仓权重
            weights = positions.loc[prev_date]
            # 乘以当天的收益率
            daily_ret = (weights * returns.loc[date]).sum()
            portfolio_returns.loc[date] = daily_ret

        # ---- 扣除交易成本 ----
        # 计算换手率：每日持仓变化
        turnover = positions.diff().abs().sum(axis=1) / 2
        # 交易成本 = 换手率 × (佣金率 + 滑点)
        total_cost_rate = self.cfg['commission_rate'] + self.cfg['slippage']
        costs = turnover * total_cost_rate
        portfolio_returns = portfolio_returns - costs

        # ---- 计算净值曲线 ----
        nav = (1 + portfolio_returns).cumprod() * self.cfg['start_capital']
        nav = nav.replace(0, np.nan).ffill().fillna(self.cfg['start_capital'])

        # ---- 计算回测指标 ----
        result = self._calc_metrics(nav, portfolio_returns, price_matrix)

        details = {
            'positions': positions,
            'returns': portfolio_returns,
            'turnover': turnover,
            'costs': costs,
        }

        return result, nav, details

    def _calc_metrics(self, nav, portfolio_returns, price_matrix):
        """计算回测绩效指标"""
        # 总收益率
        total_return = nav.iloc[-1] / nav.iloc[0] - 1

        # 年化收益率
        days = (nav.index[-1] - nav.index[0]).days
        years = days / 365.25
        annual_ret = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # 最大回撤
        rolling_max = nav.cummax()
        drawdown = (nav - rolling_max) / rolling_max
        max_dd = drawdown.min()

        # 年化波动率
        annual_vol = portfolio_returns.std() * np.sqrt(252)

        # 夏普比率（假设无风险利率=2%）
        risk_free = 0.02
        sharpe = (annual_ret - risk_free) / annual_vol if annual_vol > 0 else 0

        # 索提诺比率
        negative_returns = portfolio_returns[portfolio_returns < 0]
        if len(negative_returns) > 1:
            downside = negative_returns.std()
            downside_annual = downside * np.sqrt(252)
            sortino = (annual_ret - risk_free) / downside_annual if downside_annual > 0 else 0
        else:
            sortino = 0

        # Calmar比率
        calmar = annual_ret / abs(max_dd) if max_dd != 0 else 0

        # 胜率
        win_days = (portfolio_returns > 0).sum()
        total_days = (portfolio_returns != 0).sum()
        win_rate = win_days / total_days if total_days > 0 else 0

        # 基准收益（等权持有所有ETF）
        bench_returns = price_matrix.pct_change().fillna(0).mean(axis=1)
        bench_nav = (1 + bench_returns).cumprod() * self.cfg['start_capital']
        bench_total_return = bench_nav.iloc[-1] / bench_nav.iloc[0] - 1
        bench_annual_ret = (1 + bench_total_return) ** (1 / years) - 1 if years > 0 else 0

        # Alpha/Beta
        bench_vol = bench_returns.std()
        if bench_vol > 0 and len(portfolio_returns) == len(bench_returns):
            corr = np.corrcoef(portfolio_returns, bench_returns)[0, 1] if len(portfolio_returns) > 1 else 0
            beta = corr * (portfolio_returns.std() / bench_returns.std()) if bench_returns.std() > 0 else 0
            alpha = annual_ret - (risk_free + beta * (bench_annual_ret - risk_free))
        else:
            beta = 0
            alpha = 0

        # 最大回撤区间
        dd_series = drawdown
        dd_start = None
        dd_end = None
        max_dd_found = 0
        for i in range(len(dd_series)):
            if dd_series.iloc[i] < max_dd_found:
                max_dd_found = dd_series.iloc[i]
                if dd_start is None or dd_series.iloc[i] < dd_series.iloc[i-1]:
                    dd_start = dd_series.index[i]
                dd_end = dd_series.index[i]

        return {
            'total_return': total_return,
            'annual_ret': annual_ret,
            'max_dd': max_dd,
            'sharpe': sharpe,
            'sortino': sortino,
            'calmar': calmar,
            'win_rate': win_rate,
            'annual_vol': annual_vol,
            'benchmark_return': bench_annual_ret,
            'alpha': alpha,
            'beta': beta,
            'total_days': len(nav),
            'years': years,
            'start_date': nav.index[0].strftime('%Y-%m-%d'),
            'end_date': nav.index[-1].strftime('%Y-%m-%d'),
            'max_dd_start': dd_start.strftime('%Y-%m-%d') if dd_start else '',
            'max_dd_end': dd_end.strftime('%Y-%m-%d') if dd_end else '',
        }

    @staticmethod
    def print_result(label, result):
        """格式化打印回测结果"""
        if result is None:
            print(f"  {label}: 无结果")
            return

        print(f"  {label}:")
        print(f"    区间: {result['start_date']} ~ {result['end_date']} ({result['total_days']}天)")
        print(f"    年化={result['annual_ret']*100:.2f}%  "
              f"回撤={result['max_dd']*100:.2f}%  "
              f"Sharpe={result['sharpe']:.3f}  "
              f"Calmar={result['calmar']:.3f}")
        print(f"    总收益={result['total_return']*100:.2f}%  "
              f"年化波动={result['annual_vol']*100:.2f}%  "
              f"胜率={result['win_rate']*100:.1f}%")
        print(f"    基准年化={result['benchmark_return']*100:.2f}%  "
              f"Alpha={result['alpha']*100:.2f}%  "
              f"Beta={result['beta']:.3f}  "
              f"Sortino={result['sortino']:.3f}")