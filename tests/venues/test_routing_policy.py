from __future__ import annotations

import json

from astra_backend.exchanges import routing_policy


def test_multi_venue_routing_defaults_on_for_legacy_config(tmp_path, monkeypatch):
    path = tmp_path / "venue_routing.json"
    monkeypatch.setattr(routing_policy, "ROUTING_FILE", path)
    assert routing_policy.load_multi_venue_routing_enabled() is True
    path.write_text(json.dumps({"preferred_venue": "gate"}), encoding="utf-8")
    assert routing_policy.load_multi_venue_routing_enabled() is True


def test_routing_switch_write_preserves_unrelated_configuration(tmp_path, monkeypatch):
    path = tmp_path / "venue_routing.json"
    path.write_text(json.dumps({"preferred_venue": "gate", "gate": {"assets": ["BTC"]}}), encoding="utf-8")
    monkeypatch.setattr(routing_policy, "ROUTING_FILE", path)
    assert routing_policy.save_multi_venue_routing_enabled(False) is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["multi_venue_routing_enabled"] is False
    assert persisted["preferred_venue"] == "gate"
    assert persisted["gate"] == {"assets": ["BTC"]}


def test_single_venue_resolution_requires_exactly_one_raw_open_venue():
    opened = {"binance"}
    check = lambda venue, _environment: venue in opened
    assert routing_policy.active_execution_venue("live", open_checker=check) == "binance"
    assert not routing_policy.execution_open_conflict("live", open_checker=check)
    opened.add("gate")
    assert routing_policy.active_execution_venue("live", open_checker=check) is None
    assert routing_policy.execution_open_conflict("live", open_checker=check)
    opened.clear()
    assert routing_policy.active_execution_venue("live", open_checker=check) is None
    assert not routing_policy.execution_open_conflict("live", open_checker=check)
