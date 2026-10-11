"""持仓分批平仓止盈执行引擎（Scale-Out Engine）。

职责：
当持仓浮盈达到设定的门槛（如 1.2x ATR）时，自动市价平仓指定比例（如 50%）锁定现金利润，
同时原子级撤销旧的全量云端 OCO 保护单，换挂剩余仓位的新 OCO 保护单，并将止损价推进至
开仓成本保本位（entry_px + 0.25%），且对当前持仓生命周期加锁互斥金字塔加仓。

防御机制：
1. 最小合约张数与精度防御（pos_sz < 2*minSz 时优雅降级为整仓追踪止盈）；
2. 原生 reduceOnly 市价平仓委托（交易所底层物理杜绝反向开仓）；
3. 云端 OCO 覆盖单超额撤销重置（彻底消除原 100% 止损单残留导致的反向开仓）；
4. 金字塔顺势加仓单向锁（scale_count = 999，互斥锁定，防止边平边加）；
5. 幂等性守卫（scale_out_phase = 1，单次持仓生命周期内仅执行一次）。
"""
from __future__ import annotations

import math
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from scripts.trader.protection_identity import (
    extract_protection_identities,
    normalize_typed_identity,
    protection_identity_key,
)

try:
    from scripts.risk_constants import (
        SCALE_OUT_ENABLED,
        SCALE_OUT_RATIO,
        SCALE_OUT_TRIGGER_ATR,
    )
except Exception:
    SCALE_OUT_ENABLED = True
    SCALE_OUT_RATIO = 0.50
    SCALE_OUT_TRIGGER_ATR = 1.20


SCALE_OUT_ACCEPTED_WITH_ID = "accepted_with_id"
SCALE_OUT_EXPLICIT_REJECTION = "explicit_rejection"
SCALE_OUT_UNKNOWN = "unknown"
SCALE_OUT_FILLED = "filled"
SCALE_OUT_NOT_FILLED = "not_filled"
SCALE_OUT_PARTIAL = "partial"


def _order_id_from_response(response: Any) -> str:
    """Return a server order identity, never a client-side placeholder."""
    rows = response if isinstance(response, (list, tuple)) else [response]
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        for key in ("ordId", "orderId", "order_id", "id", "algoId", "id_string"):
            value = row.get(key) or raw.get(key)
            if value not in (None, ""):
                return str(value).strip()
        for nested_key in ("data", "result", "orders"):
            nested = row.get(nested_key)
            nested_id = _order_id_from_response(nested) if nested is not None else ""
            if nested_id:
                return nested_id
    return ""


def _response_is_explicit_rejection(response: Any) -> bool:
    """Recognize only a server-declared rejection; missing fields are unknown."""
    rows = response if isinstance(response, (list, tuple)) else [response]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("ok") is False or row.get("accepted") is False or row.get("rejected") is True:
            return True
        code = row.get("code")
        if code not in (None, "", 0, "0"):
            return True
        status = str(row.get("status") or row.get("state") or "").lower()
        if status in {"rejected", "reject", "failed", "failure", "error", "cancelled"}:
            return True
    return False


def classify_scale_out_order_result(response: Any = None, error: BaseException = None) -> Tuple[str, str]:
    """Classify a close submission without confusing transport failure with rejection.

    ``accepted_with_id`` is the only state that permits fill reconciliation.  An
    exception is explicit rejection only when its text clearly says the venue
    rejected the request; timeout/connection/router exceptions remain unknown.
    """
    if error is not None:
        text = str(error).lower()
        rejection_words = ("reject", "rejected", "拒绝", "拒单", "insufficient",
                           "invalid", "参数错误", "余额不足", "数量不足", "被拒")
        if any(word in text for word in rejection_words):
            return SCALE_OUT_EXPLICIT_REJECTION, str(error)
        return SCALE_OUT_UNKNOWN, str(error)
    if _response_is_explicit_rejection(response):
        return SCALE_OUT_EXPLICIT_REJECTION, str(response)
    order_id = _order_id_from_response(response)
    if order_id:
        return SCALE_OUT_ACCEPTED_WITH_ID, order_id
    return SCALE_OUT_UNKNOWN, "响应没有可核验的服务端订单号"


def reconcile_scale_out_position_delta(before_size: float, after_size: Any,
                                       requested_close: float, *,
                                       tolerance: float = 1e-9) -> Tuple[str, float, str]:
    """Reconcile a close by observed position delta, not by submit acceptance."""
    try:
        before = abs(float(before_size))
        after = abs(float(after_size))
        requested = abs(float(requested_close))
    except (TypeError, ValueError):
        return SCALE_OUT_UNKNOWN, 0.0, "无法解析平仓前后仓位"
    if before <= 0 or requested <= 0 or after < -tolerance or after > before + tolerance:
        return SCALE_OUT_UNKNOWN, 0.0, "仓位快照不一致"
    delta = max(0.0, before - after)
    if delta + tolerance < requested:
        if delta <= tolerance:
            return SCALE_OUT_NOT_FILLED, delta, f"实际减仓 {delta:g}，尚未达到请求量 {requested:g}"
        return SCALE_OUT_PARTIAL, delta, f"实际减仓 {delta:g}，小于请求量 {requested:g}"
    return SCALE_OUT_FILLED, delta, f"实际减仓 {delta:g} 已达到请求量 {requested:g}"


def _persist_scale_out_state(trackers, save_trackers) -> None:
    if callable(save_trackers):
        try:
            save_trackers(trackers)
        except Exception as exc:
            # The durable claim remains in memory; callers must not turn a
            # bookkeeping error into permission to submit a second close.
            print(f"[Scale-Out] warn 状态落盘失败（仍保留恢复标记）: {exc}")


