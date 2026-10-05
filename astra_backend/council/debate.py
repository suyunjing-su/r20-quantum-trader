"""委员会辩论引擎：单席位调用、互评、提示词渲染与整场辩论编排。

**薄壳 + 核心**：会读门面常量的兄弟函数（load_council_config / resolve_seat_model）
与**被测试直接 patch 的** _call_single_trader(_critique) 均由门面在调用时解析后注入，
核心只调用注入的可调用对象 —— 故 patch 依然生效，且本模块不 import council_manager。
结构优化阶段 2（B5）。
"""
from __future__ import annotations

import concurrent.futures
import inspect
import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from astra_backend.council.role_normalizer import (
    process_brain_output as _normalize_cio_adopted_roles,
)
from astra_backend.council.policy import (
    DEFAULT_CONSENSUS_MODE,
    CIO_MIN_ARBITRATION_TIME,
    DEFAULT_PRESET_TEMPLATES,
    MIN_SAFE_REASONING_TIME,
    VALID_CONSENSUS_MODES,
)


_CYCLE_STATE_LOCK = threading.Lock()
_ACTIVE_CYCLE: Optional[Tuple[str, threading.Event]] = None
_LAST_COMPLETED_CYCLE_TOKEN: Optional[str] = None


def _begin_council_cycle(external_event=None) -> Tuple[str, threading.Event]:
    global _ACTIVE_CYCLE, _LAST_COMPLETED_CYCLE_TOKEN
    token = uuid.uuid4().hex
    event = external_event or threading.Event()
    with _CYCLE_STATE_LOCK:
        previous = _ACTIVE_CYCLE
        if previous is not None:
            previous[1].set()
        _LAST_COMPLETED_CYCLE_TOKEN = None
        _ACTIVE_CYCLE = (token, event)
    return token, event


def _is_active_council_cycle(token: str, event: threading.Event) -> bool:
    with _CYCLE_STATE_LOCK:
        return _ACTIVE_CYCLE == (token, event) and not event.is_set()


def is_council_cycle_current(cycle_token: Optional[str]) -> bool:
    """Validate the last committed Council generation before persistence."""
    if not cycle_token:
        return False
    with _CYCLE_STATE_LOCK:
        return cycle_token == _LAST_COMPLETED_CYCLE_TOKEN


def _finish_council_cycle(token: str, event: threading.Event) -> None:
    global _ACTIVE_CYCLE
    event.set()
    with _CYCLE_STATE_LOCK:
        if _ACTIVE_CYCLE == (token, event):
            _ACTIVE_CYCLE = None


def _supports_keyword(target: Callable[..., Any], keyword: str) -> bool:
    """Return whether a legacy callback can accept an optional keyword."""
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _deadline_task_kwargs(target: Callable[..., Any], deadline: float, cancellation_event=None) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"deadline": deadline}
    if cancellation_event is not None and _supports_keyword(target, "cancellation_event"):
        kwargs["cancellation_event"] = cancellation_event
    return kwargs


def _bounded_parallel_calls(
    tasks: list[tuple[str, tuple[Any, ...], Dict[str, Any]]],
    *,
    deadline: float,
    max_workers: int,
    stagger_seconds: float = 0.8,
    cancellation_event=None,
) -> tuple[Dict[str, Any], set[str], Dict[str, Dict[str, Any]]]:
    """Run bounded calls without waiting past the absolute Council deadline.

    The third return value is raw task timing telemetry.  A worker records its
    own completion timestamp so a late sibling cannot cause an on-time result
    to be discarded (and so the Council can report real, not inferred, timing).
    """
    if not tasks:
        return {}, set(), {}
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(int(max_workers), len(tasks))),
        thread_name_prefix="astra-council",
    )
    future_keys: Dict[concurrent.futures.Future, str] = {}
    results: Dict[str, Any] = {}
    task_metrics: Dict[str, Dict[str, Any]] = {}
    task_started_at: Dict[str, float] = {}
    task_start_lock = threading.Lock()
    unfinished = {key for key, _args, _kwargs in tasks}
    def _invoke(task_key: str, call_args: tuple[Any, ...], call_kwargs: Dict[str, Any]):
        target, *positional = call_args
        started_at = time.time()
        with task_start_lock:
            task_started_at[task_key] = started_at
        try:
            if cancellation_event is not None and cancellation_event.is_set():
                return started_at, time.time(), True, TimeoutError("Council cycle cancelled before callback start")
            payload = target(*positional, **call_kwargs)
            return started_at, time.time(), False, payload
        except Exception as exc:
            return started_at, time.time(), True, exc

    try:
        for index, (key, args, kwargs) in enumerate(tasks):
            if cancellation_event is not None and cancellation_event.is_set():
                break
            if index and stagger_seconds > 0:
                remaining = deadline - time.time()
                if remaining <= 0:
                    if cancellation_event is not None:
                        cancellation_event.set()
                    break
                time.sleep(min(stagger_seconds, remaining))
                if deadline - time.time() <= 0:
                    if cancellation_event is not None:
                        cancellation_event.set()
                    break
            future_keys[pool.submit(_invoke, key, args, kwargs)] = key
        remaining = max(0.0, deadline - time.time())
        done, _pending = concurrent.futures.wait(
            future_keys, timeout=remaining,
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        if cancellation_event is not None and time.time() >= deadline:
            cancellation_event.set()
        for future in done:
            key = future_keys[future]
            started_at, finished_at, failed, payload = future.result()
            late = finished_at > deadline
            task_metrics[key] = {
                "started_at": started_at,
                "ended_at": finished_at,
                "latency_ms": max(0, int(round((finished_at - started_at) * 1000))),
                "status": "timeout" if late else ("error" if failed else "ok"),
            }
            if late:
                continue
            unfinished.discard(key)
            results[key] = payload
        timed_out_at = time.time()
        for key in unfinished:
            started_at = task_started_at.get(key)
            task_metrics[key] = {
                "started_at": started_at,
                "ended_at": min(timed_out_at, deadline),
                "latency_ms": (
                    max(0, int(round((min(timed_out_at, deadline) - started_at) * 1000)))
                    if started_at is not None else 0
                ),
                "status": "timeout" if started_at is not None else "not_started",
            }
        return results, unfinished, task_metrics
    finally:
        if cancellation_event is not None and time.time() >= deadline:
            cancellation_event.set()
        for future in future_keys:
            if not future.done():
                future.cancel()
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)


