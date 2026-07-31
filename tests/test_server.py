import importlib.util
from io import BytesIO
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("model_console_server", SERVER_PATH)
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class ContentTests(unittest.TestCase):
    def test_extracts_string_content(self):
        payload = {"choices": [{"message": {"content": "Hello"}}]}
        self.assertEqual(server_module.extract_content(payload), "Hello")

    def test_extracts_list_content(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "First"},
                            {"type": "text", "text": "Second"},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(server_module.extract_content(payload), "First\nSecond")

    def test_extracts_nested_error_message(self):
        body = json.dumps({"error": {"message": "Bad route"}}).encode()
        self.assertEqual(
            server_module.gateway_error_message(body, 400),
            "Bad route",
        )

    def test_error_fallback_does_not_reflect_raw_body(self):
        body = b"<html>gateway internals</html>"
        self.assertEqual(
            server_module.gateway_error_message(body, 502),
            "The AI gateway returned HTTP 502.",
        )

    def test_gateway_errors_are_unbranded(self):
        body = json.dumps({"error": {"message": "Portkey rejected it"}}).encode()
        self.assertEqual(
            server_module.gateway_error_message(body, 400),
            "AI gateway rejected it",
        )

    def test_gateway_errors_redact_credentials(self):
        original_key = server_module.PORTKEY_API_KEY
        server_module.PORTKEY_API_KEY = "secret-service-key"
        try:
            body = json.dumps(
                {
                    "error": {
                        "message": "Rejected secret-service-key sk-testtoken1234567890"
                    }
                }
            ).encode()
            message = server_module.gateway_error_message(body, 400)
        finally:
            server_module.PORTKEY_API_KEY = original_key
        self.assertNotIn("secret-service-key", message)
        self.assertNotIn("sk-testtoken", message)

    def test_gateway_response_size_is_bounded(self):
        original_limit = server_module.MAX_GATEWAY_RESPONSE_BYTES
        server_module.MAX_GATEWAY_RESPONSE_BYTES = 65_536
        try:
            with self.assertRaisesRegex(ValueError, "size limit"):
                server_module.read_gateway_response(BytesIO(b"x" * 65_537))
        finally:
            server_module.MAX_GATEWAY_RESPONSE_BYTES = original_limit

    def test_gateway_request_routes_only_by_role_metadata(self):
        handler = object.__new__(server_module.AppHandler)
        payload, headers = handler._gateway_request("devs", "Build a parser")
        self.assertEqual(
            payload,
            {"messages": [{"role": "user", "content": "Build a parser"}]},
        )
        self.assertNotIn("model", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("x-portkey-provider", headers)
        self.assertNotIn("x-portkey-config", headers)
        metadata = json.loads(headers["x-portkey-metadata"])
        self.assertEqual(metadata["user_role"], "devs")

    def test_gui_models_are_derived_from_portkey_config(self):
        self.assertEqual(
            server_module.role_models_from_routing_config(),
            {
                "devs": "@gemini-example/gemini-3.5-flash",
                "hr": "@gemini-example/gemini-3.6-flash",
                "sales": "@gemini-example/gemini-3.5-flash-lite",
            },
        )

    def test_validates_and_builds_onboarding_config(self):
        key, slug, roles = server_module.validate_onboarding(
            {
                "serviceKey": "test-key",
                "providerSlug": "@gemini-example",
                "roles": [
                    {"label": "Sales Team", "model": "gemini-fast"},
                    {"label": "People", "model": "gemini-balanced"},
                    {"label": "Engineering", "model": "gemini-best"},
                ],
            }
        )
        self.assertEqual(key, "test-key")
        self.assertEqual(slug, "gemini-example")
        self.assertEqual(
            [role["id"] for role in roles],
            ["sales-team", "people", "engineering"],
        )
        config = server_module.build_routing_config(slug, roles)
        self.assertEqual(
            config["targets"][2]["override_params"]["model"],
            "@gemini-example/gemini-best",
        )
        self.assertEqual(
            config["strategy"]["conditions"][0]["query"]["metadata.user_role"][
                "$eq"
            ],
            "sales-team",
        )

    def test_rejects_duplicate_onboarding_roles(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            server_module.validate_onboarding(
                {
                    "serviceKey": "test-key",
                    "providerSlug": "gemini-example",
                    "roles": [
                        {"label": "Sales", "model": "gemini-a"},
                        {"label": "sales", "model": "gemini-b"},
                        {"label": "Devs", "model": "gemini-c"},
                    ],
                }
            )

    def test_rejects_control_characters_in_onboarding(self):
        with self.assertRaisesRegex(ValueError, "control characters"):
            server_module.validate_onboarding(
                {
                    "serviceKey": "test-key",
                    "providerSlug": "gemini-example",
                    "roles": [
                        {"label": "Sales\nInjected", "model": "gemini-a"},
                        {"label": "HR", "model": "gemini-b"},
                        {"label": "Devs", "model": "gemini-c"},
                    ],
                }
            )

    def test_environment_loader_does_not_expand_shell_syntax(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text('SECURITY_REVIEW_TEST="$(touch /tmp/never-run)"\n')
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SECURITY_REVIEW_TEST", None)
                server_module.load_environment_file(env_file)
                self.assertEqual(
                    os.environ["SECURITY_REVIEW_TEST"],
                    "$(touch /tmp/never-run)",
                )
                os.environ.pop("SECURITY_REVIEW_TEST", None)

    def test_state_writer_refuses_symlinks(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW is unavailable")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.write_text("unchanged")
            link = Path(temp_dir) / "state"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                server_module.overwrite_state_file(link, "replaced")
            self.assertEqual(target.read_text(), "unchanged")

    def test_roles_can_be_loaded_from_environment(self):
        custom = {
            "PORTKEY_ROLE_1_ID": "legal",
            "PORTKEY_ROLE_1_LABEL": "Legal",
            "PORTKEY_ROLE_1_MODEL": "gemini-legal",
        }
        with (
            mock.patch.dict(os.environ, custom),
            mock.patch.object(server_module, "PROVIDER_SLUG", "custom-profile"),
        ):
            roles = server_module.roles_from_environment()
        self.assertEqual(roles["legal"]["label"], "Legal")
        self.assertEqual(
            roles["legal"]["model"],
            "@custom-profile/gemini-legal",
        )


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_key = server_module.PORTKEY_API_KEY
        server_module.PORTKEY_API_KEY = ""
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.AppHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        server_module.PORTKEY_API_KEY = cls.original_key

    def test_health_endpoint(self):
        with urllib.request.urlopen(f"{self.base_url}/api/health") as response:
            payload = json.load(response)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["configured"])

    def test_spa_is_served_with_security_headers(self):
        with urllib.request.urlopen(f"{self.base_url}/") as response:
            body = response.read().decode()
            self.assertIn("AI Role Demo", body)
            self.assertIn('id="profile-switcher"', body)
            self.assertIn('id="profile-role"', body)
            self.assertNotIn("Maya Chen", body)
            self.assertNotIn('id="role-switch"', body)
            self.assertNotIn("Portkey", body)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(
                response.headers["Cross-Origin-Opener-Policy"],
                "same-origin",
            )
            self.assertEqual(
                response.headers["Cross-Origin-Resource-Policy"],
                "same-origin",
            )
            self.assertEqual(
                response.headers["Strict-Transport-Security"],
                "max-age=31536000",
            )
            self.assertNotIn("Python", response.headers["Server"])

    def test_public_config_does_not_expose_secret_or_gateway_url(self):
        config = server_module.public_config()
        self.assertNotIn("gateway", config)
        self.assertNotIn("apiKey", config)

    def test_rejects_misleading_json_content_type(self):
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=b"{}",
            headers={"Content-Type": "text/application/json-pretend"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request)
        self.assertEqual(context.exception.code, 415)

    def test_spa_supports_head_requests(self):
        request = urllib.request.Request(f"{self.base_url}/", method="HEAD")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertGreater(int(response.headers["Content-Length"]), 0)

    def test_chat_requires_server_side_key(self):
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps({"role": "sales", "prompt": "Hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request)
        self.assertEqual(context.exception.code, 503)
        payload = json.load(context.exception)
        self.assertIn("not configured", payload["error"])

    def test_onboarding_rejects_wrong_existing_key(self):
        original_key = server_module.PORTKEY_API_KEY
        server_module.PORTKEY_API_KEY = "current-key"
        try:
            request = urllib.request.Request(
                f"{self.base_url}/api/onboarding",
                data=json.dumps(
                    {
                        "serviceKey": "wrong-key",
                        "providerSlug": "gemini-example",
                        "roles": [
                            {"label": "Sales", "model": "gemini-fast"},
                            {"label": "HR", "model": "gemini-balanced"},
                            {"label": "Devs", "model": "gemini-best"},
                        ],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            self.assertEqual(context.exception.code, 403)
        finally:
            server_module.PORTKEY_API_KEY = original_key

    def test_onboarding_writes_env_and_json(self):
        original_key = server_module.PORTKEY_API_KEY
        original_slug = server_module.PROVIDER_SLUG
        original_roles = server_module.ROLES
        original_env_file = server_module.ENV_FILE
        original_config_file = server_module.ROUTING_CONFIG_PATH

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {}, clear=False
        ):
            env_file = Path(temp_dir) / ".env"
            config_file = Path(temp_dir) / "routing.json"
            server_module.ENV_FILE = env_file
            server_module.ROUTING_CONFIG_PATH = config_file
            server_module.PORTKEY_API_KEY = ""

            try:
                request = urllib.request.Request(
                    f"{self.base_url}/api/onboarding",
                    data=json.dumps(
                        {
                            "serviceKey": "new-key",
                            "providerSlug": "gemini-example",
                            "roles": [
                                {"label": "Sales", "model": "gemini-fast"},
                                {"label": "HR", "model": "gemini-balanced"},
                                {"label": "Devs", "model": "gemini-best"},
                            ],
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    payload = json.load(response)

                self.assertTrue(payload["saved"])
                self.assertIn("PORTKEY_PROVIDER_SLUG", env_file.read_text())
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
                written_config = json.loads(config_file.read_text())
                self.assertEqual(
                    written_config["targets"][0]["override_params"]["model"],
                    "@gemini-example/gemini-fast",
                )
            finally:
                server_module.PORTKEY_API_KEY = original_key
                server_module.PROVIDER_SLUG = original_slug
                server_module.ROLES = original_roles
                server_module.ENV_FILE = original_env_file
                server_module.ROUTING_CONFIG_PATH = original_config_file

    def test_rejects_unknown_static_file(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(f"{self.base_url}/missing.js")
        self.assertEqual(context.exception.code, 404)

    def test_static_path_cannot_escape_asset_directory(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(f"{self.base_url}/%2e%2e/server.py")
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
