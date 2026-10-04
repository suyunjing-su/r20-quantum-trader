"""Deterministic symbol grouping for Council trader proposal fan-out."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_MAX_SYMBOLS_PER_GROUP = 7
MIN_SYMBOLS_PER_GROUP = 1
MAX_CONFIGURED_SYMBOLS_PER_GROUP = 100
# Backward-compatible name for callers/tests that used the old constant.  It is
# the default, not a forced production limit.
MAX_SYMBOLS_PER_GROUP = DEFAULT_MAX_SYMBOLS_PER_GROUP
_MARKET_HEADING = "【全标的池原生行情、技术指标与筹码矩阵】"
_TIME_HEADING = "【当前决策时间戳与市场时效】"
_MARKET_MARKER = f"======================= {_MARKET_HEADING} ======================="
_TIME_MARKER = f"======================= {_TIME_HEADING} ======================="
_SEPARATOR = "---------------------------------------------------------"


@dataclass(frozen=True)
class SymbolGroup:
    index: int
    symbols: tuple[str, ...]
    prompt: str
    reference_symbols: tuple[str, ...] = ()


def _symbol(package: Mapping[str, Any]) -> str:
    return str(package.get("name") or package.get("instId") or "").strip()


def _canonical(symbol: str) -> str:
    text = str(symbol or "").strip().upper().replace("_", "-")
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    for suffix in ("-USDT-SWAP", "-USDT", "USDT"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def normalize_max_symbols_per_group(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_SYMBOLS_PER_GROUP
    return max(MIN_SYMBOLS_PER_GROUP, min(MAX_CONFIGURED_SYMBOLS_PER_GROUP, parsed))


def stable_symbol_groups(
    packages: Sequence[Mapping[str, Any]],
    max_symbols: int = DEFAULT_MAX_SYMBOLS_PER_GROUP,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """Keep the existing package/canonical order and split into bounded groups.

    The instrument pool order is the project's existing stable order.  We do not
    introduce a score-based reorder here: universe_score is a pool admission
    signal, not a runtime portfolio/risk ranking.
    """
    try:
        size = int(max_symbols)
    except (TypeError, ValueError):
        raise ValueError("max_symbols must be positive") from None
    if size <= 0:
        raise ValueError("max_symbols must be positive")
    ordered: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for package in packages or ():
        if not isinstance(package, Mapping):
            continue
        key = _canonical(_symbol(package))
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(package)
    return tuple(tuple(ordered[pos:pos + size]) for pos in range(0, len(ordered), size))


def _extract_market_blocks(prompt: str) -> tuple[str, str, str] | None:
    heading = f"{_MARKET_MARKER}\n"
    start = prompt.find(heading)
    if start < 0:
        return None
    body_start = start + len(heading)
    end_marker = f"\n\n{_TIME_MARKER}"
    end = prompt.find(end_marker, body_start)
    if end < 0:
        return None
    return prompt[:body_start], prompt[body_start:end], prompt[end:]


def _block_for_symbol(market_body: str, symbol: str) -> str:
    marker = f"{_SEPARATOR}\n【{symbol}"
    start = market_body.find(marker)
    if start < 0:
        # The renderer normally uses package name, but allow instId lookup when
        # a package has an empty display name.
        return ""
    next_start = market_body.find(f"\n{_SEPARATOR}\n", start + len(marker))
    if next_start < 0:
        return market_body[start:].strip()
    return market_body[start:next_start].strip()


def build_group_prompts(
    full_prompt: str,
    packages: Sequence[Mapping[str, Any]],
    *,
    max_symbols: int = DEFAULT_MAX_SYMBOLS_PER_GROUP,
    global_reference_names: Sequence[str] = ("BTC", "ETH"),
) -> tuple[SymbolGroup, ...]:
    """Create prompts that retain global context and bound primary symbols.

    The only changed region is the rendered market-matrix body.  Account,
    pending orders, news, risk budget, timestamp and deterministic market
    regime remain byte-for-byte from the original prompt.  BTC/ETH are included
    as global reference context in every group when available; they are not
    counted in the group's primary symbol budget.
    """
    grouped = stable_symbol_groups(
        [
            package for package in (packages or ())
            if isinstance(package, Mapping)
            and _canonical(_symbol(package)) not in {
                _canonical(name) for name in global_reference_names
            }
        ],
        max_symbols=max_symbols,
    )
    if not grouped:
        return ()
    parts = _extract_market_blocks(full_prompt)
    if parts is None:
        raise ValueError("full market prompt has no replaceable market-matrix section")
    prefix, market_body, suffix = parts
    references: list[str] = []
    wanted = {_canonical(name) for name in global_reference_names}
    for package in packages or ():
        key = _canonical(_symbol(package))
        if key in wanted and key not in {_canonical(item) for item in references}:
            references.append(_symbol(package))
    result: list[SymbolGroup] = []
    for index, group in enumerate(grouped):
        primary_names = tuple(_symbol(package) for package in group)
        selected_names = list(primary_names)
        for name in references:
            if name not in selected_names:
                selected_names.append(name)
        blocks = [_block_for_symbol(market_body, name) for name in selected_names]
        blocks = [block for block in blocks if block]
        if not blocks:
            raise ValueError(f"market prompt has no blocks for group {index}")
        group_body = "\n".join(blocks)
        scope = ", ".join(primary_names)
        reference_note = ", ".join(name for name in references if name not in primary_names)
        note = (
            "\n\n【本次交易员提案分组范围】\n"
            f"本组主责标的（仅对这些标的提交新开/加仓报价单）：{scope}\n"
            f"全局参考标的（保留用于市场联动判断，不属于本组主责输出）：{reference_note or '无'}\n"
            "账户、持仓、挂单、新闻、风险预算、市场 regime 与全局风险上下文仍以本提示词完整内容为准。"
        )
        result.append(SymbolGroup(index, primary_names, prefix + group_body + suffix + note,
                                  tuple(name for name in references if name not in primary_names)))
    return tuple(result)