def _max_council_concurrency(config: Dict[str, Any], task_count: int) -> int:
    """Resolve one bounded provider-safe concurrency budget for this cycle."""
    raw = config.get("max_concurrency") or os.getenv("ASTRA_COUNCIL_MAX_CONCURRENCY", "16")
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = 16
    # Keep a bounded executor, but do not impose the old four-worker ceiling on
    # multi-group fan-out. Provider/model gates remain the authoritative safety
    # limit for actual HTTP concurrency.
    return max(1, min(limit, 16, max(1, task_count)))


def _timeout_trader_result(key: str, role_spec: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "proposal_id": f"{key}_prop",
        "role_id": key,
        "role_name": role_spec.get("name", key),
        "status": "timeout" if "timeout" in reason.lower() else "error",
        "content": reason,
        "reasoning": "",
        "latency_ms": 0,
        "weight": 0.0,
    }


def _render_seat_prompt(prompt: str, runtime_context: Optional[Dict[str, Any]]) -> str:
    """席位提示词变量渲染（审计 P1-4d）。

    旧实现把席位提示词原样塞进 system prompt，线上 4 个席位都带 `{{macro_4h}}` 之类
    占位符——模型看到的是花括号字面量（render_variables 在委员会全文出现 0 次）。
    现在走 prompt_library 的同一渲染器：context=None 保持模板原样（预览语义），
    有 context 时未知变量标 [UNKNOWN_VARIABLE:x]、缺值标 [MISSING_CONTEXT:x]，绝不静默吞。
    """
    if not prompt:
        return ""
    try:
        from prompt_library import render_variables
    except Exception:
        try:
            from scripts.prompt_library import render_variables
        except Exception:
            return prompt
    if runtime_context is None:
        return prompt
    return render_variables(prompt, runtime_context)


