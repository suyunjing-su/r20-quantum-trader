"""统一执行路由：大模型标准决策 JSON → 场所原生受保护开仓（Gate/Binance 平权）。

铁律（与 OKX 遗留链路同源）：
1. 分发前物理校验不可绕过——数值有限、多空几何、R:R 底线复用 scripts.order_risk
   单一事实源（fail-closed）；
2. 开仓必须 100% 云端保护单覆盖：TP/SL 双腿挂成并回读验证才算成功，
   任一步失败 → 已挂触发单回滚 + 撤入场单，绝不留裸仓；
3. 场所执行开闸由 registry.execution_open 决定（Gate 默认关，需 ASTRA_GATE_EXECUTION=1）；
4. 本模块不吞异常语义：路由结果 {ok, stage, detail} 供上层台账归因。

决策契约（与 ai_brain 输出同构，币种为裸资产名）：
    {"asset": "BTC", "action": "BUY_LONG"|"SELL_SHORT", "margin_usdt": 150.0,
     "leverage": 3, "entry_price": 79650.0, "take_profit_price": 82000.0,
     "stop_loss_price": 78500.0}
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional

from .execution import own_records as own_position_records

from .execution.risk_gates import (
    check_total_exposure as _check_total_exposure,
    clamp_leverage as _clamp_leverage,
    clamp_margin as _clamp_margin,
)
# 2026-09-28 三所平权：入场闸门（跨所敞口 / 池 / 持仓模式）的**单一策略源**。
# 本函数此前把这三段判据内联在这里，而 OKX 直签路径一个都没有 ⇒
# 「OKX 的仓占着敞口上限、OKX 的下单却不查上限」。现在两条路径共用同一函数。
from .execution.venue_gate import venue_entry_gate as _venue_entry_gate
from .exchanges import (ExchangeCapabilityError, canonical_base, execution_open,
                        get_adapter, is_sandbox_environment, require_execution)

try:  # 单一事实源：几何 + R:R 底线（与执行层遗留链路同一把尺）
    from scripts.order_risk import validate_quote_geometry_and_rr
except ImportError:
    from order_risk import validate_quote_geometry_and_rr

try:  # 单一事实源：全局保证金闸门（审计 P0-1 前本模块 0 处引用 risk_constants）
    from scripts.risk_constants import MAX_LEVERAGE, MAX_MARGIN_EQUITY_RATIO, MAX_SINGLE_ASSET_MARGIN, MIN_LEVERAGE
    try:
        from scripts.risk_constants import MAX_TOTAL_EXPOSURE_USDT as TOTAL_EXPOSURE_CAP
    except ImportError:
        TOTAL_EXPOSURE_CAP = 0.0
except ImportError:  # pragma: no cover - scripts/ 在 sys.path 时走上面分支
    try:
        from risk_constants import MAX_MARGIN_EQUITY_RATIO, MAX_SINGLE_ASSET_MARGIN
    except ImportError:
        MAX_MARGIN_EQUITY_RATIO, MAX_SINGLE_ASSET_MARGIN = 0.20, 0.0
        # 常量不可得时不臆造区间：夹取退化为 no-op（下面靠 `or leverage` 短路），
        # 绝不因为读不到配置就凭空放大或缩小杠杆。
        MAX_LEVERAGE = MIN_LEVERAGE = None  # type: ignore[assignment]


#: 平仓后是否撤销**已证明属于本系统**的该合约保护腿（治本：遗留腿的产生源头）。
#: 只有"核验归零 + 归属可证明"两个条件同时满足才会撤；撤单失败/归属不明
#: 会让路由显式失败并保留本地状态，避免上层把未完成清理误当成可重试的完整成功。
CANCEL_STALE_PROTECTION_ON_CLOSE: bool = True
#: 归零核验的轮询节奏（平仓是低频操作，多读几次换"确实归零"的确定性划算）
CLOSE_FLAT_POLL_TRIES: int = 5
CLOSE_FLAT_POLL_SLEEP: float = 0.6


def _exposure_venues(venue: str, environment: Optional[str]):
    """核算跨所敞口时应统计的场所 → `(counted, skipped)`。

    判据 = **凭证已配置**（本部署是否在这里有账户），注册表驱动、不硬编码。

    ⚠️ 为什么**不用** `execution_open` 判（本刀实测踩到的第二处坑）：
    OKX 走的是 `ai_factor_trader` 直签链路，能力表里**没有**声明
    `adapter_execution_flag` ⇒ `execution_open("okx")` **结构性恒 False**
    （见 `registry.execution_open` 文档）。而 OKX 恰恰是持仓最多的那一所
    （当时日志写作「持仓 OKX 1/9｜跨所 4 笔」；该措辞已于 2026-09 改为逐所点名，
    见 `scripts/trader/cycle_snapshot.py::venue_position_span`）。
    若按开闸判，跨所敞口会**把 OKX 整个漏掉**
    —— 本机实测第一版就是这样：只统计到 binance 的 726U，而 OKX 在持仓位完全没算。

    - 计入 = 本次场所 + 其它**凭证齐备**的已登记场所；
    - 凭证未配置/读取失败 ⇒ 不计入，但**进 `skipped` 留痕**（不假装统计是全量的）；
    - 计入场所**读失败 ⇒ 抛错 fail-closed**（"读不到"绝不渲染成"没有敞口"）。
    """
    counted: List[str] = [str(venue)]
    skipped: List[str] = []
    try:
        from .exchanges.registry import (registered_venues as _venues,
                                         venue_credentials as _creds)
        for _v in _venues():
            _v = str(_v)
            if _v in counted:
                continue
            try:
                if all(_creds(_v, environment)):
                    counted.append(_v)
                else:
                    skipped.append(f"{_v}(凭证未配置)")
            except Exception:
                skipped.append(f"{_v}(凭证读取失败)")
    except Exception:
        pass                      # 枚举失败：退回"只算本次场所"，并由 skipped 显式留痕
    return counted, skipped


def collect_cross_venue_positions(venue: str, environment: Optional[str] = None,
                                  adapter: Any = None
                                  ) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """跨所持仓集合 → `(rows, counted, skipped)`；每行都打了 `venue` 标签。

    `check_total_exposure` 靠行上的 `venue` 做**逐腿归因**（"同向来自 binance/gate"）。

    统计口径 = `_exposure_venues`（**凭证齐备**的已登记场所，含 OKX —— 见那里的长注释）。
    `venue` 那一所复用调用方已建好的 `adapter`，不重复实例化。
    任一所读失败 ⇒ **抛 `RuntimeError`（fail-closed）**：绝不把"读不到"渲染成"没有敞口"。

    ⚠️ 公开出去是 2026-09-28 三所平权的一部分：**OKX 直签路径**此前没有敞口闸门，
    而敞口统计却把 OKX 的仓算进去。让两条路径共用这一份统计，才不会再造一次
    "名字比实现大"（同一个闸门被实现两遍、口径各说各话）。
    """
    rows: List[Dict[str, Any]] = []
    _counted_candidates, _skipped = _exposure_venues(venue, environment)
    _counted: List[str] = []
    for _v in _counted_candidates:
        try:
            _ad_v = adapter if (_v == venue and adapter is not None) else get_adapter(_v, environment=environment)
            for _p in (_ad_v.positions() or []):
                rows.append(dict(_p, venue=_v))
            _counted.append(_v)
        except Exception as exc:
            raise RuntimeError(
                f"跨所敞口不可核算：{_v} 持仓读取失败（{type(exc).__name__}: "
                f"{str(exc)[:80]}）——按 fail-closed 拒开") from exc
    return rows, _counted, _skipped


class RouteResult(Dict[str, Any]):
    """dict 子类型：{ok, venue, stage, detail, submission_state, order_id...}.

    ``submission_state`` is deliberately separate from ``ok``. A false result
    can mean a confirmed refusal before the write, or an unknown outcome after
    a timeout/ambiguous response. Callers must release an entry claim only for
    ``rejected_before_send``.
    """


class ProtectionCleanupResult(str):
    """人读 detail + 机器可判定的保护腿收尾结果。

    旧调用方把 ``_cancel_proven_own_legs`` 当作字符串拼进 detail；保留
    ``str`` 子类形状，新增 ``ok``/``attempted``/``failures`` 属性，让平仓
    路径不再靠中文片段猜测条件单是否清理完成。

    ``failures`` 只表示本方已获准撤除的腿没有完成撤单/撤后核验；
    ``report_only`` 表示按所有权安全规则**故意保留**的腿（用户手单、
    ledger-only、方向不匹配或无法判定）。后者必须披露，但不能被误报成
    本方清理失败，也不能授权自动撤销。
    """

    ok: bool
    attempted: bool
    applicable: bool
    verified: bool
    failures: tuple[str, ...]
    report_only: tuple[Dict[str, Any], ...]

    def __new__(cls, detail: str, *, ok: bool, attempted: bool = True,
                applicable: bool = True, verified: bool = False,
                failures: tuple[str, ...] = (),
                report_only: tuple[Dict[str, Any], ...] = ()):
        value = str.__new__(cls, detail)
        value.ok = bool(ok)
        value.attempted = bool(attempted)
        value.applicable = bool(applicable)
        value.verified = bool(verified)
        value.failures = tuple(failures)
        # These are legs deliberately retained because ownership/direction
        # could not authorize a DELETE.  They are evidence for operations,
        # not cleanup failures and must not block releasing a fully-cleaned
        # target position.
        value.report_only = tuple(report_only)
        return value


#: 各所"检测得到、但载荷未核验"的持仓模式 → 给运维的人读危害说明。
#: 放在这里是因为它是**运维文案**（各所自己的模式词汇仍归各所 capability 声明）。
MODE_HAZARDS: Dict[str, str] = {
    "dual_plus": "拆仓语义，本系统不把它折叠成净仓/双向解读",
    "long_short": "Hedge 对冲语义，positionSide 下单/保护腿载荷未在真实账户核验",
}


#: 己仓对账所用台账路径。空 = 用默认（`<repo>/data/trading_ledger.json`）。
#: ⚠️ 必须**调用期读取**（`import` 期快照会让测试 patch 失效，且本仓已有先例）；
#: 测试把它指向夹具，否则预检会读到**线上真台账**、断言随机器数据漂移
#: （本刀实测踩到：BTC 的判定被线上一条 closed 行左右成 stale_closed）。
OWN_POSITION_LEDGER_FILE: str = ""


def _fail(stage: str, detail: str, venue: str = "gate", **extra: Any) -> RouteResult:
    r = RouteResult(ok=False, venue=venue, stage=stage, detail=detail)
    r.update(extra)
    return r


def _load_venue_pool_soft(venue: str) -> Dict[str, Any]:
    """读取该所池配置；读取失败只告警返回 {}（池门禁是加固，不制造新的阻塞点）。"""
    try:
        from .exchanges.routing_policy import load_venue_pool
        pool = load_venue_pool(venue)
        return pool if isinstance(pool, dict) else {}
    except Exception as exc:
        print(f"[所池门禁] {venue.upper()} 池配置读取失败，按无限制继续（仅告警）: {exc}")
        return {}


def _venue_equity_ceiling(ad: Any, venue: str, *, equity_ratio: Any,
                          fallback: Optional[float]) -> Optional[float]:
    """按**该所自己**的可用余额算单笔保证金权益顶；读不到则退回调用方给的顶。

    调用方传进来的 `max_margin_usdt` 是按 **OKX** 可用余额算的 —— 引擎的资金读取
    历史上只覆盖 OKX 直签链。三所平权后这不再成立：币安/Gate 的仓花的是**它们自己**
    账户里的钱。实测 2026-09-28：币安账户 4739U、Gate 1302U，而下单仍按 OKX 的权益顶
    夹取；叠加池预算那道**假 50U 上限**，最终每单只占 ~50U 保证金 —— 账户里近 5000U
    却开出几十 U 的仓。

    读失败/非正数一律**退回调用方的顶**（不臆造更松的上限）。OKX 适配器未实装
    `account_snapshot`（基类抛 `ExchangeCapabilityError`）⇒ 自动回落，OKX 行为逐位不变。
    """
    try:
        snap = ad.account_snapshot()
    except Exception as exc:
        print(f"[权益顶] warn {str(venue).upper()} 账户快照读取失败，沿用调用方权益顶: {exc}")
        return fallback
    try:
        avail = float((snap or {}).get("available_usdt") or 0.0)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(avail) or avail <= 0:
        return fallback
    ceiling = round(avail * float(equity_ratio or 0.0), 4)
    return ceiling if ceiling > 0 else fallback


def open_protected_position(decision: Dict[str, Any], *,
                            price_ref: Optional[float] = None,
                            trigger_expiration: int = 604800,
                            adapter: Any = None,
                            own_position: Optional[Dict[str, Any]] = None,
                            margin_mode: Optional[str] = None,
                            environment: Optional[str] = None,
                            max_margin_usdt: Optional[float] = None) -> RouteResult:
    """标准决策 → Gate/Binance 受保护开仓（US-005 多所对等）。全程 fail-closed：任何缺口先撤后抛。

    own_position：调用方（lab/trader）在该合约上的在管仓位记录（含 size_signed/side）；
    交易所既有仓与之一致视为己仓放行，否则视为外部连坐风险拒开（stage=precheck）。
    margin_mode：由账户实况推导传入（cross/isolated）；缺省维持历史行为 cross。
    持仓模式：对**实现了只读探测**的场所先体检 —— 探测所得必须在该所
    `capabilities.position_modes` 声明域内，且落在 `entry_ready_position_modes`
    （载荷已在真实账户核验过的子集）内才放行；否则禁新开仓并写明原因。
    本系统**永不自动切换**账户模式（审计 §2）。
    environment：显式资金环境（demo/live），优先使用；缺省读 decision.get('environment')。
    max_margin_usdt：调用方按权益算出的单笔保证金硬顶（权益×ASTRA_MAX_MARGIN_EQUITY_RATIO）；
    缺省 0/None = 不臆造占比上限，但仍强制单标的绝对封顶（审计 P0-1）。
    """
    env_name = str(environment or decision.get("environment") or "").strip().lower() or None
    venue = str(decision.get("venue") or getattr(getattr(adapter, "capabilities", None), "venue", "gate") or "gate").strip().lower()
    if env_name and is_sandbox_environment(env_name) and venue == "gate" and env_name != "sandbox":
        env_name = "sandbox"
    ad = adapter or get_adapter(venue, environment=env_name)
    asset = canonical_base(str(decision.get("asset") or decision.get("name") or ""))
    action = str(decision.get("action") or "").upper()
    if not asset:
        return _fail("validate", "决策缺少 asset/币种", venue=venue)
    try:
        margin = float(decision.get("margin_usdt") or 0)
        leverage = float(decision.get("leverage") or 0)
        entry = float(decision.get("entry_price") or 0)
        tp = float(decision.get("take_profit_price") or 0)
        sl = float(decision.get("stop_loss_price") or 0)
    except (TypeError, ValueError):
        return _fail("validate", "决策数值字段非法", venue=venue)
    for tag, v in (("margin", margin), ("leverage", leverage), ("entry", entry)):
        if not math.isfinite(v) or v <= 0:
            return _fail("validate", f"{tag} 必须为有限正数，收到 {v}", venue=venue)

    # ── 杠杆区间（审计 P2-8，2026-09-13）──────────────────────────────────
    # 旧实现只有主脑写决策时夹 [MIN_LEVERAGE, MAX_LEVERAGE]，执行层只夹上限：
    # 决策缓存被回填/多所路径直接构造决策时，1x 这种低于配置下限的杠杆会真的发出去，
    # 「杠杆区间」在市场侧不成立。执行层是最后一道，必须有同样的两端夹取。
    # 每所池门禁（审计 P1-7，2026-09-13）：data/venue_routing.json 的 dry_run / assets /
    # max_open / margin_per_trade_usdt / min_confidence 此前在 routing_policy.py 之外
    # **零消费者** —— 管理员以为设了 per-venue 闸门其实没有。这里读一次，供下方
    # 保证金夹取与池准入判定共用。
    pool = _load_venue_pool_soft(venue)

    _cur_min_lev = float(os.getenv("ASTRA_MIN_LEVERAGE", "") or MIN_LEVERAGE or 2.0)
    _cur_max_lev = float(os.getenv("ASTRA_MAX_LEVERAGE", "") or MAX_LEVERAGE or 5.0)
    if _cur_min_lev > _cur_max_lev:
        _cur_min_lev = _cur_max_lev

    leverage, decision = _clamp_leverage(
        venue=venue, asset=asset, decision=decision, leverage=leverage,
        min_leverage=_cur_min_lev, max_leverage=_cur_max_lev)

    _margin_unclamped = margin
    # 权益顶改用**该所自己**的可用余额（见 `_venue_equity_ceiling`）：调用方按 OKX
    # 权益算出的顶对币安/Gate 没有意义——钱在它们各自的账户里。
    max_margin_usdt = _venue_equity_ceiling(
        ad, venue, equity_ratio=MAX_MARGIN_EQUITY_RATIO, fallback=max_margin_usdt)
    margin, decision, margin_clamped_from = _clamp_margin(
        venue=venue, asset=asset, decision=decision, margin=margin,
        max_margin_usdt=max_margin_usdt,
        max_single_asset_margin=MAX_SINGLE_ASSET_MARGIN,
        max_margin_equity_ratio=MAX_MARGIN_EQUITY_RATIO,
        pool=pool)

    # ⚠️ `_all_positions` 在本函数里**是局部变量**（真正的赋值在下方"外部持仓前置体检"
    # 处，L209 `_all_positions = ad.positions()`）。故原代码那句
    # `_all_positions if _all_positions is not None else ad.positions()`
    # 的**前半支永远取不到**（走到这里必然 UnboundLocalError），实际只会走
    # `ad.positions()`；而同一函数后面还会**再调一次** `ad.positions()`。
    # 抽离时把这次调用收成**惰性缓存**：行为等价（每次都是新取的 ad.positions()），
    # 且顺带把原来的**两次取数收成一次**。
    _pos_cache: dict = {}

    def _positions_for_exposure():
        """**跨所**同向敞口所需的持仓集合（本刀修正一处会说谎的风控）。

        ⚠️ 2026-09-20 实测：本闸门名叫「跨所同向敞口」、文档写「跨所同向名义额合计」，
        但 `positions_reader` 一直是 `ad.positions()` —— **只读被下单的那一个场所**。
        而 `.env` 里 `ASTRA_MAX_TOTAL_EXPOSURE_USDT` 是**真的配了的**
        （`scripts/risk_constants.py` 在 cron/手动路径下显式加载 `.env`；现值 **50000.0**，
        见 2026-09-28 核对），三所平权后上限最多可被突破到 3 倍
        （每所各算自己那份）——**是活着的闸门在少数**，不是死代码。

        ⚠️ 2026-09-28 三所平权补记：上面那句"每所各算自己那份"当时**只说对了一半** ——
        统计确实已是跨所（本函数），但闸门**只挂在 gate/binance 上**，
        OKX 直签路径不经过本函数 ⇒ **OKX 的仓算进上限、OKX 的下单却不查上限**。
        现已把池/敞口/模式三类判据收进 `execution/venue_gate.py`，两条路径共用。

        统计口径（显式写清，避免再次"名字比实现大"）：
        - 计入 = 本次下单场所 + 其它 `execution_open` 为真的**已开闸场所**；
        - 每个场所读失败 ⇒ **fail-closed 拒开**（抛错由闸门兜成 `stage=exposure`）：
          "读不到"绝不能渲染成"没有敞口"；
        - 仅在 `total_exposure_cap > 0` 时才被调用（闸门自身短路）⇒ 闸门停用时
          **不产生任何额外网络调用**。
        - 已知残留：某场所**已关闸但仍有仓**时不计入（该场所已不交易，
          其保护单健康由 venue-protection 巡检覆盖）；这条写在注释里而不是假装没有。
        """
        if not _pos_cache:
            rows, _counted, _skipped_venues = collect_cross_venue_positions(
                venue, env_name, adapter=ad)
            _pos_cache["rows"] = rows
            _pos_cache["venues"] = tuple(_counted)
            _pos_cache["skipped"] = tuple(_skipped_venues)
        return _pos_cache["rows"]

    _exposure_fail = _check_total_exposure(
        venue=venue, asset=asset, action=action, margin=_margin_unclamped,
        leverage=leverage, total_exposure_cap=TOTAL_EXPOSURE_CAP,
        all_positions=None, positions_reader=_positions_for_exposure,
        fail_factory=_fail)
    if _exposure_fail is not None:
        # 覆盖率留痕：写明"统计了哪些场所、哪些没计及原因"。风控说"跨所"，
        # 就必须让运维看得见这个"跨"到底跨到了哪几所（否则又是名字比实现大）。
        try:
            _covered = tuple(_pos_cache.get("venues") or ())
            _not_counted = tuple(_pos_cache.get("skipped") or ())
            _note = f"；统计范围={'/'.join(_covered) or '—'}"
            if _not_counted:
                _note += f"，未计={'、'.join(_not_counted)}"
            _exposure_fail["detail"] = f"{_exposure_fail.get('detail') or ''}{_note}"
            _exposure_fail["counted_venues"] = list(_covered)
            _exposure_fail["skipped_venues"] = list(_not_counted)
        except Exception:
            pass


    # 止盈宽度平滑收窄：防止 AI 规划过远天际线挂单无法落袋（受最大 R:R 与 ATR 跨度上限约束）
    try:
        from scripts.trader.brackets import clamp_take_profit_width
        _prec = len(str(entry).split(".")[1]) if "." in str(entry) else 2
        _ctx_atr = float(decision.get("atr") or decision.get("atr_1h") or 0.0)
        tp = clamp_take_profit_width(
            is_long=(action == "BUY_LONG"),
            limit_px=entry,
            sl_px=sl,
            tp_px=tp,
            atr=_ctx_atr,
            prec=_prec,
        )
        decision["take_profit_price"] = tp
    except Exception:
        pass

    ok, reason, rr = validate_quote_geometry_and_rr(action, entry, tp, sl)
    if not ok:
        return _fail("risk_gate", f"物理风控拒绝: {reason}", venue=venue, rr=rr)

    # 执行开闸（默认关；env 显式打开且凭证就绪前一切免谈）——保持既有契约：
    # 未开闸一律抛 ExchangeCapabilityError。
    require_execution(venue, environment=str(getattr(ad, "environment", "live") or "live"))

    # 环境维合约存在性对账（US-007 扩展）：下单前核对**本环境**合约
    # 目录——已下架/未上市在发送前拦截（fail-closed 拒开）；目录拉不到 →
    # fail-open 放行（对账是增强不是风控闸门，绝不阻塞交易）。
    try:
        from .exchanges.listing import ensure_contract_listed
        _check = ensure_contract_listed(
            venue, str(getattr(ad, "environment", "live") or "live"),
            ad.native_symbol(asset))
        if not _check.ok:
            return _fail("listing", f"合约对账拒绝: {_check.reason}", venue=venue)
    except Exception:  # 对账自身异常一律 fail-open（含目录缓存污染等未知面）
        pass

    # ⚠️ 位置说明：本闸门**放在 listing 对账之后** —— 下架/未上市合约
    # 必须在**任何**持仓探针之前就被拦掉（`test_listing_gate_refuses_delisted_contract`
    # 断言 `ad.calls == []`：下架合约绝不许走到任何 IO）。
    # ── 入场闸门（池 / 敞口 / 持仓模式）─────────────────────────────────────
    # 判据住在 `astra_backend/execution/venue_gate.py`，与 OKX 直签路径**共用同一份**。
    # 本函数不再内联这些判据 —— 那正是「闸门实现两遍、只有一遍接了 OKX」的成因。
    #
    # ⚠️ 持仓/模式探针一律**惰性**（传零参可调用给闸门）：纯静态的拒单
    # （`dry_run` / 准入币种 / 置信度 / 敞口）**不得产生任何网络调用** ——
    # 这是既有契约，`test_dry_run_blocks_the_send` 断言 `ad.calls == []`。
    _open_count: Optional[int] = None
    _all_positions = None
    _declared_modes = tuple(getattr(getattr(ad, "capabilities", None), "position_modes", ()) or ())
    _mode_probe = getattr(ad, "detect_position_mode", None)
    _mode_checked = bool(_declared_modes and callable(_mode_probe))
    _ready_modes: tuple = ()
    _probe_cache: dict = {}

    def _own_positions():
        """本所持仓探针（**只取一次**）：闸门算 `max_open` 与下方"外部持仓前置体检"共用。
        原实现此处与下方各取一次，函数注释里已自认"后面还会再调一次"。"""
        nonlocal _all_positions
        if _all_positions is None:
            _all_positions = ad.positions()
        return _all_positions

    def _own_open_count() -> int:
        if "open_count" not in _probe_cache:
            _probe_cache["open_count"] = len(
                [p for p in (_own_positions() or [])
                 if isinstance(p, dict) and abs(float(p.get("size_signed") or 0)) > 1e-9])
        return _probe_cache["open_count"]

    def _own_position_mode() -> str:
        if "mode" not in _probe_cache:
            _probe_cache["mode"] = str(_mode_probe() or "unknown").strip().lower()
        return _probe_cache["mode"]

    if _mode_checked:
        _ready_modes = tuple(getattr(getattr(ad, "capabilities", None),
                                     "entry_ready_position_modes", ()) or ())
    try:
        _venue_gate_fail = _venue_entry_gate(
            venue=venue, asset=asset, confidence=decision.get("confidence"),
            pool=pool, exposure_cap=TOTAL_EXPOSURE_CAP, exposure_fail=_exposure_fail,
            open_count=_own_open_count,
            position_mode=_own_position_mode,
            declared_modes=_declared_modes, entry_ready_modes=_ready_modes,
            mode_checked=_mode_checked, mode_hazards=MODE_HAZARDS)
    except ExchangeCapabilityError:
        raise
    except Exception as exc:
        # 闸门自身是纯函数；能抛的只有注入的持仓探针。
        return _fail("precheck",
                     f"{asset} 既有持仓探针失败，无法排除外部仓，拒开: {exc}", venue=venue)
    if _venue_gate_fail is not None:
        # 闸门返回的是**策略载荷**（`{"stage","detail",...}`），本函数对外是 `RouteResult`
        # （`{"ok": False, ...}`）。敞口闸门自己就是用 `_fail` 造的、已经是完整形状
        # ⇒ 原样透传（保住 `projected_exposure` / `contributing_venues` 等证据字段）；
        # 其余阶段（池 / 模式）在这里补齐外壳。
        if not isinstance(_venue_gate_fail, dict):
            return _venue_gate_fail          # 非 dict：自定义 fail_factory 形状，原样透传
        if "ok" in _venue_gate_fail:
            return _venue_gate_fail
        _extra = {k: v for k, v in _venue_gate_fail.items()
                  if k not in ("stage", "detail", "venue")}
        return _fail(str(_venue_gate_fail.get("stage") or "venue_pool"),
                     str(_venue_gate_fail.get("detail") or ""),
                     venue=str(_venue_gate_fail.get("venue") or venue), **_extra)

    spec = ad.fetch_instrument_spec(asset)
    if spec is None:
        return _fail("specs", f"{venue.upper()} 无法获取 {asset} 合约规格（下架或网络故障）", venue=venue)

    tick = float(spec.tick_size or 0.1) or 0.1

    def _q(px: float) -> float:
        import math as _m
        return round(round(px / tick) * tick, max(0, int(-_m.log10(tick)) + 1))

    entry, tp, sl = _q(entry), _q(tp), _q(sl)
    ref_price = float(price_ref or 0) or 0.0
    if ref_price <= 0:
        tick_data = ad.fetch_ticker(asset) or {}
        ref_price = float(tick_data.get("mark_price") or tick_data.get("last") or 0)
    if ref_price <= 0:
        return _fail("price", f"{venue.upper()} {asset} 现价不可得，禁止盲单", venue=venue)

    notional = margin * leverage
    contracts = ad.quote_qty_to_native(notional, ref_price, spec)
    if contracts <= 0:
        # 文案只说**钱**：合约面值各币种不同（BTC 一张 0.01 币、XRP 一张 100 币），
        # 报"张"用户无法判断规模。最小下单额用钱表示才是跨币种可比的。
        return _fail("sizing",
                     f"名义 {notional:.2f}U @ {ref_price:g} 不足 {venue.upper()} 最小下单额"
                     f"（该所最小可下单名义约 {spec.ct_val * ref_price:g}U）", venue=venue)
    side = "long" if action == "BUY_LONG" else "short"
    is_base_asset = getattr(ad.capabilities, "quantity_unit", "") == "base_asset"
    signed = contracts if (side == "long") else -contracts
    if not is_base_asset:
        signed = int(signed)
        contracts = int(contracts)

    # —— 外部持仓前置体检（US-009）：同合约存在来源不明/尺寸不符既有仓 = 连坐风险 ——
    # `_own_positions()` 已缓存（闸门算 max_open 时可能已探过），此处不会重复触网。
    # ⚠️ 该所池上限 `max_open` **已上移到共用闸门**（与 OKX 直签路径共用同一份判据）。
    try:
        _all_positions = _own_positions()
    except ExchangeCapabilityError:
        raise
    except Exception as exc:
        return _fail("precheck", f"{asset} 既有持仓探针失败，无法排除外部仓，拒开: {exc}", venue=venue)
    existing = [p for p in _all_positions
                if str(p.get("base") or "").upper() == asset
                and abs(float(p.get("size_signed") or 0)) > 1e-9]
    # 己仓归属对账：`own_position` 未传入时**不再默认宣称"外部仓"**（2026-09-20 实盘
    # 证据：UNI/binance 是台账 holding 行里的**本方**仓、ARB/binance 是**账实不符**
    # （交易所仍持有而台账该行已 closed），旧文案一律报成"外部仓连坐拒开"=说谎的诊断，
    # 会把运维引去找根本不存在的"外部仓"）。判定仍**全部拒开**（本刀不改交易行为），
    # 只是把"谁在持有/能不能判定"说清楚，并把判定放进入参供调用方与巡检消费。
    _own_ledger = None
    if not own_position:
        try:
            _own_ledger = own_position_records.load_ledger(
                OWN_POSITION_LEDGER_FILE or None)
        except Exception:
            _own_ledger = None            # 读不到 → 判"不可判定"，绝不判"外部仓"
    for p in existing:
        ex_signed = float(p.get("size_signed") or 0)
        ex_side = str(p.get("side") or "")
        if own_position:
            own_signed = float(own_position.get("size_signed") or 0)
            own_match = (abs(ex_signed - own_signed) < 1e-6
                         and str(own_position.get("side") or "").lower() == ex_side.lower())
            if own_match:
                continue
            return _fail("precheck",
                         f"{asset} 交易所既有仓与**调用方在管记录**不符 "
                         f"size_signed={ex_signed:g}({ex_side}) vs 记录 {own_signed:g}"
                         f"({own_position.get('side')})——拒开（记录漂移须先核对账实）",
                         venue=venue, existing_size=ex_signed, own_verdict="mismatch")
        try:
            _verdict = own_position_records.classify_exchange_position(
                venue=venue, asset=asset, size_signed=ex_signed, side=ex_side,
                ledger=_own_ledger)
        except Exception as exc:                      # 判定件异常 ≠ 外部仓
            _verdict = {"verdict": "ledger_unavailable",
                        "reason": f"己仓对账失败（{type(exc).__name__}）——归属**不可判定**"}
        _v = str(_verdict.get("verdict") or "untracked")
        _why = str(_verdict.get("reason") or "")
        if _v == "own":
            _detail = (f"{asset} **本方已在管该仓**（{_why}），交易所实况 "
                       f"size_signed={ex_signed:g}({ex_side})——本入口不重复开仓"
                       "（重复开仓=敞口翻倍）；加仓/减仓请走持仓管理路径")
        elif _v == "stale_closed":
            _detail = (f"{asset} **账实不符**（{_why}），交易所实况 "
                       f"size_signed={ex_signed:g}({ex_side})——拒开并须人工核对"
                       "（该敞口可能无人管理）")
        elif _v == "mismatch":
            _detail = (f"{asset} 本方记录与交易所不符（{_why}），交易所实况 "
                       f"size_signed={ex_signed:g}({ex_side})——拒开")
        else:
            _detail = (f"{asset} {_why}；交易所实况 size_signed={ex_signed:g}({ex_side})"
                       "——拒开（不宣称『外部仓』）")
        return _fail("precheck", _detail, venue=venue, existing_size=ex_signed,
                     own_verdict=_v)

    # 持仓模式只读体检（审计 §2 Gate 的既定政策，此前只有政策没有执行）：
    # 本系统**永不自动切换**用户账户的持仓模式；"测不出来"与"dual_plus 拆仓"
    # 一律**禁止新开仓并显示原因**（fail-closed）。dual 走 auto_size 载荷、
    # single 走 close=true 载荷，二者都是本仓已实现的形态，故受支持、放行。
    #
    # ⚠️ 2026-09-28 三所平权：**判据已上移到共用闸门** `_venue_entry_gate`（本函数上方），
    # 与 OKX 直签路径共用同一份 —— 此前只有 binance/Gate 有体检，OKX 完全没有。
    # 这里只保留 `position_mode` 的取值（下方载荷构造要用）。
    # ⚠️ 只在**该适配器真的实现了只读探测**时才体检。实测（DEMO）：Binance 声明的是
    # `('net','long_short')` 这套**不同词汇**、且没有探测方法 —— 若按"声明了就体检、
    # 探测不到就拒"处理，会**直接把币安新开仓全部停掉**（本刀实测拦下的自伤）。
    # 币安侧的持仓模式探测（dualSidePosition）是独立的一刀；在它有探测之前维持原行为。

    # 杠杆档位（失败即止，未下单无风险）；margin_mode 由账户实况推导，缺省 cross
    try:
        ad.set_leverage(asset, leverage, margin_mode=margin_mode or "cross")
    except ExchangeCapabilityError:
        raise
    except Exception as exc:
        return _fail("leverage", f"设置杠杆失败: {exc}", venue=venue)

    # Binance/Gate 发单前采集公开盘口证据；采集失败不阻断交易，字段保持 None。
    execution_evidence = None
    if venue in ("binance", "gate"):
        execution_evidence = {
            "schema_version": 1,
            "venue": venue,
            "evidence_type": "entry",
            "asset": asset,
            "position_side": side,
            "observed_at_ms": int(time.time() * 1000),
            "signal_price": float(entry) if entry and float(entry) > 0 else None,
            "book_status": "UNOBSERVED",
            "spread_bps": None,
            "bid_depth_5_usdt": None,
            "ask_depth_5_usdt": None,
            "estimated_slippage_bps": None,
            "protection_status": "PENDING",
            "protection_leg_ids": None,
        }
        try:
            book = ad.fetch_orderbook(asset, depth=5)
            bids = book.get("bids") if isinstance(book, dict) else None
            asks = book.get("asks") if isinstance(book, dict) else None
            if bids and asks:
                bid_rows = [(float(row[0]), float(row[1])) for row in bids[:5]]
                ask_rows = [(float(row[0]), float(row[1])) for row in asks[:5]]
                best_bid, best_ask = bid_rows[0][0], ask_rows[0][0]
                mid = (best_bid + best_ask) / 2
                if mid > 0:
                    execution_evidence.update({
                        "book_status": "OBSERVED",
                        "spread_bps": (best_ask - best_bid) / mid * 10000,
                        "bid_depth_5_usdt": sum(px * qty * float(spec.ct_val or 1.0) for px, qty in bid_rows),
                        "ask_depth_5_usdt": sum(px * qty * float(spec.ct_val or 1.0) for px, qty in ask_rows),
                    })
                    want_buy = action in ("BUY_LONG", "BUY", "LONG")
                    levels = ask_rows if want_buy else bid_rows
                    remaining = abs(float(contracts))
                    quote = 0.0
                    filled = 0.0
                    for px, qty in levels:
                        take = min(remaining, qty)
                        quote += take * px
                        filled += take
                        remaining -= take
                        if remaining <= 0:
                            break
                    if filled > 0 and remaining <= 0:
                        vwap = quote / filled
                        reference = best_ask if want_buy else best_bid
                        execution_evidence["estimated_slippage_bps"] = abs(vwap - reference) / reference * 10000
        except Exception as evidence_exc:
            print(f"[{venue} execution evidence] warn {asset} pre-entry book unavailable: {evidence_exc}")

    # This is the last write boundary for direct router callers. The normal
    # order-submit facade passes an existing claim; direct/manual callers get
    # one here rather than bypassing durable de-duplication.
    _entry_claim_id = str(decision.get("entry_claim_id") or "")
    _entry_client_order_id = ""
    _entry_claim_inst = str(decision.get("inst_id") or decision.get("instId") or asset)
    _entry_claim_side = "long" if action in ("BUY_LONG", "BUY", "LONG", "buy", "long") else "short"
    try:
        from scripts.trader.open_entry_guard import (
            claim_open_entry, begin_open_entry_submission, commit_open_entry,
            get_open_entry_claim, mark_open_entry_unknown, classify_entry_submission_exception,
            release_open_entry, validate_open_entry_claim,
        )
        _entry_env = str(decision.get("environment") or environment or "live")
        if _entry_claim_id:
            if not validate_open_entry_claim(
                    _entry_claim_id, environment=_entry_env,
                    inst_id=_entry_claim_inst, side=_entry_claim_side):
                return _fail("entry_claim", "调用方开仓 claim 无效或不属于该标的/方向",
                             venue=venue, submission_state="rejected_before_send")
        else:
            _claim_ok, _claim_reason, _entry_claim_id = claim_open_entry(
                environment=_entry_env, venue=venue,
                inst_id=_entry_claim_inst, side=_entry_claim_side)
            if not _claim_ok:
                return _fail("entry_claim", _claim_reason or "开仓防重拒绝",
                             venue=venue, submission_state="rejected_before_send")
        _claim_row = get_open_entry_claim(_entry_claim_id)
        _entry_client_order_id = str((_claim_row or {}).get("client_order_id") or "")
        if not _entry_client_order_id:
            return _fail("entry_claim", "持久化 claim 缺少 client order ID，拒绝无关联标识发单",
                         venue=venue, submission_state="rejected_before_send")
        try:
            import inspect
            _text_param = inspect.signature(ad.place_order).parameters.get("text")
            _supports_text = bool(_text_param and _text_param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY))
        except (TypeError, ValueError):
            _supports_text = False
        if not _supports_text:
            release_open_entry(_entry_claim_id, confirmed_not_sent=True)
            return _fail(
                "entry_claim",
                "场所适配器 place_order 不支持 client text，拒绝发送未关联的开仓单",
                venue=venue, submission_state="rejected_before_send")
        if not begin_open_entry_submission(_entry_claim_id):
            return _fail("entry_claim", "开仓防重 claim 已消费或无法持久化 dispatching，安全拒绝下单",
                         venue=venue, submission_state="rejected_before_send")
    except Exception as exc:
        return _fail("entry_claim", f"开仓防重状态不可用，安全拒绝下单: {exc}",
                     venue=venue, submission_state="rejected_before_send")

    # 入场单（按模式发市价单或限价单，系统默认市价单）
    order_mode = str(decision.get("order_mode") or os.getenv("ASTRA_ORDER_MODE", "market")).strip().lower()
    entry_px = None if order_mode == "market" else entry
    try:
        placed = ad.place_order(
            asset, side, abs(contracts), price=entry_px, text=_entry_client_order_id)
    except ExchangeCapabilityError:
        if _entry_claim_id:
            release_open_entry(_entry_claim_id, confirmed_not_sent=True)
        raise
    except Exception as exc:
        # A TypeError can originate inside an adapter after dispatch. Signature
        # compatibility is checked before invocation above; never infer that a
        # runtime TypeError proves the venue did not receive the request.
        _state = ("unknown_after_send" if isinstance(exc, TypeError)
                  else classify_entry_submission_exception(exc))
        if _state == "rejected_before_send" and _entry_claim_id:
            release_open_entry(_entry_claim_id, confirmed_not_sent=True)
        elif _entry_claim_id:
            mark_open_entry_unknown(_entry_claim_id, str(exc))
        return _fail("entry", f"入场委托提交失败: {exc}", venue=venue,
                     submission_state=_state, entry_claim_id=_entry_claim_id)

    # ``text`` is a client tag, not a server order identity. Gate accepts
    # id_string/id; Binance exposes orderId through the adapter's id/order_id.
    # An empty identity is unknown, never a synthetic ``venue-ok`` success.
    order_id = ""
    if isinstance(placed, dict):
        if venue == "gate":
            order_id = str(placed.get("id_string") or placed.get("id") or "").strip()
        else:
            order_id = str(placed.get("id") or placed.get("order_id") or "").strip()
    if not order_id:
        mark_open_entry_unknown(_entry_claim_id, "exchange response without server order id")
        return _fail("entry", "交易所回执缺少可核验服务端订单号，等待对账恢复",
                     venue=venue, submission_state="unknown_after_send",
                     entry_claim_id=_entry_claim_id)
    if _entry_claim_id:
        # Commit before protection attachment: an accepted entry must block a
        # duplicate even if protection setup later fails.
        commit_open_entry(_entry_claim_id, order_id)

    # 云端 TP/SL 双腿 + 回读验证；任何缺口撤入场单回滚
    legs: dict = {}
    try:
        try:
            # 只有**真的探测到模式**时才多传这个 kwarg：sandbox 等适配器没有
            # `**kwargs`，无条件传会 TypeError（本刀实测拦下的一处潜在炸点）。
            _mode_kwargs = ({"position_mode": _own_position_mode()}
                            if _mode_checked else {})
            legs = ad.attach_protective_orders(asset, side, tp_px=tp, sl_px=sl,
                                               expiration=trigger_expiration,
                                               contracts=contracts, **_mode_kwargs)
        except TypeError:
            legs = ad.attach_protective_orders(asset, side, tp_px=tp, sl_px=sl,
                                               expiration=trigger_expiration)
        open_orders = ad.list_protective_orders(asset)
        open_ids = {str(o.get("id") or o.get("algo_id")) for o in open_orders if isinstance(o, dict)}
        if str(legs.get("tp")) not in open_ids or str(legs.get("sl")) not in open_ids:
            raise RuntimeError("回读未见双腿触发单")
    except Exception as exc:
        # 审计④#9(2026-09-13)：铁律「任一步失败→已挂触发单回滚+撤入场单」旧实现只
        # 撤入场单——tp 挂成、sl 失败时 tp 孤儿遗留至 expiration（无仓挂保护单不可对账）。
        # 现双段清理：①已知 legs 逐腿 best-effort 撤；②枚举该资产残留触发单，只撤
        # 带 astra 前缀的本系统单（用户手动保护单绝不触碰）；枚举失败如实标注。
        rollback_notes = []
        rollback_target_keys = set()
        try:
            from scripts.trader.protection_identity import (
                extract_protection_identities, protection_identity_key,
            )
            from scripts.trader.venue_protection import (
                _ownership_evidence, cancel_protective_leg,
            )
        except ImportError:
            from trader.protection_identity import (
                extract_protection_identities, protection_identity_key,
            )
            from trader.venue_protection import (
                _ownership_evidence, cancel_protective_leg,
            )

        def _rollback_cancel_identity(identity):
            key = protection_identity_key(identity)
            if key is None:
                return False
            ok = cancel_protective_leg(
                ad, identity["identity_value"], identity=identity, venue=venue)
            if ok:
                rollback_target_keys.add(key)
            return ok

        def _rollback_route_identity(value):
            identity_kind = "id_string" if venue == "gate" else (
                "algo_id" if venue == "binance" else "")
            family = "price_order" if venue == "gate" else (
                "algo_order" if venue == "binance" else "")
            if not identity_kind or not family:
                return False
            return _rollback_cancel_identity({
                "venue": venue, "resource_family": family,
                "identity_kind": identity_kind, "identity_value": str(value),
            })

        for _leg in ("tp", "sl"):
            _lid = str((legs or {}).get(_leg) or "")
            if not _lid or _lid in ("None", ""):
                continue
            try:
                if not _rollback_route_identity(_lid):
                    rollback_notes.append(
                        f"{_leg.upper()}腿 {_lid} 未获条件单接口明确撤销确认")
            except Exception as leg_exc:
                rollback_notes.append(f"{_leg.upper()}腿 {_lid} 撤销失败({leg_exc})")

        try:
            residue = ad.list_protective_orders(asset)
            if not isinstance(residue, (list, tuple)):
                raise RuntimeError(f"条件单列表结构异常: {type(residue).__name__}")
            unproven_count = 0
            unrouteable_count = 0
            seen_residue_keys = set()
            for row in residue:
                if not isinstance(row, dict):
                    unrouteable_count += 1
                    continue
                if _ownership_evidence(row) != "tag":
                    unproven_count += 1
                    continue  # 归属未证明的（包括人工单）只报告，不撤销
                identities = extract_protection_identities(
                    row, venue=venue, ownership_evidence="tag")
                if venue == "gate":
                    identity = (next((x for x in identities
                                      if x["identity_kind"] == "id_string"), None)
                                or next((x for x in identities
                                         if x["identity_kind"] == "id"), None))
                elif venue == "binance":
                    identity = (next((x for x in identities
                                      if x["identity_kind"] == "algo_id"), None)
                                or next((x for x in identities
                                         if x["identity_kind"] == "client_algo_id"), None))
                else:
                    identity = None
                key = protection_identity_key(identity) if identity else None
                if identity is None or key is None:
                    unrouteable_count += 1
                    continue
                if key in seen_residue_keys:
                    continue
                seen_residue_keys.add(key)
                try:
                    if not _rollback_cancel_identity(identity):
                        rollback_notes.append(
                            f"带 ASTRA 标记的孤儿条件单 {identity['identity_value']} "
                            "未获专用接口明确撤销确认")
                except Exception as rexc:
                    rollback_notes.append(
                        f"带 ASTRA 标记的孤儿条件单 {identity['identity_value']} "
                        f"撤销失败({rexc})")
            if unproven_count:
                rollback_notes.append(
                    f"保留并报告 {unproven_count} 条归属未证明的保护单（不执行撤销）")
            if unrouteable_count:
                rollback_notes.append(
                    f"保留并报告 {unrouteable_count} 条缺少可路由 typed identity 的保护记录")

            # Explicit cancel acknowledgement is not enough: relist the exact
            # typed identities. Any unreadable or still-live result is disclosed.
            remaining = ad.list_protective_orders(asset)
            if not isinstance(remaining, (list, tuple)):
                raise RuntimeError(f"撤后条件单回读结构异常: {type(remaining).__name__}")
            remaining_keys = {
                protection_identity_key(identity)
                for row in remaining if isinstance(row, dict)
                for identity in extract_protection_identities(row, venue=venue)
                if protection_identity_key(identity) is not None
            }
            still_present = rollback_target_keys & remaining_keys
            if still_present:
                rollback_notes.append(
                    f"撤后回读仍见 {len(still_present)} 条本次目标 typed identity")
        except Exception as lexc:
            rollback_notes.append(
                f"孤儿触发单清理/撤后核验未完成({lexc})——未证明归属的单保持不动")
        try:
            ad.cancel_order(asset, order_id)
            rollback_notes.append("入场单已撤销")
        except Exception as cexc:
            rollback_notes.append(f"入场单撤销失败({cexc})——交易所侧 OCO/手动兜底")
        detail = f"保护单覆盖失败: {exc}"
        if rollback_notes:
            detail += "；回滚记录: " + "；".join(rollback_notes)
        return _fail("protective", detail, venue=venue, order_id=order_id,
                     entry_claim_id=_entry_claim_id,
                     submission_state="accepted_with_id")

    if execution_evidence is not None:
        execution_evidence["protection_status"] = "VERIFIED"
        execution_evidence["protection_leg_ids"] = {"take_profit": str(legs.get("tp") or ""),
                                                    "stop_loss": str(legs.get("sl") or "")}
        execution_evidence["entry_order_id"] = order_id or None
        execution_evidence["entry_order_status"] = str(placed.get("status") or "UNKNOWN")
        execution_evidence["entry_order_filled_qty"] = float(placed.get("executedQty") or 0.0)

    return RouteResult(ok=True, venue=venue, stage="done",
                       submission_state="accepted_with_id",
                       asset=asset, action=action, order_id=order_id,
                       tp_id=str(legs.get("tp")), sl_id=str(legs.get("sl")),
                       size_signed=signed,
                       contracts=contracts, notional_usdt=round(notional, 2),
                       margin_usdt=round(margin, 4),
                       margin_clamped_from_usdt=margin_clamped_from or None,
                       ref_price=ref_price, rr=round(rr, 3), leverage=leverage,
                       execution_evidence=execution_evidence,
                       detail=f"{venue.upper()} 入场限价挂单 + TP/SL 双腿云端触发单已回读验证")


def _position_row_side(row: Dict[str, Any]) -> str:
    """Return a normalized long/short side from an adapter position row."""
    side = str(row.get("side") or row.get("pos_side") or row.get("posSide") or "").strip().lower()
    if side in {"long", "short"}:
        return side
    try:
        amount = float(row.get("size_signed") if row.get("size_signed") is not None
                       else row.get("pos") or 0)
    except (TypeError, ValueError):
        return ""
    return "long" if amount > 0 else "short" if amount < 0 else ""


def _verify_position_side_flat(ad: Any, base: str,
                               pos_side: Optional[str] = None) -> tuple:
    """Poll until the requested direction is flat: ``(flat, remaining)``.

    A Binance/Gate hedge account can have the opposite direction open while the
    target close is already complete.  Whole-symbol flatness is therefore only
    the right predicate when no target side is supplied; using it for every
    close leaves the just-closed side's own protection legs behind forever.
    """
    want = str(base or "").upper()
    requested = str(pos_side or "").strip().lower()
    if requested not in {"long", "short"}:
        requested = ""
    remaining = 0.0
    for _ in range(max(1, int(CLOSE_FLAT_POLL_TRIES))):
        try:
            rows = ad.positions() or []
        except Exception:
            time.sleep(CLOSE_FLAT_POLL_SLEEP)
            continue
        remaining = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("base") or "").upper() != want:
                continue
            row_side = _position_row_side(row)
            if requested and row_side and row_side != requested:
                continue
            try:
                amount = abs(float(row.get("size_signed") or row.get("pos") or 0))
            except (TypeError, ValueError):
                # A matching row whose amount cannot be parsed is still live.
                amount = 1.0
            # With a target direction, a non-zero row whose direction is
            # unreadable cannot safely be treated as the opposite hedge leg.
            # Fail closed rather than claiming target flatness from ambiguity.
            if requested and not row_side and amount > 1e-9:
                amount = max(amount, 1.0)
            remaining = max(remaining, amount)
        if remaining <= 1e-9:
            return True, 0.0
        time.sleep(CLOSE_FLAT_POLL_SLEEP)
    return False, remaining


def _verify_symbol_flat(ad: Any, base: str) -> tuple:
    """轮询核验该合约**整体归零**（任何方向都没有仓）→ `(flat, 剩余量)`。"""
    return _verify_position_side_flat(ad, base, None)


def _read_symbol_position(ad: Any, base: str, pos_side: Optional[str] = None) -> Dict[str, Any]:
    """平仓前抓该合约的仓位事实 → `{"base", "side", "size_signed"}`（读不到则退化为只有 base/side）。

    退化不是失败：Gate 腿带 `t-astra` 标签，靠标签即可归因；Binance 腿没标签，
    退化后就只能"归属不可判定"⇒ **不撤**（保守，符合"不撤用户手单"的铁律）。
    """
    want = str(base or "").upper()
    out: Dict[str, Any] = {"base": want}
    if pos_side:
        out["side"] = str(pos_side).strip().lower()
    try:
        for r in (ad.positions() or []):
            if not isinstance(r, dict):
                continue
            if str(r.get("base") or "").upper() != want:
                continue
            out["size_signed"] = r.get("size_signed")
            if r.get("side"):
                out["side"] = str(r.get("side")).lower()
            requested = str(pos_side or "").strip().lower()
            if requested not in {"long", "short"} or str(out.get("side") or "").lower() == requested:
                break
    except Exception:
        pass
    return out


def _cancel_result_error(result: Any) -> str:
    """Require an explicit successful cancellation acknowledgement."""
    if not isinstance(result, dict) or not result:
        return f"ambiguous_response={type(result).__name__}"
    for key in ("error", "error_code"):
        value = result.get(key)
        if value not in (None, "", 0, "0"):
            return f"{key}={value}"
    # Binance uses code=0; OKX-style adapters may expose sCode=0.  A zero code
    # is an explicit exchange success, unlike a missing code.
    for key in ("code", "sCode"):
        if key in result:
            value = result.get(key)
            # Binance USD-M `/fapi/v1/algoOrder` documents `code: "200"`
            # for a successful cancel, while OKX uses `sCode: "0"` and
            # some adapters expose numeric zero.  These are explicit success
            # codes; an absent code remains ambiguous below.
            if value in (0, "0") or (key == "code" and value in (200, "200")):
                return ""
            if value not in (None, ""):
                return f"{key}={value}"
    if result.get("ok") is True or result.get("success") is True:
        return ""
    status = str(result.get("status") or result.get("algoStatus") or "").strip().lower()
    if status in {"canceled", "cancelled", "closed", "finished", "expired"}:
        return ""
    return "ambiguous_response=no_explicit_success"


def _cancel_proven_own_legs(ad: Any, base: str,
                            before_position: Optional[Dict[str, Any]] = None,
                            *, venue: Optional[str] = None,
                            only_identities: Optional[set[str]] = None
                            ) -> ProtectionCleanupResult:
    """Cancel only exchange-tagged target legs and verify disappearance by relisting."""
    from .execution import own_records as _own
    try:
        from scripts.trader.venue_protection import (
            _ownership_evidence, leg_base, select_legs_to_cancel_after_close,
        )
    except ImportError:
        from trader.venue_protection import (
            _ownership_evidence, leg_base, select_legs_to_cancel_after_close,
        )

    venue = str(venue or getattr(getattr(ad, "capabilities", None), "venue", "") or
                getattr(ad, "venue", "") or "").strip().lower()
    list_fn = getattr(ad, "list_protective_orders", None)
    if not callable(list_fn):
        applicable = venue in {"binance", "gate"}
        return ProtectionCleanupResult(
            "；保护腿清理未执行（条件单资源适用但适配器未暴露列表能力）" if applicable
            else "；保护腿清理未执行（适配器无可识别条件单资源）",
            ok=not applicable, attempted=False, applicable=applicable,
            verified=not applicable,
            failures=(("缺少条件单列表能力",) if applicable else ()))

    try:
        rows = list_fn(base)
        if not isinstance(rows, (list, tuple)):
            raise RuntimeError(f"条件单列表结构异常: {type(rows).__name__}")
        legs = list(rows)
    except Exception as exc:
        return ProtectionCleanupResult(
            f"；保护腿清单读取失败（{type(exc).__name__}: {str(exc)[:100]}），需人工核对",
            ok=False, applicable=True, verified=False,
            failures=("保护腿清单读取失败",))

    try:
        expected_base = canonical_base(base)
        symbol_legs = [row for row in legs if isinstance(row, dict) and
                       canonical_base(leg_base(row)) == expected_base]
        own_position = dict(before_position or {"base": base})
        try:
            ledger = _own.read_ledger_rows(_own.load_ledger(OWN_POSITION_LEDGER_FILE or None))
        except Exception:
            ledger = None
        sel = select_legs_to_cancel_after_close(own_position, symbol_legs, ledger,
                                                 venue=venue)
        if only_identities is not None:
            try:
                from scripts.trader.protection_identity import (
                    extract_protection_identities,
                    normalize_typed_identity,
                    protection_identity_key,
                )
            except ImportError:
                from trader.protection_identity import (
                    extract_protection_identities,
                    normalize_typed_identity,
                    protection_identity_key,
                )

            requested = list(only_identities) if not isinstance(only_identities, str) \
                else [only_identities]
            allowed_keys = set()
            legacy_tokens = set()
            for value in requested:
                typed = normalize_typed_identity(value)
                if typed is not None:
                    allowed_keys.add(protection_identity_key(typed))
                elif isinstance(value, str) and value.strip():
                    legacy_tokens.add(value.strip())

            # Legacy records are opaque tokens. Upgrade one only when exactly
            # one typed identity on exactly one live ASTRA-tagged row matches.
            legacy_ambiguities = []
            for token in legacy_tokens:
                candidates = {}
                for row in symbol_legs:
                    if not isinstance(row, dict) or _ownership_evidence(row) != "tag":
                        continue
                    for identity in extract_protection_identities(row, venue=venue,
                                                                  ownership_evidence="tag"):
                        if identity["identity_value"] == token:
                            key = protection_identity_key(identity)
                            if key is not None:
                                candidates.setdefault(key, []).append(row)
                if len(candidates) == 1 and len(next(iter(candidates.values()))) == 1:
                    allowed_keys.update(candidates)
                else:
                    legacy_ambiguities.append({"legacy_token": token,
                                               "candidate_count": sum(len(v) for v in candidates.values()),
                                               "why": "旧身份无法唯一升级，保留并报告"})

            def _identity_keys(item):
                if not isinstance(item, dict):
                    return set()
                out = set()
                for identity in (item.get("protection_identities") or []):
                    normalized = normalize_typed_identity(identity)
                    key = protection_identity_key(normalized) if normalized else None
                    if key is not None:
                        out.add(key)
                return out

            sel["to_cancel"] = [item for item in sel.get("to_cancel", [])
                                if _identity_keys(item) & allowed_keys]
            sel["ids"] = [str(item.get("id") or "") for item in sel["to_cancel"]
                           if str(item.get("id") or "")]
            sel["legacy_identity_ambiguities"] = legacy_ambiguities
            # Scale-out attaches replacement protection before this helper runs.
            # Any old snapshot identity that no longer qualifies as a cleanup
            # candidate is disclosed only if its exact typed key still matches.
            for bucket, items in list((sel.get("not_touched") or {}).items()):
                sel["not_touched"][bucket] = [
                    item for item in (items or [])
                    if _identity_keys(item) & allowed_keys]
    except Exception as exc:
        return ProtectionCleanupResult(
            f"；保护腿归属判定失败（{type(exc).__name__}: {str(exc)[:100]}）",
            ok=False, applicable=True, verified=False,
            failures=("保护腿归属判定失败",))

    try:
        from scripts.trader.protection_identity import (
            extract_protection_identities, normalize_typed_identity,
            protection_identity_key,
        )
    except ImportError:
        from trader.protection_identity import (
            extract_protection_identities, normalize_typed_identity,
            protection_identity_key,
        )

    cancel_price = getattr(ad, "cancel_price_order", None)
    cancel_algo = getattr(ad, "cancel_algo_order", None)
    def _selected_identity(item):
        records = [normalize_typed_identity(x)
                   for x in (item.get("protection_identities") or [])]
        records = [x for x in records if x and x["venue"] == venue]
        if venue == "gate":
            return (next((x for x in records if x["identity_kind"] == "id_string"), None)
                    or next((x for x in records if x["identity_kind"] == "id"), None))
        if venue == "binance":
            return (next((x for x in records if x["identity_kind"] == "algo_id"), None)
                    or next((x for x in records if x["identity_kind"] == "client_algo_id"), None))
        return None

    if any(_selected_identity(item) is None for item in sel.get("to_cancel", [])
           if isinstance(item, dict)):
        return ProtectionCleanupResult(
            "；保护腿清理未执行（缺少可路由的 typed identity，保守保留）",
            ok=False, attempted=False, applicable=True, verified=False,
            failures=("缺少可路由的 typed identity",))
    if sel.get("to_cancel") and (
            (venue == "gate" and not callable(cancel_price))
            or (venue == "binance" and not callable(cancel_algo))
            or venue not in {"gate", "binance"}):
        return ProtectionCleanupResult("；保护腿清理未执行（缺少匹配资源族的逐腿撤单能力）",
                                       ok=False, attempted=False, applicable=True,
                                       verified=False, failures=("缺少匹配资源族的逐腿撤单能力",))

    # A successful list is itself an attempted cleanup audit.  If there are no
    # target legs, the result is verified complete without issuing DELETE.
    attempted = True
    requested = []
    failures: List[str] = []
    for item in sel.get("to_cancel", []):
        if not isinstance(item, dict):
            failures.append("撤单目标结构异常")
            continue
        identity_record = _selected_identity(item)
        if identity_record is None:
            failures.append("缺少 typed conditional-order identity")
            continue
        identity_value = identity_record["identity_value"]
        try:
            if identity_record["venue"] == "gate" and identity_record["resource_family"] == "price_order":
                response = cancel_price(identity_value)
            elif identity_record["venue"] == "binance" and identity_record["resource_family"] == "algo_order":
                if identity_record["identity_kind"] == "algo_id":
                    response = cancel_algo(algo_id=identity_value)
                elif identity_record["identity_kind"] == "client_algo_id":
                    response = cancel_algo(client_algo_id=identity_value)
                else:
                    raise RuntimeError("Binance algoOrder identity kind is unsupported")
            else:
                raise RuntimeError("typed identity resource family does not match the venue")
            response_error = _cancel_result_error(response)
            if response_error:
                raise RuntimeError(response_error)
            typed_key = protection_identity_key(identity_record)
            requested.append((typed_key, identity_record, str(item.get("symbol") or base)))
        except Exception as exc:
            failures.append(f"{identity_value}: {exc}")

    verified_ids = set()
    relist_error = ""
    if requested:
        try:
            after = list_fn(base)
            if not isinstance(after, (list, tuple)):
                raise RuntimeError(f"撤后列表结构异常: {type(after).__name__}")
            for row in after:
                if not isinstance(row, dict) or canonical_base(leg_base(row)) != canonical_base(base):
                    continue
                for identity in extract_protection_identities(row, venue=venue):
                    key = protection_identity_key(identity)
                    if key is not None:
                        verified_ids.add(key)
        except Exception as exc:
            relist_error = f"撤后复查失败: {type(exc).__name__}: {str(exc)[:100]}"

    still_open = [identity["identity_value"] for key, identity, _symbol in requested
                  if relist_error or key in verified_ids]
    if relist_error:
        failures.append(relist_error)
    if still_open:
        failures.append(f"撤单后仍在列表或无法确认 {len(still_open)} 张")

    not_touched = sel.get("not_touched") or {}
    # These buckets are deliberately retained, not failed: no exchange-side
    # ownership proof means we must not issue DELETE.  Keep the complete leg
    # records machine-readable so callers can disclose/manual-review them
    # without turning a safe refusal into a false cleanup failure.
    report_only = tuple(
        dict(leg, bucket=bucket)
        for bucket in (
            "matched_unattributed", "size_mismatch", "orphan_attributed_ledger",
            "orphan_unattributed", "unparsed", "side_mismatch", "foreign",
            "ambiguous_position", "duplicate_identities",
        )
        for leg in (not_touched.get(bucket) or [])
        if isinstance(leg, dict)
    ) + tuple(
        dict(leg, bucket="legacy_identity_ambiguity")
        for leg in (sel.get("legacy_identity_ambiguities") or [])
        if isinstance(leg, dict)
    )
    unresolved_buckets = (
        "matched_unattributed", "size_mismatch", "orphan_attributed_ledger",
        "orphan_unattributed", "unparsed", "ambiguous_position", "duplicate_identities",
    )
    unresolved = sum(len(not_touched.get(key) or []) for key in unresolved_buckets)
    side_mismatch = len(not_touched.get("side_mismatch") or [])
    done_count = len(requested) - len(still_open)
    note = f"；保护腿已核验撤除 {done_count} 张"
    if unresolved:
        note += f"；另有 {unresolved} 张归属/方向未确认，保留并需人工核对"
    if side_mismatch:
        # The opposite hedge direction is intentionally preserved.  It is not
        # a cleanup failure for the requested side, but omitting it from the
        # result makes a successful close look like a symbol-wide cancellation.
        note += f"；另有 {side_mismatch} 张方向不匹配保护腿保留未撤"
    if report_only and not unresolved and not side_mismatch:
        note += f"；另有 {len(report_only)} 张未获撤销授权，保留并报告"
    if still_open:
        note += f"；撤后仍存在/未核实 {len(still_open)} 张"
    if failures:
        note += "；清理未完成: " + "；".join(failures)
    return ProtectionCleanupResult(note, ok=not failures, attempted=attempted,
                                   applicable=True, verified=not failures,
                                   failures=tuple(failures), report_only=report_only)

def reduce_only_position(symbol: str, quantity: float, *, venue: str = "gate",
                         pos_side: Optional[str] = None, adapter: Any = None,
                         environment: Optional[str] = None) -> RouteResult:
    """Submit a bounded, exchange-native reduce-only market order.

    This is the protective execution boundary for Fast Decision.  It deliberately
    does not use ``fast_close_position``: REDUCE_ONLY must carry an explicit
    quantity and must never silently become a whole-position close.  The venue
    adapter is the only object allowed to serialize the native request; this
    router performs the execution gate, live-position check, side selection, and
    quantity bound before delegating.
    """
    v = str(venue or getattr(getattr(adapter, "capabilities", None), "venue", "gate") or "gate").lower()
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return _fail("reduce_only_validate", "减仓数量必须是有限正数", venue=v)
    if not math.isfinite(qty) or qty <= 0:
        return _fail("reduce_only_validate", "减仓数量必须是有限正数", venue=v)

    try:
        ad = adapter or get_adapter(v, environment=environment)
        env_name = str(environment or getattr(ad, "environment", "live") or "live")
        # Existing-position protection is maintenance, not a new-entry request.
        # The adapter capability check remains mandatory.
        require_execution(v, environment=env_name, maintenance=True)
        native = ad.native_symbol(canonical_base(symbol))
        rows = [row for row in (ad.positions() or []) if isinstance(row, dict)]
        wanted_side = str(pos_side or "").strip().lower()

        def row_symbol(row: Dict[str, Any]) -> str:
            return str(row.get("inst_id") or row.get("instId") or row.get("symbol") or "")

        def row_side(row: Dict[str, Any]) -> str:
            side = str(row.get("side") or row.get("posSide") or "").strip().lower()
            if side in {"long", "short"}:
                return side
            try:
                return "long" if float(row.get("size_signed") or row.get("pos") or 0) > 0 else "short"
            except (TypeError, ValueError):
                return ""

        matches = [
            row for row in rows
            if (row_symbol(row) == native or canonical_base(row_symbol(row)) == canonical_base(symbol))
            and (not wanted_side or wanted_side == "net" or row_side(row) == wanted_side)
        ]
        if len(matches) != 1:
            return _fail(
                "reduce_only_validate",
                "无法唯一确定目标持仓，拒绝减仓",
                venue=v,
                asset=native,
            )
        target = matches[0]
        try:
            current = abs(float(target.get("size_signed") or target.get("pos") or target.get("amount") or 0))
        except (TypeError, ValueError):
            current = 0.0
        if not math.isfinite(current) or current <= 0 or qty > current + max(1e-12, current * 1e-9):
            return _fail(
                "reduce_only_validate",
                f"减仓数量 {qty:g} 超过当前持仓 {current:g}",
                venue=v,
                asset=native,
            )

        # Gate's currently verified path accepts integer contracts only.  Never
        # let its adapter round a Fast Decision quantity into an unintended size.
        if v == "gate" and not bool(getattr(ad.capabilities, "decimal_amount", False)):
            if abs(qty - round(qty)) > 1e-9:
                return _fail(
                    "reduce_only_validate",
                    "Gate 当前能力未验证小数张减仓，拒绝隐式取整",
                    venue=v,
                    asset=native,
                )

        side = row_side(target)
        if side not in {"long", "short"}:
            return _fail("reduce_only_validate", "持仓方向无法确定，拒绝减仓", venue=v, asset=native)
        close_side = "sell" if side == "long" else "buy"
        raw = target.get("raw") if isinstance(target.get("raw"), dict) else {}

        if v == "okx":
            raw_pos_side = str(raw.get("posSide") or "").strip().lower()
            api_pos_side = raw_pos_side if raw_pos_side in {"long", "short"} else "net"
            result = ad.place_order(
                canonical_base(symbol), close_side, qty, price=None,
                order_type="market", pos_side=api_pos_side, reduce_only=True,
            )
        elif v == "binance":
            hedge_side = str(raw.get("positionSide") or "").strip().upper()
            if hedge_side in {"LONG", "SHORT"}:
                result = ad.place_order(
                    canonical_base(symbol), close_side, qty, price=None,
                    position_side=hedge_side,
                )
            else:
                result = ad.place_order(
                    canonical_base(symbol), close_side, qty, price=None,
                    reduce_only=True,
                )
        else:
            result = ad.place_order(
                canonical_base(symbol), close_side, qty, price=None,
                reduce_only=True,
            )
        if isinstance(result, dict) and result.get("closed") is False:
            return _fail("reduce_only_submit", str(result.get("reason") or result), venue=v, asset=native)
        return RouteResult(
            ok=True,
            venue=v,
            stage="reduce_only_submitted",
            asset=native,
            quantity=qty,
            reduce_only=True,
            position_side=side,
            detail=f"{v.upper()} reduce-only market order submitted",
            raw=result,
        )
    except ExchangeCapabilityError as exc:
        return _fail("reduce_only_gate", str(exc), venue=v)
    except Exception as exc:
        return _fail("reduce_only_submit", f"{v.upper()} reduce-only 下单失败: {exc}", venue=v)


def close_position(symbol: str, *, venue: str = "gate", adapter: Any = None,
                   environment: Optional[str] = None, pos_side: Optional[str] = None) -> RouteResult:
    """市价全平（close=true + ioc），依赖同前：开闸 + 凭证。"""
    v = str(venue or getattr(getattr(adapter, "capabilities", None), "venue", "gate") or "gate").lower()
    if environment and is_sandbox_environment(environment) and v == "gate" and environment != "sandbox":
        environment = "sandbox"
    ad = adapter or get_adapter(v, environment=environment)
    require_execution(v, environment=str(getattr(ad, "environment", "live") or "live"))
    asset = canonical_base(symbol)
    # 平仓**前**抓一份待平仓的事实：归属判定要用它的量/方向，而平完就读不到了
    # （本刀实测踩到：平完再读只剩空仓 ⇒ matched 判不出来 ⇒ 一张腿都没撤）。
    _before_position = _read_symbol_position(ad, asset, pos_side)
    close_evidence = None
    if v in ("binance", "gate"):
        close_evidence = {
            "schema_version": 1,
            "venue": v,
            "evidence_type": "close",
            "asset": asset,
            "position_side": str((_before_position or {}).get("side") or "").lower(),
            "observed_at_ms": int(time.time() * 1000),
            "book_status": "UNOBSERVED",
            "close_reference_price": None,
            "close_side": None,
            "close_spread_bps": None,
            "close_bid_depth_5_usdt": None,
            "close_ask_depth_5_usdt": None,
        }
        try:
            pos_side_actual = str((_before_position or {}).get("side") or "").lower()
            close_side = "SELL" if pos_side_actual == "long" else "BUY" if pos_side_actual == "short" else ""
            book = ad.fetch_orderbook(asset, depth=5)
            bids = book.get("bids") if isinstance(book, dict) else None
            asks = book.get("asks") if isinstance(book, dict) else None
            if close_side and bids and asks:
                bid_rows = [(float(row[0]), float(row[1])) for row in bids[:5]]
                ask_rows = [(float(row[0]), float(row[1])) for row in asks[:5]]
                best_bid, best_ask = bid_rows[0][0], ask_rows[0][0]
                mid = (best_bid + best_ask) / 2
                if mid > 0:
                    close_evidence.update({
                        "book_status": "OBSERVED",
                        "close_side": close_side,
                        "close_reference_price": best_bid if close_side == "SELL" else best_ask,
                        "close_spread_bps": (best_ask - best_bid) / mid * 10000,
                        "close_bid_depth_5_usdt": sum(px * qty for px, qty in bid_rows),
                        "close_ask_depth_5_usdt": sum(px * qty for px, qty in ask_rows),
                    })
        except Exception as evidence_exc:
            print(f"[binance execution evidence] warn {asset} pre-close book unavailable: {evidence_exc}")
    try:
        kwargs: Dict[str, Any] = {}
        try:
            import inspect
            if "pos_side" in inspect.signature(ad.fast_close_position).parameters and pos_side:
                kwargs["pos_side"] = str(pos_side)
        except (TypeError, ValueError):
            pass
        data = ad.fast_close_position(asset, **kwargs)
    except ExchangeCapabilityError:
        raise
    except Exception as exc:
        return _fail("close", f"{v.upper()} 平仓失败: {exc}", venue=v, asset=asset)
    # 审计 B1：适配器明示未平（closed:False，如无持仓/双向歧义/非整数张数）绝不
    # 换算成功——旧实现只查异常，把 {"closed": False} 也报成 ok=True。
    if isinstance(data, dict) and data.get("closed") is False:
        return _fail("close", f"{v.upper()} 平仓未受理: {data.get('reason') or data}", venue=v, asset=asset)
    if close_evidence is not None:
        evidence_data = data if isinstance(data, dict) else {}
        close_evidence["order_id"] = str(evidence_data.get("order_id") or evidence_data.get("id") or "") or None
        close_evidence["order_status"] = str(evidence_data.get("status") or "UNKNOWN")
        if close_evidence.get("order_id"):
            try:
                from scripts.trader.execution_evidence import persist_venue_execution_evidence
                persist_venue_execution_evidence(close_evidence)
            except Exception as evidence_exc:
                print(f"[binance execution evidence] warn close persist skipped: {evidence_exc}")

    # ── 平仓后收尾：核验**目标方向**归零 → 撤掉可证明属于本系统的保护腿 ─────
    # 对冲模式允许另一方向继续持仓；此时不能用整合约归零作为前置，否则刚
    # 关闭方向的 TP/SL 会永久遗留。选择器仍按平仓前的方向/数量证据区分，
    # 因而另一方向的保护腿只报告、不触碰。
    note = ""
    cleanup_ok = True
    cleanup_status = "disabled"
    if CANCEL_STALE_PROTECTION_ON_CLOSE and v != "okx":
        requested_side = str(pos_side or "").strip().lower()
        if requested_side not in {"long", "short"}:
            requested_side = str((_before_position or {}).get("side") or "").strip().lower()
            if requested_side not in {"long", "short"}:
                requested_side = ""
        try:
            if pos_side:
                flat, remaining_target = _verify_position_side_flat(ad, asset, requested_side)
            else:
                # Legacy callers that omit pos_side only reach here after the
                # adapter accepted an unambiguous single-position close; retain
                # the stronger whole-symbol check for that legacy contract.
                flat, remaining_target = _verify_symbol_flat(ad, asset)
            if not flat:
                note = ("；未核验归零（目标方向仍有 %.6g），保护腿保持不动" % remaining_target
                        if remaining_target
                        else "；未核验归零（目标方向持仓读不到），保护腿保持不动")
                return _fail("close_verify", f"{v.upper()} 平仓已提交但未确认目标仓位归零: {note}",
                             venue=v, asset=asset, raw=data, cleanup_ok=False)
            cleanup = _cancel_proven_own_legs(ad, asset, _before_position, venue=v)
            note = str(cleanup)
            cleanup_ok = bool(getattr(cleanup, "ok", False)) and (
                not bool(getattr(cleanup, "applicable", True)) or
                bool(getattr(cleanup, "verified", False)))
            cleanup_status = ("complete" if cleanup_ok else
                              ("not_applicable" if not getattr(cleanup, "applicable", True)
                               else "incomplete"))
            if not cleanup_ok:
                return _fail("close_cleanup",
                             f"{v.upper()} 仓位已归零但条件单清理未完成: {note}",
                             venue=v, asset=asset, raw=data, cleanup_ok=False,
                             protection_cleanup=note)
        except Exception as exc:
            note = f"；保护腿收尾异常（{type(exc).__name__}: {str(exc)[:100]}），需人工核对"
            return _fail("close_cleanup",
                         f"{v.upper()} 仓位已归零但条件单清理未完成: {note}",
                         venue=v, asset=asset, raw=data, cleanup_ok=False,
                         protection_cleanup=note)

    return RouteResult(ok=True, venue=v, stage="done", asset=asset,
                       cleanup_ok=cleanup_ok, cleanup_status=cleanup_status,
                       protection_cleanup=note,
                       detail=f"{v.upper()} 市价全平已提交: {str(data)[:120]}{note}")
