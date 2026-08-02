"""V300-极光 行业ETF轮动策略 — 因子计算引擎

三大因子：
  1. 动量因子（50%）— 多周期风险调整动量，涨=高分/跌=低分
  2. 均值回归因子（25%）— BIAS+RSI+布林带，恐慌=低分/贪婪=高分
  3. 趋势偏离因子（25%）— 趋势均线偏离度，远离均线=低分/靠近均线=高分

所有因子输出统一为0-100分，低分=买入机会，高分=卖出信号。
★ 滚动Z-score标准化，防止未来函数。
"""
import pandas as pd
import numpy as np

from params import (
    MOMENTUM_PERIOD_WEIGHTS, MR_SUB_WEIGHTS,
    CORE_PARAMS,
)


class FactorEngine:
    """多因子计算引擎 — 每个因子输出0-100分"""

    # ============================================================
    # 1. 数据准备
    # ============================================================
    @staticmethod
    def calc_returns(price):
        """计算日收益率"""
        return price.pct_change().fillna(0)

    @staticmethod
    def calc_zscore_rolling(series, window=504):
        """滚动Z-Score标准化（防未来函数）

        仅使用过去window天的数据计算均值和标准差，
        确保每个时间点的Z-score只基于历史信息。

        Args:
            series: 待标准化序列
            window: 滚动窗口（默认504日≈2年）

        Returns:
            Series: Z-score序列
        """
        mean = series.rolling(window, min_periods=60).mean()
        std = series.rolling(window, min_periods=60).std()
        # 避免除零
        std = std.replace(0, np.nan)
        zscore = (series - mean) / std
        # 窗口不足时填充0
        zscore = zscore.fillna(0)
        return zscore

    @staticmethod
    def _map_to_0_100(zscore, clip_range=2.0):
        """将Z-Score映射到0-100分"""
        return (zscore.clip(-clip_range, clip_range) + clip_range) / (2 * clip_range) * 100

    # ============================================================
    # 2. 动量因子 — 多周期风险调整（涨=高分/跌=低分）
    # ============================================================
    def calc_momentum_score(self, price):
        """计算多周期风险调整动量得分（0-100分）

        涨→高分，跌→低分。方向与策略一致。

        Returns:
            Series: 0-100分
        """
        returns = self.calc_returns(price)
        p = CORE_PARAMS
        zscore_window = p['zscore_window']
        mom_scores = []

        for period, weight in MOMENTUM_PERIOD_WEIGHTS.items():
            # 计算该周期总收益率
            total_return = price.pct_change(period)

            # 计算该周期波动率（年化）
            vol = returns.rolling(window=min(period, p['mom_vol_window'])).std() * np.sqrt(252)
            vol = vol.replace(0, np.nan)

            # 风险调整动量 = 总收益率 / 波动率
            risk_adj_mom = total_return / vol

            # ★ 滚动Z-score标准化（防未来函数）
            zscore = self.calc_zscore_rolling(risk_adj_mom, zscore_window)
            score = self._map_to_0_100(zscore)
            mom_scores.append(score * weight)

        # 加权合成
        composite = sum(mom_scores)
        return composite.clip(0, 100)

    # ============================================================
    # 3. 均值回归因子 — BIAS+RSI+布林带（恐慌=低分/贪婪=高分）
    # ============================================================
    def calc_mean_reversion_score(self, price):
        """计算均值回归综合得分（0-100分）— 热度指标

        低分=恐慌/低估（买入机会），高分=贪婪/过热（止盈信号）

        Returns:
            Series: 0-100分
        """
        p = CORE_PARAMS

        # ---- 3.1 BIAS乖离率评分 ----
        ma = price.rolling(p['bias_window']).mean()
        bias = (price - ma) / ma
        # 对称评分：负乖离（恐慌/低估）→ 低分，正乖离（贪婪/高估）→ 高分
        # 乖离率从 -10% 到 +10% → 0分到100分
        bias_score = (bias.clip(-0.10, 0.10) + 0.10) / 0.20 * 100
        bias_score = bias_score.clip(0, 100)

        # ---- 3.2 RSI评分 ----
        delta = price.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(p['rsi_window']).mean()
        avg_loss = loss.rolling(p['rsi_window']).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        # RSI越高 → 高分（超买/贪婪）
        rsi_score = rsi.clip(0, 100)

        # ---- 3.3 布林带位置评分 ----
        bb_ma = price.rolling(p['bb_window']).mean()
        bb_std = price.rolling(p['bb_window']).std()
        upper = bb_ma + p['bb_std'] * bb_std
        lower = bb_ma - p['bb_std'] * bb_std
        # 位置: 0=下轨, 0.5=中轨, 1=上轨
        bb_pos = (price - lower) / (upper - lower).replace(0, np.nan)
        bb_pos = bb_pos.clip(0, 1)
        # 越接近上轨 → 高分（贪婪）
        bb_score = bb_pos * 100

        # ---- 加权合成 ----
        composite = (
            bias_score * MR_SUB_WEIGHTS['bias'] +
            rsi_score * MR_SUB_WEIGHTS['rsi'] +
            bb_score * MR_SUB_WEIGHTS['bollinger']
        )
        return composite.clip(0, 100)

    # ============================================================
    # 4. 趋势均线偏离度因子（新增）
    #    在下跌趋势中，价格远离趋势均线=强信号
    # ============================================================
    def calc_trend_deviation_score(self, price):
        """计算趋势均线偏离度得分（0-100分）

        逻辑：在下跌趋势中，价格对趋势均线（如120日线）的偏离度
        - 价格远低于均线（恐慌）→ 低分，买入机会
        - 价格在均线附近 → 中性分
        - 价格远高于均线（贪婪）→ 高分，卖出信号

        与BIAS(20日)互补：BIAS捕捉短期乖离，此因子捕捉长期趋势偏离
        与布林带(20日/2σ)互补：布林带看短期波动，趋势均线看长期方向

        Returns:
            Series: 0-100分，低分=买入，高分=卖出
        """
        p = CORE_PARAMS
        ma = price.rolling(p['trend_ma_window']).mean()
        deviation = (price - ma) / ma

        # 线性映射：偏离度从 -20% 到 +20% → 0分到100分
        # -20% → 0分（极度恐慌），0% → 50分（中性），+20% → 100分（极度贪婪）
        clip_val = p['trend_dev_clip']
        score = (deviation.clip(-clip_val, clip_val) / clip_val) * 50 + 50
        return score.clip(0, 100)

    # ============================================================
    # 5. MACD底背离检测
    # ============================================================
    @staticmethod
    def detect_macd_bullish_divergence(price, lookback=30):
        """检测MACD看涨信号（宽松版）— 下跌动能减弱

        不要求严格的"价格新低+MACD未新低"底背离（太罕见），
        改为检测"MACD柱状线从低位回升"——表明下跌动能减弱。

        信号条件（同时满足）：
        1. MACD DIF线从低位开始回升（DIF比3天前高）
        2. 价格仍在中短期均线下方（确认仍在调整区，不是追高）
        3. 过去N天内MACD DIF出现过更低值（确认是从低位回升）

        Returns:
            Series[bool]: 每日是否出现看涨信号
        """
        ema12 = price.ewm(span=12, adjust=False).mean()
        ema26 = price.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26  # DIF线
        # DIF的3日变化（DIF在回升还是在下降）
        dif_change = dif.diff(3)

        divergence = pd.Series(False, index=price.index)

        ma20 = price.rolling(20).mean()

        for i in range(lookback, len(price)):
            # 条件1: MACD DIF从低位回升（DIF比3天前高）
            dif_rising = dif.iloc[i] > dif.iloc[i-3]

            # 条件2: 过去N天内DIF有更低值（确认是从低位回升）
            past_dif = dif.iloc[max(0,i-lookback):i]
            if len(past_dif) == 0:
                continue
            had_lower_dif = past_dif.min() < dif.iloc[i]

            # 条件3: 价格在20日均线下方（确认在调整区，不是追高）
            price_below_ma = price.iloc[i] < ma20.iloc[i]

            # 综合信号：DIF回升 + 从低位回升 + 价格在均线下方
            if dif_rising and had_lower_dif and price_below_ma:
                divergence.iloc[i] = True

        return divergence

    # ============================================================
    # 6. 综合评分（0-100分）
    # ============================================================
    def compute_composite_score(self, price):
        """计算综合评分（0-100分）

        低分=买入机会，高分=卖出信号

        Args:
            price: Series, ETF收盘价

        Returns:
            scores: dict, 包含各因子得分和综合得分
        """
        from params import SCORING_WEIGHTS

        # 计算各因子得分
        mom_score = self.calc_momentum_score(price)
        mr_score = self.calc_mean_reversion_score(price)
        trend_dev_score = self.calc_trend_deviation_score(price)

        # 加权合成
        composite = (
            mom_score * SCORING_WEIGHTS['momentum'] +
            mr_score * SCORING_WEIGHTS['mean_reversion'] +
            trend_dev_score * SCORING_WEIGHTS['trend_deviation']
        )

        return {
            'composite': composite.clip(0, 100),
            'momentum': mom_score,
            'mean_reversion': mr_score,
            'trend_deviation': trend_dev_score,
        }