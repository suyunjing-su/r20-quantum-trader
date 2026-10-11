"""Unit tests for Scale-Out Execution Engine (scripts/trader/scale_out.py)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.trader.scale_out import execute_scale_out_if_eligible


class _StatefulOkx:
    """最小 OKX 条件单适配器：撤单后必须从下一次列表中消失。"""

    def __init__(self, *, inst_id="BTC-USDT-SWAP", quantity=10.0):
        self.inst_id = inst_id
        identity = "algo_1"
        self.rows = [{
            "algoId": identity,
            "algoClOrdId": f"t-astrasl-{identity}",
            "instId": inst_id,
            "symbol": inst_id,
            "side": "sell",
            "quantity": str(quantity),
            "type": "STOP_MARKET",
            "reduceOnly": True,
            "state": "live",
        }]
        self.placed = []
        self.cancel_calls = []

    def place_order(self, inst_id, side, qty, **kwargs):
        self.placed.append((inst_id, side, qty, kwargs))
        return [{"ordId": "12345"}]

    def pending_algo_orders(self, inst_id):
        return [dict(row) for row in self.rows]

    def ensure_protection(self, inst_id, pos_side, size, tp_px, sl_px):
        self.rows.append({
            "algoId": "algo_2",
            "algoClOrdId": "t-astraProtect-algo_2",
            "instId": inst_id,
            "symbol": inst_id,
            "posSide": pos_side,
            "side": "sell" if pos_side == "long" else "buy",
            "sz": str(size),
            "reduceOnly": True,
            "ordType": "oco",
            "slTriggerPx": str(sl_px),
            "tpTriggerPx": str(tp_px),
            "state": "live",
        })
        return True, "ok"

    def cancel_algo_orders(self, ids, *, inst_id):
        identities = []
        for value in ids:
            if isinstance(value, dict):
                identities.extend(str(value.get(key) or "")
                                  for key in ("algoId", "algoClOrdId")
                                  if value.get(key) not in (None, ""))
            else:
                identities.append(str(value))
        self.cancel_calls.append((identities, inst_id))
        wanted = set(identities)
        retained = []
        for row in self.rows:
            row_ids = {str(row.get(key) or "") for key in (
                "id", "algoId", "algo_id", "algoClOrdId", "clientAlgoId")}
            if not row_ids.intersection(wanted):
                retained.append(row)
        self.rows = retained
        return [{"algoId": identities[0] if identities else "", "sCode": "0"}]


class _StatefulVenueAdapter:
    """Binance/Gate double with ownership, explicit ACK, and relist state."""

    def __init__(self, venue, *, symbol="BTCUSDT", quantity=10.0,
                 identity="old-protection"):
        self.venue = str(venue).lower()
        self.symbol = symbol
        self.protective_rows = [{
            "id": identity,
            "algo_id": identity if self.venue == "binance" else "",
            "id_string": identity if self.venue == "gate" else "",
            "symbol": symbol,
            "side": "sell",
            "quantity": str(quantity),
            "type": "STOP_MARKET",
            "triggerPrice": "80200",
            "reduceOnly": True,
            "raw": {
                "symbol": symbol,
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "quantity": str(quantity),
                "reduceOnly": True,
                "clientAlgoId": f"t-astrasl-{identity}",
            },
        }]
        self.placed = []
        self.attached = []
        self.listed = []
        self.cancelled = []
        self.symbol_wide_cancelled = []
        if self.venue == "gate":
            self.cancel_price_order = self._cancel_price_order
            self.cancel_algo_order = None
        else:
            self.cancel_price_order = None
            self.cancel_algo_order = self._cancel_algo_order

    def place_order(self, symbol, side, qty, **kwargs):
        self.placed.append((symbol, side, qty, kwargs))
        return {"id": f"{self.venue}-order-1"}

    def list_protective_orders(self, symbol=None):
        self.listed.append(symbol)
        return [dict(row) for row in self.protective_rows]

    def attach_protective_orders(self, name, pos_side, *, tp_px, sl_px, contracts):
        self.attached.append((name, pos_side, tp_px, sl_px, contracts))
        identity = "new-protection"
        self.protective_rows.append({
            "id": identity,
            "algo_id": identity if self.venue == "binance" else "",
            "id_string": identity if self.venue == "gate" else "",
            "symbol": self.symbol,
            "side": "sell" if pos_side == "long" else "buy",
            "quantity": str(contracts),
            "type": "STOP_MARKET",
            "triggerPrice": str(sl_px),
            "reduceOnly": True,
            "raw": {
                "symbol": self.symbol,
                "orderType": "STOP_MARKET",
                "side": "SELL" if pos_side == "long" else "BUY",
                "quantity": str(contracts),
                "triggerPrice": str(sl_px),
                "reduceOnly": True,
                "clientAlgoId": f"t-astrasl-{identity}",
            },
        })
        if tp_px is not None:
            tp_identity = "new-protection-tp"
            self.protective_rows.append({
                "id": tp_identity,
                "algo_id": tp_identity if self.venue == "binance" else "",
                "id_string": tp_identity if self.venue == "gate" else "",
                "symbol": self.symbol,
                "side": "sell" if pos_side == "long" else "buy",
                "quantity": str(contracts),
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": str(tp_px),
                "reduceOnly": True,
                "raw": {"symbol": self.symbol, "orderType": "TAKE_PROFIT_MARKET",
                        "side": "SELL" if pos_side == "long" else "BUY",
                        "quantity": str(contracts), "triggerPrice": str(tp_px),
                        "reduceOnly": True, "clientAlgoId": f"t-astratp-{tp_identity}"},
            })
        return {"sl": identity, "tp": "new-protection-tp" if tp_px is not None else None}

    def cancel_protective_orders(self, symbol):
        self.symbol_wide_cancelled.append(symbol)

    def _remove_identity(self, identity):
        identity = str(identity or "")
        retained = []
        for row in self.protective_rows:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            row_ids = {str(row.get(key) or "") for key in (
                "id", "id_string", "algo_id", "algoId", "client_algo_id",
                "clientAlgoId", "order_id", "ordId")}
            row_ids.update(str(raw.get(key) or "") for key in (
                "clientAlgoId", "client_algo_id", "orderId", "ordId"))
            if identity not in row_ids:
                retained.append(row)
        self.protective_rows = retained

    def _cancel_price_order(self, order_id):
        self.cancelled.append(str(order_id))
        self._remove_identity(order_id)
        return {"status": "finished"}

    def _cancel_algo_order(self, *, algo_id=None, client_algo_id=None):
        identity = str(algo_id or client_algo_id or "")
        self.cancelled.append(identity)
        self._remove_identity(identity)
        return {"code": "200", "msg": "success"}


class _SingleVenueRegistry:
    def __init__(self, adapter):
        self.adapter = adapter
        self.requests = []

    def get_adapter(self, venue, environment=None):
        self.requests.append((venue, environment))
        return self.adapter


class ScaleOutExecutionTests(unittest.TestCase):
    def setUp(self):
        self.mock_okx = _StatefulOkx()
        self.mock_record_trade = MagicMock()
        self.mock_notify = MagicMock()
        self.mock_ensure_oco = MagicMock(return_value=(True, "ok"))
        self.mock_ensure_oco.side_effect = lambda inst, side, size, tp, sl: \
            self.mock_okx.ensure_protection(inst, side, size, tp, sl)
        self.mock_close_fee = MagicMock(return_value=0.5)
        self.mock_payload = MagicMock(return_value={"action": "close"})

        self.sample_f_long = {
            "instId": "BTC-USDT-SWAP",
            "name": "BTC",
            "price": 82000.0,
            "atr": 1000.0,
            "precision": 2,
            "ctVal": 1.0,
            "minSz": 0.01,
            "market_data_valid": True,
        }

        self.sample_pos_long = {
            "side": "long",
            "avgPx": 80000.0,
            "pos": 10.0,
            "venue": "okx",
        }

        self.sample_trackers = {
            "BTC-USDT-SWAP_long": {
                "initialSz": 10.0,
                "currentSz": 10.0,
                "takeProfitPx": 85000.0,
                "trailingStopPx": 78000.0,
                "scale_out_phase": 0,
                "scale_count": 0,
            }
        }

    def test_disabled_by_switch(self):
        with patch("scripts.trader.scale_out.SCALE_OUT_ENABLED", False):
            actions = []
            ok, reason = execute_scale_out_if_eligible(
                self.sample_f_long, self.sample_pos_long, self.sample_trackers,
                "2026-09-20 12:00:00", actions,
                okx_rest=self.mock_okx,
            )
            self.assertFalse(ok)
            self.assertEqual(reason, "分批止盈未启用")
            self.assertEqual(len(actions), 0)
            self.assertEqual(self.mock_okx.placed, [])

    def test_profit_below_trigger_threshold(self):
        # cur_px = 80500, entry = 80000 -> profit = 500 < 1.2 * 1000 (1200)
        f_low_profit = dict(self.sample_f_long, price=80500.0)
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            f_low_profit, self.sample_pos_long, self.sample_trackers,
            "2026-09-20 12:00:00", actions,
            okx_rest=self.mock_okx,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "浮盈未达分批止盈门槛")
        self.assertEqual(self.mock_okx.placed, [])
        # 验证未达标时已为操盘手和主脑计算并存入首批止盈位 TP1
        t = self.sample_trackers["BTC-USDT-SWAP_long"]
        self.assertEqual(t.get("scale_out_tp"), 81200.0)
        self.assertIn("首批止盈目标 TP1: 81200", t.get("stage_desc", ""))

    def test_insufficient_size_graceful_degrade(self):
        # pos = 0.01, minSz = 0.01 -> 0.01 < 2 * 0.01, cannot split
        pos_tiny = dict(self.sample_pos_long, pos=0.01)
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, pos_tiny, self.sample_trackers,
            "2026-09-20 12:00:00", actions,
            okx_rest=self.mock_okx,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "保证金不足以切分")
        self.assertIn("降级为全仓追踪", actions[0])
        self.assertEqual(self.mock_okx.placed, [])

    def test_notification_failure_does_not_change_the_outcome(self):
        """**通知是 best-effort**：平仓单已经打到交易所 ⇒ 通知（Telegram/邮件等）失败
        只吞掉，返回值仍是成功。否则上层会误以为平仓失败而重试。
        """
        boom = MagicMock(side_effect=RuntimeError("通知通道炸了"))
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, self.sample_pos_long, self.sample_trackers,
            "2026-09-20 12:00:00", actions,
            okx_rest=self.mock_okx,
            record_trade=self.mock_record_trade,
            notify_trade_close=boom,
            close_fee=self.mock_close_fee,
            close_trade_payload=self.mock_payload,
            ensure_cloud_position_protection=self.mock_ensure_oco,
        )
        self.assertTrue(ok, f"通知失败不得改变平仓结果：{reason}")
        self.assertEqual(reason, "首批分批平仓成功")
        boom.assert_called()

    def test_trade_recording_failure_currently_propagates(self):
        """⚠️ **实测边界（如实钉住现状，未擅自改）**：与「通知」不同，**台账记账是裸调用**
        （`record_trade(...)` 没有 `try` 包裹，只有 `notify_trade_close` 那一段有）。
        于是台账写失败会把异常抛给调用方 —— **而平仓单此刻已经打到交易所了**。

        为什么值得记：这一族（本仓反复出现的形态）是「**成交已发生，副作用失败不该改变结果**」；
        通知做到了 best-effort，台账没有，两者**不一致**。改它属钱路行为变更
        （涉及是否重试、如何披露记账缺口）⇒ 列为待议项。
        """
        boom = MagicMock(side_effect=RuntimeError("台账库锁住了"))
        actions = []
        with self.assertRaises(RuntimeError):
            execute_scale_out_if_eligible(
                self.sample_f_long, self.sample_pos_long, self.sample_trackers,
                "2026-09-20 12:00:00", actions,
                okx_rest=self.mock_okx,
                record_trade=boom,
                notify_trade_close=self.mock_notify,
                close_fee=self.mock_close_fee,
                close_trade_payload=self.mock_payload,
                ensure_cloud_position_protection=self.mock_ensure_oco,
            )

    def test_successful_long_scale_out_and_state_lock(self):
        # profit = 82000 - 80000 = 2000 >= 1.2 * 1000
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, self.sample_pos_long, self.sample_trackers,
            "2026-09-20 12:00:00", actions,
            okx_rest=self.mock_okx,
            record_trade=self.mock_record_trade,
            notify_trade_close=self.mock_notify,
            close_fee=self.mock_close_fee,
            close_trade_payload=self.mock_payload,
            ensure_cloud_position_protection=self.mock_ensure_oco,
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "首批分批平仓成功")
        
        # 验证下达平仓市价单（reduceOnly=True，卖出 5 张）
        self.assertEqual(self.mock_okx.placed, [(
            "BTC-USDT-SWAP", "sell", "5",
            {"pos_side": "long", "td_mode": "cross", "ord_type": "market",
             "reduce_only": True},
        )])

        # 验证旧 OCO 被撤销
        self.assertEqual(self.mock_okx.cancel_calls,
                         [(["algo_1"], "BTC-USDT-SWAP")])

        # 验证新 OCO 重挂：剩余 5 张，保本止损价 80200 (80000 + 0.25%)
        self.mock_ensure_oco.assert_called_once_with(
            "BTC-USDT-SWAP", "long", 5.0, 85000.0, 80200.0
        )

        # 验证 tracker 状态变更与金字塔加仓互斥锁定
        t = self.sample_trackers["BTC-USDT-SWAP_long"]
        self.assertEqual(t["scale_out_phase"], 1)
        self.assertEqual(t["currentSz"], 5.0)
        self.assertEqual(t["scale_count"], 999)
        self.assertEqual(t["trailingStopPx"], 80200.0)

        # 验证台账双写与通知触发
        self.mock_record_trade.assert_called_once()
        self.mock_notify.assert_called_once()

    def test_idempotent_no_duplicate_scale_out(self):
        # scale_out_phase 已为 1 时，再次调用直接拒绝，绝不重复平仓
        self.sample_trackers["BTC-USDT-SWAP_long"]["scale_out_phase"] = 1
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, self.sample_pos_long, self.sample_trackers,
            "2026-09-20 12:00:00", actions,
            okx_rest=self.mock_okx,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "已执行过分批平仓")
        self.assertEqual(self.mock_okx.placed, [])

    def test_ledger_holding_row_reflects_scale_out_status(self):
        import sys
        from pathlib import Path
        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from scripts.sync_full_ledger import _holding_row
        import datetime

        trackers = {
            "BTC-USDT-SWAP_long": {
                "scale_out_phase": 1,
                "stage_desc": "已分批止盈50% (余5张 · 保本止损 80200)",
            }
        }
        mock_env = MagicMock(mode="live")
        tz = datetime.timezone(datetime.timedelta(hours=8))
        pos_raw = {
            "instId": "BTC-USDT-SWAP",
            "pos": "5.0",
            "posSide": "long",
            "avgPx": "80000",
            "markPx": "82000",
            "upl": "10000",
            "lever": "3",
            "cTime": "1789857503880",
        }
        row = _holding_row(
            pos_raw, "okx",
            env=mock_env,
            trackers=trackers,
            tz_bj=tz,
            allowed={"BTC-USDT-SWAP"},
            council_by_inst={},
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "holding")
        self.assertIn("已分批止盈50%", row["exit_reason"])
        self.assertEqual(row["scale_out_phase"], 1)

    def test_binance_scale_out_cancels_old_protective_and_sets_reduce_only(self):
        mock_bn_adapter = _StatefulVenueAdapter("binance")
        mock_venue_registry = _SingleVenueRegistry(mock_bn_adapter)

        pos_bn = {
            "side": "long",
            "avgPx": 80000.0,
            "pos": 10.0,
            "venue": "binance",
            "raw": {"positionSide": "BOTH"},
        }
        trackers = {
            "BTC-USDT-SWAP_long": {
                "initialSz": 10.0,
                "currentSz": 10.0,
                "takeProfitPx": 85000.0,
                "trailingStopPx": 78000.0,
                "scale_out_phase": 0,
                "scale_count": 0,
            }
        }
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, pos_bn, trackers,
            "2026-09-20 12:00:00", actions,
            venue_registry=mock_venue_registry,
            record_trade=self.mock_record_trade,
            notify_trade_close=self.mock_notify,
            close_fee=self.mock_close_fee,
            close_trade_payload=self.mock_payload,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(mock_bn_adapter.placed,
                         [("BTC", "sell", 5.0, {"reduce_only": True})])
        self.assertEqual(mock_bn_adapter.symbol_wide_cancelled, [])
        self.assertGreaterEqual(len(mock_bn_adapter.listed), 2)
        self.assertEqual(mock_bn_adapter.cancelled, ["old-protection"])
        self.assertEqual(mock_bn_adapter.attached,
                         [("BTC", "long", 85000.0, 80200.0, 5.0)])

    def test_gate_scale_out_cancels_old_protective_and_sets_reduce_only(self):
        mock_gate_adapter = _StatefulVenueAdapter("gate")
        mock_venue_registry = _SingleVenueRegistry(mock_gate_adapter)

        pos_gate = {
            "side": "long",
            "avgPx": 80000.0,
            "pos": 10.0,
            "venue": "gate",
        }
        trackers = {
            "BTC-USDT-SWAP_long": {
                "initialSz": 10.0,
                "currentSz": 10.0,
                "takeProfitPx": 85000.0,
                "trailingStopPx": 78000.0,
                "scale_out_phase": 0,
                "scale_count": 0,
            }
        }
        actions = []
        ok, reason = execute_scale_out_if_eligible(
            self.sample_f_long, pos_gate, trackers,
            "2026-09-20 12:00:00", actions,
            venue_registry=mock_venue_registry,
            record_trade=self.mock_record_trade,
            notify_trade_close=self.mock_notify,
            close_fee=self.mock_close_fee,
            close_trade_payload=self.mock_payload,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(mock_gate_adapter.placed,
                         [("BTC", "sell", 5.0, {"reduce_only": True})])
        self.assertEqual(mock_gate_adapter.symbol_wide_cancelled, [])
        self.assertGreaterEqual(len(mock_gate_adapter.listed), 2)
        self.assertEqual(mock_gate_adapter.cancelled, ["old-protection"])
        self.assertEqual(mock_gate_adapter.attached,
                         [("BTC", "long", 85000.0, 80200.0, 5.0)])

    def test_cycle_parts_scale_out_tp_derivation(self):
        from scripts.brain.cycle_parts import _calculate_scale_out_tp, build_history_record

        # 多头：80000 + 1.2 * 1000 = 81200
        tp_long = _calculate_scale_out_tp(80000.0, "BUY_LONG", 1000.0, precision=2)
        self.assertEqual(tp_long, 81200.0)

        # 空头：80000 - 1.2 * 1000 = 78800
        tp_short = _calculate_scale_out_tp(80000.0, "SELL_SHORT", 1000.0, precision=2)
        self.assertEqual(tp_short, 78800.0)

        # WAIT 或无价格返回 None
        self.assertIsNone(_calculate_scale_out_tp(0.0, "WAIT", 1000.0))
        self.assertIsNone(_calculate_scale_out_tp(80000.0, "WAIT", 0.0))

        # build_history_record 集成测试
        pkgs = [{"name": "BTC", "instId": "BTC-USDT-SWAP", "atr": 1000.0, "precision": 2}]
        std_cache = {
            "BTC-USDT-SWAP": {
                "decision": {
                    "action": "BUY_LONG",
                    "confidence": 88,
                    "entry_price": 80000.0,
                    "stop_loss_price": 79000.0,
                    "take_profit_price": 85000.0,
                    "risk_reward_ratio": 5.0,
                    "summary_reason": "4H结构突破做多",
                },
                "data_quality": {"status": "ok"},
            }
        }
        rec = build_history_record(
            time_str="2026-09-20 10:00:00",
            policy_version="v8.1.1",
            policy_hash="test_hash",
            policy_snapshot={},
            policy_summary="",
            macro_summary="情绪健康",
            council_status={"ran": True},
            ai_last_prompt="",
            pos_mgmt_list=[],
            council_transcript=None,
            packages=pkgs,
            standard_cache=std_cache,
        )
        self.assertEqual(len(rec["top_opportunities"]), 1)
        opp = rec["top_opportunities"][0]
        self.assertEqual(opp["scale_out_tp"], 81200.0)
        self.assertEqual(opp["take_profit_price"], 85000.0)


class CrossVenueUnitScaleTests(unittest.TestCase):
    """外所分批止盈必须用**该所自己的**合约面值与最小步长。

    三所持仓接管时新增（用户报「币安/Gate 的仓一直挂在那、也不会被平」）：
    `f["ctVal"]` / `f["minSz"]` 来自 **OKX 合约池**（DOGE = 1000 张面值），
    而 Binance 的 `pos` 是**币数**、Gate 的是**自家张数**（DOGE 面值 10）。
    沿用 OKX 口径 ⇒ 名义额与平仓手续费错 1000 倍，切片量也按错误步长对齐
    （真下单就是按错的数量平仓）。持仓记录上的值必须优先。
    """

    def _f(self):
        return {"instId": "DOGE-USDT-SWAP", "name": "DOGE", "price": 0.1,
                "atr": 0.002, "precision": 4, "ctVal": 1000.0, "minSz": 0.01,
                "market_data_valid": True}

    def _run(self, pos):
        venue = str(pos.get("venue") or "").lower()
        if venue == "okx":
            adapter = _StatefulOkx(
                inst_id="DOGE-USDT-SWAP", quantity=abs(float(pos["pos"]))
            )
            registry = None
            okx_rest = adapter
        else:
            adapter = _StatefulVenueAdapter(
                venue, symbol="DOGEUSDT", quantity=abs(float(pos["pos"]))
            )
            registry = _SingleVenueRegistry(adapter)
            okx_rest = None
        fee = MagicMock(return_value=0.0)
        trackers = {
            "DOGE-USDT-SWAP_long": {
                "scale_out_phase": 0,
                "takeProfitPx": 0.1,
            }
        }
        protection_fn = adapter.ensure_protection if venue == "okx" else None
        ok, reason = execute_scale_out_if_eligible(
            self._f(), pos, trackers, "2026-09-28 12:00:00", [],
            okx_rest=okx_rest, venue_registry=registry,
            record_trade=MagicMock(), notify_trade_close=MagicMock(),
            close_fee=fee, close_trade_payload=MagicMock(return_value={}),
            ensure_cloud_position_protection=protection_fn,
        )
        return ok, reason, adapter, fee

    def test_gate_uses_its_own_contract_value_not_the_okx_pool_value(self):
        pos = {"side": "long", "avgPx": 0.09615, "pos": 861.0, "venue": "gate",
               "ctVal": 10.0, "minSz": 10.0, "precision": 0}
        ok, reason, adapter, fee = self._run(pos)
        self.assertTrue(ok, reason)
        self.assertEqual(fee.call_args[0][1], 10.0,
                         "手续费必须按 Gate 的面值(10)算，不是 OKX 池的 1000")
        self.assertEqual(adapter.placed[0][2], 430.0,
                         "切片量按 Gate 的 step(10) 对齐")

    def test_binance_uses_coin_units_with_a_contract_value_of_one(self):
        pos = {"side": "long", "avgPx": 0.09615, "pos": 861.0, "venue": "binance",
               "ctVal": 1.0, "minSz": 1.0, "precision": 0}
        ok, reason, adapter, fee = self._run(pos)
        self.assertTrue(ok, reason)
        self.assertEqual(fee.call_args[0][1], 1.0,
                         "币安 pos 已是币数 ⇒ 面值必须是 1，否则名义额错 1000 倍")
        self.assertEqual(adapter.placed[0][2], 430.0,
                         "切片量按币安的 step(1) 对齐")

    def test_an_okx_position_still_falls_back_to_the_pool_values(self):
        """没有持仓级覆盖时逐位回落 OKX 口径 —— 不许改变 OKX 路径的行为。"""
        pos = {"side": "long", "avgPx": 0.09615, "pos": 861.0, "venue": "okx"}
        fee = MagicMock(return_value=0.0)
        okx_rest = _StatefulOkx(inst_id="DOGE-USDT-SWAP", quantity=861.0)
        ok, reason = execute_scale_out_if_eligible(
            self._f(), pos, {"DOGE-USDT-SWAP_long": {
                "scale_out_phase": 0, "takeProfitPx": 0.1}},
            "2026-09-28 12:00:00", [],
            okx_rest=okx_rest,
            record_trade=MagicMock(), notify_trade_close=MagicMock(),
            close_fee=fee, close_trade_payload=MagicMock(return_value={}),
            ensure_cloud_position_protection=okx_rest.ensure_protection,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(fee.call_args[0][1], 1000.0,
                         "OKX 无覆盖值 ⇒ 沿用合约池面值")


if __name__ == "__main__":
    unittest.main()
