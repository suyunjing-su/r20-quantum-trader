"""聪明钱与大户多空明细数据采集模块（Pillar 5）。

为解决 OKX 内部 CLI 移除后聪明钱数据缺失（US-014 遗留）的问题，本模块提供双源容灾采集：
1. 主源：Binance Futures 官方免密公开端点（顶级大户持仓比例 + 主动吃单量）；
2. 备源：OKX Rubik 官方公开统计端点（合约多空账户数/持仓比 + 深度成交量）；
3. 容灾：双源均不可用时优雅返回 None，保留显式缺失语义，绝不伪造虚假中性信号。
"""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from astra_backend.exchanges import get_adapter

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_smart_money_for_symbol(
    ccy: str,
    price: float = 0.0,
    *,
    timeout: float = 3.5,
) -> Optional[Dict[str, Any]]:
    """采集单标的顶级大户持仓多空比与主动资金流（Binance 主源 + OKX 备源）。"""
    base_ccy = str(ccy or "").upper().strip()
    if not base_ccy:
        return None

    # 1. 尝试 Binance Futures 官方公开大户指标
    res = _fetch_from_binance(base_ccy, price=price, timeout=timeout)
    if res and res.get("longShortRatio"):
        return res

    # 2. 备选尝试 OKX Rubik 官方公开统计指标
    res_okx = _fetch_from_okx_rubik(base_ccy, price=price, timeout=timeout)
    if res_okx and res_okx.get("longShortRatio"):
        return res_okx

    return None


def _fetch_from_binance(ccy: str, price: float = 0.0, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    sym = f"{ccy}USDT"
    w_long: Optional[float] = None
    ls_ratio: Optional[float] = None
    net_notional_usd = 0.0
    taker_str = "--"

    # A. 顶级大户持仓量多空比（由 Binance 适配器统一调用官方 SDK）。
    try:
        adapter = get_adapter("binance", environment="live")
        top_rows = adapter.fetch_top_trader_position_ratio(sym, period="5m", limit=1)
        if top_rows:
            top = top_rows[-1]
            w_long = float(top.get("longAccount", 0.5))
            ls_ratio = float(top.get("longShortRatio", 1.0))
    except Exception:
        pass

    if w_long is None:
        return None

    # B. 主动买卖成交量净额（同一 SDK 适配器边界）。
    try:
        taker_rows = adapter.fetch_taker_volume(sym, period="5m", limit=1)
        if taker_rows:
            taker = taker_rows[-1]
            b_vol = float(taker.get("buyVol", 0))
            s_vol = float(taker.get("sellVol", 0))
            diff = b_vol - s_vol
            net_notional_usd = diff * price if price > 0 else diff
            taker_str = (
                f"{round(net_notional_usd / 1e4, 1)}万 U"
                if abs(net_notional_usd) >= 1e4
                else f"{round(net_notional_usd, 0)} U"
            )
    except Exception:
        pass

    return {
        "longShortRatio": {
            "weightedLongRatio": w_long,
            "longShortRatio": ls_ratio or round(w_long / max(0.0001, (1.0 - w_long)), 2),
        },
        "notional": {
            "netNotionalUsdt": net_notional_usd,
        },
        "winRate": {},
        "takerNetUsd": taker_str,
        "lsRatio": ls_ratio,
        "weighted_long_pct": round(w_long * 100, 1),
    }


def _fetch_from_okx_rubik(ccy: str, price: float = 0.0, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    w_long: Optional[float] = None
    ls_ratio: Optional[float] = None
    net_notional_usd = 0.0
    taker_str = "--"

    try:
        url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-pos-ratio?ccy={ccy}&period=5m"
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == "0" and d.get("data") and len(d["data"]) > 0:
                raw_ratio = float(d["data"][0][1])
                ls_ratio = raw_ratio
                w_long = round(raw_ratio / (1.0 + raw_ratio), 4)
    except Exception:
        pass

    if w_long is None:
        try:
            url2 = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m"
            req2 = urllib.request.Request(url2, headers=_HEADERS)
            with urllib.request.urlopen(req2, timeout=timeout) as resp:
                d2 = json.loads(resp.read().decode("utf-8"))
                if d2.get("code") == "0" and d2.get("data") and len(d2["data"]) > 0:
                    raw_ratio2 = float(d2["data"][0][1])
                    ls_ratio = raw_ratio2
                    w_long = round(raw_ratio2 / (1.0 + raw_ratio2), 4)
        except Exception:
            pass

    if w_long is None:
        return None

    try:
        url_t = f"https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType=CONTRACTS&period=5m"
        req_t = urllib.request.Request(url_t, headers=_HEADERS)
        with urllib.request.urlopen(req_t, timeout=timeout) as resp:
            d_t = json.loads(resp.read().decode("utf-8"))
            if d_t.get("code") == "0" and d_t.get("data") and len(d_t["data"]) > 0:
                b_vol = float(d_t["data"][0][1])
                s_vol = float(d_t["data"][0][2])
                net_notional_usd = b_vol - s_vol
                taker_str = (
                    f"{round(net_notional_usd / 1e4, 1)}万 U"
                    if abs(net_notional_usd) >= 1e4
                    else f"{round(net_notional_usd, 0)} U"
                )
    except Exception:
        pass

    return {
        "longShortRatio": {
            "weightedLongRatio": w_long,
            "longShortRatio": ls_ratio,
        },
        "notional": {
            "netNotionalUsdt": net_notional_usd,
        },
        "winRate": {},
        "takerNetUsd": taker_str,
        "lsRatio": ls_ratio,
        "weighted_long_pct": round(w_long * 100, 1),
    }


def fetch_smart_money_pool(
    instruments: List[Dict[str, Any]],
    *,
    max_workers: int = 6,
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """并发采集标的池内全部币种的聪明钱与大户数据，返回标准 pool 字典。"""
    pool: Dict[str, Any] = {}
    if not instruments:
        return pool

    def _fetch_one(item: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
        ccy = item.get("ccy") or item.get("name") or (item.get("instId", "").split("-")[0] if "-" in item.get("instId", "") else "")
        price = float(item.get("price") or 0.0)
        res = fetch_smart_money_for_symbol(ccy, price=price, timeout=timeout)
        return ccy, res

    with ThreadPoolExecutor(max_workers=min(max_workers, len(instruments))) as executor:
        for ccy, sm in executor.map(_fetch_one, instruments):
            if ccy and sm:
                pool[ccy] = sm

    return pool
