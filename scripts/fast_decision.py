"""One bounded Fast Decision worker invocation.

GatewayScheduler starts this as an isolated subprocess. It exits after one
request/action; there is no resident loop and it cannot delay the trader job.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from astra_backend.fast_decision_service import evaluate, execute_protective_decision
from astra_backend.fast_decision_store import load_config, record_latency_sample
from astra_backend.llm_manager import validate_model_runtime

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LAST_FILE = DATA / "fast_decision_last.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _snapshot(event: dict[str, Any] | None = None) -> dict[str, Any]:
    factor = _read_json(DATA / "factor_library_snapshot.json", {})
    try:
        import astra_backend.dashboard_cache as dashboard_cache
        cache = dashboard_cache.CACHE_DATA if isinstance(dashboard_cache.CACHE_DATA, dict) else {}
    except Exception:
        cache = {}
    return {
        "factors": factor,
        "market": factor.get("instruments", []) if isinstance(factor, dict) else [],
        "account": cache.get("account") or {},
        "positions": cache.get("positions") or [],
        "pending_orders": cache.get("pending_orders") or [],
        "risk": cache.get("risk") or {},
        "protection": cache.get("protection") or {},
        "stale_after": 60,
        # A market event timestamp is supplied only by the resident stream bridge;
        # factor timestamp remains snapshot age telemetry, not event time.
        "market_event_timestamp": event.get("exchange_timestamp") if isinstance(event, dict) else None,
        "market_event": event if isinstance(event, dict) else {},
        "factor_snapshot_at": factor.get("timestamp") if isinstance(factor, dict) else None,
    }


def _execution_timestamps(execution: Any) -> tuple[float | None, float | None]:
    """Return the observed execution start and latest order-submit timestamps."""
    rows = execution if isinstance(execution, list) else [execution]
    starts: list[float] = []
    submits: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value, target in ((row.get("execution_started_at"), starts), (row.get("execution_sent_at"), submits)):
            try:
                if value is not None:
                    target.append(float(value))
            except (TypeError, ValueError):
                continue
    return (min(starts) if starts else None, max(submits) if submits else None)


def _write(value: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = LAST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LAST_FILE)


def run_once() -> dict[str, Any]:
    trigger_at = time.time()
    submitted_at = float(os.environ.get("ASTRA_FAST_DECISION_SUBMITTED_AT") or trigger_at)
    scheduler_queue_ms = max(0.0, (trigger_at - submitted_at) * 1000)
    cfg = load_config()
    if not cfg.get("enabled"):
        result = {"ok": False, "action": "HOLD", "failure_safe": True, "error": "fast_decision_disabled"}
        _write(result)
        return result
    try:
        runtime = validate_model_runtime(str(cfg.get("model_id") or ""), required_capability="structured_decision")
    except Exception as exc:
        result = {"ok": False, "action": "HOLD", "failure_safe": True, "error": f"runtime_invalid: {exc}"}
        _write(result)
        return result
    runtime.update({"timeout_seconds": cfg.get("timeout_seconds"), "decision_ttl_seconds": cfg.get("decision_ttl_seconds")})
    event_driven = os.environ.get("ASTRA_FAST_DECISION_EVENT_DRIVEN") == "1"
    event = _read_json(Path(os.environ.get("ASTRA_FAST_DECISION_EVENT_FILE", "")), {}) if event_driven else {}
    snapshot = _snapshot(event if isinstance(event, dict) else None)
    snapshot_built_at = time.time()
    market = snapshot.get("market")
    if isinstance(market, dict) and snapshot.get("market_event_timestamp") is None:
        snapshot["factor_snapshot_at"] = market.get("updated_at_epoch") or snapshot.get("factor_snapshot_at")
    result = evaluate(snapshot, runtime)
    result.setdefault("latency", {})["scheduler_queue_ms"] = round(scheduler_queue_ms, 3)
    result["latency"]["trigger_to_result_ms"] = round(max(0.0, (time.time() - trigger_at) * 1000), 3)
    result.setdefault("timestamps", {})["snapshot_built_timestamp"] = snapshot_built_at
    if result.get("ok") and result.get("action") != "HOLD":
        # Execution callbacks are imported only after the typed judgment succeeds.
        # They are existing venue-aware paths; this worker has no exchange API of its own.
        try:
            import scripts.ai_factor_trader as trader
            from astra_backend import execution_router
            frozen = trader.freeze_okx_environment()
            try:
                positions = snapshot.get("positions") or []
                if result.get("action") == "DISABLE_NEW_ENTRY":
                    result["execution"] = execute_protective_decision(
                        result["decision"], {}, snapshot=snapshot,
                        ai_tightens_stop=trader.ai_tightens_stop,
                        close_position_confirmed=trader.close_position_confirmed,
                        amend_venue_stop_loss=trader.amend_venue_stop_loss,
                        reduce_only_position=lambda inst, quantity, venue, pos_side: execution_router.reduce_only_position(
                            inst,
                            quantity=quantity,
                            venue=venue,
                            pos_side=pos_side,
                            environment=str(trader.current_environment().mode),
                        ),
                        venue_registry=trader.venue_registry,
                        current_environment=trader.current_environment,
                        re_read_position=lambda *_args: None,
                        dedup_seconds=float(cfg.get("action_dedup_seconds") or 60),
                        ttl_seconds=float(cfg.get("decision_ttl_seconds") or 45),
                    )
                else:
                    executions = []
                    for position in positions:
                        if not isinstance(position, dict):
                            continue
                        candidate = dict(snapshot)
                        candidate["tighten_stop_price"] = position.get("suggested_sl_price") or position.get("trailingStopPx")
                        def reread(inst_id: str, pos_side: str, venue: str, _trader=trader):
                            venue_name = str(venue).lower()
                            if venue_name == "okx":
                                ok, rows, _ = _trader.query_positions()
                                if not ok:
                                    return None
                                for row in rows:
                                    if str(row.get("instId")) != inst_id:
                                        continue
                                    row_side = str(row.get("posSide") or "net").lower()
                                    if pos_side not in {"", "net"} and row_side not in {pos_side, "net"}:
                                        continue
                                    if abs(float(row.get("pos", 0) or 0)) <= 0:
                                        continue
                                    return row
                                return None

                            # Re-read non-OKX positions through the existing venue-aware
                            # snapshot path; never fall back to the worker's stale input.
                            try:
                                env = _trader.current_environment()
                                ok, grouped, _ = _trader.fetch_other_venue_positions(str(env.mode))
                                if not ok:
                                    return None
                                base = str(inst_id).split("-")[0].upper()
                                for row in grouped.get(venue_name, []):
                                    if str(row.get("base") or "").upper() != base:
                                        continue
                                    size = float(row.get("size_signed") or 0)
                                    if abs(size) <= 0:
                                        continue
                                    row_side = "long" if size > 0 else "short"
                                    if pos_side not in {"", "net"} and row_side != pos_side:
                                        continue
                                    return {
                                        "instId": inst_id,
                                        "posSide": row_side,
                                        "pos": abs(size),
                                        "size_signed": size,
                                        "venue": venue_name,
                                    }
                            except Exception:
                                return None
                            return None
                        executions.append(execute_protective_decision(
                            result["decision"], position, snapshot=candidate,
                            ai_tightens_stop=trader.ai_tightens_stop,
                            close_position_confirmed=trader.close_position_confirmed,
                            amend_venue_stop_loss=trader.amend_venue_stop_loss,
                            reduce_only_position=lambda inst, quantity, venue, pos_side: execution_router.reduce_only_position(
                                inst,
                                quantity=quantity,
                                venue=venue,
                                pos_side=pos_side,
                                environment=str(trader.current_environment().mode),
                            ),
                            venue_registry=trader.venue_registry,
                            current_environment=trader.current_environment,
                            re_read_position=reread,
                            dedup_seconds=float(cfg.get("action_dedup_seconds") or 60),
                            ttl_seconds=float(cfg.get("decision_ttl_seconds") or 45),
                        ))
                    result["execution"] = executions
            finally:
                trader.unfreeze_okx_environment()
        except Exception as exc:
            result["execution"] = {"ok": False, "failure_safe": True, "error": str(exc)}
    result["finished_at"] = time.time()
    execution = result.get("execution")
    execution_started_at, order_submitted_at = _execution_timestamps(execution)
    timestamps = result.setdefault("timestamps", {})
    timestamps["execution_start_timestamp"] = execution_started_at
    timestamps["order_submitted_timestamp"] = order_submitted_at
    sent_at = order_submitted_at
    latency = result.setdefault("latency", {})
    if sent_at:
        latency["trigger_to_execution_submit_ms"] = round(
            max(0.0, (float(sent_at) - trigger_at) * 1000), 3
        )
    else:
        latency["trigger_to_execution_submit_ms"] = None
    factor = _read_json(DATA / "factor_library_snapshot.json", {})
    try:
        snapshot_at = float(factor.get("timestamp") or trigger_at) if isinstance(factor, dict) else trigger_at
    except (TypeError, ValueError):
        snapshot_at = trigger_at
    latency["snapshot_age_at_trigger_ms"] = round(max(0.0, (trigger_at - snapshot_at) * 1000), 3)
    latency["scheduler_queue_ms"] = round(scheduler_queue_ms, 3)
    event_timestamp = timestamps.get("market_event_timestamp")
    event_sample = {
        "scheduler_queue_ms": scheduler_queue_ms,
        "snapshot_age_at_trigger_ms": latency["snapshot_age_at_trigger_ms"],
        "trigger_to_execution_submit_ms": latency["trigger_to_execution_submit_ms"],
    }
    if event_timestamp is not None:
        try:
            event_ts = float(event_timestamp)
            if sent_at:
                event_sample["event_to_execution_ms"] = max(0.0, (float(sent_at) - event_ts) * 1000)
            policy_ts = timestamps.get("policy_timestamp")
            if policy_ts is not None:
                event_sample["event_to_policy_ms"] = max(0.0, (float(policy_ts) - event_ts) * 1000)
        except (TypeError, ValueError):
            pass
    record_latency_sample(event_sample)
    _write(result)
    return result


if __name__ == "__main__":
    print(json.dumps(run_once(), ensure_ascii=False))
