"""单次请求的构建、发送与响应解析（传输层）。

纯函数，不读任何模块级常量；TRANSIENT_MARKERS 随本模块一起迁出。
对 llm_manager 内**会读常量**的函数（init_llm_config / get_active_llm_runtime 等）
没有任何依赖，故可直接搬迁而不破坏测试注入接缝。
结构优化阶段 2（B4）。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

from astra_backend.llm.capabilities import _detect_reasoning_type
from astra_backend.llm.providers import _join_api_path
from astra_backend.llm.system_one import is_system_one_format


# Transient upstream faults (gateway route flaps, bot/rate shields, 5xx) must not
# silently degrade a trading or self-evolution cycle into NO_CHANGE. Retry with backoff.
# 注：unknown provider / model_not_found 已移出瞬时名单（2026-09-09 事件复盘）——
# 网关不认识该模型是轮内持续故障，原地重试只会白白烧掉请求窗口，按硬故障立即换模型。
TRANSIENT_MARKERS = (
    "upstream", "temporarily unavailable", "overloaded", "rate limit",
    "too many requests", "capacity", "busy", "bad gateway", "gateway timeout",
)


class _LLMTransientError(Exception):
    """可重试错误：瞬时 HTTP、超时、连接层异常（拒绝/重置/DNS/TLS）、坏响应体、空正文。

    fail_over_now=True：错误本身可再试（末位模型仍会重试），但链上还有下一个模型时
    立即切换——504/思考超时属"慢故障"，同一轮内原地重试大概率再烧满一个超时窗口。"""

    def __init__(self, message: str, timed_out: bool = False, fail_over_now: bool = False):
        super().__init__(message)
        self.timed_out = timed_out
        self.fail_over_now = fail_over_now


_LLM_GATES: dict[str, tuple[threading.BoundedSemaphore, int]] = {}
_LLM_GATES_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, tuple[float, float, float]] = {}
_RATE_BUCKETS_LOCK = threading.Lock()


def _candidate_gate_limit(cand: Dict[str, Any]) -> int:
    raw = (
        cand.get("concurrency_limit")
        or cand.get("provider_concurrency_limit")
        or os.getenv("ASTRA_LLM_PROVIDER_CONCURRENCY", "1")
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _candidate_key(cand: Dict[str, Any]) -> str:
    provider = str(
        cand.get("provider_id")
        or cand.get("provider_name")
        or cand.get("base_url")
        or "default"
    ).strip().lower()
    model = str(cand.get("model") or "default").strip().lower()
    return f"{provider}:{model}"


def _candidate_gate(cand: Dict[str, Any]) -> threading.BoundedSemaphore:
    key = _candidate_key(cand)
    limit = _candidate_gate_limit(cand)
    with _LLM_GATES_LOCK:
        current = _LLM_GATES.get(key)
        if current is None or current[1] != limit:
            current = (threading.BoundedSemaphore(limit), limit)
            _LLM_GATES[key] = current
        return current[0]


def _candidate_rate_limit(cand: Dict[str, Any]) -> float:
    raw = (
        cand.get("rate_limit_per_minute")
        or cand.get("provider_rate_limit_per_minute")
        or os.getenv("ASTRA_LLM_PROVIDER_RPM", "")
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _acquire_candidate_rate(cand: Dict[str, Any], deadline: Optional[float], cancellation_event=None) -> None:
    """Consume one shared provider/model token without crossing the deadline."""
    rate = _candidate_rate_limit(cand)
    if rate <= 0:
        return
    key = _candidate_key(cand)
    # Conservative capacity-one bucket: no burst above the configured RPM.
    capacity = 1.0
    while True:
        now = time.monotonic()
        with _RATE_BUCKETS_LOCK:
            old_rate, tokens, last = _RATE_BUCKETS.get(key, (rate, capacity, now))
            if old_rate != rate:
                old_rate, tokens, last = rate, capacity, now
            tokens = min(capacity, tokens + max(0.0, now - last) * rate / 60.0)
            if tokens >= 1.0:
                _RATE_BUCKETS[key] = (rate, tokens - 1.0, now)
                return
            wait_for = (1.0 - tokens) * 60.0 / rate
            _RATE_BUCKETS[key] = (rate, tokens, now)
        if cancellation_event is not None and cancellation_event.is_set():
            raise _LLMTransientError(
                f"LLM provider rate gate 已被委员会 cycle 取消（模型 {cand.get('model', '')}）",
                timed_out=True, fail_over_now=True,
            )
        remaining = None if deadline is None else float(deadline) - time.time()
        if remaining is not None and remaining <= 0:
            raise _LLMTransientError(
                f"LLM provider rate gate 已超过委员会绝对截止时间（模型 {cand.get('model', '')}）",
                timed_out=True, fail_over_now=True,
            )
        wait_for = min(wait_for, remaining) if remaining is not None else wait_for
        if cancellation_event is not None:
            cancellation_event.wait(timeout=max(0.0, wait_for))
        else:
            time.sleep(max(0.0, wait_for))


def _acquire_candidate_gate(cand: Dict[str, Any], deadline: Optional[float], cancellation_event=None) -> threading.BoundedSemaphore:
    gate = _candidate_gate(cand)
    if cancellation_event is not None and cancellation_event.is_set():
        raise _LLMTransientError(
            f"LLM 请求已被委员会 cycle 取消（模型 {cand.get('model', '')}）",
            timed_out=True, fail_over_now=True,
        )
    remaining = None if deadline is None else float(deadline) - time.time()
    if remaining is not None and remaining <= 0:
        raise _LLMTransientError(
            f"LLM provider gate 已超过委员会绝对截止时间（模型 {cand.get('model', '')}）",
            timed_out=True, fail_over_now=True,
        )
    acquired = gate.acquire(timeout=max(0.0, remaining) if remaining is not None else None)
    if not acquired:
        raise _LLMTransientError(
            f"LLM provider gate 等待超过委员会绝对截止时间（模型 {cand.get('model', '')}）",
            timed_out=True, fail_over_now=True,
        )
    # A semaphore wake-up can race the wall clock.  Never let a permit acquired
    # after the absolute deadline start an HTTP request.
    if cancellation_event is not None and cancellation_event.is_set():
        gate.release()
        raise _LLMTransientError(
            f"LLM provider gate 获取后 cycle 已取消（模型 {cand.get('model', '')}）",
            timed_out=True, fail_over_now=True,
        )
    if deadline is not None and float(deadline) - time.time() <= 0:
        gate.release()
        raise _LLMTransientError(
            f"LLM provider gate 获取后已超过委员会绝对截止时间（模型 {cand.get('model', '')}）",
            timed_out=True, fail_over_now=True,
        )
    return gate


class _LLMHardError(Exception):
    """不可重试错误（对该模型）：认证失败、404、参数被拒等——直接切换下一个回退模型。"""


def _is_transient_http(code: int, body: str) -> bool:
    low = (body or "").lower()
    if code in (408, 409, 425, 429, 500, 502, 503, 504):
        return True
    if code in (400, 401, 402, 403) and any(m in low for m in TRANSIENT_MARKERS):
        return True
    return False


def _coerce_reasoning_text(value: Any) -> str:
    """Normalize provider-specific reasoning fields without exposing them as content."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "thinking", "reasoning", "content", "summary"):
            if value.get(key) is not None:
                return _coerce_reasoning_text(value[key])
        return ""
    if isinstance(value, list):
        return "".join(_coerce_reasoning_text(item) for item in value)
    return "" if value is None else str(value)


