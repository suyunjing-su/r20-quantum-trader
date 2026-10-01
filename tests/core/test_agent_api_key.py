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

    def test_agent_authentication_reads_scopes_without_self_escalation(self):
        from astra_backend.dependencies import authenticate_agent_api_key

        with mock.patch.dict(A.os.environ, {
            "ASTRA_AGENT_API_KEY": "secret",
            "ASTRA_AGENT_API_KEY_SCOPES": "equity_bands:write",
        }, clear=False):
            actor = authenticate_agent_api_key("secret")
        self.assertEqual(actor["scopes"], ["equity_bands:write"])

    def test_status_only_exposes_presence(self):
        with mock.patch.dict(A.os.environ, {"ASTRA_AGENT_API_KEY": "secret"}, clear=False):
            self.assertEqual(A.get_agent_api_key_status(), {"configured": True})
        self.superadmin.assert_called_once_with()

    def test_superadmin_can_grant_equity_band_scope(self):
        result = A.put_agent_api_key_scopes(A.AgentApiScopesUpdate(scopes=["equity_bands:write"]))
        self.assertEqual(result["scopes"], ["equity_bands:write"])
        self.update_env.assert_called_once_with({"ASTRA_AGENT_API_KEY_SCOPES": "equity_bands:write"})
        self.audit.assert_called_once_with(
            "agent_api_key.scopes.update", "success",
            {"actor": "root", "scopes": ["equity_bands:write"]},
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