def _parse_persisted_protection_snapshot(operation: Dict[str, Any], venue: str):
    """Recover typed identities first, then structured snapshots, then legacy tokens."""
    from scripts.trader.venue_protection import _ownership_evidence

    verified_flag = operation.get("old_protection_snapshot_verified")
    if verified_flag is False:
        return [], [], False, "持久化的旧保护身份快照标记为不完整"

    typed_values = operation.get("old_protection_identities")
    if isinstance(typed_values, list):
        normalized = [normalize_typed_identity(value) for value in typed_values]
        if any(value is None for value in normalized):
            return [], [], False, "持久化 typed identity 结构无效"
        keys = [protection_identity_key(value) for value in normalized]
        if len(keys) != len(set(keys)):
            return [], [], False, "持久化 typed identity 重复"
        return normalized, [], True, ""

    snapshot = operation.get("old_protection_snapshot")
    if isinstance(snapshot, list):
        identities = []
        invalid = ""
        for row in snapshot:
            if not isinstance(row, dict):
                invalid = "旧保护快照包含非对象行"
                break
            if _ownership_evidence(row) != "tag":
                invalid = "旧保护快照中含有无法证明归属的行"
                break
            row_ids = extract_protection_identities(
                row, venue=venue, ownership_evidence="tag")
            if not row_ids:
                invalid = "ASTRA 旧保护快照缺少可用的 typed identity"
                break
            identities.extend(row_ids)
        if invalid:
            return [], [], False, invalid
        keys = [protection_identity_key(value) for value in identities]
        if len(keys) != len(set(keys)):
            return [], [], False, "结构化旧保护快照存在重复 typed identity"
        if identities or verified_flag is True:
            return identities, [], True, ""

    # A legacy string is opaque. It must be resolved uniquely against live,
    # exchange-tagged rows immediately before cleanup; it is never used to
    # issue a cancellation directly.
    legacy = operation.get("old_protection_ids")
    if isinstance(legacy, (list, tuple, set)):
        tokens = [str(value).strip() for value in legacy if str(value).strip()]
        if tokens and verified_flag is not False:
            return [], tokens, True, ""
    return [], [], False, "旧保护快照缺失或未经验证"


def _snapshot_live_protection(rows: Any, *, venue: str, inst_id: str, pos_side: str):
    """Build a complete typed pre-close snapshot from a well-formed live list."""
    from scripts.trader.venue_protection import (
        _leg_position_side, _ownership_evidence, leg_base,
    )
    from astra_backend.exchanges.base import canonical_base

    if not isinstance(rows, (list, tuple)):
        return [], [], False, f"保护单列表结构异常: {type(rows).__name__}"
    wanted_base = canonical_base(inst_id)
    snapshot = []
    identities = []
    seen_keys = set()
    for row in rows:
        if not isinstance(row, dict):
            return [], snapshot, False, "保护单列表包含非对象行"
        if _ownership_evidence(row) != "tag":
            continue
        row_base = canonical_base(leg_base(row))
        if not row_base:
            return [], snapshot, False, "带 ASTRA 标签的保护腿缺少可核验合约"
        if wanted_base and row_base != wanted_base:
            continue
        row_side = _leg_position_side(row)
        if row_side not in {"long", "short"}:
            return [], snapshot, False, "带 ASTRA 标签的保护腿方向不可判定"
        if row_side != pos_side:
            continue
        row_ids = extract_protection_identities(
            row, venue=venue, ownership_evidence="tag")
        if not row_ids:
            return [], snapshot, False, "带 ASTRA 标签的旧保护腿缺少 typed server identity"
        row_keys = [protection_identity_key(item) for item in row_ids]
        if any(key is None for key in row_keys) or any(key in seen_keys for key in row_keys):
            return [], snapshot, False, "旧保护腿 typed identity 无效或重复"
        seen_keys.update(row_keys)
        identities.extend(row_ids)
        snapshot.append(dict(row))
    return identities, snapshot, True, ""


def _resolve_legacy_protection_tokens(tokens: List[str], rows: Any, *, venue: str,
                                      inst_id: str, pos_side: str):
    """Upgrade each old flat token only on one live ASTRA-tagged typed match."""
    from scripts.trader.venue_protection import (
        _leg_position_side, _ownership_evidence, leg_base,
    )
    from astra_backend.exchanges.base import canonical_base

    if not isinstance(rows, (list, tuple)):
        return [], list(tokens), "保护单回读结构异常，旧身份无法升级"
    wanted_base = canonical_base(inst_id)
    out = []
    unresolved = []
    for token in tokens:
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or _ownership_evidence(row) != "tag":
                continue
            if canonical_base(leg_base(row)) != wanted_base:
                continue
            if _leg_position_side(row) != pos_side:
                continue
            for identity in extract_protection_identities(
                    row, venue=venue, ownership_evidence="tag"):
                if identity["identity_value"] == token:
                    candidates.append((protection_identity_key(identity), identity))
        unique = {key for key, _identity in candidates if key is not None}
        if len(candidates) == 1 and len(unique) == 1:
            out.append(candidates[0][1])
        else:
            unresolved.append(token)
    detail = ("旧保护身份未能唯一升级: " + ",".join(unresolved)) if unresolved else ""
    return out, unresolved, detail


def _list_live_protection(venue: str, inst_id: str, name: str, *,
                          okx_rest: Any, venue_registry: Any):
    if venue == "okx":
        if not callable(getattr(okx_rest, "pending_algo_orders", None)):
            raise RuntimeError("OKX 缺少保护单列表能力")
        return okx_rest.pending_algo_orders(inst_id)
    if venue in {"binance", "gate"}:
        if venue_registry is None:
            raise RuntimeError(f"未提供 {venue.upper()} 适配器注册表")
        adapter = venue_registry.get_adapter(venue)
        list_orders = getattr(adapter, "list_protective_orders", None)
        if not callable(list_orders):
            raise RuntimeError(f"{venue.upper()} 适配器缺少逐腿保护单列表能力")
        return list_orders(name)
    raise RuntimeError(f"{venue.upper()} 不支持安全分批止盈所需的保护单身份能力")