def _parse_llm_response(target_format: str, res_json: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    content = ""
    reasoning_content = ""
    raw_usage = res_json.get("usage", {}) if isinstance(res_json, dict) else {}
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}

    # Protocol 1: Claude Messages Response
    if target_format == "claude_messages":
        text_chunks = [c.get("text", "") for c in res_json.get("content", []) if c.get("type") == "text"]
        thinking_chunks = [c.get("thinking", "") for c in res_json.get("content", []) if c.get("type") == "thinking"]
        content = "".join(text_chunks).strip()
        reasoning_content = "\n".join(thinking_chunks).strip()
        if not usage:
            usage = {
                "total_tokens": res_json.get("usage", {}).get("input_tokens", 0) + res_json.get("usage", {}).get("output_tokens", 0)
            }

    # Protocol 2: OpenAI Responses Response
    elif target_format == "openai_responses":
        content = str(res_json.get("output_text") or "").strip()
        for item in res_json.get("output", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and not content:
                for part in item.get("content", []):
                    if part.get("type") == "output_text" or "text" in part:
                        content += str(part.get("text", ""))
            elif item.get("type") == "reasoning":
                reasoning_content += str(item.get("content") or item.get("summary") or "")
        content = content.strip()
        reasoning_content = reasoning_content.strip()

    # Protocol 3: OpenAI Chat Completions Response
    else:
        msg = res_json.get("choices", [{}])[0].get("message", {})
        content = str(msg.get("content", "")).strip()
        reasoning_content = _coerce_reasoning_text(
            msg.get("reasoning_content") or msg.get("reasoning")
        ).strip()
        if not reasoning_content:
            reasoning_content = _coerce_reasoning_text(msg.get("reasoning_details")).strip()

    # 规范化提取各厂商 Prompt Caching 缓存命中指标（OpenAI, DeepSeek, Claude, Gemini, Qwen）
    prompt_details = usage.get("prompt_tokens_details", {}) if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached_tokens = (
        prompt_details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cached_content_token_count")
        or usage.get("cached_tokens")
    )
    if cached_tokens is not None:
        try:
            usage["cached_tokens"] = int(cached_tokens)
        except (TypeError, ValueError):
            pass

    return content, reasoning_content, usage


class _StreamAccumulator:
    """Accumulate protocol-native SSE events without buffering the response body."""

    def __init__(self, target_format: str):
        self.target_format = target_format
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.usage: Dict[str, Any] = {}
        self.completed_response: Optional[Dict[str, Any]] = None
        self.finish_reason: Optional[str] = None

    def feed(self, event: Dict[str, Any]) -> None:
        event_usage = event.get("usage")
        if isinstance(event_usage, dict) and event_usage:
            self.usage.update(event_usage)

        if self.target_format == "claude_messages":
            event_type = event.get("type")
            if event_type == "message_start":
                message = event.get("message") or {}
                if isinstance(message.get("usage"), dict):
                    self.usage.update(message["usage"])
            elif event_type == "content_block_delta":
                delta = event.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    self.content_parts.append(str(delta.get("text") or ""))
                elif delta_type == "thinking_delta":
                    self.reasoning_parts.append(str(delta.get("thinking") or ""))
                # input_json_delta belongs to tool input, not assistant text.
            elif event_type == "message_delta":
                delta_usage = event.get("usage")
                if isinstance(delta_usage, dict):
                    self.usage.update(delta_usage)
                delta = event.get("delta") or {}
                stop_reason = delta.get("stop_reason")
                if stop_reason:
                    self.finish_reason = str(stop_reason)
            elif event_type == "message_stop":
                self.finish_reason = self.finish_reason or "stop"
            return

        if self.target_format == "openai_responses":
            event_type = str(event.get("type") or "")
            if event_type == "response.output_text.delta":
                self.content_parts.append(str(event.get("delta") or ""))
            elif event_type == "response.output_text.done" and not self.content_parts:
                self.content_parts.append(str(event.get("text") or ""))
            elif "reasoning" in event_type and event_type.endswith(".delta"):
                self.reasoning_parts.append(str(event.get("delta") or event.get("text") or ""))
            elif event_type == "response.completed" and isinstance(event.get("response"), dict):
                self.completed_response = event["response"]
                self.finish_reason = "stop"
            elif event_type == "response.incomplete":
                self.finish_reason = "length"
            return

        # OpenAI Chat Completions: each event is a chat.completion.chunk.
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            piece = delta.get("content")
            if isinstance(piece, list):
                piece = "".join(
                    str(p.get("text") or p) if isinstance(p, dict) else str(p)
                    for p in piece
                )
            if piece is not None:
                self.content_parts.append(str(piece))
            reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_piece is not None:
                self.reasoning_parts.append(_coerce_reasoning_text(reasoning_piece))
            elif delta.get("reasoning_details") is not None:
                # Some OpenAI-compatible gateways omit `reasoning` and only send
                # the structured detail list. It is still reasoning, never answer text.
                self.reasoning_parts.append(
                    _coerce_reasoning_text(delta.get("reasoning_details"))
                )

    def finish(self) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
        if self.completed_response:
            final_content, final_reasoning, final_usage = _parse_llm_response(
                self.target_format, self.completed_response
            )
            if not self.content_parts:
                self.content_parts.append(final_content)
            if not self.reasoning_parts:
                self.reasoning_parts.append(final_reasoning)
            if not self.usage:
                self.usage = final_usage
            else:
                for key, value in final_usage.items():
                    self.usage.setdefault(key, value)

        content = "".join(self.content_parts).strip()
        reasoning = "\n".join(self.reasoning_parts).strip()
        return content, reasoning, _normalize_usage(self.usage), self.finish_reason


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize cache-hit counters for all supported provider usage shapes."""
    normalized = dict(usage or {})
    prompt_details = normalized.get("prompt_tokens_details", {})
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    cached_tokens = (
        prompt_details.get("cached_tokens")
        or normalized.get("prompt_cache_hit_tokens")
        or normalized.get("cache_read_input_tokens")
        or normalized.get("cached_content_token_count")
        or normalized.get("cached_tokens")
    )
    if cached_tokens is not None:
        try:
            normalized["cached_tokens"] = int(cached_tokens)
        except (TypeError, ValueError):
            pass
    return normalized


def _parse_sse_data(data_lines: List[str]) -> Optional[Dict[str, Any]]:
    """Decode one SSE event's data fields; comments and [DONE] are ignored."""
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except (TypeError, ValueError):
        # Keep compatibility with gateways that emit keep-alives or malformed
        # non-data events; a valid later event can still complete the stream.
        return None
    return event if isinstance(event, dict) else None


def _check_stream_control(deadline: Optional[float], cancellation_event=None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise TimeoutError("LLM 流式响应已被委员会 cycle 取消")
    if deadline is not None and float(deadline) - time.time() <= 0:
        raise TimeoutError("LLM 流式响应已超过绝对截止时间")


def _consume_sse_lines(
    target_format: str,
    lines: Iterable[bytes],
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
    """Consume an SSE stream line-by-line and return the completed response."""
    accumulator = _StreamAccumulator(target_format)
    data_lines: List[str] = []

    def dispatch() -> None:
        nonlocal data_lines
        event = _parse_sse_data(data_lines)
        data_lines = []
        if event is not None:
            accumulator.feed(event)

    for raw_line in lines:
        _check_stream_control(deadline, cancellation_event)
        if deadline is not None and float(deadline) - time.time() <= 0:
            raise TimeoutError("LLM 流式响应已超过绝对截止时间")
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            dispatch()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # event:, id:, retry: and comment lines are framing metadata. The JSON
        # data field is authoritative across all three provider protocols.
    dispatch()
    _check_stream_control(deadline, cancellation_event)
    return accumulator.finish()


def _set_response_read_timeout(resp: Any, deadline: Optional[float]) -> None:
    """Refresh the underlying socket timeout before each potentially blocking read."""
    if deadline is None:
        return
    remaining = float(deadline) - time.time()
    if remaining <= 0:
        raise TimeoutError("LLM 流式响应已超过绝对截止时间")
    candidates = [resp]
    for attr in ("fp", "raw", "_sock"):
        expanded: List[Any] = []
        for item in candidates:
            value = getattr(item, attr, None)
            if value is not None:
                expanded.append(value)
        candidates.extend(expanded)
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            try:
                setter(remaining)
            except (AttributeError, OSError, ValueError):
                pass
            return


def _consume_stream_response(
    target_format: str,
    resp: Any,
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
    """Consume a real HTTP stream incrementally, with JSON-only compatibility."""
    readline = getattr(resp, "readline", None)
    if not callable(readline):
        _check_stream_control(deadline, cancellation_event)
        _set_response_read_timeout(resp, deadline)
        body = resp.read()
        _check_stream_control(deadline, cancellation_event)
        _check_stream_control(deadline, cancellation_event)
        try:
            return (*_parse_llm_response(target_format, json.loads(body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body)), None)
        except (TypeError, ValueError) as exc:
            raise ValueError("流式响应体非 JSON/SSE") from exc

    _check_stream_control(deadline, cancellation_event)
    _set_response_read_timeout(resp, deadline)
    first = readline()
    _check_stream_control(deadline, cancellation_event)
    if not isinstance(first, (bytes, bytearray)):
        _check_stream_control(deadline, cancellation_event)
        _set_response_read_timeout(resp, deadline)
        body = resp.read()
        _check_stream_control(deadline, cancellation_event)
        _check_stream_control(deadline, cancellation_event)
        try:
            return (*_parse_llm_response(target_format, json.loads(body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body)), None)
        except (TypeError, ValueError) as exc:
            raise ValueError("流式响应体非 JSON/SSE") from exc

    first = bytes(first)
    first_stripped = first.lstrip()
    is_sse = first_stripped.startswith((b"data:", b"event:", b":"))
    if not is_sse:
        body = bytearray(first)
        iterator = iter(resp)
        while True:
            _check_stream_control(deadline, cancellation_event)
            _set_response_read_timeout(resp, deadline)
            try:
                raw_line = next(iterator)
            except StopIteration:
                break
            if isinstance(raw_line, (bytes, bytearray)):
                body.extend(raw_line)
        if deadline is not None and float(deadline) - time.time() <= 0:
            raise TimeoutError("LLM 响应已超过绝对截止时间")
        try:
            return (*_parse_llm_response(target_format, json.loads(bytes(body).decode("utf-8", errors="replace"))), None)
        except (TypeError, ValueError) as exc:
            raise ValueError("流式响应体非 JSON/SSE") from exc

    def remaining_lines() -> Iterable[bytes]:
        yield first
        iterator = iter(resp)
        while True:
            _check_stream_control(deadline, cancellation_event)
            _set_response_read_timeout(resp, deadline)
            try:
                raw_line = next(iterator)
            except StopIteration:
                break
            if isinstance(raw_line, (bytes, bytearray)):
                yield bytes(raw_line)

    return _consume_sse_lines(
        target_format, remaining_lines(), deadline=deadline,
        cancellation_event=cancellation_event,
    )


def _parse_stream_response(target_format: str, body: bytes | str) -> Tuple[str, str, Dict[str, Any]]:
    """Parse a complete SSE/JSON fixture; live requests use incremental consumption."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    if "data:" not in text:
        try:
            content, reasoning, usage = _parse_llm_response(target_format, json.loads(text))
            return content, reasoning, _normalize_usage(usage)
        except (TypeError, ValueError) as exc:
            raise ValueError("流式响应体非 JSON/SSE") from exc

    lines = (line.encode("utf-8") for line in text.splitlines(keepends=True))
    content, reasoning, usage, _ = _consume_sse_lines(target_format, lines)
    return content, reasoning, usage


def _read_stream_body(resp: Any) -> bytes:
    """Legacy body reader retained for compatibility tests and JSON fallbacks."""
    return resp.read()



def _coerce_max_tokens(value: Any, default: int = 8192) -> int:
    """Return a safe output-token budget for all supported provider protocols."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(256, min(131072, parsed))

def build_request_spec(
    model: str,
    messages: List[Dict[str, str]],
    base_url: str,
    api_key: str = "",
    api_format: str = "openai_chat",
    reasoning_effort: str = "high",
    temperature: Optional[float] = 0.2,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_type: str = "auto",
    # 交易委员会需要为全标的决策预留完整 JSON 输出；未显式设置时，兼容网关常用的
    # 2048 token 默认会在约 9~10KB 处截断 JSON，随后由调用方触发 Unterminated string。
    max_tokens: int = 8192,
    api_path: str = "",
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Build endpoint URL, headers, and request payload according to the specific API protocol format."""
    max_tokens = _coerce_max_tokens(max_tokens)
    cleaned_url = base_url.rstrip("/")
    if is_system_one_format(api_format):
        raise ValueError("TypeSafe System One uses build_system_one_request, not chat messages")
    # 「API 路径」字段生效：标准路径由协议格式决定；仅非标准自定义路径覆盖之。
    custom_path = str(api_path or "").strip()
    if custom_path and not custom_path.startswith("/"):
        custom_path = "/" + custom_path
    if custom_path in ("/chat/completions", "/messages", "/responses", "/v1/chat/completions", "/v1/messages", "/v1/responses"):
        custom_path = ""
    m_lower = model.lower()
    rtype = reasoning_type if reasoning_type != "auto" else _detect_reasoning_type(model)
    effort = (reasoning_effort or "auto").strip().lower()

    # Protocol 1: Anthropic Claude Messages API
    if api_format == "claude_messages":
        endpoint = _join_api_path(cleaned_url, custom_path or "/messages")

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "AstraQuant/8.3 (Claude-Messages)",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key

        # Separate system message
        system_chunks = [m["content"] for m in messages if m.get("role") == "system"]
        chat_messages = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") != "system"]

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": chat_messages,
            "stream": True,
        }
        if system_chunks:
            payload["system"] = "\n\n".join(system_chunks)

        if effort in ("max", "xhigh", "high", "medium", "low"):
            budget_map = {
                "max": 64000,
                "xhigh": 32000,
                "high": 16000,
                "medium": 8000,
                "low": 2048,
            }
            budget = budget_map[effort]
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = budget + max_tokens
        elif effort == "none":
            payload["thinking"] = {"type": "disabled"}
            if temperature is not None:
                payload["temperature"] = temperature
        else:
            if temperature is not None:
                payload["temperature"] = temperature

        return endpoint, headers, payload

    # Protocol 2: OpenAI Responses API (/responses)
    elif api_format == "openai_responses":
        endpoint = _join_api_path(cleaned_url, custom_path or "/responses")

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "AstraQuant/8.3 (OpenAI-Responses)",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: Dict[str, Any] = {
            "model": model,
            "input": messages,
            "stream": True,
            "max_output_tokens": max_tokens,
        }
        if response_format and response_format.get("type") == "json_object":
            payload["text"] = {"format": {"type": "json_object"}}
        if effort in ("max", "xhigh", "high", "medium", "low", "minimal"):
            payload["reasoning"] = {"effort": effort}

        return endpoint, headers, payload

    # Protocol 3: OpenAI Chat Completions (/chat/completions, Default)
    else:
        endpoint = _join_api_path(cleaned_url, custom_path or "/chat/completions")

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "AstraQuant/8.3 (OpenAI-Chat)",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            # Chat Completions emits a final usage-only chunk when requested.
            # Disable obfuscation because this is an internal trusted connection.
            "stream_options": {"include_usage": True, "include_obfuscation": False},
        }

        # Temperature handling for reasoning models vs normal models
        is_reasoning_model = (
            rtype in ("deepseek_reasoner", "standard_effort")
            or m_lower.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6", "chatgpt-6"))
            or "gpt-5" in m_lower or "gpt-6" in m_lower or "chatgpt-6" in m_lower
            or "deepseek-v4" in m_lower or "v4.1" in m_lower
            or "reasoner" in m_lower
            or "-r1" in m_lower
            or "qwen3" in m_lower or "qwen-3" in m_lower or "qwq" in m_lower
            or "kimi-k" in m_lower or "glm-5" in m_lower
            or "nemotron" in m_lower
        )
        if not is_reasoning_model:
            if temperature is not None:
                payload["temperature"] = temperature
        else:
            if "gemini" in m_lower and temperature is not None:
                payload["temperature"] = temperature

        # Standard reasoning effort parameter (supports max, xhigh, high, medium, low, minimal, none)
        if rtype == "standard_effort" or (rtype == "auto" and (
            "gemini" in m_lower or "qwen3" in m_lower or "qwen-3" in m_lower or "qwq" in m_lower
            or m_lower.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6", "chatgpt-6"))
            or "gpt-5" in m_lower or "gpt-6" in m_lower or "chatgpt-6" in m_lower
            or "deepseek-v4" in m_lower or "v4.1" in m_lower
            or "kimi-k" in m_lower or "glm-5" in m_lower
            or "nemotron" in m_lower
        )):
            if effort in ("max", "xhigh", "high", "medium", "low", "minimal"):
                payload["reasoning_effort"] = effort
            elif effort == "none":
                payload["reasoning_effort"] = "none"

        if response_format and rtype != "deepseek_reasoner":
            payload["response_format"] = response_format

        return endpoint, headers, payload


def build_chat_payload(
    model: str,
    messages: List[Dict[str, str]],
    reasoning_effort: str = "high",
    temperature: Optional[float] = 0.2,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_type: str = "auto",
) -> Dict[str, Any]:
    """Compatibility wrapper for standard chat payload generation."""
    _, _, payload = build_request_spec(
        model=model,
        messages=messages,
        base_url="https://api.openai.com/v1",
        api_format="openai_chat",
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        response_format=response_format,
        reasoning_type=reasoning_type,
    )
    return payload


def _attempt_llm_call(
    cand: Dict[str, Any],
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    response_format: Optional[Dict[str, Any]],
    effective_timeout: float,
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Tuple[str, str, Dict[str, Any], int]:
    """Run one attempt behind the shared provider/model concurrency gate."""
    _acquire_candidate_rate(cand, deadline, cancellation_event)
    gate = _acquire_candidate_gate(cand, deadline, cancellation_event)
    try:
        return _attempt_llm_call_unbounded(
            cand, messages, temperature, response_format, effective_timeout,
            deadline, cancellation_event,
        )
    finally:
        gate.release()


def _attempt_llm_call_unbounded(
    cand: Dict[str, Any],
    messages: List[Dict[str, str]],
    temperature: Optional[float],
    response_format: Optional[Dict[str, Any]],
    effective_timeout: float,
    deadline: Optional[float] = None,
    cancellation_event=None,
) -> Tuple[str, str, Dict[str, Any], int]:
    """单次请求一个模型；失败时抛 _LLMTransientError（可重试）或 _LLMHardError（换模型）。"""
    max_tokens = _coerce_max_tokens(cand.get("max_tokens"))
    endpoint, headers, payload = build_request_spec(
        model=cand["model"],
        messages=messages,
        base_url=cand["base_url"],
        api_key=cand.get("api_key", ""),
        api_format=cand.get("api_format", "openai_chat"),
        reasoning_effort=cand.get("reasoning_effort", "high"),
        temperature=temperature,
        response_format=response_format,
        reasoning_type=cand.get("reasoning_type", "auto"),
        max_tokens=max_tokens,
        api_path=cand.get("api_path", ""),
    )

    t0 = time.perf_counter()
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers)

    def request_timeout() -> float:
        if cancellation_event is not None and cancellation_event.is_set():
            raise _LLMTransientError(
                f"LLM 请求已被委员会 cycle 取消（模型 {cand['model']}）",
                timed_out=True, fail_over_now=True,
            )
        if deadline is None:
            return effective_timeout
        remaining = float(deadline) - time.time()
        if remaining <= 0:
            raise _LLMTransientError(
                f"LLM 请求已超过委员会绝对截止时间（模型 {cand['model']}）",
                timed_out=True,
                fail_over_now=True,
            )
        return min(effective_timeout, remaining)

    try:
        resp_handle = urllib.request.urlopen(req, timeout=request_timeout())
        if cancellation_event is not None and cancellation_event.is_set():
            resp_handle.close()
            raise _LLMTransientError(
                f"LLM 响应已被委员会 cycle 取消（模型 {cand['model']}）",
                timed_out=True, fail_over_now=True,
            )
    except urllib.error.HTTPError as exc:
        err_b = ""
        try:
            err_b = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # Adaptive fallback retry on rejected parameter (400: reasoning_effort/temperature/response_format)
        if (
            exc.code == 400
            and cand.get("api_format", "openai_chat") == "openai_chat"
            and any(kw in err_b.lower() for kw in ["reasoning_effort", "temperature", "response_format", "invalid parameter"])
        ):
            fb_payload = {
                "model": cand["model"],
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
            }
            fb_req = urllib.request.Request(endpoint, data=json.dumps(fb_payload).encode("utf-8"), headers=headers)
            try:
                with urllib.request.urlopen(fb_req, timeout=request_timeout()) as fb_resp:
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    content, reasoning, usage, finish_reason = _consume_stream_response(
                        cand.get("api_format", "openai_chat"), fb_resp,
                        deadline=deadline, cancellation_event=cancellation_event,
                    )
                if finish_reason in ("length", "max_tokens"):
                    raise _LLMTransientError(
                        f"模型 {cand['model']} 流式输出达到长度上限（finish_reason={finish_reason}）"
                    )
                if not content and not reasoning:
                    raise _LLMTransientError(f"模型 {cand['model']} 返回空正文（已自适应去参数重试）")
                if deadline is not None and float(deadline) - time.time() <= 0:
                    raise TimeoutError("LLM 自适应重试响应已超过绝对截止时间")
                return content, reasoning, usage, latency_ms
            except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError) as fb_exc:
                fb_code = getattr(fb_exc, "code", 0) or 0
                if isinstance(fb_exc, (TimeoutError, socket.timeout)):
                    raise _LLMTransientError(
                        f"LLM 自适应重试超时（模型 {cand['model']}）",
                        timed_out=True,
                        fail_over_now=True,
                    ) from fb_exc
                if fb_code and not _is_transient_http(fb_code, str(getattr(fb_exc, "msg", "") or fb_exc)):
                    raise _LLMHardError(f"LLM 网关返回 HTTP {fb_code}（模型 {cand['model']}）：{str(fb_exc)[:280]}") from fb_exc
                raise _LLMTransientError(f"LLM 网关返回 HTTP {exc.code}（模型 {cand['model']}）：{(err_b or '')[:280]}") from fb_exc
        if _is_transient_http(exc.code, err_b):
            raise _LLMTransientError(
                f"LLM 网关返回 HTTP {exc.code}（模型 {cand['model']}）：{(err_b or '')[:280]}",
                fail_over_now=(exc.code == 504),  # 504=上游已超时：链上有下一个模型则立即切换
            ) from exc
        raise _LLMHardError(f"LLM 网关返回 HTTP {exc.code}（模型 {cand['model']}）：{(err_b or '')[:280]}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise _LLMTransientError(
            f"LLM 推演超时（已达到思考上限时间 {effective_timeout:.0f}s）：模型思考链过长未在时限内完成响应，可前往后台 AI 模型设置中调大思考上限时间",
            timed_out=True,
            fail_over_now=True,  # 慢故障：有回退链时立即切换，不再原地烧第二个超时窗口
        ) from exc
    except urllib.error.URLError as exc:
        # 连接层异常（拒绝/重置/DNS/TLS/断线）与超时包装同样属于瞬时故障：
        # 旧版在此处直接 raise，导致「失败一次就不再请求」——现在纳入重试与回退。
        reason = getattr(exc, "reason", None)
        timed_out = isinstance(reason, (socket.timeout, TimeoutError))
        raise _LLMTransientError(
            f"LLM 连接层异常（模型 {cand['model']}）：{type(reason).__name__ if reason is not None else type(exc).__name__}: {str(reason or exc)[:220]}",
            timed_out=timed_out,
            fail_over_now=timed_out,  # 连接层包装的超时同样按慢故障快速换模型
        ) from exc
    except (ValueError, OSError) as exc:
        # 响应体非 JSON（如反代 HTML 错误页）、读取中断等：可重试
        raise _LLMTransientError(f"LLM 响应体解析失败（模型 {cand['model']}）：{str(exc)[:200]}") from exc

    with resp_handle as resp:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            content, reasoning, usage, finish_reason = _consume_stream_response(
                cand.get("api_format", "openai_chat"), resp,
                deadline=deadline, cancellation_event=cancellation_event,
            )
        except (ValueError, TimeoutError, socket.timeout) as exc:
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise _LLMTransientError(
                    f"LLM 流式响应超时（模型 {cand['model']}）",
                    timed_out=True,
                    fail_over_now=True,
                ) from exc
            raise _LLMTransientError(
                f"LLM 响应体非 JSON/SSE（模型 {cand['model']}）：{str(exc)[:200]}"
            ) from exc
    if deadline is not None and float(deadline) - time.time() <= 0:
        raise _LLMTransientError(
            f"LLM 响应已超过绝对截止时间（模型 {cand['model']}）",
            timed_out=True,
            fail_over_now=True,
        )
    if finish_reason in ("length", "max_tokens"):
        raise _LLMTransientError(
            f"模型 {cand['model']} 流式输出达到长度上限（finish_reason={finish_reason}）",
            fail_over_now=True,
        )
    if not content and not reasoning:
        raise _LLMTransientError(f"模型 {cand['model']} 返回空正文（HTTP 200 但无 content/reasoning，疑似上游静默失败）")
    return content, reasoning, usage, latency_ms