def _call_single_trader(resolve_seat: Callable[..., Any],
    role_id: str,
    role_spec: Dict[str, Any],
    market_prompt: str,
    master_constitutional_rules: str,
    timeout: float = 20.0,
    runtime_context: Optional[Dict[str, Any]] = None,
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Dict[str, Any]:
    """Invokes a senior trader role to pitch their complete trade proposal and account review."""
    from astra_backend.llm_manager import execute_llm_request, get_active_llm_runtime, load_llm_config

    # 审计 P1-4b：未登记模型不再静默回落（resolved 里带 registered/fallback 事实）
    resolved = resolve_seat(role_spec)
    override_model = resolved["model"] or None
    override_url = resolved["base_url"]
    override_key = resolved["api_key"]
    override_format = resolved["api_format"]
    override_effort = resolved["effort"]
    temperature = float(role_spec.get("temperature", 0.2))

    prompt_content = _render_seat_prompt(str(role_spec.get("prompt") or ""), runtime_context)
    role_name = role_spec.get("name", role_id)
    proposal_id = f"{role_id}_prop"

    trader_system_prompt = (
        f"【最高交易宪法与策略纪律】\n"
        f"{master_constitutional_rules}\n\n"
        f"====================================================\n"
        f"【你的交易员身份与操盘职责】\n"
        f"{prompt_content}\n"
        f"注意：你作为专业交易员，必须在上述【最高交易宪法】框架内提交实战作战提案（提案标识: {proposal_id}），"
        f"重点覆盖【账户可用余额】、【在途持仓动态处理】、【在途未成交挂单撤留】与【新标的点位规划】！"
    )

    trader_user_prompt = (
        f"【当前全景市场数据、账户资金与在途持仓挂单】\n"
        f"{market_prompt}\n\n"
        f"请以你「{role_name}」（提案标识: {proposal_id}）的专业视角，向首席投资官 (CIO) 提交本轮实操审查与作战方案：\n"
        f"1. 账户持仓与挂单审查：\n"
        f"   - 对在途持仓逐一给出管理建议：HOLD（波段完好继续持有）、CLOSE_MARKET（结构破位斩仓）或 UPDATE_SL（浮盈锁定移动止损）；\n"
        f"   - 对在途未成交限价挂单逐一给出建议：CANCEL（偏离盘口或动能失效立即撤单）或 KEEP（继续保留）；\n"
        f"2. 标的池全标的新开/加仓作战提案（以行情矩阵清单为准，逐标的）：\n"
        f"   - 针对各标的输出明确方案：倾向（BUY_LONG / SELL_SHORT / WAIT）、入场限价、1.8~2.2x 1H ATR 分层止损、≥2.0R 止盈、拟投入保证金与置信度；\n"
        f"3. 质询与风控：简要指出其他交易员方案可能带来的资金过载或流动性风险（60字内/标的）。\n\n"
        f"【提案输出格式（强制）】正文分析之后，必须以标准报价单块收尾（每标的一行，无明确结论的标的也必须列 WAIT 行），供 CIO 与执行层逐项横向对比：\n"
        f"标的 | 倾向 | 限价 | 止损 | 止盈 | 拟用保证金(USDT) | 置信度(0-100) | 一句话依据\n"
        f"示例：BTC-USDT-SWAP | WAIT | - | - | - | - | 55 | 箱体中段乱跳，无概率优势"
    )

    messages = [
        {"role": "system", "content": trader_system_prompt},
        {"role": "user", "content": trader_user_prompt},
    ]

    try:
        content, reasoning, usage, latency = execute_llm_request(
            messages=messages,
            model=override_model,
            base_url=override_url,
            api_key=override_key,
            api_format=override_format,
            reasoning_effort=override_effort,
            temperature=temperature,
            timeout=timeout,
            max_tokens=resolved.get("max_tokens"),
            allow_fallback=False,  # 委员会成员优先以其登记模型作答
            deadline=deadline,
            **({"cancellation_event": cancellation_event} if cancellation_event is not None else {}),
        )
        return {
            "proposal_id": proposal_id,
            "role_id": role_id,
            "role_name": role_name,
            "model_used": override_model or get_active_llm_runtime().get("model", "default"),
            "model_requested": resolved["requested"],
            "model_registered": resolved["registered"],
            "model_fallback": resolved["fallback"],
            "model_note": resolved["reason"],
            "status": "ok",
            "content": content.strip(),
            "reasoning": reasoning.strip() if reasoning else "",
            "latency_ms": latency,
            "weight": role_spec.get("weight", 1.0),
        }
    except Exception as e:
        # 遇网关并发 504/502 超时，退避 1.5s 后自适应降低强度并重试一次，保住席位
        err_str = str(e)
        if ("504" in err_str or "502" in err_str or "timeout" in err_str.lower()) and timeout > 35.0:
            try:
                if deadline is not None:
                    remaining = float(deadline) - time.time()
                    if remaining <= MIN_SAFE_REASONING_TIME:
                        raise TimeoutError(
                            f"Trader retry deadline exhausted: {remaining:.2f}s remaining"
                        )
                time.sleep(1.5)
                if deadline is not None and float(deadline) - time.time() <= 0:
                    raise TimeoutError("Trader retry deadline exhausted after backoff")
                retry_effort = override_effort
                c_retry, r_retry, _, lat_retry = execute_llm_request(
                    messages=messages,
                    model=override_model,
                    base_url=override_url,
                    api_key=override_key,
                    api_format=override_format,
                    reasoning_effort=retry_effort,
                    temperature=temperature,
                    timeout=max(20.0, timeout - 20.0),
                    max_tokens=resolved.get("max_tokens"),
                    allow_fallback=True,
                    deadline=deadline,
                    **({"cancellation_event": cancellation_event} if cancellation_event is not None else {}),
                )
                return {
                    "proposal_id": proposal_id,
                    "role_id": role_id,
                    "role_name": role_name,
                    "model_used": override_model or get_active_llm_runtime().get("model", "default"),
                    "model_requested": resolved["requested"],
                    "model_registered": resolved["registered"],
                    "model_fallback": resolved["fallback"],
                    "model_note": resolved["reason"],
                    "status": "ok",
                    "content": c_retry.strip(),
                    "reasoning": r_retry.strip() if r_retry else "",
                    "latency_ms": lat_retry,
                    "weight": role_spec.get("weight", 1.0),
                }
            except Exception as retry_exc:
                e = retry_exc

        return {
            "proposal_id": proposal_id,
            "role_id": role_id,
            "role_name": role_name,
            "model_used": override_model or "unknown",
            "model_requested": resolved["requested"],
            "model_registered": resolved["registered"],
            "model_fallback": resolved["fallback"],
            "model_note": resolved["reason"],
            "status": "error",
            "content": f"交易员方案提交异常/超时降级: {e}",
            "reasoning": "",
            "latency_ms": 0,
            "weight": 0.0,
        }


def _call_single_trader_critique(resolve_seat: Callable[..., Any],
    role_id: str,
    role_spec: Dict[str, Any],
    my_proposal: str,
    peer_proposals: str,
    master_constitutional_rules: str,
    timeout: float = 15.0,
    runtime_context: Optional[Dict[str, Any]] = None,
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Dict[str, Any]:
    """Invokes a senior trader role to cross-examine peer proposals for hidden risks, timing, or sizing flaws."""
    from astra_backend.llm_manager import execute_llm_request, get_active_llm_runtime, load_llm_config

    # 审计 P1-4b：未登记模型不再静默回落（resolved 里带 registered/fallback 事实）
    resolved = resolve_seat(role_spec)
    override_model = resolved["model"] or None
    override_url = resolved["base_url"]
    override_key = resolved["api_key"]
    override_format = resolved["api_format"]
    override_effort = resolved["effort"]
    temperature = float(role_spec.get("temperature", 0.2))

    prompt_content = _render_seat_prompt(str(role_spec.get("prompt") or ""), runtime_context)
    role_name = role_spec.get("name", role_id)

    critique_system_prompt = (
        f"【最高交易宪法与策略纪律】\n"
        f"{master_constitutional_rules}\n\n"
        f"====================================================\n"
        f"【你的交易员身份与操盘职责】\n"
        f"{prompt_content}\n"
        f"注意：你现在进入第二轮「同行方案交叉漏洞质询（Cross-Examination）」。你的职责是站在你的专业立场，严肃审查同行交易员的方案，指出其盲区、追高风险或防插针止损不足！"
    )

    critique_user_prompt = (
        f"【你第一轮提交的作战提案】\n"
        f"{my_proposal}\n\n"
        f"====================================================\n"
        f"【同行交易员提交的第一轮作战提案卷宗】\n"
        f"{peer_proposals}\n\n"
        f"====================================================\n"
        f"请以你「{role_name}」的专业视角，对同行的方案展开针对性质询（Cross-Examination）：\n"
        f"1. 逐一质询同行方案在点位入场（是否追高）、2.0x ATR 止损距离、拟用保证金或假突破风险上的漏洞；\n"
        f"2. 明确论证为何你的方案在当前资金与市场环境下更安全或盈亏比更优；\n"
        f"3. 保持专业精炼，直击漏洞要害。"
    )

    messages = [
        {"role": "system", "content": critique_system_prompt},
        {"role": "user", "content": critique_user_prompt},
    ]

    try:
        content, reasoning, usage, latency = execute_llm_request(
            messages=messages,
            model=override_model,
            base_url=override_url,
            api_key=override_key,
            api_format=override_format,
            reasoning_effort=override_effort,
            temperature=temperature,
            timeout=timeout,
            max_tokens=resolved.get("max_tokens"),
            allow_fallback=False,  # 委员会成员必须以其登记模型作答，保住模型身份；只享重试
            deadline=deadline,
            **({"cancellation_event": cancellation_event} if cancellation_event is not None else {}),
        )
        return {
            "role_id": role_id,
            "role_name": role_name,
            "model_used": override_model or get_active_llm_runtime().get("model", "default"),
            "model_requested": resolved["requested"],
            "model_registered": resolved["registered"],
            "model_fallback": resolved["fallback"],
            "model_note": resolved["reason"],
            "status": "ok",
            "content": content.strip(),
            "reasoning": reasoning.strip() if reasoning else "",
            "latency_ms": latency,
        }
    except Exception as e:
        return {
            "role_id": role_id,
            "role_name": role_name,
            "model_used": override_model or "unknown",
            "status": "error",
            "content": f"质询提交异常/超时降级: {e}",
            "reasoning": "",
            "latency_ms": 0,
        }


def _build_cio_verdict_summary(brain_output: Dict[str, Any]) -> str:
    """Build a short user-facing verdict from the final JSON, not native CoT."""
    parts: List[str] = []
    macro = str(brain_output.get("macro_assessment") or "").strip()
    if macro:
        parts.append(f"宏观：{macro[:180]}")

    decisions = brain_output.get("decisions")
    decision_items = decisions.items() if isinstance(decisions, dict) else enumerate(decisions or [])
    for symbol, decision in list(decision_items)[:8]:
        if not isinstance(decision, dict):
            continue
        action = str(decision.get("action") or "").strip()
        reason = str(decision.get("reasoning") or decision.get("reason") or "").strip()
        if not action and not reason:
            continue
        label = str(symbol)
        line = f"{label}：{action or '已裁决'}"
        if reason:
            line += f"，{reason[:120]}"
        parts.append(line)

    return "\n".join(parts)[:1000]


def _execute_council_debate_impl(load_config: Callable[[], Dict[str, Any]], resolve_seat: Callable[..., Any], call_trader: Callable[..., Any], call_critique: Callable[..., Any],
    market_prompt: str,
    original_system_prompt: str,
    timeout: float = 240.0,
    runtime_context: Optional[Dict[str, Any]] = None,
    group_prompts: Optional[Tuple[Any, ...]] = None,
    cancellation_event=None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Execute Hedge Fund Investment Committee Deliberation:

    Converged Real Modes:
    - "standard": 1-round trader proposals -> compiled docket -> CIO final verdict.
    - "cross_examination": Round 1 proposals -> Round 2 peer cross-examination -> CIO verdict.

    Strict Timeout Control:
    - Anchored on deadline = t_start + timeout.
    - Dynamically evaluates rem = deadline - time.time() before every stage.
    - Safely downgrades or raises TimeoutError if remaining budget < MIN_SAFE_REASONING_TIME (5.0s).

    Structured Contract & Adoption Traceability:
    - Proposals are tagged with proposal_id (e.g. trader_trend_prop).
    - CIO decisions must include adopted_role (e.g. 'trader_trend' or 'REJECT_ALL'/None).
    """
    from astra_backend.llm_manager import execute_llm_request, get_active_llm_runtime, load_llm_config

    # 杠杆契约与后台风控页联动（避免委员会路径被静态"2~5"钉死；2026-09-10 补下限）
    try:
        from risk_constants import MAX_LEVERAGE as _ASTRA_MAX_LEVERAGE, MIN_LEVERAGE as _ASTRA_MIN_LEVERAGE
    except Exception:
        try:
            from scripts.risk_constants import MAX_LEVERAGE as _ASTRA_MAX_LEVERAGE, MIN_LEVERAGE as _ASTRA_MIN_LEVERAGE
        except Exception:
            _ASTRA_MAX_LEVERAGE = 5.0
            _ASTRA_MIN_LEVERAGE = 2.0
    _ASTRA_MAX_LEVERAGE = float(os.getenv("ASTRA_MAX_LEVERAGE", "") or _ASTRA_MAX_LEVERAGE or 5.0)
    _ASTRA_MIN_LEVERAGE = float(os.getenv("ASTRA_MIN_LEVERAGE", "") or _ASTRA_MIN_LEVERAGE or 2.0)
    if _ASTRA_MIN_LEVERAGE > _ASTRA_MAX_LEVERAGE:
        _ASTRA_MIN_LEVERAGE = _ASTRA_MAX_LEVERAGE

    config = load_config()
    roles = config.get("roles", {})
    consensus_mode = str(config.get("consensus_mode", DEFAULT_CONSENSUS_MODE)).strip().lower()
    if consensus_mode not in VALID_CONSENSUS_MODES:
        consensus_mode = DEFAULT_CONSENSUS_MODE

    t_start = time.time()
    effective_timeout = max(1.0, float(timeout))
    # 委员会预算只受调用方/网关任务超时约束，不再人为夹到 420 秒。
    effective_timeout = max(0.0, float(effective_timeout))
    deadline = t_start + effective_timeout
    # Every Council cycle owns one cancellation signal shared by all stages.
    cancellation_event = cancellation_event or threading.Event()

    rem = deadline - time.time()
    if rem < MIN_SAFE_REASONING_TIME:
        cancellation_event.set()
        raise TimeoutError(
            f"Council deliberation timeout: remaining time {rem:.2f}s is below safety threshold {MIN_SAFE_REASONING_TIME}s"
        )

    # Identify CIO (Arbitrator) and Active Traders
    cio_key = next(
        (k for k, r in roles.items() if r.get("is_arbitrator") or k in {"cio", "arbitrator"}),
        "cio",
    )
    cio_spec = roles.get(cio_key, DEFAULT_PRESET_TEMPLATES["cio"])
    trader_keys = [
        k for k in roles.keys()
        if k != cio_key and roles[k].get("enabled", True) is not False
    ]

    trader_proposals: Dict[str, Dict[str, Any]] = {}
    trader_critiques: Dict[str, Dict[str, Any]] = {}
    proposal_task_metrics: Dict[str, Dict[str, Any]] = {}
    critique_task_metrics: Dict[str, Dict[str, Any]] = {}
    proposal_unfinished: set[str] = set()
    critique_unfinished: set[str] = set()
    proposal_stage_started_at: Optional[float] = None
    proposal_stage_ended_at: Optional[float] = None
    critique_stage_started_at: Optional[float] = None
    critique_stage_ended_at: Optional[float] = None
    group_mode = bool(group_prompts)
    if group_mode:
        proposal_groups = []
        for group_pos, group in enumerate(group_prompts or ()):
            group_index = getattr(group, "index", group_pos)
            group_prompt = getattr(group, "prompt", str(group))
            group_symbols = tuple(getattr(group, "symbols", ()) or ())
            proposal_groups.append((group_index, group_prompt, group_symbols))
    else:
        proposal_groups = [(None, market_prompt, ())]
    proposal_order = [
        (f"group_{group_index}:{key}" if group_index is not None else key,
         group_index, key, group_prompt, group_symbols)
        for group_index, group_prompt, group_symbols in proposal_groups
        for key in trader_keys
    ]
    concurrency = _max_council_concurrency(config, len(proposal_order))

    if not trader_keys:
        # Fallback: Solo CIO decision if no active traders enabled
        pass
    elif consensus_mode == "cross_examination":
        # === MODE: Cross-Examination (Double-Round Real Debate) ===
        rem = deadline - time.time()
        if rem < MIN_SAFE_REASONING_TIME * 2.0:
            cancellation_event.set()
            raise TimeoutError(
                f"Council timeout: remaining time {rem:.2f}s insufficient for cross-examination mode (requires >= {MIN_SAFE_REASONING_TIME * 2.0}s)"
            )

        # Stage 1: Round 1 Independent Proposals
        # 首轮提案预算：思考型模型出一次带推理链的提案实测需 60~180s，纯比例切分
        # 在中低总预算下会把它压到 53s 级必死（2026-09-10 05:15 实测），故加 90s 地板。
        # 审计 P2-14：跨审模式同样为 CIO 预留（两轮之后仍要有裁决时间）
        cio_reserve = min(CIO_MIN_ARBITRATION_TIME, max(MIN_SAFE_REASONING_TIME * 2.0, rem * 0.35))
        round1_budget = max(2.0, min(max(rem * 0.55, 90.0), rem - cio_reserve))
        tasks = [
            (composite,
             (call_trader, key, roles[key], group_prompt, original_system_prompt,
              round1_budget, runtime_context),
             _deadline_task_kwargs(call_trader, deadline, cancellation_event))
            for composite, _group_index, key, group_prompt, _symbols in proposal_order
        ]
        proposal_stage_started_at = time.time()
        completed, unfinished, proposal_task_metrics = _bounded_parallel_calls(
            tasks, deadline=deadline, max_workers=concurrency,
            stagger_seconds=0.0 if group_mode else 0.8,
            cancellation_event=cancellation_event)
        proposal_stage_ended_at = time.time()
        proposal_unfinished = set(unfinished)
        for composite, _group_index, key, _group_prompt, _symbols in proposal_order:
            result = completed.get(composite)
            if composite in unfinished:
                trader_proposals[composite] = _timeout_trader_result(
                    key, roles[key], "Trader proposal timeout: Committee deadline exhausted")
            elif isinstance(result, Exception):
                trader_proposals[composite] = _timeout_trader_result(
                    key, roles[key], f"Proposal exception: {result}")
            else:
                trader_proposals[composite] = result or _timeout_trader_result(
                    key, roles[key], "Trader proposal error: empty result")

        # Stage 2: Round 2 Cross-Examination Critiques
        critique_stage_started_at = time.time()
        rem = deadline - time.time()
        if rem < MIN_SAFE_REASONING_TIME + 2.0:
            # Insufficient budget for second round -> safe degradation: skip critiques to preserve CIO verdict
            for composite, group_index, k, _group_prompt, group_symbols in proposal_order:
                trader_critiques[composite] = {
                    "role_id": k,
                    "role_name": roles[k].get("name", k),
                    "status": "skipped",
                    "content": f"时间预算紧缺 (剩余 {rem:.2f}s < 7.0s)，安全降级跳过交叉质询以确保 CIO 终审",
                    "latency_ms": 0,
                    "group_index": group_index,
                    "group_symbols": tuple(group_symbols),
                }
            critique_stage_ended_at = time.time()
        else:
            round2_budget = max(2.0, min(rem * 0.40, rem - min(CIO_MIN_ARBITRATION_TIME, max(MIN_SAFE_REASONING_TIME, rem * 0.5))))
            critique_tasks = []
            for composite, group_index, key, _group_prompt, _symbols in proposal_order:
                peer_entries = [
                    (other_composite, other_key)
                    for other_composite, other_group_index, other_key, _p, _s in proposal_order
                    if other_group_index == group_index and other_key != key
                ]
                my_prop = trader_proposals.get(composite, {}).get(
                    "content", "（该交易员第一轮未提交有效提案）")
                peers_text_list = []
                for other_composite, other_key in peer_entries:
                    p_res = trader_proposals.get(other_composite, {})
                    p_id = p_res.get("proposal_id", f"{other_key}_prop")
                    p_name = p_res.get("role_name", other_key)
                    peers_text_list.append(
                        f"=== 【{p_name}】(提案标识: {p_id}) ===\n"
                        f"{p_res.get('content', '（未提交）')}"
                    )
                peers_text = "\n\n".join(peers_text_list) if peers_text_list else "（无其他同行提案）"
                critique_tasks.append(
                    (
                        composite,
                        (call_critique, key, roles[key], my_prop, peers_text,
                         original_system_prompt, round2_budget, runtime_context),
                        _deadline_task_kwargs(call_trader, deadline, cancellation_event),
                    )
                )
            completed, unfinished, critique_task_metrics = _bounded_parallel_calls(
                critique_tasks, deadline=deadline, max_workers=concurrency,
                stagger_seconds=0.0)
            critique_stage_ended_at = time.time()
            critique_unfinished = set(unfinished)
            for composite, _group_index, key, _group_prompt, _symbols in proposal_order:
                result = completed.get(composite)
                if composite in unfinished:
                    trader_critiques[composite] = {
                        "role_id": key,
                        "role_name": roles[key].get("name", key),
                        "status": "timeout",
                        "content": "质询超时：委员会截止时间已耗尽",
                        "latency_ms": 0,
                    }
                elif isinstance(result, Exception):
                    trader_critiques[composite] = {
                        "role_id": key,
                        "role_name": roles[key].get("name", key),
                        "status": "error",
                        "content": f"质询异常: {result}",
                        "latency_ms": 0,
                    }
                else:
                    trader_critiques[composite] = result or {
                        "role_id": key,
                        "role_name": roles[key].get("name", key),
                        "status": "error",
                        "content": "质询返回为空",
                        "latency_ms": 0,
                    }
    else:
        # === MODE: Standard (Single-Round Proposals -> CIO Verdict) ===
        rem = deadline - time.time()
        if rem < MIN_SAFE_REASONING_TIME + 2.0:
            cancellation_event.set()
            raise TimeoutError(
                f"Council timeout: remaining time {rem:.2f}s insufficient for standard deliberation (requires >= {MIN_SAFE_REASONING_TIME + 2.0}s)"
            )

        # 审计 P2-14：CIO 是唯一会被执行的输出，必须给它留足预算——旧实现只留
        # MIN_SAFE_REASONING_TIME(5s)，四席思考型模型吃满后 CIO 只能在几秒内草率定稿。
        cio_reserve = min(CIO_MIN_ARBITRATION_TIME, max(MIN_SAFE_REASONING_TIME, rem * 0.35))
        member_timeout = max(2.0, min(max(rem * 0.55, 90.0), rem - cio_reserve))
        tasks = [
            (composite,
             (call_trader, key, roles[key], group_prompt, original_system_prompt,
              member_timeout, runtime_context),
             _deadline_task_kwargs(call_trader, deadline, cancellation_event))
            for composite, _group_index, key, group_prompt, _symbols in proposal_order
        ]
        proposal_stage_started_at = time.time()
        completed, unfinished, proposal_task_metrics = _bounded_parallel_calls(
            tasks, deadline=deadline, max_workers=concurrency,
            stagger_seconds=0.0 if group_mode else 0.8,
            cancellation_event=cancellation_event)
        proposal_stage_ended_at = time.time()
        proposal_unfinished = set(unfinished)
        for composite, _group_index, key, _group_prompt, _symbols in proposal_order:
            result = completed.get(composite)
            if composite in unfinished:
                trader_proposals[composite] = _timeout_trader_result(
                    key, roles[key], "Trader proposal timeout: Committee deadline exhausted")
            elif isinstance(result, Exception):
                trader_proposals[composite] = _timeout_trader_result(
                    key, roles[key], f"Proposal exception: {result}")
            else:
                trader_proposals[composite] = result or _timeout_trader_result(
                    key, roles[key], "Trader proposal error: empty result")

    # Compile the Structured Investment Committee Docket
    transcript_blocks = []
    docket_order = proposal_order if group_mode else [
        (key, None, key, market_prompt, ()) for key in trader_keys
    ]
    for composite, group_index, k, _group_prompt, group_symbols in docket_order:
        res = trader_proposals.get(composite, {})
        weight_str = f" [绩效权重: {res.get('weight', 1.0)}]" if res.get("weight") is not None else ""
        p_id = res.get("proposal_id", f"{k}_prop")
        group_label = (
            f" [Group {group_index}; 主责标的: {', '.join(group_symbols) or '未指定'}]"
            if group_index is not None else ""
        )
        transcript_blocks.append(
            f"=== 【{res.get('role_name', k)}】实操审查与作战提案{group_label} "
            f"[提案标识: {p_id}]（模型：{res.get('model_used', 'default')}"
            f"{'（⚠ 席位绑定模型未登记，本席由主脑代答）' if res.get('model_fallback') else ''}{weight_str}）===\n"
            f"{res.get('content', '（该交易员本轮未提交有效提案）')}\n"
        )
    compiled_proposals = "\n".join(transcript_blocks) if transcript_blocks else "（无其他交易员提交方案，首席投资官独立决策）"

    if consensus_mode == "cross_examination" and trader_critiques:
        critique_blocks = []
        for composite, group_index, k, _group_prompt, group_symbols in docket_order:
            c_res = trader_critiques.get(composite, {})
            group_label = (
                f" [Group {group_index}; 主责标的: {', '.join(group_symbols) or '未指定'}]"
                if group_index is not None else ""
            )
            critique_blocks.append(
                f"=== 【{c_res.get('role_name', k)}】针对同行方案的交叉漏洞质询{group_label} ===\n"
                f"{c_res.get('content', '（该交易员未提交质询）')}\n"
            )
        compiled_critiques = "\n".join(critique_blocks)
        docket_content = (
            f"【第一轮：各交易员独立作战提案卷宗】\n{compiled_proposals}\n\n"
            f"====================================================\n"
            f"【第二轮：同行方案交叉漏洞质询与攻防辩论】\n{compiled_critiques}"
        )
    else:
        docket_content = f"【交易员实战作战提案卷宗】\n{compiled_proposals}"

    # CIO Final Review & Funding Verdict
    rem = deadline - time.time()
    if rem < MIN_SAFE_REASONING_TIME:
        cancellation_event.set()
        raise TimeoutError(
            f"Council deliberation timeout before CIO arbitration: {rem:.2f}s remaining is below safety threshold {MIN_SAFE_REASONING_TIME}s"
        )

    # 审计 P1-4b：仲裁官席位同样不得静默回落（事实进裁决载荷，UI 可标注）
    cio_resolved = resolve_seat({**cio_spec, "reasoning_effort": cio_spec.get("reasoning_effort") or "high"})
    override_model = cio_resolved["model"] or None
    override_url = cio_resolved["base_url"]
    override_key = cio_resolved["api_key"]
    override_format = cio_resolved["api_format"]
    override_effort = cio_resolved["effort"] or "high"
    cio_temperature = float(cio_spec.get("temperature", 0.2))

    cio_system_prompt = (
        f"{original_system_prompt}\n\n"
        "====================================================\n"
        f"【身份特别授权：你是对冲基金首席投资官 (CIO) 兼交易总监】\n"
        f"{_render_seat_prompt(str(cio_spec.get('prompt') or ''), runtime_context)}\n\n"
        "====================================================\n"
        "【投委会终审发单契约强约束（全面落盘持仓处理、挂单撤留与新标的点位！）】\n"
        "你必须对全局资金、在途持仓、在途挂单及标的池全部标的做出终审裁决：\n"
        "1. 【持仓与挂单闭环管理】：\n"
        "   - 在 position_management 中对所有活动持仓下达权威指令（HOLD / CLOSE_MARKET / UPDATE_SL）及理由；\n"
        "   - 在 pending_orders_management 中对所有在途未成交挂单下达处理指令（CANCEL / KEEP）及理由；\n"
        "2. 【标的池全标的开仓方案终审 (decisions) 与采纳归属 (adopted_role)】：\n"
        f"   - 仔细比对各位交易员提交的方案{'与交叉质询辩论' if consensus_mode == 'cross_examination' else ''}，评估逻辑最扎实者采纳，存在漏洞者驳回；\n"
        f"   - 各席提案末尾附有标准报价单（标的|倾向|限价|止损|止盈|保证金|置信度|依据），请逐项横向对比后再裁决；"
        f"你批复的点位若与被采纳参谋报价单明显偏离，必须在 reasoning 中说明调整原因；\n"
        "   - decisions 必须是标的字典（如 \"BTC-USDT-SWAP\"），每个标的必须包含 \"adopted_role\" 字段：\n"
        "     * 采纳某位交易员方案时填写其 role_id（例如 \"trader_trend\"、\"trader_momentum\"、\"trader_quant\"）；\n"
        "     * 全员驳回或无人被采纳时填写 \"REJECT_ALL\" 或 null；\n"
        "   - 在 reasoning 中明确写出你的仲裁依据（如「【CIO批复】采纳交易员 A 对 BTC 稳健回踩买多方案，驳回交易员 B 的追多」或「【CIO批复】驳回全员方案，市场震荡全员空仓 WAIT」）；\n"
        "   - 若批准对某标的开仓（BUY_LONG 或 SELL_SHORT），必须输出完整的四维点位与采纳归属：\n"
        "     {\n"
        '       "action": "BUY_LONG" 或 "SELL_SHORT",\n'
        '       "adopted_role": "trader_trend",  // 明确采纳的交易员 ID（如 trader_trend / trader_momentum / trader_quant），若无则填写 null\n'
        '       "confidence": 82,  // 最终核定置信度整数 0~100\n'
        '       "entry_price": 78250.0,  // 挂单入场限价（数字），严禁市价追高\n'
        '       "limit_price": 78250.0,  // 入场限价同义兼容\n'
        '       "stop_loss": 76500.0,  // 严格基于 1.8~2.2x 1H ATR 设置的防插针止损价（数字）\n'
        '       "stop_loss_price": 76500.0,  // 止损价同义兼容\n'
        '       "take_profit": 81750.0,  // 目标盈亏比 2.0~3.5R 的合理波段止盈价（数字），严禁超远天际线挂单\n'
        '       "take_profit_price": 81750.0,  // 止盈价同义兼容\n'
        f'       "leverage": {int(max(_ASTRA_MIN_LEVERAGE, min(_ASTRA_MAX_LEVERAGE, (_ASTRA_MIN_LEVERAGE + _ASTRA_MAX_LEVERAGE) / 2)))},  // 杠杆整数：必须落在 [{_ASTRA_MIN_LEVERAGE:g}~{_ASTRA_MAX_LEVERAGE:g}] 区间按信心自主裁决，严禁照抄模板占位值\n'
        '       "margin_usdt": 150.0,  // 拟投入保证金（须在可用余额安全范围内）\n'
        '       "reasoning": "【CIO批复】采纳/驳回了哪位交易员的提案，资金与风控考量"\n'
        "     }\n"
        '   - 若判定为 WAIT 观望，输出: {"action": "WAIT", "adopted_role": "REJECT_ALL", "confidence": 50, "reasoning": "【CIO批复】驳回理由与资金保全考量"}\n\n'
        "3. 最终必须且只能输出严格符合交易契约的 JSON 格式，绝不包含任何 markdown 代码块外部的多余文本！\n"
        "必须包含三个顶层键：\"macro_assessment\", \"position_management\", \"decisions\"（可选包含 \"pending_orders_management\"）。"
    )

    cio_user_prompt = (
        "【市场实时全景数据、账户可用资金与在途持仓挂单】\n"
        f"{market_prompt}\n\n"
        "====================================================\n"
        f"{docket_content}\n\n"
        "====================================================\n"
        "请作为首席投资官 (CIO) 审阅卷宗，统筹资金安全，裁定本轮发单并输出标准 JSON：\n"
        "1. 在 macro_assessment 中给出全局资金偏好、仓位总敞口与宏观裁定总括。\n"
        "2. 在 position_management 中落实每一个现有持仓的动态处理。\n"
        "3. 在 decisions 中对标的池全部标的逐一下达方案采纳或驳回批复（包含 adopted_role 与 reasoning），并给出完整四维点位！"
    )

    cio_started_at = time.time()
    cio_start_remaining = max(0.0, deadline - cio_started_at)
    cio_timeout = cio_start_remaining
    try:
        content, reasoning, usage, latency = execute_llm_request(
            messages=[
                {"role": "system", "content": cio_system_prompt},
                {"role": "user", "content": cio_user_prompt},
            ],
            model=override_model,
            base_url=override_url,
            api_key=override_key,
            api_format=override_format,
            reasoning_effort=override_effort,
            temperature=cio_temperature,
            response_format={"type": "json_object"},
            timeout=cio_timeout,
            max_tokens=cio_resolved.get("max_tokens"),
            allow_fallback=False,  # CIO 终审同理由登记模型作答；整链失败由上层降级单模型决策
            deadline=deadline,
            cancellation_event=cancellation_event,
        )
    except BaseException:
        cancellation_event.set()
        raise
    cio_ended_at = time.time()
    cio_end_remaining = max(0.0, deadline - cio_ended_at)
    if cio_ended_at > deadline:
        cancellation_event.set()
        raise TimeoutError(
            "Council deliberation timeout: CIO returned after the absolute deadline"
        )

    clean_content = content.strip()
    if clean_content.startswith("```json"):
        clean_content = clean_content[7:]
    if clean_content.startswith("```"):
        clean_content = clean_content[3:]
    if clean_content.endswith("```"):
        clean_content = clean_content[:-3]
    clean_content = clean_content.strip()

    brain_output = json.loads(clean_content)
    if not isinstance(brain_output, dict):
        raise ValueError("CIO output root must be a JSON object")

    # Post-process & normalize adopted_role in decisions for traceability
    # （阶段 4·B3 第二十八刀：迁至 council/role_normalizer.py）
    _normalize_cio_adopted_roles(brain_output, roles, trader_keys)

    council_end_at = time.time()

    def _stage_telemetry(started_at: Optional[float], ended_at: Optional[float]) -> Dict[str, Any]:
        if started_at is None or ended_at is None:
            return {"start_ts": None, "end_ts": None, "latency_ms": 0}
        return {
            "start_ts": started_at,
            "end_ts": ended_at,
            "latency_ms": max(0, int(round((ended_at - started_at) * 1000))),
        }

    group_telemetry = []
    for group_pos, (group_index, _group_prompt, group_symbols) in enumerate(proposal_groups):
        group_key_set = {
            composite for composite, candidate_index, _key, _prompt, _symbols in proposal_order
            if candidate_index == group_index
        }
        metrics = [proposal_task_metrics[key] for key in group_key_set if key in proposal_task_metrics]
        starts = [item["started_at"] for item in metrics if item.get("started_at") is not None]
        ends = [item["ended_at"] for item in metrics if item.get("ended_at") is not None]
        group_timeout = bool(group_key_set & proposal_unfinished) or any(
            item.get("status") == "timeout" for item in metrics
        )
        group_error = any(item.get("status") == "error" for item in metrics)
        if group_timeout:
            group_status = "timeout"
        elif group_error:
            group_status = "error"
        elif metrics:
            group_status = "ok"
        else:
            group_status = "not_started"
        group_telemetry.append({
            "index": group_index if group_index is not None else group_pos,
            "primary_symbols": list(group_symbols),
            "start_ts": min(starts) if starts else None,
            "end_ts": max(ends) if ends else None,
            "latency_ms": (
                max(0, int(round((max(ends) - min(starts)) * 1000)))
                if starts and ends else 0
            ),
            "status": group_status,
            "timeout": group_timeout,
            "error": group_error,
        })

    council_transcript = {
        "council_mode": True,
        "council_architecture": "Hedge Fund Investment Committee",
        "consensus_mode": consensus_mode,
        "total_duration_ms": int((council_end_at - t_start) * 1000),
        "telemetry": {
            "global_council_start_ts": t_start,
            "global_council_deadline_ts": deadline,
            "global_council_end_ts": council_end_at,
            "total_cycle_time_ms": max(0, int(round((council_end_at - t_start) * 1000))),
            "group_count": len(group_telemetry),
            "symbols_per_group": [item["primary_symbols"] for item in group_telemetry],
            "groups": group_telemetry,
            "trader_stage": _stage_telemetry(proposal_stage_started_at, proposal_stage_ended_at),
            "critique_stage": _stage_telemetry(critique_stage_started_at, critique_stage_ended_at),
            "cio_stage": {
                "start_ts": cio_started_at,
                "end_ts": cio_ended_at,
                "latency_ms": max(0, int(round((cio_ended_at - cio_started_at) * 1000))),
                "start_remaining_budget_ms": max(0, int(round(cio_start_remaining * 1000))),
                "end_remaining_budget_ms": max(0, int(round(cio_end_remaining * 1000))),
            },
            "proposal_tasks": proposal_task_metrics,
            "critique_tasks": critique_task_metrics,
        },
        "arbitrator": {
            "role_name": cio_spec.get("name", "首席投资官 (CIO)"),
            "model_used": override_model or get_active_llm_runtime().get("model", "default"),
            "model_requested": cio_resolved["requested"],
            "model_registered": cio_resolved["registered"],
            "model_fallback": cio_resolved["fallback"],
            "model_note": cio_resolved["reason"],
            "latency_ms": latency,
            # `reasoning` is provider-native CoT and must remain audit-only.
            "reasoning": reasoning,
            "summary": _build_cio_verdict_summary(brain_output),
        },
        "advisors": trader_proposals,
        "cross_examinations": trader_critiques if consensus_mode == "cross_examination" else {},
    }

    brain_output["council_transcript"] = council_transcript
    return brain_output, council_transcript


def execute_council_debate(
    load_config: Callable[[], Dict[str, Any]],
    resolve_seat: Callable[..., Any],
    call_trader: Callable[..., Any],
    call_critique: Callable[..., Any],
    market_prompt: str,
    original_system_prompt: str,
    timeout: float = 240.0,
    runtime_context: Optional[Dict[str, Any]] = None,
    group_prompts: Optional[Tuple[Any, ...]] = None,
    cancellation_event=None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Own one cycle generation and reject stale results at the return boundary."""
    cycle_token, cycle_event = _begin_council_cycle(cancellation_event)
    try:
        if cycle_event.is_set():
            raise TimeoutError("Council cycle was cancelled before it started")
        brain_output, transcript = _execute_council_debate_impl(
            load_config, resolve_seat, call_trader, call_critique,
            market_prompt, original_system_prompt, timeout, runtime_context,
            group_prompts, cycle_event,
        )
        if not _is_active_council_cycle(cycle_token, cycle_event):
            raise TimeoutError("Council cycle became stale before persistence")
        transcript["cycle_token"] = cycle_token
        brain_output["council_transcript"] = transcript
        global _LAST_COMPLETED_CYCLE_TOKEN
        with _CYCLE_STATE_LOCK:
            if _ACTIVE_CYCLE != (cycle_token, cycle_event):
                raise TimeoutError("Council cycle became stale before commit")
            _LAST_COMPLETED_CYCLE_TOKEN = cycle_token
        return brain_output, transcript
    finally:
        _finish_council_cycle(cycle_token, cycle_event)
