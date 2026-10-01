"""自进化复盘的**上下文装配**（`scripts/evolution/` 部件，从门面搬出）。

| 函数 | 职责 |
|---|---|
| `summarize_closed_trades` | 平仓统计汇总（笔数/胜负/胜率/净利/手续费）+ 数理快照可观测性审计摘要 |
| `build_host_constitution` | **宿主宪章**文本：代码层硬约束，profile 只能调措辞风格，永远无法删改证据纪律与基准心法保护（Code is Law，2026-09-10） |

## 为什么单独成模块

宿主宪章是**安全语义文本**：它规定"字段缺失不得解读为证据""基准心法不得静默删除"
"证据不足必须 NO_CHANGE"。埋在 95 行提示词装配里时，改错一行不会有人发现；
独立成函数后，门可以直接断言四条硬约束**逐条存在**且 `observability_brief` 真的被插值。

两个函数都是**纯函数**（零副作用、零模块全局读取 —— 依赖全部显式入参）。
"""


from __future__ import annotations

import json

from typing import Any, Dict, List, Tuple


def summarize_closed_trades(*,
        audit_snapshot_observability,
        closed_trades,
        render_observability_brief):
    total = len(closed_trades)
    wins = [t for t in closed_trades if t["net_pnl"] > 0]
    losses = [t for t in closed_trades if t["net_pnl"] <= 0]
    win_rate = round(len(wins) / total * 100, 1) if total > 0 else 0.0
    total_net = round(sum(t["net_pnl"] for t in closed_trades), 2)
    total_fees = round(sum(t["fee"] for t in closed_trades), 2)
    snapshot_audit = audit_snapshot_observability(closed_trades)
    observability_brief = render_observability_brief(snapshot_audit)
    return (total, wins, losses, win_rate, total_net, total_fees, snapshot_audit, observability_brief)


def build_host_constitution(*,
        observability_brief,
        execution_quality_brief="无执行质量数据"):
    host_constitution = (
        "\n\n======================= 【宿主宪章·代码层硬约束（任何提示词风格档案不可覆盖）】 =======================\n"
        f"1. 数理快照可观测性审计（宿主确定性统计，非模型推断）：{observability_brief}。\n"
        f"2. 执行质量可观测性审计（宿主确定性统计，非模型推断）：{execution_quality_brief}。\n"
        "3. 逐单标注含义：DYNAMICS_OBSERVED=开仓动力学/积分/概率链完整，可作数理因果归因；"
        "PARTIAL=仅可引用 entry_snapshot 中实际非空字段；PRICE_ONLY / NONE=数理快照不可观测，"
        "严禁编造或倒推 v/a/j/I、energy_integral、deviation_area_integral、延续/击穿概率、VaR/CVaR 因果，"
        "字段缺失本身不得解读为任何证据。执行质量字段为 null 或 UNOBSERVED 时不得作低流动性、滑点或成交失败归因；"
        "旧交易缺字段只可标为历史不可观测，改进建议仅限未来采集。\n"
        "4. ai_long_term_memory 给出生效后完整清单时必须原样包含全部现有基准心法（is_baseline）："
        "省略条目会被宿主原样补回并留痕；认定基准失效只能写入 diagnosis_insights 交人工复核，禁止静默删除。\n"
        "5. 证据不足必须 NO_CHANGE；NO_CHANGE 永不覆盖或清空长期记忆。\n"
    )
    return (host_constitution)


def parse_review_json(*,
        content):
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]

    review_json = json.loads(content.strip())
    if not isinstance(review_json, dict):
        review_json = {}
    return content, review_json


def normalize_asset_multipliers(*,
        TARGET_INSTRUMENTS,
        clamp,
        llm_review):
    raw_asset_mults = llm_review.get("asset_multipliers", {})
    if not isinstance(raw_asset_mults, dict):
        raw_asset_mults = {}
    asset_mults = {
        asset: clamp(raw_asset_mults.get(asset, 1.0), 0.5, 1.5, 1.0)
        for asset in TARGET_INSTRUMENTS
    }
    return asset_mults
