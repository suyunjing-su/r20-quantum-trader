"""交易标的池（instrument_pool.json）的 venues 字段与三所准入池的同步层。

背景（2026-09-26 需求收口）：
- 三所独立永续合约池的**单一事实源**改为交易标的池——每个合约在池内直接
  声明可参与撮合的场所列表（``venues: ["okx", "binance", "gate"]``）；
- 同一合约勾选多个所时，撮合路由策略（A/B/C 三档 + 手选优先）决定最终执行所；
- ``data/venue_routing.json`` 的 ``{venue}.instruments`` 退化为**派生视图**：
  ``routing_policy.load_venue_pool`` 在主池出现 venues 字段后按主池派生，
  旧配置文件仅作读取兼容；本模块负责把直接写旧文件的入口折回主池。
"""
from __future__ import annotations

from typing import Any, Dict, List

VENUES = ("okx", "binance", "gate")


def _normalize(items: List[str]) -> List[str]:
    return sorted({str(v).strip().lower() for v in items if str(v).strip().lower() in VENUES})


def sync_venue_pools_to_master(pool_updates: Dict[str, List[str]]) -> Dict[str, Any]:
    """把直接写入三所池的变更折回交易标的池的 venues 字段（单一事实源）。

    语义：
    - 某合约出现在 ``pool_updates[venue]`` ⇒ 主池该合约的 venues 并入该所；
    - 某合约从某所池中移除 ⇒ 主池该合约的 venues 减去该所；**但至少保留一个所**，
      全部清空会令合约失去任何执行场所（管理员应改用删除标的功能）。
    - 主池中不存在的合约直接忽略（三所池选项本就来自主池）。
    """
    from scripts.instrument_pool import mutate_instruments

    touched: Dict[str, List[str]] = {}

    def _apply(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        master_ids = {str(item.get("instId") or "").strip().upper(): item for item in pool}
        venue_of: Dict[str, set] = {
            inst_id: set(_normalize(item.get("venues") or ["okx"]))
            for inst_id, item in master_ids.items()
        }
        for venue, instruments in (pool_updates or {}).items():
            vkey = str(venue or "").strip().lower()
            if vkey not in VENUES:
                continue
            selected = {str(i).strip().upper() for i in (instruments or [])}
            for inst_id, current in venue_of.items():
                inst_id_up = str(inst_id).upper()
                if inst_id_up in selected:
                    current.add(vkey)
                else:
                    current.discard(vkey)
        for item in pool:
            inst_id = str(item.get("instId") or "").strip().upper()
            new_venues = sorted(venue_of.get(inst_id, set()))
            if not new_venues:
                raise ValueError(
                    f"{inst_id} 在旧所池同步后没有任何可交易所；请至少保留一个交易所")
            old_venues = sorted(_normalize(item.get("venues") or ["okx"]))
            if new_venues != old_venues:
                item["venues"] = new_venues
                touched[inst_id] = new_venues
        return pool

    mutate_instruments(_apply)
    return {"updated": touched}
