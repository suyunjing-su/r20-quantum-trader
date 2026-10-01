"""单标的数据包装配（B3 抽取，`ai_brain_trader.py` 的第一块）。

从 `scripts/ai_brain_trader.py` 搬出的 `fetch_single_instrument_package`（254 行）——
主脑每 15 分钟对每个标的跑一次的行情/指标装配。搬走前门面 1839 行。

## 为什么是这块

- **全仓零测试覆盖、零源码锚点**：`grep -rn fetch_single_instrument_package tests/`
  与 `grep -rn 'split("def ' tests/` 双向实测，均无引用；
- **依赖面极窄**：整段只读 `json` / `urllib` 两个标准库模块、
  `Dict` / `Any` 两个 typing 名，外加 `fetch_candles` / `fetch_single_indicator`
  两个**注入**进来的函数（见下），以及函数体内一处延迟导入的
  `calculus_engine.calculate_multi_timeframe`（保持延迟，测试环境里不一定可导入）；
- 因此是「整段搬走」而非「消除抄写」，收益是行数，不需要逐值对拍 —— 但仍做了
  两条路径的差分（见 `tests/extraction/test_brain_package_extraction.py`）。

## 为什么两个行情函数是注入而不是 import

与 `scripts/trader/factors.py` 同一理由：`tests/risk_test_env.py::pin_baseline_risk_env()`
的**原地 reload 名单只有** `risk_constants` / `ai_factor_trader` / `ai_brain_trader`，
**不含任何子模块**。若本模块在 import 期把 `fetch_candles` 绑死，
门面重载后指向的就不再是同一个对象；更实际的是**测试若对门面做
`patch.object(abt, "fetch_candles")` 就会失效**。注入从结构上消除这两种陷阱。

正因如此，本模块**不 import 门面的任何东西**，只在调用期接收依赖。
"""
import json
import math
import time
import urllib.request

from typing import Any, Dict

# 行情取数失败的可观测性（第 137 刀）：只计数 + 每类一次性告警，绝不改变取值行为。
try:                      # 以脚本方式运行（SCRIPTS_DIR 在 sys.path 上）
    from market_data_health import note_failure
except ImportError:       # 以 scripts.brain.* 包被导入（PROJECT_ROOT 在 sys.path 上）
    from scripts.market_data_health import note_failure


def _minimum_order_requirements(base: str, price: float, allowed_venues=None) -> Dict[str, Any]:
    """从标的池允许的交易所公共合约规格计算当前价格下的最小开仓名义价值。"""
    requirements: Dict[str, Any] = {
        venue: {"minimum_notional_usdt": None, "source": "unavailable"}
        for venue in ("okx", "binance", "gate")
    }
    try:
        from astra_backend.exchanges.registry import adapter_environment, get_adapter
    except Exception:
        return requirements

    venues = ({str(v).strip().lower() for v in allowed_venues}
              if isinstance(allowed_venues, (list, tuple, set)) else {"okx", "binance", "gate"})
    for venue in ("okx", "binance", "gate"):
        if venue not in venues:
            continue
        try:
            adapter = get_adapter(venue, environment=adapter_environment(venue))
            spec = adapter.fetch_instrument_spec(base)
            if spec is None:
                continue
            raw = getattr(spec, "raw", {}) or {}
            minimum = 0.0
            if venue == "binance":
                # USDⓈ-M exchangeInfo 的 MIN_NOTIONAL / NOTIONAL 为 USDT 名义门槛；
                # minQty × 价格也必须同时满足。
                for item in raw.get("filters", []):
                    if item.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                        minimum = max(minimum, float(item.get("notional") or item.get("minNotional") or 0))
                minimum = max(minimum, float(getattr(spec, "min_size", 0) or 0) * price)
            else:
                # OKX: minSz × ctVal；Gate: order_size_min × quanto_multiplier。
                minimum = (float(getattr(spec, "min_size", 0) or 0)
                           * float(getattr(spec, "ct_val", 0) or 0) * price)
            if minimum > 0:
                requirements[venue] = {
                    "minimum_notional_usdt": round(minimum, 8),
                    "source": "public_contract_spec",
                }
        except Exception as exc:
            note_failure(f"{venue}_minimum_order_spec", exc)
    return requirements


