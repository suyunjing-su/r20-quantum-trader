"""多所 6 账户权益聚合与组合风险隔离引擎（US-006）。

设计原则与铁律：
1. 严格分区：demo 与 live 两环境绝对物理隔离，严禁跨环境混合相加。
2. 未知≠0：读不到或非 ready 状态的交易所不计入聚合分母，同时在 reporting_venues 中明确标示参与所。
3. 纯函数：本模块不持有全局锁或网络 IO，便于端点复用与 100% 封闭单测。
4. 资产分布与风控利用率：
   - asset_distribution: 各所在当前环境总权益中的占比 (0.0 ~ 100.0%)
   - portfolio_risk_utilization: 保证金占用额与利用率百分比，给出 LOW/MEDIUM/HIGH 风险级别。
"""
from __future__ import annotations

from typing import Any, Dict, List


def aggregate_venue_accounts(venues_dict: Dict[str, Any], environment: str,
                             *, routing_enabled: bool = True,
                             calculation_venue: str | None = None) -> Dict[str, Any]:
    """按资金环境聚合三所展示权益，并计算实际风险使用权益。

    ``total_equity`` 始终是三所展示聚合；当多所撮合关闭时，
    ``calculation_equity`` 只取唯一开闸所的 settled_equity，未知不会降级为 0。
    """
    env = str(environment or "").strip().lower()
    if env not in ("demo", "live"):
        raise ValueError(f"environment 必须为 'demo' 或 'live'，收到: {environment!r}")

    total_equity = 0.0
    total_available = 0.0
    total_positions = 0
    total_open_orders = 0
    active_venues: List[str] = []
    venue_equities: Dict[str, float] = {}

    target_venues = ("okx", "binance", "gate")
    for v in target_venues:
        card = venues_dict.get(v)
        if not isinstance(card, dict):
            continue
        # 仅当状态为 ready 且具有有效数值时计入聚合
        if card.get("status") == "ready":
            eq = card.get("equity")
            avail = card.get("available")
            # 审计⑤(2026-09-13)·门对称：旧实现 eq 与 avail 各判各的——某卡 eq 失效
            # （缺字段/负数）但 avail 有效时，它不进 reporting_venues 却把 avail
            # 混进聚合，margin_used=Σeq-Σavail 口径失真（假负值被 max(0,…) 掩盖）。
            # 一张卡要么整体参与聚合，要么整体不进；avail 缺失但 eq 有效仍按 0 占用
            # 计入（全额 margin 是保守侧）。
            if eq is not None and isinstance(eq, (int, float)) and eq >= 0:
                eq_val = float(eq)
                total_equity += eq_val
                venue_equities[v] = eq_val
                active_venues.append(v)
                if avail is not None and isinstance(avail, (int, float)) and avail >= 0:
                    total_available += float(avail)
            pos_cnt = card.get("positions_count")
            if isinstance(pos_cnt, int) and pos_cnt >= 0:
                total_positions += pos_cnt
            ord_cnt = card.get("open_orders_count")
            if isinstance(ord_cnt, int) and ord_cnt >= 0:
                total_open_orders += ord_cnt

    total_equity = round(total_equity, 2)
    total_available = round(total_available, 2)
    margin_used = round(max(0.0, total_equity - total_available), 2)
    utilization_pct = round((margin_used / total_equity) * 100.0, 2) if total_equity > 0 else 0.0

    # 风险等级判定
    if utilization_pct > 70.0:
        risk_level = "HIGH"
    elif utilization_pct > 30.0:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    # 资产分布百分比
    distribution: Dict[str, Dict[str, float]] = {}
    for v in target_venues:
        v_eq = venue_equities.get(v, 0.0)
        share = round((v_eq / total_equity) * 100.0, 2) if total_equity > 0 else 0.0
        distribution[v] = {
            "equity": round(v_eq, 2),
            "share_pct": share,
        }

    calc_equity = None
    calc_status = "multi_venue_display_equity"
    calc_basis = "multi_venue_display_equity"
    if not routing_enabled:
        calc_basis = "single_open_venue_settled_equity"
        calc_status = "unknown_active_venue"
        if calculation_venue in target_venues:
            card = venues_dict.get(calculation_venue)
            settled = (card.get("settled_equity") if isinstance(card, dict)
                       else None)
            if settled is None and isinstance(card, dict):
                settled = card.get("_settled_equity")
            if isinstance(settled, (int, float)) and settled >= 0:
                calc_equity = round(float(settled), 2)
                calc_status = "ok"
            else:
                calc_status = "unknown_settled_equity"
    else:
        calc_equity = total_equity
        calc_status = "ok" if active_venues else "unknown_display_equity"

    calculation_available = None
    calculation_margin_used = None
    calculation_utilization_pct = None
    calculation_risk_level = None
    if routing_enabled:
        if calc_equity is not None:
            calculation_available = total_available
    elif calculation_venue in target_venues:
        calc_card = venues_dict.get(calculation_venue)
        raw_available = calc_card.get("available") if isinstance(calc_card, dict) else None
        if isinstance(raw_available, (int, float)) and raw_available >= 0:
            calculation_available = round(float(raw_available), 2)

    if calc_equity is not None and calculation_available is not None:
        calculation_margin_used = round(max(0.0, calc_equity - calculation_available), 2)
        calculation_utilization_pct = round(
            calculation_margin_used / calc_equity * 100.0, 2) if calc_equity > 0 else 0.0
        if calculation_utilization_pct > 70.0:
            calculation_risk_level = "HIGH"
        elif calculation_utilization_pct > 30.0:
            calculation_risk_level = "MEDIUM"
        else:
            calculation_risk_level = "LOW"

    return {
        "environment": env,
        "total_equity": total_equity,
        "display_total_equity": total_equity,
        "total_available": total_available,
        "calculation_equity": calc_equity,
        "calculation_venue": calculation_venue if not routing_enabled else None,
        "calculation_basis": calc_basis,
        "calculation_status": calc_status,
        "routing_enabled": bool(routing_enabled),
        "calculation_available": calculation_available,
        "calculation_margin_used": calculation_margin_used,
        "calculation_utilization_pct": calculation_utilization_pct,
        "calculation_risk_level": calculation_risk_level,
        "margin_used": margin_used,
        "utilization_pct": utilization_pct,
        "risk_level": risk_level,
        "positions_count": total_positions,
        "open_orders_count": total_open_orders,
        "active_venues_count": len(active_venues),
        "reporting_venues": active_venues,
        "asset_distribution": distribution,
    }
