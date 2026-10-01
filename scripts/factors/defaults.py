"""因子库的默认结构（结构优化阶段 4·B3 第二十五刀）。

原样搬自 `scripts/factor_library.py::compute_instrument_factors` 开头的
95 行 `factors = {...}` 字面量。

## 为什么这段值得抽

`compute_instrument_factors` 是 438 行的单函数，而其中 **95 行是一个纯默认值
字面量** —— 六个 Pillar 的完整结构、每个字段的初值与占位符。它不读任何外部
状态（除了 `time.time()`），却占了整个函数近四分之一，把真正的计算逻辑挤到了
后半段。

抽出来之后，`compute_instrument_factors` 一开头就是"建默认结构 → 逐段填充"，
读者能立刻看到**数据形状**与**计算过程**的分界。

## ⚠️ `timestamp` 用函数调用而不是模块级常量

结构里唯一的"活值"是 `"timestamp": int(time.time())`。**必须每次调用求值** ——
若写成模块级常量（`_DEFAULTS = {... "timestamp": int(time.time())}`），
那么所有标的、所有周期都会拿到**模块首次导入那一刻**的时间戳，
且因子库缓存的时间判定会全线偏早。这是一个很容易在抽取时犯的错，故写在这里。

## 字段语义（原样保留，不得"顺手归一"）

- **Pillar 5 `smart_money_derivatives` 的 `available=False` 是刻意的**：
  OKX CLI 已移除且 smartmoney 无公开 V5 等价接口。数据源缺失时显式
  `available=False` + `"--"` 占位符，**禁止以 50/NEUTRAL/0 中性值冒充真实信号**
  —— 那会让前端把"没有数据"渲染成"信号中性"。注释也原样搬。
- **各 Pillar 的初值口径不同且都有含义**：比率类用 `1.0`（中性比值）、
  百分比类用 `50.0`（中性百分位）、幅度类用 `0.0`。不要统一成 `0.0` —
  那会把"中性"变成"极度看空"。
- `"taker_net_usd": "0 U"` 是**带单位的字符串**，不是数字。
"""
from __future__ import annotations

import time
from typing import Any, Dict

__all__ = ["build_default_factors"]


def build_default_factors(inst_id: str, name: str) -> Dict[str, Any]:
    """按标的名建出因子字典的完整默认结构。

    `timestamp` 在**调用时**取当前时间（见模块文档串）。
    """
    return {
        "instId": inst_id,
        "name": name,
        "timestamp": int(time.time()),
        "price": 0.0,
        "chg24h": 0.0,

        # Pillar 1: Trend & Momentum
        "trend_momentum": {
            "adx_1h": 0.0,
            "rsi_14": 50.0,
            "kdj_j": 50.0,
            "vwap_bias_pct": 0.0,
            "trend_regime": "NEUTRAL"
        },

        # Pillar 2: Volatility & Channel
        "volatility_channel": {
            "atr_14": 0.0,
            "atr_pct": 0.0,
            "atr_1h": 0.0,
            "atr_1h_pct": 0.0,
            "bb_width_1h": 0.0,
            "volatility_regime": "NORMAL"
        },

        # Pillar 3: Volume & Money Flow
        "volume_money_flow": {
            "vol_ratio_15m": 1.0,
            "obv_flow": "NEUTRAL",
            "cmf_1h": 0.0,
            "taker_net_usd": "0 U",
            "flow_sentiment": "BALANCED"
        },

        # Pillar 4: Microstructure & Orderbook
        "microstructure": {
            "bid_px": 0.0,
            "ask_px": 0.0,
            "spread_pct": 0.0,
            "bid_ask_depth_ratio": 1.0,
            "depth_bias": "NEUTRAL"
        },

        # Pillar 5: Smart Money & Derivatives
        # 缺失语义：OKX CLI 已移除且 smartmoney 无公开 V5 等价接口。数据源缺失时
        # 显式 available=False + 占位符，禁止以 50/NEUTRAL/0 中性值冒充真实信号。
        "smart_money_derivatives": {
            "available": False,
            "reason": "OKX CLI 已移除，smartmoney 无公开 V5 等价接口（待接新数据源）",
            "weighted_long_pct": "--",
            "smart_money_flow_usd": "--",
            "funding_rate_pct": "--",
            "oi_usd": "--",
            "long_short_ratio": "--",
            "avg_long_entry": "--",
            "avg_short_entry": "--",
            "top_win_rate": "--",
            "signal": "UNAVAILABLE"
        },

        # Pillar 6: Calculus, Definite Integrals & Probability Theory
        "calculus_dynamics": {
            "velocity": 0.0,
            "acceleration": 0.0,
            "impulse": 0.0,
            "jerk": 0.0,
            "curvature": 0.0,
            "power": 0.0,
            "power_regime": "STEADY_FLUX",
            "regime": "RANGE_LOW_VELOCITY",
            "quality": 0.0,
            "direction": 0
        },
        "definite_integrals": {
            "energy_integral": 0.0,
            "deviation_area_integral": 0.0,
            "volume_action_integral": 0.0,
            "integral_regime": "BALANCED_ENERGY"
        },
        "probability_theory": {
            "skewness": 0.0,
            "kurtosis": 0.0,
            "continuation_prob_pct": 50.0,
            "breakdown_prob_pct": 50.0,
            "var_95_pct": 1.5,
            "cvar_95_pct": 2.2,
            "prob_regime": "GAUSSIAN_BALANCED",
            "is_fat_tail": False
        },

        # Composite Factor Score (-100 to +100)
        "composite_alpha_score": 0.0,
        "signal_recommendation": "WAIT"
    }
