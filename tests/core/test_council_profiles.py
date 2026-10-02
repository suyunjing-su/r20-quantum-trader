from __future__ import annotations

import json
from unittest import mock

import pytest

from astra_backend import council_manager as cm
from astra_backend import equity_bands


def _config():
    return {
        "enabled": True,
        "consensus_mode": "majority",
        "timeout_seconds": 120,
        "roles": {"cio": {"is_arbitrator": True, "enabled": True}},
    }


def test_profile_crud_and_apply_preserves_stable_id(tmp_path, monkeypatch):
    profile_file = tmp_path / "council_profiles.json"
    bands_file = tmp_path / "equity_bands.json"
    monkeypatch.setattr(cm, "COUNCIL_PROFILES_FILE", profile_file)
    monkeypatch.setattr(equity_bands, "BANDS_FILE", bands_file)
    monkeypatch.setattr(cm, "_validate_council_profile_config", lambda config: config)

    created = cm.save_council_profile("consensus-v1", "consensus", "initial", _config())
    assert created["id"] == "consensus-v1"
    assert cm.get_council_profile("consensus-v1")["name"] == "consensus"

    updated = cm.save_council_profile(
        "consensus-v1", "consensus-2", "revision",
        {**_config(), "consensus_mode": "unanimous"}, update=True,
    )
    assert updated["id"] == "consensus-v1"
    assert cm.get_council_profile("consensus-v1")["config"]["consensus_mode"] == "unanimous"

    with mock.patch.object(cm, "save_council_config", return_value={"roles": _config()["roles"]}) as save:
        applied = cm.apply_council_profile("consensus-v1")
    assert applied["active_profile_id"] == "consensus-v1"
    save.assert_called_once()

    assert json.loads(profile_file.read_text(encoding="utf-8"))["profiles"][0]["id"] == "consensus-v1"
    assert cm.delete_council_profile("consensus-v1") is True
    assert cm.get_council_profile("consensus-v1") is None


def test_profile_delete_is_blocked_while_referenced_by_enabled_band(tmp_path, monkeypatch):
    profile_file = tmp_path / "council_profiles.json"
    bands_file = tmp_path / "equity_bands.json"
    monkeypatch.setattr(cm, "COUNCIL_PROFILES_FILE", profile_file)
    monkeypatch.setattr(equity_bands, "BANDS_FILE", bands_file)
    monkeypatch.setattr(cm, "_validate_council_profile_config", lambda config: config)
    cm.save_council_profile("consensus-v1", "consensus", "", _config())
    equity_bands.save_bands("council", [{
        "id": "band-1", "min_equity": 0, "max_equity": None,
        "target_id": "consensus-v1", "enabled": True,
    }])

    with pytest.raises(ValueError):
        cm.delete_council_profile("consensus-v1")
    assert cm.get_council_profile("consensus-v1") is not None

    equity_bands.save_bands("council", [])
    assert cm.delete_council_profile("consensus-v1") is True


def test_apply_profile_preserves_external_timeout(tmp_path, monkeypatch):
    profile_file = tmp_path / "council_profiles.json"
    config_file = tmp_path / "council_config.json"
    monkeypatch.setattr(cm, "COUNCIL_PROFILES_FILE", profile_file)
    monkeypatch.setattr(cm, "COUNCIL_CONFIG_FILE", config_file)
    monkeypatch.setattr(cm, "_validate_council_profile_config", lambda config: config)
    config_file.write_text(json.dumps({
        "enabled": True,
        "consensus_mode": "majority",
        "timeout_seconds": 900,
        "roles": {"cio": {"is_arbitrator": True}},
    }), encoding="utf-8")
    cm.save_council_profile(
        "slow-profile", "slow", "", {**_config(), "timeout_seconds": 30},
    )

    with mock.patch.object(cm, "save_council_config", return_value={
        "timeout_seconds": 900, "roles": _config()["roles"],
    }) as save:
        applied = cm.apply_council_profile("slow-profile")

    assert applied["timeout_seconds"] == 900
    assert save.call_args.args[0]["timeout_seconds"] == 900


def test_equity_profile_timeout_cannot_override_external_timeout(tmp_path, monkeypatch):
    profile_file = tmp_path / "council_profiles.json"
    config_file = tmp_path / "council_config.json"
    bands_file = tmp_path / "equity_bands.json"
    monkeypatch.setattr(cm, "COUNCIL_PROFILES_FILE", profile_file)
    monkeypatch.setattr(cm, "COUNCIL_CONFIG_FILE", config_file)
    monkeypatch.setattr(equity_bands, "BANDS_FILE", bands_file)
    monkeypatch.setattr(cm, "_validate_council_profile_config", lambda config: config)
    config_file.write_text(json.dumps({
        "enabled": True,
        "consensus_mode": "majority",
        "timeout_seconds": 1200,
        "roles": {"cio": {"is_arbitrator": True}},
    }), encoding="utf-8")
    cm.save_council_profile(
        "band-profile", "band", "", {**_config(), "timeout_seconds": 9999},
    )
    equity_bands.save_domain_config("council", [{
        "id": "band-1", "min_equity": 0, "max_equity": None,
        "target_id": "band-profile", "enabled": True,
    }])

    selected = cm.load_council_config(equity=100)

    assert selected["active_profile_id"] == "band-profile"
    assert selected["timeout_seconds"] == 1200


def test_unified_profile_timeout_cannot_override_external_timeout(tmp_path, monkeypatch):
    profile_file = tmp_path / "council_profiles.json"
    config_file = tmp_path / "council_config.json"
    bands_file = tmp_path / "equity_bands.json"
    monkeypatch.setattr(cm, "COUNCIL_PROFILES_FILE", profile_file)
    monkeypatch.setattr(cm, "COUNCIL_CONFIG_FILE", config_file)
    monkeypatch.setattr(equity_bands, "BANDS_FILE", bands_file)
    monkeypatch.setattr(cm, "_validate_council_profile_config", lambda config: config)
    config_file.write_text(json.dumps({
        "enabled": True,
        "consensus_mode": "majority",
        "timeout_seconds": 1800,
        "roles": {"cio": {"is_arbitrator": True}},
    }), encoding="utf-8")
    cm.save_council_profile(
        "unified-profile", "unified", "", {**_config(), "timeout_seconds": 30},
    )
    equity_bands.save_domain_config(
        "council", [], mode="unified", unified_target_id="unified-profile",
    )

    selected = cm.load_council_config()

    assert selected["active_profile_id"] == "unified-profile"
    assert selected["timeout_seconds"] == 1800
