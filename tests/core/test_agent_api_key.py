"""Agent API key lifecycle routes: generate once, revoke explicitly, never read the secret."""
import unittest
from unittest import mock

from astra_backend.routers import agent_api as A


class AgentApiKeyRoutesTest(unittest.TestCase):
    def setUp(self):
        self.actor = {"username": "root", "role": "superadmin"}
        self.superadmin = mock.Mock(return_value=self.actor)
        self.update_env = mock.Mock()
        self.audit = mock.Mock()
        self.patches = [
            mock.patch.object(A, "require_superadmin", self.superadmin),
            mock.patch.object(A, "update_env", self.update_env),
            mock.patch.object(A, "audit_record", self.audit),
            mock.patch.object(A.secrets, "token_urlsafe", return_value="generated-agent-key"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_agent_key_cannot_call_its_administrator_management_route(self):
        from astra_backend.app import _agent_api_key_scope_allows

        for method in ("GET", "POST", "DELETE"):
            self.assertFalse(
                _agent_api_key_scope_allows(method, "/api/v1/admin/agent-api-key")
            )
        self.assertTrue(
            _agent_api_key_scope_allows("GET", "/api/v1/agent/capabilities")
        )

    def test_equity_band_read_is_available_but_write_requires_explicit_scope(self):
        from astra_backend.app import _agent_api_key_scope_allows

        path = "/api/v1/admin/equity-bands/prompt"
        actor = {"auth_method": "api_key", "scopes": []}
        self.assertTrue(_agent_api_key_scope_allows("GET", path, actor))
        self.assertFalse(_agent_api_key_scope_allows("PUT", path, actor))
        actor["scopes"] = ["equity_bands:write"]
        self.assertTrue(_agent_api_key_scope_allows("PUT", path, actor))

    def test_capital_tier_read_is_sanitized_and_write_requires_its_scope(self):
        from astra_backend.app import _agent_api_key_scope_allows

        actor = {"auth_method": "api_key", "scopes": []}
        self.assertTrue(_agent_api_key_scope_allows("GET", "/api/v1/agent/capital-tiers", actor))
        path = "/api/v1/admin/instruments/BTC-USDT-SWAP/capital-tier"
        self.assertFalse(_agent_api_key_scope_allows("PUT", path, actor))
        actor["scopes"] = ["capital_tiers:write"]
        self.assertTrue(_agent_api_key_scope_allows("PUT", path, actor))
        self.assertTrue(_agent_api_key_scope_allows("PUT", "/api/v1/admin/instruments/BTC-USDT-SWAP/venues", actor))
        self.assertFalse(_agent_api_key_scope_allows("PUT", "/api/v1/admin/instruments/BTC-USDT-SWAP/capital-tier/extra", actor))

    def test_risk_suite_crud_requires_its_explicit_write_scope(self):
        from astra_backend.app import _agent_api_key_scope_allows

        actor = {"auth_method": "api_key", "scopes": []}
        routes = [
            ("POST", "/api/v1/admin/risk/custom-suites"),
            ("PUT", "/api/v1/admin/risk/custom-suites/suite-1"),
            ("DELETE", "/api/v1/admin/risk/custom-suites/suite-1"),
        ]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertFalse(_agent_api_key_scope_allows(method, path, actor))
        actor["scopes"] = ["risk_suites:write"]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertTrue(_agent_api_key_scope_allows(method, path, actor))
        self.assertFalse(_agent_api_key_scope_allows("DELETE", "/api/v1/admin/risk/custom-suites/a/b", actor))

    def test_capital_tier_read_view_excludes_sizing_and_capital_fields(self):
        with mock.patch.object(A, "require_admin_header") as _auth:
            with mock.patch("scripts.instrument_pool.load_instruments", return_value=[{
                "instId": "BTC-USDT-SWAP", "name": "BTC", "tier": "tier_1_bluechip",
                "max_leverage": 5, "sl_atr_mult": 1.8, "base_sz": 10,
                "risk_per_trade_usd": 500, "capital_tier": "private",
            }]):
                result = A.get_agent_capital_tiers()
        self.assertEqual(result["items"][0], {
            "instId": "BTC-USDT-SWAP", "name": "BTC", "tier": "tier_1_bluechip",
            "tier_label": "蓝筹主流", "max_leverage": 5, "sl_atr_mult": 1.8,
        })

    def test_capital_tier_mutation_changes_only_classification_and_derived_fields(self):
        from astra_backend import schemas
        from astra_backend.routers import risk

        pool = [{"instId": "SOL-USDT-SWAP", "tier": "tier_2_momentum",
                 "max_leverage": 3, "sl_atr_mult": 2.2, "base_sz": 7,
                 "risk_per_trade_usd": 25}]
        with mock.patch.object(risk, "require_superadmin", return_value={"username": "root"}) as _admin:
            with mock.patch.object(risk, "_live_holdings", return_value=(False, [], "")):
                with mock.patch.object(risk, "DATA_DIR", __import__("pathlib").Path("Z:/astra-test-no-trackers")):
                    with mock.patch.object(risk, "mutate_instruments", side_effect=lambda callback: callback(pool)):
                        with mock.patch.object(risk, "audit_record") as audit:
                            with mock.patch("scripts.instrument_pool.derive_instrument_leverage_cap", return_value=5):
                                result = risk.update_admin_instrument_capital_tier(
                                    "SOL-USDT-SWAP", schemas.InstrumentTierUpdate(tier="tier_1_bluechip")
                                )
        self.assertEqual(result["tier"], "tier_1_bluechip")
        self.assertEqual(pool[0]["max_leverage"], 5)
        self.assertEqual(pool[0]["sl_atr_mult"], 1.8)
        self.assertEqual(pool[0]["base_sz"], 7)
        self.assertEqual(pool[0]["risk_per_trade_usd"], 25)
        audit.assert_called_once()

    def test_agent_authentication_reads_scopes_without_self_escalation(self):
        from astra_backend.dependencies import authenticate_agent_api_key

        with mock.patch.dict(A.os.environ, {
            "ASTRA_AGENT_API_KEY": "secret",
            "ASTRA_AGENT_API_KEY_SCOPES": "capital_tiers:write,equity_bands:write,risk_suites:write",
        }, clear=False):
            actor = authenticate_agent_api_key("secret")
        self.assertEqual(actor["scopes"], ["capital_tiers:write", "equity_bands:write", "risk_suites:write"])

    def test_status_only_exposes_presence(self):
        with mock.patch.dict(A.os.environ, {"ASTRA_AGENT_API_KEY": "secret"}, clear=False):
            self.assertEqual(A.get_agent_api_key_status(), {"configured": True})
        self.superadmin.assert_called_once_with()

    def test_superadmin_can_grant_equity_band_scope(self):
        result = A.put_agent_api_key_scopes(A.AgentApiScopesUpdate(
            scopes=["equity_bands:write", "risk_suites:write", "capital_tiers:write"]
        ))
        self.assertEqual(result["scopes"], ["capital_tiers:write", "equity_bands:write", "risk_suites:write"])
        self.update_env.assert_called_once_with({
            "ASTRA_AGENT_API_KEY_SCOPES": "capital_tiers:write,equity_bands:write,risk_suites:write"
        })
        self.audit.assert_called_once_with(
            "agent_api_key.scopes.update", "success",
            {"actor": "root", "scopes": ["capital_tiers:write", "equity_bands:write", "risk_suites:write"]},
        )

    def test_scope_update_rejects_unknown_scope(self):
        with self.assertRaises(ValueError):
            A.AgentApiScopesUpdate(scopes=["admin:everything"])

    def test_generate_persists_and_returns_secret_once(self):
        result = A.generate_agent_api_key()
        self.assertEqual(result, {"configured": True, "api_key": "generated-agent-key", "shown_once": True})
        self.update_env.assert_called_once_with({"ASTRA_AGENT_API_KEY": "generated-agent-key"})
        self.audit.assert_called_once_with(
            "agent_api_key.generate", "success", {"actor": "root"}
        )

    def test_delete_persists_a_blank_override_without_returning_secret(self):
        result = A.delete_agent_api_key()
        self.assertEqual(result, {"configured": False})
        self.update_env.assert_called_once_with({"ASTRA_AGENT_API_KEY": ""})
        self.assertNotIn("api_key", result)
        self.audit.assert_called_once_with(
            "agent_api_key.delete", "success", {"actor": "root"}
        )


if __name__ == "__main__":
    unittest.main()