def _row_protection_trigger(row: Dict[str, Any], kind: str):
    from scripts.trader.venue_protection import _protection_kind, _trigger_price

    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    keys = (("slTriggerPx", "sl_trigger_px", "stopLossPrice", "stop_loss_price")
            if kind == "sl" else
            ("tpTriggerPx", "tp_trigger_px", "takeProfitPrice", "take_profit_price"))
    for container in (row, raw):
        for key in keys:
            try:
                value = float(container.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    if _protection_kind(row) == kind:
        return _trigger_price(row)
    return None


def _verify_replacement_protection(rows: Any, *, venue: str, inst_id: str,
                                   pos_side: str, remaining_size: float,
                                   sl_px: float, tp_px: Optional[float],
                                   old_identities: List[Dict[str, Any]]):
    """Prove the new tagged protection is present and semantically complete."""
    from scripts.trader.venue_protection import (
        _is_full_close, _leg_position_side, _leg_size, _ownership_evidence, leg_base,
    )
    from astra_backend.exchanges.base import canonical_base

    if not isinstance(rows, (list, tuple)):
        return False, "挂载后保护单列表结构异常", []
    wanted_base = canonical_base(inst_id)
    old_keys = {protection_identity_key(value) for value in old_identities}
    old_keys.discard(None)
    live = []
    seen_keys = set()
    sl_size = 0.0
    full_close_sl = False
    sl_ok = False
    tp_ok = tp_px is None or float(tp_px or 0) <= 0
    for row in rows:
        if not isinstance(row, dict):
            return False, "挂载后保护单列表包含非对象行", []
        if canonical_base(leg_base(row)) != wanted_base:
            continue
        if _ownership_evidence(row) != "tag":
            continue
        row_side = _leg_position_side(row)
        if row_side not in {"long", "short"}:
            return False, "新保护腿方向不可判定", []
        if row_side != pos_side:
            continue
        identities = extract_protection_identities(
            row, venue=venue, ownership_evidence="tag")
        keys = {key for identity in identities
                for key in [protection_identity_key(identity)] if key is not None}
        if not keys:
            return False, "新 ASTRA 保护腿缺少 typed server identity", []
        fresh = keys - old_keys
        if not fresh:
            continue
        if keys & old_keys:
            return False, "新保护腿复用了旧 typed identity", []
        if fresh & seen_keys:
            return False, "多个新保护行共享同一 typed identity", []
        seen_keys.update(fresh)
        live.append((row, identities, fresh))

    if not live:
        return False, "挂载后没有发现带新 typed identity 的 ASTRA 保护腿", []
    for row, _identities, _keys in live:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        reduce_only = any(container.get("reduceOnly", container.get("reduce_only")) is True
                          or str(container.get("reduceOnly", container.get("reduce_only")) or "").lower()
                          in {"true", "1", "yes"} for container in (row, raw))
        if not reduce_only and not _is_full_close(row):
            return False, "新保护腿缺少 reduce-only/full-close 语义", []
        sl_trigger = _row_protection_trigger(row, "sl")
        if sl_trigger is not None:
            if abs(sl_trigger - float(sl_px)) > max(1e-8, abs(float(sl_px)) * 1e-4):
                return False, "新止损触发价与目标不匹配", []
            sl_ok = True
            size = _leg_size(row)
            if size is not None:
                sl_size += abs(float(size))
            if _is_full_close(row):
                full_close_sl = True
        tp_trigger = _row_protection_trigger(row, "tp")
        if tp_px is not None and float(tp_px) > 0 and tp_trigger is not None:
            if abs(tp_trigger - float(tp_px)) > max(1e-8, abs(float(tp_px)) * 1e-4):
                return False, "新止盈触发价与目标不匹配", []
            tp_ok = True
    if not sl_ok:
        return False, "新保护腿缺少目标止损触发价", []
    if not tp_ok:
        return False, "新保护腿缺少目标止盈触发价", []
    if not full_close_sl and sl_size + max(1e-9, remaining_size * 0.001) < remaining_size:
        return False, "新止损腿数量不足以覆盖余仓", []
    new_identities = []
    for _row, identities, fresh in live:
        new_identities.extend(identity for identity in identities
                              if protection_identity_key(identity) in fresh)
    return True, "", new_identities


def recover_scale_out_operations(f: Dict[str, Any], curr_pos: Dict[str, Any],
                                trackers: Dict[str, Any], timestamp_full: str,
                                executed_actions: List[str], **kwargs) -> Tuple[bool, str]:
    """Replay one durable scale-out operation without creating a new close.

    The normal position-management stage invokes this worker whenever a tracker
    carries ``scale_out_operation`` or ``scale_out_recovery_required``. The
    executor's operation guard then requires an observed position delta and
    reuses the snapshotted protection identities.
    """
    inst_id = str((f or {}).get("instId", ""))
    side = str((curr_pos or {}).get("side", "long"))
    tracker = (trackers or {}).get(f"{inst_id}_{side}", {})
    if not isinstance(tracker, dict) or not (
            isinstance(tracker.get("scale_out_operation"), dict)
            or tracker.get("scale_out_recovery_required")):
        return False, "无待恢复的分批平仓操作"
    return execute_scale_out_if_eligible(
        f, curr_pos, trackers, timestamp_full, executed_actions, **kwargs)


def execute_scale_out_if_eligible(
    f: Dict[str, Any],
    curr_pos: Dict[str, Any],
    trackers: Dict[str, Any],
    timestamp_full: str,
    executed_actions: List[str],
    *,
    okx_rest=None,
    venue_registry=None,
    record_trade=None,
    notify_trade_close=None,
    close_fee=None,
    close_trade_payload=None,
    TAKER_FEE_RATE: float = 0.0005,
    ensure_cloud_position_protection=None,
    save_trackers=None,
    query_position=None,
) -> Tuple[bool, str]:
    """若当前持仓达到分批止盈条件，执行半仓市价平仓、保本移损与云端 OCO 重置。"""
    if not SCALE_OUT_ENABLED:
        return False, "分批止盈未启用"

    if not f or not curr_pos or not f.get("market_data_valid"):
        return False, "行情数据不完整"

    inst_id = str(f.get("instId", ""))
    name = str(f.get("name", inst_id.split("-")[0]))
    cur_px = float(f.get("price", 0.0) or 0.0)
    atr = max(float(f.get("atr", 0.0) or 0.0), cur_px * 0.005)
    # ⚠️ 单位纪律（三所持仓接管）：`ctVal`/`minSz`/尺寸精度对**外所在仓**必须取
    # 该所自己的值 —— `f[...]` 一律来自 OKX 合约池，而币安的 `pos` 是币数
    # （OKX 面值 100 会算错 100 倍）、Gate 是自家张数（面值 10，错 10 倍）。
    # 持仓记录上挂的值优先；缺失时回落 OKX 口径 ⇒ OKX 路径逐位不变。
    # 注意 `prec` 只用于**尺寸**取整（价格精度见下方 `px_prec`，仍取 `f`）。
    _pos_ct = curr_pos.get("ctVal")
    _pos_min = curr_pos.get("minSz")
    _pos_prec = curr_pos.get("precision")
    ct_val = float(_pos_ct if _pos_ct else (f.get("ctVal", 1.0) or 1.0))
    min_sz = float(_pos_min if _pos_min else (f.get("minSz", 0.01) or 0.01))
    prec = int(_pos_prec if _pos_prec is not None else (f.get("precision", 2) or 2))

    pos_sz = abs(float(curr_pos.get("pos", 0.0) or 0.0))
    if pos_sz <= 0:
        return False, "无持仓"

    # ── 展示口径：一切对外文案只说**钱**（保证金 / 名义额），不说张 ──────────
    # 用户 2026-09-28 拍板：三所的"张"单位不同（OKX 张 / 币安币数 / Gate 张），
    # 且**各币种的合约面值算法都不一样** ⇒ 张数既不能跨场所比也不能跨币种比，
    # 交易员无法从它判断"这笔占了我多少钱"。张数仍用于**切分计算**本身（必须），
    # 但绝不进入文案。
    _lever = float(curr_pos.get("lever", curr_pos.get("leverage", 0.0)) or 0.0) or 1.0

    def _margin_of(_sz):
        """把原生张数换成该仓的**保证金**（U）。"""
        return round(_sz * ct_val * cur_px / _lever, 2)

    def _notional_of(_sz):
        """把原生张数换成**名义额**（U）——各币种面值不同，这里才是可比的量。"""
        return round(_sz * ct_val * cur_px, 2)

    is_long = "long" in str(curr_pos.get("side", "")).lower()
    pos_side = "long" if is_long else "short"
    entry_px = float(curr_pos.get("avgPx", 0.0) or 0.0)
    if entry_px <= 0:
        return False, "持仓均价无效"

    pos_key = f"{inst_id}_{curr_pos.get('side', pos_side)}"
    if pos_key not in trackers:
        return False, "未找到持仓跟踪器"

    t = trackers[pos_key]
    pos_venue = str(curr_pos.get("venue") or curr_pos.get("exchange") or "okx").lower()
    operation = t.get("scale_out_operation") if isinstance(t.get("scale_out_operation"), dict) else None
    if t.get("scale_out_recovery_required") and not callable(query_position):
        return False, "分批平仓已成交但保护替换未完成，禁止重复平仓"
    recovered_fill = False
    recovered_old_protection_identities = None
    recovered_legacy_protection_tokens = []
    recovered_old_protection_snapshot = None
    recovered_snapshot_verified = None
    protection_snapshot_detail = ""
    recovered_close_sz = None
    fresh_submit = False
    if operation:
        # A prior accepted/unknown request is a durable mutex. Only an observed
        # position delta can advance it; the order response itself is not a fill.
        observed = None
        if callable(query_position):
            try:
                observed = query_position(inst_id, pos_side, pos_venue)
                if isinstance(observed, dict):
                    observed = observed.get("pos", observed.get("size"))
                status, delta, detail = reconcile_scale_out_position_delta(
                    operation.get("before_size"), observed,
                    operation.get("requested_close"))
            except Exception as exc:
                status, delta, detail = SCALE_OUT_UNKNOWN, 0.0, str(exc)
        else:
            status, delta, detail = SCALE_OUT_UNKNOWN, 0.0, "缺少实际仓位核验器"
        operation["last_observed_size"] = observed
        operation["last_reconcile"] = detail
        operation["observed_close"] = delta
        if status not in {SCALE_OUT_FILLED, SCALE_OUT_PARTIAL}:
            operation["state"] = status
            _persist_scale_out_state(trackers, save_trackers)
            return False, f"分批平仓已提交但{detail}，保留恢复标记"
        recovered_fill = True
        # The caller's position snapshot may be stale across a restart. Use the
        # freshly queried post-close size for replacement protection and tracker
        # state, never the pre-operation size carried by curr_pos.
        pos_sz = abs(float(observed))
        (recovered_old_protection_identities,
         recovered_legacy_protection_tokens,
         recovered_snapshot_verified,
         protection_snapshot_detail) = _parse_persisted_protection_snapshot(operation, pos_venue)
        recovered_old_protection_snapshot = list(operation.get("old_protection_snapshot") or [])
        recovered_close_sz = float(delta or operation.get("requested_close") or 0.0)

    # 1. 幂等性守卫：若已执行过分批止盈，跳过
    if int(t.get("scale_out_phase", 0) or 0) >= 1:
        return False, "已执行过分批平仓"
    if t.get("scale_out_recovery_required") and not recovered_fill:
        return False, "分批平仓已成交但保护替换未完成，禁止重复平仓"

    # 2. 计算首批止盈目标价位（TP1）并持久化到跟踪器
    trigger_threshold = float(SCALE_OUT_TRIGGER_ATR or 1.20) * atr
    tp1_px = (entry_px + trigger_threshold) if is_long else (entry_px - trigger_threshold)
    px_prec = int(f.get("precision", 2) or 2)
    tp1_px = round(tp1_px, px_prec)
    t["scale_out_tp"] = tp1_px

    # 3. 浮盈判定
    cur_profit_px = (cur_px - entry_px) if is_long else (entry_px - cur_px)
    if cur_profit_px < trigger_threshold:
        current_desc = str(t.get("stage_desc") or "")
        if not current_desc or "监控中" in current_desc or "TP1" in current_desc:
            gain_pct = abs(tp1_px - entry_px) / entry_px * 100 if entry_px > 0 else 0.0
            t["stage_desc"] = f"持有中 (首批止盈目标 TP1: {tp1_px:g} · +{gain_pct:.1f}% · 达标平50%保本)"
        return False, "浮盈未达分批止盈门槛"

    # 4. 精度与最小下单量防御
    # 若总持仓不足 2 倍 minSz，无法安全切分为两半，优雅降级。
    # 文案用**最小下单名义**表述（`minSz × 面值 × 现价`）—— 那是跨币种可比的额。
    if not recovered_fill and pos_sz < (2.0 * min_sz - 1e-12):
        msg = (f"[{name}] 持仓名义 {_notional_of(pos_sz):.2f}U 低于分批下限"
               f"（最小下单名义 {_notional_of(min_sz):.2f}U 的 2 倍），自动降级为全仓追踪")
        executed_actions.append(msg)
        t["scale_out_phase"] = -1  # 标记为已评估但不可切分，防止每轮重复提示
        return False, "保证金不足以切分"

    ratio = max(0.1, min(0.9, float(SCALE_OUT_RATIO or 0.50)))
    raw_close_sz = pos_sz * ratio
    close_sz = round(raw_close_sz, prec)
    # 按 minSz 步长向下夹取对齐
    if min_sz > 0:
        close_sz = math.floor(close_sz / min_sz + 1e-9) * min_sz
        close_sz = round(close_sz, prec)

    remaining_sz = round(pos_sz - close_sz, prec)
    if not recovered_fill and (close_sz < min_sz or remaining_sz < min_sz):
        msg = (f"[{name}] 计算平仓名义 {_notional_of(close_sz):.2f}U 或剩余名义 "
               f"{_notional_of(remaining_sz):.2f}U 低于最小下单名义 "
               f"{_notional_of(min_sz):.2f}U，降级全仓追踪")
        executed_actions.append(msg)
        t["scale_out_phase"] = -1
        return False, "切片名义不满足最小精度"

    if recovered_fill:
        close_sz = float(recovered_close_sz or close_sz)
        remaining_sz = max(0.0, round(pos_sz, prec))

    # Snapshot every old ASTRA-owned protection identity before submitting the
    # close. Structured identities remain typed across persistence/restart.
    old_protection_identities = recovered_old_protection_identities
    old_protection_snapshot = recovered_old_protection_snapshot or []
    legacy_protection_tokens = list(recovered_legacy_protection_tokens or [])
    snapshot_verified = recovered_snapshot_verified is True
    protection_snapshot_detail = protection_snapshot_detail or ""
    if not recovered_fill:
        try:
            old_rows = _list_live_protection(
                pos_venue, inst_id, name, okx_rest=okx_rest,
                venue_registry=venue_registry)
            (old_protection_identities, old_protection_snapshot,
             snapshot_verified, protection_snapshot_detail) = _snapshot_live_protection(
                old_rows, venue=pos_venue, inst_id=inst_id, pos_side=pos_side)
        except Exception as snapshot_exc:
            old_protection_identities = []
            old_protection_snapshot = []
            snapshot_verified = False
            protection_snapshot_detail = f"旧保护腿枚举失败（{snapshot_exc}）"

    # Never submit a new reduce-only close unless the exact old protection
    # inventory can be persisted safely. A recovered fill is different: it
    # must repair the remaining position, but cleanup remains fail-closed.
    if not snapshot_verified and not recovered_fill:
        executed_actions.append(
            f"[{name}] ⚠️ 旧保护腿身份不可核验，拒绝提交分批平仓: {protection_snapshot_detail}")
        return False, "旧保护身份不可核验，拒绝平仓"

    # 4. 执行定向市价平仓（带有 reduceOnly=True）
    close_side = "sell" if is_long else "buy"
    order_success = False
    order_outcome = SCALE_OUT_UNKNOWN
    order_detail = ""

    if not recovered_fill:
        fresh_submit = True
        operation = {
            "operation_id": uuid.uuid4().hex,
            "state": "submitting",
            "venue": pos_venue,
            "inst_id": inst_id,
            "pos_side": pos_side,
            "before_size": pos_sz,
            "requested_close": close_sz,
            "old_protection_identities": list(old_protection_identities or []),
            # Deprecated compatibility field: no flattened identities are
            # written by new operations; readers treat legacy values as opaque.
            "old_protection_ids": [],
            "old_protection_snapshot": old_protection_snapshot or [],
            "old_protection_snapshot_verified": bool(snapshot_verified),
            "old_protection_namespace": f"{pos_venue}:{inst_id}:{pos_side}",
            "claimed_at": timestamp_full,
        }
        t["scale_out_operation"] = operation
        _persist_scale_out_state(trackers, save_trackers)

    if recovered_fill:
        order_success = True
        order_outcome = SCALE_OUT_ACCEPTED_WITH_ID
        order_detail = "重放：实际仓位减仓已核验"
    else:
        if pos_venue == "okx":
            try:
                res = okx_rest.place_order(
                    inst_id,
                    close_side,
                    f"{close_sz:g}",
                    pos_side=pos_side,
                    td_mode="cross",
                    ord_type="market",
                    reduce_only=True,
                )
                order_outcome, order_detail = classify_scale_out_order_result(res)
                order_success = order_outcome == SCALE_OUT_ACCEPTED_WITH_ID
            except Exception as exc:
                order_outcome, order_detail = classify_scale_out_order_result(error=exc)
                order_success = False
        elif pos_venue == "binance":
            if venue_registry:
                try:
                    ad = venue_registry.get_adapter(pos_venue)
                    symbol_native = inst_id.split("-")[0]
                    raw_pos = curr_pos.get("raw") if isinstance(curr_pos.get("raw"), dict) else {}
                    ps = str(curr_pos.get("positionSide") or raw_pos.get("positionSide") or "").upper()
                    if ps in ("LONG", "SHORT"):
                        res = ad.place_order(symbol_native, close_side, close_sz, position_side=ps)
                    else:
                        res = ad.place_order(symbol_native, close_side, close_sz, reduce_only=True)
                    order_outcome, order_detail = classify_scale_out_order_result(res)
                    order_success = order_outcome == SCALE_OUT_ACCEPTED_WITH_ID
                except Exception as exc:
                    order_outcome, order_detail = classify_scale_out_order_result(error=exc)
                    order_success = False
            else:
                order_success = False
                order_detail = "未提供 BINANCE 适配器注册表"
        elif pos_venue == "gate":
            if venue_registry:
                try:
                    ad = venue_registry.get_adapter(pos_venue)
                    symbol_native = inst_id.split("-")[0]
                    res = ad.place_order(symbol_native, close_side, close_sz, reduce_only=True)
                    order_outcome, order_detail = classify_scale_out_order_result(res)
                    order_success = order_outcome == SCALE_OUT_ACCEPTED_WITH_ID
                except Exception as exc:
                    order_outcome, order_detail = classify_scale_out_order_result(error=exc)
                    order_success = False
            else:
                order_success = False
                order_detail = "未提供 GATE 适配器注册表"
        else:
            # 多所适配器路径兜底
            if venue_registry:
                try:
                    ad = venue_registry.get_adapter(pos_venue)
                    symbol_native = inst_id.split("-")[0]
                    res = ad.place_order(symbol_native, close_side, close_sz, reduce_only=True)
                    order_outcome, order_detail = classify_scale_out_order_result(res)
                    order_success = order_outcome == SCALE_OUT_ACCEPTED_WITH_ID
                except Exception as exc:
                    order_outcome, order_detail = classify_scale_out_order_result(error=exc)
                    order_success = False
            else:
                order_success = False
                order_detail = f"未提供 {pos_venue.upper()} 适配器注册表"

    if not order_success:
        if fresh_submit and operation and order_outcome == SCALE_OUT_UNKNOWN:
            operation["state"] = "unknown"
            operation["last_error"] = order_detail
            _persist_scale_out_state(trackers, save_trackers)
            executed_actions.append(f"[{name}] ⚠️ 分批止盈结果未知，保留恢复标记: {order_detail}")
            return False, "平仓结果未知，已进入恢复状态"
        # Explicit rejection or a local routing/configuration failure is safe to
        # retry, but only after deleting this operation claim durably.
        t.pop("scale_out_operation", None)
        _persist_scale_out_state(trackers, save_trackers)
        executed_actions.append(f"[{name}] ⚠️ {pos_venue.upper()} 分批止盈市价平仓提交失败: {order_detail}")
        return False, "平仓提交失败"

    if not fresh_submit:
        # We are replaying a previously accepted operation. The current
        # position was reconciled above, so do not submit a second close.
        operation["state"] = "filled"
    else:
        operation["order_id"] = _order_id_from_response(res)
        operation["state"] = "accepted_with_id"
        operation["order_detail"] = order_detail
        _persist_scale_out_state(trackers, save_trackers)
        if callable(query_position):
            observed = None
            try:
                observed = query_position(inst_id, pos_side, pos_venue)
                if isinstance(observed, dict):
                    observed = observed.get("pos", observed.get("size"))
                fill_state, delta, fill_detail = reconcile_scale_out_position_delta(
                    pos_sz, observed, close_sz)
            except Exception as exc:
                fill_state, delta, fill_detail = SCALE_OUT_UNKNOWN, 0.0, str(exc)
            operation["last_observed_size"] = observed
            operation["last_reconcile"] = fill_detail
            operation["observed_close"] = delta
            if fill_state not in {SCALE_OUT_FILLED, SCALE_OUT_PARTIAL}:
                operation["state"] = fill_state
                _persist_scale_out_state(trackers, save_trackers)
                executed_actions.append(f"[{name}] ⚠️ 平仓已受理但{fill_detail}，等待恢复核验")
                return False, "平仓已受理，等待实际减仓核验"
            operation["state"] = fill_state
            close_sz = float(delta)
            remaining_sz = max(0.0, round(float(observed), prec))
        # Isolated callers without a position reader retain the durable claim;
        # production wiring supplies query_position and therefore never relies
        # on acceptance as a fill proof.

    # 5/6. First attach and verify protection for the remaining position,
    # then remove only old ASTRA-owned legs.  A failed replacement must never
    # permit a second partial close on the next cycle.
    breakeven_cushion = 0.0025 * entry_px
    breakeven_sl = round((entry_px + breakeven_cushion) if is_long else (entry_px - breakeven_cushion), px_prec)
    take_profit_px = float(t.get("takeProfitPx", 0.0) or 0.0)
    protection_ok = True
    protection_detail = ""
    replacement_identities: List[Dict[str, Any]] = []
    try:
        if not snapshot_verified:
            raise RuntimeError(protection_snapshot_detail or
                               "旧保护快照不完整，无法证明新挂保护身份与旧腿不同")
        if pos_venue == "okx":
            if not ensure_cloud_position_protection or take_profit_px <= 0:
                raise RuntimeError("缺少余仓云端保护核验能力或有效 TP 价")
            protection_result = ensure_cloud_position_protection(
                inst_id, pos_side, remaining_sz, take_profit_px, breakeven_sl
            )
            if isinstance(protection_result, tuple):
                if not bool(protection_result[0]):
                    raise RuntimeError(str(protection_result[1] if len(protection_result) > 1 else "OCO ensure 未成功"))
            elif not bool(protection_result):
                raise RuntimeError("OCO ensure 未明确成功")
        elif pos_venue in ("binance", "gate") and venue_registry:
            adapter = venue_registry.get_adapter(pos_venue)
            if not adapter or not callable(getattr(adapter, "attach_protective_orders", None)):
                raise RuntimeError("适配器缺少 attach_protective_orders")
            attached = adapter.attach_protective_orders(
                name, pos_side, tp_px=take_profit_px if take_profit_px > 0 else None,
                sl_px=breakeven_sl, contracts=remaining_sz
            )
            if attached is None or attached is False:
                raise RuntimeError("新保护腿没有明确成功回执")
        else:
            raise RuntimeError(f"{pos_venue.upper()} 缺少保护单适配器")

        replacement_rows = _list_live_protection(
            pos_venue, inst_id, name, okx_rest=okx_rest,
            venue_registry=venue_registry)
        verified, verify_detail, replacement_identities = _verify_replacement_protection(
            replacement_rows, venue=pos_venue, inst_id=inst_id, pos_side=pos_side,
            remaining_size=remaining_sz, sl_px=breakeven_sl,
            tp_px=take_profit_px if take_profit_px > 0 else None,
            old_identities=list(old_protection_identities or []))
        if not verified:
            raise RuntimeError(verify_detail)
    except Exception as oco_exc:
        protection_ok = False
        protection_detail = str(oco_exc)

    if not protection_ok:
        t["currentSz"] = remaining_sz
        t["scale_out_recovery_required"] = True
        if operation:
            operation["state"] = "filled_protection_pending"
            operation["last_error"] = protection_detail
        _persist_scale_out_state(trackers, save_trackers)
        executed_actions.append(
            f"[{name}] ⚠️ 分批平仓已成交，但余仓保护替换失败，旧保护保留，禁止重复平仓: {protection_detail}")
        return False, "余仓保护替换失败，已进入恢复状态"

    cleanup_ok = True
    cleanup_detail = ""
    cleanup_identities = list(old_protection_identities or [])
    try:
        if not snapshot_verified:
            raise RuntimeError(protection_snapshot_detail or "旧保护腿快照不完整，拒绝猜测撤单")
        live_before_cleanup = _list_live_protection(
            pos_venue, inst_id, name, okx_rest=okx_rest,
            venue_registry=venue_registry)
        if not isinstance(live_before_cleanup, (list, tuple)):
            raise RuntimeError("清理前保护单回读结构异常")

        if legacy_protection_tokens:
            upgraded, unresolved_tokens, upgrade_detail = _resolve_legacy_protection_tokens(
                legacy_protection_tokens, live_before_cleanup, venue=pos_venue,
                inst_id=inst_id, pos_side=pos_side)
            if unresolved_tokens:
                raise RuntimeError(upgrade_detail)
            cleanup_identities.extend(upgraded)
            # Upgrade the durable record before any cancellation so a restart
            # will not have to guess these identities again.
            if operation:
                operation["old_protection_identities"] = list(cleanup_identities)
                operation["old_protection_ids"] = []
                operation["old_protection_snapshot_verified"] = True
                _persist_scale_out_state(trackers, save_trackers)

        cleanup_keys = {protection_identity_key(value) for value in cleanup_identities}
        cleanup_keys.discard(None)
        if len(cleanup_keys) != len(cleanup_identities):
            raise RuntimeError("旧保护 typed identity 无效或重复")

        # Map old identities to rows by exact namespace and prove exchange-side
        # ASTRA ownership again before invoking a destructive adapter method.
        from scripts.trader.venue_protection import (
            _cancel_response_is_explicit_success, _ownership_evidence,
            cancel_protective_leg,
        )
        rows_by_key: Dict[tuple, List[Tuple[int, Dict[str, Any], Dict[str, Any]]]] = {}
        for row_index, row in enumerate(live_before_cleanup):
            if not isinstance(row, dict):
                raise RuntimeError("清理前保护单列表包含非对象行")
            for identity in extract_protection_identities(row, venue=pos_venue):
                key = protection_identity_key(identity)
                if key is not None:
                    rows_by_key.setdefault(key, []).append((row_index, row, identity))

        row_groups: Dict[int, List[Dict[str, Any]]] = {}
        for identity in cleanup_identities:
            key = protection_identity_key(identity)
            matches = rows_by_key.get(key, [])
            if not matches:
                continue  # Exact typed key is absent already; no DELETE needed.
            if len(matches) != 1:
                raise RuntimeError("旧保护 typed identity 匹配到多个 live 行，保守保留")
            row_index, row, live_identity = matches[0]
            if _ownership_evidence(row) != "tag":
                raise RuntimeError("旧保护 identity 命中无 ASTRA ownership proof 的 live 行，保守保留")
            row_groups.setdefault(row_index, []).append(live_identity)

        for _row_index, identities in row_groups.items():
            if pos_venue == "gate":
                identity = next((x for x in identities if x["identity_kind"] == "id_string"), None)
                identity = identity or next((x for x in identities if x["identity_kind"] == "id"), None)
            elif pos_venue == "binance":
                identity = next((x for x in identities if x["identity_kind"] == "algo_id"), None)
                identity = identity or next((x for x in identities if x["identity_kind"] == "client_algo_id"), None)
            elif pos_venue == "okx":
                identity = next((x for x in identities if x["identity_kind"] == "algo_id"), None)
                identity = identity or next((x for x in identities if x["identity_kind"] == "algo_cl_ord_id"), None)
            else:
                identity = None
            if identity is None:
                raise RuntimeError("旧保护腿没有可路由的 typed identity")
            if pos_venue == "okx":
                item = {"instId": inst_id}
                if identity["identity_kind"] == "algo_id":
                    item["algoId"] = identity["identity_value"]
                else:
                    item["algoClOrdId"] = identity["identity_value"]
                response = okx_rest.cancel_algo_orders([item], inst_id=inst_id)
                if isinstance(response, list):
                    if not response or any(str(row.get("sCode") or "") != "0"
                                           for row in response if isinstance(row, dict)):
                        raise RuntimeError(f"OKX 撤旧回执未明确成功: {response}")
                elif not _cancel_response_is_explicit_success(response):
                    raise RuntimeError(f"OKX 撤旧回执未明确成功: {response}")
            elif not cancel_protective_leg(
                    venue_registry.get_adapter(pos_venue), identity["identity_value"],
                    identity=identity, venue=pos_venue):
                raise RuntimeError("适配器未明确确认 typed identity 撤销成功")

        live_after_cleanup = _list_live_protection(
            pos_venue, inst_id, name, okx_rest=okx_rest,
            venue_registry=venue_registry)
        if not isinstance(live_after_cleanup, (list, tuple)):
            raise RuntimeError("撤旧后保护单回读结构异常")
        remaining_keys = {
            key
            for row in live_after_cleanup
            for identity in extract_protection_identities(row, venue=pos_venue)
            for key in [protection_identity_key(identity)]
            if key is not None
        }
        still_present = cleanup_keys & remaining_keys
        if still_present:
            raise RuntimeError(f"撤旧后仍有 {len(still_present)} 个旧 typed identity 存在")
    except Exception as cxl_exc:
        cleanup_ok = False
        cleanup_detail = str(cxl_exc)

    if not cleanup_ok:
        # New protection stays active.  Mark recovery instead of pretending the
        # scale-out lifecycle is complete; the guard above prevents another close.
        t["currentSz"] = remaining_sz
        t["scale_out_recovery_required"] = True
        if operation:
            operation["state"] = "cleanup_pending"
            operation["last_error"] = cleanup_detail
        _persist_scale_out_state(trackers, save_trackers)
        executed_actions.append(
            f"[{name}] ⚠️ 新保护已挂但旧保护清理未核验完成，保留新腿并禁止重复平仓: {cleanup_detail}")
        return False, "旧保护清理未完成，已进入恢复状态"

    # 7. 更新本地状态机与账本
    t["scale_out_phase"] = 1
    t.pop("scale_out_recovery_required", None)
    t["scale_out_tp"] = None
    t["currentSz"] = remaining_sz
    t["trailingStopPx"] = breakeven_sl
    t["scale_count"] = 999  # 永久互斥锁定金字塔加仓
    if operation:
        operation["state"] = "accounting_pending"
        operation["accounting_operation_id"] = operation.get("operation_id")
    t["stage_desc"] = (f"已分批止盈50% (余仓保证金 ~{_margin_of(remaining_sz):.2f}U"
                       f" · 保本止损 {breakeven_sl})")

    # 8. 记录平仓台账与通知
    realized_pnl = close_sz * ct_val * (cur_px - entry_px if is_long else entry_px - cur_px)
    fee_val = close_fee(close_sz, ct_val, cur_px, TAKER_FEE_RATE) if close_fee else (close_sz * ct_val * cur_px * TAKER_FEE_RATE)

    msg_action = (f"[{name}] 🎯 达到首批止盈门槛(+{trigger_threshold:.2f})，已市价平仓 "
                  f"{ratio*100:.0f}% (保证金 {_margin_of(close_sz):.2f}U)，锁定盈利 "
                  f"+{realized_pnl:.2f}U；余仓保证金 ~{_margin_of(remaining_sz):.2f}U "
                  f"推进至保本位 {breakeven_sl}")
    executed_actions.append(msg_action)

    if record_trade and close_trade_payload:
        _trade_row = close_trade_payload(
            is_long=is_long,
            timestamp_full=timestamp_full,
            name=name,
            action_type="分批止盈",
            side_suffix="首批平仓50%",
            pos_sz=close_sz,
            cur_px=cur_px,
            fee=fee_val,
            pnl=realized_pnl,
            remark=f"浮盈达到 {trigger_threshold:.2f} 触发首批止盈，锁定现金利润，余仓移损保本",
        )
        if isinstance(_trade_row, dict) and operation:
            _trade_row["operation_id"] = operation.get("operation_id")
        record_trade(_trade_row)

    if operation:
        operation["state"] = "accounted"
        operation["accounting_recorded"] = True
    t.pop("scale_out_operation", None)
    _persist_scale_out_state(trackers, save_trackers)

    if notify_trade_close:
        try:
            notify_trade_close(
                inst=name,
                pnl=realized_pnl,
                stage="首批分批止盈50%",
                exit_px=cur_px,
                side="多" if is_long else "空",
                entry_px=entry_px,
                fee=fee_val,
                venue=pos_venue,
                is_partial=True,
            )
        except Exception:
            pass

    return True, "首批分批平仓成功"
