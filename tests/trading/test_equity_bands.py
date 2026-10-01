from __future__ import annotations

import pytest

from astra_backend import equity_bands


def test_equity_bands_use_half_open_boundaries_and_independent_domains(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_bands, "BANDS_FILE", tmp_path / "bands.json")
    council = equity_bands.save_bands("council", [
        {"id": "under-500", "min_equity": 0, "max_equity": 500, "target_id": "small"},
        {"id": "500-plus", "min_equity": 500, "max_equity": None, "target_id": "large"},
    ])
    prompt = equity_bands.save_bands("prompt", [
        {"id": "prompt-all", "min_equity": 0, "max_equity": None, "target_id": "prompt-v1"},
    ])

    assert equity_bands.resolve_council_profile(499.99)["target_id"] == "small"
    assert equity_bands.resolve_council_profile(500)["target_id"] == "large"
    assert equity_bands.resolve_prompt_profile(500)["target_id"] == "prompt-v1"
    assert equity_bands.resolve_risk_suite(500) is None
    assert len(council) == 2
    assert len(prompt) == 1


def test_equity_bands_reject_enabled_overlaps(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_bands, "BANDS_FILE", tmp_path / "bands.json")
    with pytest.raises(ValueError, match="重叠"):
        equity_bands.save_bands("risk", [
            {"id": "a", "min_equity": 0, "max_equity": 600, "target_id": "balanced"},
            {"id": "b", "min_equity": 500, "max_equity": None, "target_id": "aggressive"},
        ])


def test_unknown_equity_does_not_resolve_band(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_bands, "BANDS_FILE", tmp_path / "bands.json")
    equity_bands.save_bands("risk", [
        {"id": "all", "min_equity": 0, "max_equity": None, "target_id": "balanced"},
    ])
    assert equity_bands.resolve_risk_suite(None) is None
    assert equity_bands.resolve_risk_suite(float("nan")) is None


def test_band_targets_are_validated_per_domain(monkeypatch):
    from unittest import mock

    with mock.patch("astra_backend.council_manager.list_council_profiles",
                    return_value=[{"id": "consensus-v1"}]):
        equity_bands.validate_band_targets("council", [
            {"target_id": "consensus-v1"},
        ])
        with pytest.raises(ValueError, match="不存在"):
            equity_bands.validate_band_targets("council", [
                {"target_id": "deleted-profile"},
            ])
