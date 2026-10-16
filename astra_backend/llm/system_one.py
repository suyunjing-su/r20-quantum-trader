"""Typed transport for TypeSafe System One.

System One is intentionally not represented as chat messages.  Its wire contract is
``model`` + ``state`` + typed ``questions`` and its response is a map of typed
answers.  Keeping this module separate prevents a System One model from silently
falling through the text-generation transport.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

from astra_backend.llm.providers import _join_api_path

SYSTEM_ONE_API_FORMAT = "typesafe_system_one"
SYSTEM_ONE_CAPABILITY = "structured_decision"
SYSTEM_ONE_DEFAULT_PATH = "/systemone"
SYSTEM_ONE_DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"

# The provider owns the Jev release and alias catalogue.  Keep this client
# deliberately version-agnostic: new model IDs must not require an application
# release or a guessed alias mapping.
def normalize_system_one_model(model: str) -> str:
    """Validate the shape of a provider-facing model ID without rewriting it."""
    model_id = str(model or "").strip()
    if not model_id:
        return ""
    if len(model_id) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in model_id):
        raise ValueError("System One model ID is empty, too long, or contains control characters")
    return model_id


def is_system_one_format(value: str | None) -> bool:
    return str(value or "").strip().lower() in {
        SYSTEM_ONE_API_FORMAT,
        "system_one",
        "typesafe",
        "typesafe_systemone",
    }


def canonical_system_one_format(value: str | None) -> str:
    return SYSTEM_ONE_API_FORMAT if is_system_one_format(value) else str(value or "").strip()


def _validate_questions(questions: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError("System One questions must be a non-empty object")
    normalized: Dict[str, Any] = {}
    for key, value in questions.items():
        qid = str(key).strip()
        if not qid or not isinstance(value, Mapping):
            raise ValueError("System One questions must map non-empty ids to objects")
        normalized[qid] = dict(value)
    return normalized


def build_system_one_request(
    *,
    model: str,
    state: Any,
    questions: Mapping[str, Any],
    base_url: str,
    api_key: str = "",
    api_path: str = SYSTEM_ONE_DEFAULT_PATH,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Build a native System One request; never emits ``messages`` or ``stream``."""
    model_id = normalize_system_one_model(model)
    if not model_id:
        raise ValueError("System One model is required")
    endpoint_base = str(base_url or "").strip().rstrip("/")
    if not endpoint_base.startswith(("http://", "https://")):
        raise ValueError("System One base URL must start with http:// or https://")
    path = str(api_path or SYSTEM_ONE_DEFAULT_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    endpoint = _join_api_path(endpoint_base, path)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "AstraQuant/1.0 (TypeSafe-System-One)",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return endpoint, headers, {
        "model": model_id,
        "state": state,
        "questions": _validate_questions(questions),
    }


def parse_system_one_response(payload: Any) -> Dict[str, Any]:
    """Normalize a native response while preserving typed answers and usage."""
    if not isinstance(payload, Mapping):
        raise ValueError("System One response must be a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise ValueError("System One response does not contain an answers object")
    usage_raw = payload.get("usage")
    usage = dict(usage_raw) if isinstance(usage_raw, Mapping) else {}
    normalized_usage: Dict[str, Any] = dict(usage)
    for key in ("input_tokens", "output_tokens"):
        if key in normalized_usage:
            try:
                normalized_usage[key] = int(normalized_usage[key])
            except (TypeError, ValueError):
                normalized_usage.pop(key, None)
    if "total_tokens" not in normalized_usage:
        normalized_usage["total_tokens"] = sum(
            int(normalized_usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens")
        )
    return {
        "model": str(payload.get("model") or ""),
        "answers": {str(k): v for k, v in answers.items()},
        "usage": normalized_usage,
    }


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:600]
    except Exception:
        return ""


def _is_model_registry_error(body: str) -> bool:
    text = str(body or "").lower()
    return (
        "unsupported typesafe model" in text
        or "not served by this plugin" in text
        or "declared jev model" in text
    )


def _model_registry_hint(model: str) -> str:
    return (
        f"TypeSafe 拒绝模型 {model!r}：这是供应商端模型注册/服务列表错误，"
        "不是 System One 请求格式错误。请刷新 TypeSafe 模型目录或检查该 API Key 的模型权限；"
        "客户端不会把请求模型静默替换成其他版本。"
    )


def execute_system_one_request(
    *,
    model: str,
    state: Any,
    questions: Mapping[str, Any],
    base_url: str,
    api_key: str,
    api_path: str = SYSTEM_ONE_DEFAULT_PATH,
    timeout: float = 12.0,
    attempts: int = 2,
) -> Dict[str, Any]:
    """Execute a bounded typed request.

    TypeSafe documents 429 and 529 as retryable.  The retry budget is deliberately
    small and bounded; no caller can turn this into an unbounded loop.
    """
    endpoint, headers, body = build_system_one_request(
        model=model, state=state, questions=questions, base_url=base_url,
        api_key=api_key, api_path=api_path,
    )
    requested_model = str(body["model"])
    tries = max(1, min(int(attempts or 1), 3))
    last_error = ""
    started = time.perf_counter()
    request_sent_at = 0.0
    response_received_at = 0.0
    for attempt in range(tries):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), 2))
        request = urllib.request.Request(
            endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
        )
        request_sent_at = time.time()
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, min(float(timeout), 60.0))) as response:
                response_received_at = time.time()
                raw = response.read().decode("utf-8", errors="replace")
                parsed = parse_system_one_response(json.loads(raw))
                parsed.update({
                    "ok": True,
                    "status_code": response.getcode(),
                    "endpoint": endpoint,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "request_sent_at": request_sent_at,
                    "response_received_at": response_received_at,
                    "api_format": SYSTEM_ONE_API_FORMAT,
                    "requested_model": requested_model,
                    "model_fallback": body.get("model") != requested_model,
                })
                return parsed
        except urllib.error.HTTPError as exc:
            body_text = _error_body(exc)
            last_error = f"HTTP {exc.code}: {body_text}"
            registry_error = exc.code == 400 and _is_model_registry_error(body_text)
            if registry_error:
                last_error = f"{last_error} | {_model_registry_hint(body.get('model', ''))}"
            if exc.code not in (429, 529) or attempt + 1 >= tries:
                return {
                    "ok": False, "status_code": exc.code, "endpoint": endpoint,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "request_sent_at": request_sent_at,
                    "response_received_at": response_received_at,
                    "api_format": SYSTEM_ONE_API_FORMAT,
                    "model": body.get("model", ""),
                    "requested_model": requested_model,
                    "model_fallback": body.get("model") != requested_model,
                    "model_registry_error": registry_error,
                    "error": last_error,
                }
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            # Network and malformed responses are bounded retry candidates, but a
            # diagnostic/runtime caller still receives a deterministic failure.
            if attempt + 1 >= tries:
                return {
                    "ok": False, "status_code": 0, "endpoint": endpoint,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "request_sent_at": request_sent_at,
                    "response_received_at": response_received_at,
                    "api_format": SYSTEM_ONE_API_FORMAT,
                    "model": requested_model,
                    "requested_model": requested_model,
                    "model_fallback": False,
                    "model_registry_error": False,
                    "error": last_error,
                }
    return {
        "ok": False, "status_code": 0, "endpoint": endpoint,
        "request_sent_at": request_sent_at,
        "response_received_at": response_received_at,
        "api_format": SYSTEM_ONE_API_FORMAT,
        "model": requested_model,
        "requested_model": requested_model,
        "model_fallback": False,
        "model_registry_error": False,
        "error": last_error or "request failed",
    }


def build_diagnostic_request(*, model: str, base_url: str, api_key: str, api_path: str = SYSTEM_ONE_DEFAULT_PATH):
    """Build a minimal typed request for connection diagnostics."""
    return build_system_one_request(
        model=model,
        state={"diagnostic": True},
        questions={
            "connection_check": {
                "type": "choice",
                "instructions": "Choose yes if this request reached the TypeSafe System One endpoint.",
                "criteria": {"yes": None, "no": None},
            }
        },
        base_url=base_url,
        api_key=api_key,
        api_path=api_path,
    )
