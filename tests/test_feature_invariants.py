import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import api
import main
from test_main import asgi_get, asgi_ws


class TestFeatureInvariants(unittest.TestCase):
    def setUp(self):
        try:
            if main.docker_manager.is_running():
                main.docker_manager.execute_container_action("test_persistence_stack-web-1", "remove")
        except Exception:
            pass
        main.docker_manager.invalidate_cache()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "invariants_test.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("invuser", "invpass123", email="inv@test.local")
        self.session_token = main.create_session("invuser")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    # =========================================================================
    # 1. Docker Terminal Invariant
    # =========================================================================
    def test_docker_terminal_invariant_default_env(self):
        dm = main.DockerManager()
        with patch.object(dm, "is_running", return_value=True), \
             patch.object(dm, "_socket_request") as mock_req:
            mock_req.return_value = (201, b'{"Id": "exec_abc123"}')
            res = dm.create_container_exec("test_container", cmd=["/bin/bash"])
            self.assertEqual(res["id"], "exec_abc123")

            call_args = mock_req.call_args
            req_body = json.loads(call_args[1]["body"].decode("utf-8") if isinstance(call_args[1]["body"], bytes) else call_args[1]["body"])
            env_vars = req_body.get("Env", [])

            self.assertIn("TERM=xterm-256color", env_vars)
            self.assertIn("COLORTERM=truecolor", env_vars)
            self.assertIn("LANG=C.UTF-8", env_vars)

    def test_docker_terminal_invariant_custom_env_override(self):
        dm = main.DockerManager()
        with patch.object(dm, "is_running", return_value=True), \
             patch.object(dm, "_socket_request") as mock_req:
            mock_req.return_value = (201, b'{"Id": "exec_def456"}')
            dm.create_container_exec("test_container", env={"TERM": "vt100", "CUSTOM_VAR": "custom_val"})
            req_body = json.loads(mock_req.call_args[1]["body"].decode("utf-8"))
            env_vars = req_body.get("Env", [])
            self.assertIn("TERM=vt100", env_vars)
            self.assertIn("CUSTOM_VAR=custom_val", env_vars)
            self.assertIn("COLORTERM=truecolor", env_vars)

    def test_docker_terminal_invariant_resize_handling(self):
        dm = main.DockerManager()
        with patch.object(dm, "is_running", return_value=True), \
             patch.object(dm, "_socket_request") as mock_req:
            mock_req.return_value = (200, b"")
            ok = dm.resize_container_exec("exec_999", height=40, width=120)
            self.assertTrue(ok)
            mock_req.assert_called_once_with(
                "POST",
                "/exec/exec_999/resize?h=40&w=120",
                timeout=5.0,
            )

    def test_docker_terminal_invariant_websocket_auth_handshake(self):
        # Unauthenticated rejected
        unauth_ws = asgi_ws(api.app, "/api/docker/containers/c123/terminal")
        self.assertEqual(unauth_ws.close_code, 1008)

        # Authenticated accepted
        auth_ws = asgi_ws(
            api.app,
            f"/api/docker/containers/c123/terminal?token={self.session_token}",
            client_messages=[{"type": "websocket.receive", "text": '{"type": "resize", "cols": 100, "rows": 30}'}],
        )
        self.assertTrue(auth_ws.accepted)

    # =========================================================================
    # 2. Docker Compose & Template Invariant
    # =========================================================================
    def test_docker_templates_invariant_all_11_valid(self):
        dm = main.DockerManager()
        templates = dm.get_templates()
        self.assertEqual(len(templates), 11, "Must contain exactly 11 curated homelab templates")

        for tmpl in templates:
            self.assertTrue(tmpl.get("name"), "Template must have a name")
            yaml_content = tmpl.get("compose_yaml", "")
            self.assertTrue(yaml_content.strip(), f"Template '{tmpl.get('name')}' compose_yaml cannot be empty")
            val = dm.validate_compose_yaml(yaml_content)
            self.assertTrue(
                val.get("valid"),
                f"Template '{tmpl.get('name')}' must be valid compose YAML: {val.get('errors')}",
            )

    def test_docker_compose_invariant_reject_empty_and_invalid(self):
        dm = main.DockerManager()
        # Empty compose YAML
        empty_res = dm.validate_compose_yaml("")
        self.assertFalse(empty_res.get("valid"))
        self.assertIn("cannot be empty", empty_res.get("errors", [""])[0])

        whitespace_res = dm.validate_compose_yaml("   \n\t  ")
        self.assertFalse(whitespace_res.get("valid"))

        # Invalid YAML syntax
        bad_syntax_res = dm.validate_compose_yaml("services:\n  app:\n    ports: [unclosed bracket")
        self.assertFalse(bad_syntax_res.get("valid"))
        self.assertTrue(len(bad_syntax_res.get("errors", [])) > 0)

    def test_docker_compose_invariant_env_persistence(self):
        stacks_dir = Path(self.temp_dir.name) / "stacks"
        dm = main.DockerManager(stacks_dir=stacks_dir)
        stack_name = "test_persistence_stack"
        compose_yaml = "version: '3.8'\nservices:\n  web:\n    image: nginx:alpine\n"
        env_content = "APP_ENV=production\nSECRET_KEY=123456\nPORT=8080\n"

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "_run_compose", return_value=""),
        ):
            # Save stack
            save_res = dm.save_stack(
                stack_name,
                compose_yaml,
                env_content=env_content,
                deploy=False,
            )
            self.assertEqual(save_res["name"], stack_name)
            self.assertEqual(save_res["env_content"], env_content)

            # Verify database persistence
            conn = main._connect_state_db(self.db_path)
            row = conn.execute("SELECT env_content FROM docker_stacks WHERE name = ?", (stack_name,)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], env_content)

            # Verify disk persistence
            env_file = stacks_dir / stack_name / ".env"
            self.assertTrue(env_file.is_file())
            self.assertEqual(env_file.read_text(encoding="utf-8"), env_content)

            # Verify get_stack returns exact env_content
            loaded_stack = dm.get_stack(stack_name)
            self.assertIsNotNone(loaded_stack)
            self.assertEqual(loaded_stack["env_content"], env_content)

    # =========================================================================
    # 3. Multi-Channel Alert Hub Invariant
    # =========================================================================
    def test_multichannel_alert_hub_invariant_all_6_providers(self):
        mgr = main.NotificationManager()
        expected_providers = ["discord", "telegram", "webhook", "pushover", "ntfy", "gotify"]
        self.assertEqual(set(mgr.providers.keys()), set(expected_providers))

        for p_name in expected_providers:
            prov = mgr.providers[p_name]
            self.assertIsInstance(prov.routing, dict)
            self.assertIn("raid_alerts", prov.routing)
            self.assertIn("cron_failures", prov.routing)

    def test_multichannel_alert_hub_credential_masking(self):
        cfg = {
            "channels": {
                "discord": {"enabled": True, "bot_token": "secret_bot_token_discord_12345", "channel_id": "123456"},
                "telegram": {"enabled": True, "bot_token": "secret_telegram_token_67890", "chat_id": "999"},
                "webhook": {"enabled": True, "url": "https://hooks.example.com/alerts", "headers": {}},
                "pushover": {"enabled": True, "user_key": "pushover_user_secret_abc", "api_token": "pushover_app_token_def"},
                "ntfy": {"enabled": True, "server_url": "https://ntfy.sh", "topic": "test", "access_token": "ntfy_token_secret_xyz"},
                "gotify": {"enabled": True, "server_url": "https://gotify.local", "app_token": "gotify_app_secret_123"},
            }
        }
        masked = main.get_masked_notification_center_config(cfg)
        chans = masked["channels"]

        self.assertIn("token_masked", chans["discord"])
        self.assertNotIn("secret_bot_token_discord_12345", chans["discord"]["token_masked"])
        self.assertTrue(chans["discord"]["token_masked"].startswith("••••"))
        self.assertEqual(chans["discord"]["bot_token"], "********")

        self.assertIn("token_masked", chans["telegram"])
        self.assertNotIn("secret_telegram_token_67890", chans["telegram"]["token_masked"])
        self.assertTrue(chans["telegram"]["token_masked"].startswith("••••"))
        self.assertEqual(chans["telegram"]["bot_token"], "********")

        self.assertIn("user_key_masked", chans["pushover"])
        self.assertNotIn("pushover_user_secret_abc", chans["pushover"]["user_key_masked"])
        self.assertTrue(chans["pushover"]["user_key_masked"].startswith("••••"))
        self.assertEqual(chans["pushover"]["user_key"], "********")

        self.assertIn("token_masked", chans["ntfy"])
        self.assertNotIn("ntfy_token_secret_xyz", chans["ntfy"]["token_masked"])
        self.assertTrue(chans["ntfy"]["token_masked"].startswith("••••"))
        self.assertEqual(chans["ntfy"]["access_token"], "********")

        self.assertIn("token_masked", chans["gotify"])
        self.assertNotIn("gotify_app_secret_123", chans["gotify"]["token_masked"])
        self.assertTrue(chans["gotify"]["token_masked"].startswith("••••"))
        self.assertEqual(chans["gotify"]["app_token"], "********")

    def test_multichannel_alert_hub_routing_filters(self):
        mgr = main.NotificationManager()
        # Set discord to accept raid_alerts but not cron_failures
        mgr.providers["discord"].enabled = True
        mgr.providers["discord"].routing["raid_alerts"] = True
        mgr.providers["discord"].routing["cron_failures"] = False
        mgr.providers["discord"].send_notification = AsyncMock(return_value=(True, "ok"))

        # Set telegram to reject raid_alerts but accept cron_failures
        mgr.providers["telegram"].enabled = True
        mgr.providers["telegram"].routing["raid_alerts"] = False
        mgr.providers["telegram"].routing["cron_failures"] = True
        mgr.providers["telegram"].send_notification = AsyncMock(return_value=(True, "ok"))

        # Disable all other providers
        for p in ["webhook", "pushover", "ntfy", "gotify"]:
            mgr.providers[p].enabled = False

        # Dispatch raid_alerts event
        results_raid = asyncio.run(mgr.send_event("raid_alerts", "RAID Alert", "Disk degraded"))
        self.assertIn("discord", results_raid)
        self.assertNotIn("telegram", results_raid)
        mgr.providers["discord"].send_notification.assert_awaited_once()
        mgr.providers["telegram"].send_notification.assert_not_awaited()

        # Dispatch cron_failures event
        mgr.providers["discord"].send_notification.reset_mock()
        mgr.providers["telegram"].send_notification.reset_mock()
        results_cron = asyncio.run(mgr.send_event("cron_failures", "Cron Failed", "Job backup failed"))
        self.assertNotIn("discord", results_cron)
        self.assertIn("telegram", results_cron)
        mgr.providers["discord"].send_notification.assert_not_awaited()
        mgr.providers["telegram"].send_notification.assert_awaited_once()

    # =========================================================================
    # 4. Telemetry Invariant
    # =========================================================================
    def test_telemetry_invariant_schema_and_bounds(self):
        conn = main._connect_state_db(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry_samples)").fetchall()}
        conn.close()

        expected_columns = {
            "id",
            "timestamp",
            "cpu_percent",
            "memory_percent",
            "memory_used_bytes",
            "memory_total_bytes",
            "net_rx_rate_bps",
            "net_tx_rate_bps",
            "cpu_temp",
        }
        self.assertTrue(expected_columns.issubset(cols), f"Missing telemetry columns: {expected_columns - cols}")

        # Insert telemetry samples within expected bounds
        now = int(asyncio.get_event_loop().time()) if False else 1700000000
        main.record_telemetry_sample(db_path=self.db_path, timestamp=now - 200)
        main.record_telemetry_sample(db_path=self.db_path, timestamp=now - 100)
        main.record_telemetry_sample(db_path=self.db_path, timestamp=now)

        data = main.get_telemetry_history(range_key="1h", points_limit=20, db_path=self.db_path, end_time=now)
        self.assertIn("points", data)
        self.assertIn("summary", data)

        summary = data["summary"]
        self.assertIn("cpu", summary)
        self.assertIn("memory", summary)
        self.assertGreaterEqual(data["total_samples"], 1)

        # Metric bounds invariants
        for pt in data["points"]:
            self.assertGreaterEqual(pt["cpu_percent"], 0.0)
            self.assertLessEqual(pt["cpu_percent"], 100.0)
            self.assertGreaterEqual(pt["memory_percent"], 0.0)
            self.assertLessEqual(pt["memory_percent"], 100.0)
            self.assertGreaterEqual(pt["net_rx_rate_bps"], 0.0)
            self.assertGreaterEqual(pt["net_tx_rate_bps"], 0.0)

    # =========================================================================
    # 5. Security Matrix Invariant
    # =========================================================================
    def test_security_matrix_invariant_dynamic_route_introspection(self):
        """Introspect all registered FastAPI routes to verify 100% of non-public routes require authentication."""
        from starlette.routing import WebSocketRoute

        public_http_routes = {
            "/api/auth/status",
            "/api/auth/setup",
            "/api/auth/login",
            "/api/auth/google",
            "/api/auth/logout",
        }

        unprotected_api_routes = []
        for route in api.app.routes:
            path = getattr(route, "path", None)
            if not path or not path.startswith("/api/"):
                continue

            # WebSocket routes are handled separately via handshake security
            if isinstance(route, WebSocketRoute):
                continue

            if path in public_http_routes:
                continue

            # Check route dependencies
            dependencies = getattr(route, "dependencies", []) or []
            dep_callables = [d.dependency for d in dependencies if hasattr(d, "dependency")]

            endpoint = getattr(route, "endpoint", None)
            has_auth = api.require_auth in dep_callables

            # Also check parameter dependencies in endpoint
            if endpoint and not has_auth:
                import inspect
                sig = inspect.signature(endpoint)
                for param in sig.parameters.values():
                    if hasattr(param.default, "dependency") and param.default.dependency == api.require_auth:
                        has_auth = True
                        break

            if not has_auth:
                unprotected_api_routes.append(f"{getattr(route, 'methods', set())} {path}")

        self.assertEqual(
            unprotected_api_routes,
            [],
            f"Found unprotected API routes without require_auth: {unprotected_api_routes}",
        )