def fetch_single_instrument_package(item: Dict[str, Any], *,
                                   fetch_candles,
                                   fetch_single_indicator,
                                   fetch_market_bundle=None) -> Dict[str, Any]:
    """装配单标的数据包；行情优先使用按标的池 venue 选择的同源数据包。"""
    inst_id = item["instId"]
    name = item["name"]
    ccy = item.get("ccy", "")
    headers = {"User-Agent": "Mozilla/5.0"}
    market_bundle = None
    if fetch_market_bundle is not None:
        try:
            market_bundle = fetch_market_bundle(
                inst_id, item.get("venues"), {"15m": 24, "1H": 60, "4H": 16})
        except Exception as exc:
            note_failure("venue_market_bundle", exc)

    def _candles(bar: str, limit: int):
        if fetch_market_bundle is not None:
            return (market_bundle or {}).get("candles", {}).get(bar, [])
        return fetch_candles(inst_id, bar=bar, limit=limit)

    pkg = {
        "instId": inst_id,
        "name": name,
        "type": item["type"],
        "precision": item["precision"],
        "price": 0.0,
        "chg24h": 0.0,
        "bidPx": 0.0,
        "askPx": 0.0,
        "fundingRate": 0.0,
        "oiUsd": "N/A",
        "vol24h": 0.0,
        "lsRatio": "N/A",
        "takerNetUsd": "N/A",
        "atr": 0.0,
        "rsi": 50.0,
        "vwap_bias": 0.0,
        "macd_hist": 0.0,
        "macd_accel": 0.0,
        "vol_ratio": 1.0,
        "obv_flow": "NEUTRAL",
        "adx_1h": 0.0,
        "smart_money": {
            "available": False,
            "reason": "OKX CLI 已移除，smartmoney 无公开 V5 等价接口（待接新数据源）",
            "weighted_long_pct": "--",
            "net_flow_usdt": "--",
            "avg_long_entry": "--",
            "avg_short_entry": "--",
            "top_win_rate": "--"
        },
        "recent_15m": [],
        "recent_1h": [],
        "recent_4h": [],
        "minimum_order_requirements": {},
        "market_data_venue": "",
        "calculus": {"valid": False, "regime": "DATA_UNRELIABLE", "quality": 0.0},
        "data_quality": "invalid"
    }

    # 1. Ticker: production supplies a venue-constrained bundle; legacy test callers retain the OKX path.
    t_okx0 = time.time()
    if market_bundle is not None:
        t = market_bundle.get("ticker") or {}
        pkg["price"] = float(t.get("last") or 0)
        pkg["bidPx"] = float(t.get("bid") or 0)
        pkg["askPx"] = float(t.get("ask") or 0)
        pkg["chg24h"] = round(float(t.get("chg_24h_pct") or 0), 2)
        pkg["vol24h"] = round(float(t.get("vol_24h_base") or 0), 2)
        pkg["market_data_venue"] = str(market_bundle.get("venue") or "")
        pkg["market_data_latency_ms"] = max(1, int(round((time.time() - t_okx0) * 1000)))
        funding = market_bundle.get("funding_rate")
        if funding is not None:
            pkg["fundingRate"] = round(float(funding) * 100, 4)
    elif fetch_market_bundle is None:
        try:
            req = urllib.request.Request(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == "0" and d.get("data"):
                    t = d["data"][0]
                    pkg["price"] = float(t.get("last", 0))
                    pkg["bidPx"] = float(t.get("bidPx", pkg["price"]) or pkg["price"])
                    pkg["askPx"] = float(t.get("askPx", pkg["price"]) or pkg["price"])
                    op = float(t.get("open24h", 0) or 0)
                    pkg["chg24h"] = round(((pkg["price"] - op) / op * 100) if op > 0 else 0, 2)
                    pkg["vol24h"] = round(float(t.get("vol24h", 0) or 0), 2)
                    pkg["okx_latency_ms"] = max(1, int(round((time.time() - t_okx0) * 1000)))
        except Exception as exc:
            note_failure("okx_ticker", exc)

    # 2. 15M Candles (recent 24, about 6 hours) & Technical Indicators Calculation
    try:
        d = {"data": _candles("15m", 24)}
        if d["data"]:
            raw_candles = d["data"]
            pkg["recent_15m"] = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), round(float(c[5]), 1)] for c in raw_candles[:12]]

            # Calculate 15M indicators
            if len(raw_candles) >= 15:
                closes = [float(c[4]) for c in reversed(raw_candles)]
                highs = [float(c[2]) for c in reversed(raw_candles)]
                lows = [float(c[3]) for c in reversed(raw_candles)]
                vols = [float(c[5]) for c in reversed(raw_candles)]

                # ATR 15M
                tr_list = []
                for i in range(1, len(closes)):
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                    tr_list.append(tr)
                if len(tr_list) >= 14:
                    pkg["atr_15m"] = round(sum(tr_list[-14:]) / 14, 4)
                    pkg["atr"] = pkg["atr_15m"]

                # RSI 15M
                diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gains = [d if d > 0 else 0 for d in diffs]
                losses = [-d if d < 0 else 0 for d in diffs]
                if len(gains) >= 14:
                    avg_g = sum(gains[-14:]) / 14
                    avg_l = sum(losses[-14:]) / 14
                    rs = (avg_g / avg_l) if avg_l > 0 else 100.0
                    pkg["rsi"] = round(100.0 - (100.0 / (1.0 + rs)), 1)
                    pkg["rsi_15m"] = pkg["rsi"]

                # VWAP Bias
                pv_sum = sum(closes[i] * vols[i] for i in range(len(closes)))
                v_sum = sum(vols)
                if v_sum > 0:
                    vwap = pv_sum / v_sum
                    pkg["vwap_bias"] = round((pkg["price"] - vwap) / vwap * 100, 2)

                # Volume Ratio (Last vs MA5)
                if len(vols) >= 6:
                    avg_v5 = sum(vols[-6:-1]) / 5
                    if avg_v5 > 0:
                        pkg["vol_ratio"] = round(vols[-1] / avg_v5, 2)

                # OBV Flow
                obv = 0
                for i in range(1, len(closes)):
                    if closes[i] > closes[i-1]:
                        obv += vols[i]
                    elif closes[i] < closes[i-1]:
                        obv -= vols[i]
                pkg["obv_flow"] = "BULL_FLOW" if obv > 0 else ("BEAR_FLOW" if obv < 0 else "NEUTRAL")
        else:
            print(f"[AI Brain] ⚠️ {inst_id} 15m K线获取失败（www/aws/CLI 三级容灾均未取回），本包 15M 微观指标降级缺省")
    except Exception as exc:
        print(f"[AI Brain] ⚠️ {inst_id} 15m K线处理异常: {exc}")

    # 3. 1H Candles (recent 24, about 24 hours) & 1H ATR / 1H RSI
    try:
        d = {"data": _candles("1H", 24)}
        if d["data"]:
            raw_1h = d["data"]
            pkg["recent_1h"] = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), round(float(c[5]), 1)] for c in raw_1h[:12]]
            if len(raw_1h) >= 15:
                closes_1h = [float(c[4]) for c in reversed(raw_1h)]
                highs_1h = [float(c[2]) for c in reversed(raw_1h)]
                lows_1h = [float(c[3]) for c in reversed(raw_1h)]

                tr_list_1h = []
                for i in range(1, len(closes_1h)):
                    tr = max(highs_1h[i] - lows_1h[i], abs(highs_1h[i] - closes_1h[i-1]), abs(lows_1h[i] - closes_1h[i-1]))
                    tr_list_1h.append(tr)
                if len(tr_list_1h) >= 14:
                    pkg["atr_1h"] = round(sum(tr_list_1h[-14:]) / 14, 4)
                    pkg["atr"] = pkg["atr_1h"]  # Elevate primary ATR to 1H

                diffs_1h = [closes_1h[i] - closes_1h[i-1] for i in range(1, len(closes_1h))]
                gains_1h = [d if d > 0 else 0 for d in diffs_1h]
                losses_1h = [-d if d < 0 else 0 for d in diffs_1h]
                if len(gains_1h) >= 14:
                    avg_g_1h = sum(gains_1h[-14:]) / 14
                    avg_l_1h = sum(losses_1h[-14:]) / 14
                    rs_1h = (avg_g_1h / avg_l_1h) if avg_l_1h > 0 else 100.0
                    pkg["rsi_1h"] = round(100.0 - (100.0 / (1.0 + rs_1h)), 1)

                # 1H Swing Structure
                if len(closes_1h) >= 10:
                    ma7_1h = sum(closes_1h[-7:]) / 7
                    ma20_1h = sum(closes_1h[-20:]) / min(len(closes_1h), 20)
                    if closes_1h[-1] > ma7_1h > ma20_1h:
                        pkg["structure_1h"] = "1H_SWING_BULL"
                    elif closes_1h[-1] < ma7_1h < ma20_1h:
                        pkg["structure_1h"] = "1H_SWING_BEAR"
                    else:
                        pkg["structure_1h"] = "1H_SWING_CHOP"
        else:
            print(f"[AI Brain] ⚠️ {inst_id} 1H K线获取失败（www/aws/CLI 三级容灾均未取回），1H ATR/RSI/结构字段降级缺省")
    except Exception as exc:
        print(f"[AI Brain] ⚠️ {inst_id} 1H K线处理异常: {exc}")

    # 4. 4H Candles (recent 16, about 64 hours) & 4H Macro Structure
    try:
        d = {"data": _candles("4H", 16)}
        if d["data"]:
            raw_4h = d["data"]
            pkg["recent_4h"] = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), round(float(c[5]), 1)] for c in raw_4h[:8]]
            if len(raw_4h) >= 8:
                closes_4h = [float(c[4]) for c in reversed(raw_4h)]
                ma5_4h = sum(closes_4h[-5:]) / 5
                ma12_4h = sum(closes_4h[-12:]) / min(len(closes_4h), 12)
                if closes_4h[-1] > ma5_4h > ma12_4h:
                    pkg["macro_4h"] = "4H_MACRO_BULL (大级别多头通道)"
                elif closes_4h[-1] < ma5_4h < ma12_4h:
                    pkg["macro_4h"] = "4H_MACRO_BEAR (大级别空头承压)"
                else:
                    pkg["macro_4h"] = "4H_MACRO_RANGE (大级别区间震荡)"
        else:
            print(f"[AI Brain] ⚠️ {inst_id} 4H K线获取失败（www/aws/CLI 三级容灾均未取回），4H 宏观结构字段降级缺省")
    except Exception as exc:
        print(f"[AI Brain] ⚠️ {inst_id} 4H K线处理异常: {exc}")

    # 5. Funding Rate & OI
    if item["type"] == "crypto" and (market_bundle is None or market_bundle.get("venue") == "okx"):
        try:
            req = urllib.request.Request(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == "0" and d.get("data"):
                    pkg["fundingRate"] = round(float(d["data"][0].get("fundingRate", 0)) * 100, 4)
        except Exception as exc:
            note_failure("okx_funding_rate", exc)

        try:
            req = urllib.request.Request(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={inst_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == "0" and d.get("data"):
                    usd = float(d["data"][0].get("oiUsd", 0) or 0)
                    pkg["oiUsd"] = f"{round(usd / 1e8, 2)}亿 U" if usd > 1e8 else f"{round(usd / 1e4, 1)}万 U"
        except Exception as exc:
            note_failure("okx_open_interest", exc)

        if ccy:
            try:
                req = urllib.request.Request(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m", headers=headers)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == "0" and d.get("data") and len(d["data"]) > 0:
                        pkg["lsRatio"] = float(d["data"][0][1])
            except Exception as exc:
                note_failure("okx_ls_ratio", exc)

            try:
                req = urllib.request.Request(f"https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType=CONTRACTS&period=5m", headers=headers)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == "0" and d.get("data") and len(d["data"]) > 0:
                        b_vol = float(d["data"][0][1])
                        s_vol = float(d["data"][0][2])
                        net_diff = b_vol - s_vol
                        pkg["takerNetUsd"] = f"{round(net_diff / 1e4, 1)}万 U"
            except Exception as exc:
                note_failure("okx_taker_volume", exc)

        # 6. ADX Trend Strength (1H). Non-OKX fallback data is calculated from the
        # selected venue's own candle history rather than remaining at default zero.
        try:
            adx_rows = (market_bundle or {}).get("candles", {}).get("1H", [])
            if market_bundle is None or market_bundle.get("venue") == "okx":
                adx_data = fetch_single_indicator(inst_id, "ADX", bar="1H")
                if adx_data and "adx" in adx_data and adx_data.get("adx") is not None:
                    adx_value = float(adx_data["adx"])
                    if math.isfinite(adx_value):
                        pkg["adx_1h"] = adx_value
            if pkg["adx_1h"] == 0.0 and adx_rows:
                from scripts.market_data_service import calculate_adx_from_candles
                adx_value = calculate_adx_from_candles(adx_rows, newest_first=True)
                if adx_value is not None:
                    pkg["adx_1h"] = round(adx_value, 2)
        except Exception as exc:
            note_failure("okx_adx_1h", exc)

    # Funding/OI endpoints above are OKX-specific; ADX is venue-neutral math and
    # must still be populated when the same-source market bundle selected Binance/Gate.
    if (item["type"] == "crypto" and market_bundle is not None
            and market_bundle.get("venue") != "okx"):
        try:
            adx_rows = market_bundle.get("candles", {}).get("1H", [])
            if adx_rows:
                from scripts.market_data_service import calculate_adx_from_candles
                adx_value = calculate_adx_from_candles(adx_rows, newest_first=True)
                if adx_value is not None:
                    pkg["adx_1h"] = round(adx_value, 2)
        except Exception as exc:
            note_failure("venue_adx_1h", exc)

    required_market_data = (
        pkg["price"] > 0
        and pkg["bidPx"] > 0
        and pkg["askPx"] >= pkg["bidPx"]
        and len(pkg["recent_15m"]) >= 12
        and len(pkg["recent_1h"]) >= 8
        and len(pkg["recent_4h"]) >= 6
    )
    try:
        from calculus_engine import calculate_multi_timeframe
        pkg["calculus"] = calculate_multi_timeframe({
            "15M": pkg["recent_15m"],
            "1H": pkg["recent_1h"],
            "4H": pkg["recent_4h"],
        })
    except Exception as exc:
        pkg["calculus"] = {"valid": False, "regime": "DATA_UNRELIABLE", "quality": 0.0, "error": str(exc)}
    pkg["data_quality"] = "valid" if required_market_data else "invalid"
    if pkg["price"] > 0:
        pkg["minimum_order_requirements"] = _minimum_order_requirements(
            name, pkg["price"], item.get("venues"))
    return pkg
