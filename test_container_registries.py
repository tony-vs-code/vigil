import asyncio
import base64
import collections
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import api
import main
from test_main import asgi_delete, asgi_get, asgi_post, asgi_put


def _make_mock_response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    json_data: dict | None = None,
):
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    else:
        resp.json = AsyncMock(return_value={})
    return resp


def _make_async_cm(value):
    return AsyncMock(
        __aenter__=AsyncMock(return_value=value),
        __aexit__=AsyncMock(return_value=False),
    )


class SecretEncryptionTests(unittest.TestCase):
    def test_encrypt_decrypt_round_trip_and_uniqueness(self):
        secret = "ghp_SuperSecretTokenValue_9876"
        ct1 = main.encrypt_secret(secret)
        ct2 = main.encrypt_secret(secret)

        self.assertTrue(ct1.startswith("enc:v1:"))
        self.assertTrue(ct2.startswith("enc:v1:"))
        self.assertNotEqual(ct1, ct2)
        self.assertNotIn(secret, ct1)

        self.assertEqual(main.decrypt_secret(ct1), secret)
        self.assertEqual(main.decrypt_secret(ct2), secret)

    def test_empty_strings(self):
        self.assertEqual(main.encrypt_secret(""), "")
        self.assertEqual(main.decrypt_secret(""), "")

    def test_non_ascii_secret_round_trip(self):
        secret = "tökén_🔐_日本語_пароль"
        ct = main.encrypt_secret(secret)
        self.assertEqual(main.decrypt_secret(ct), secret)

    def test_tampered_and_corrupted_ciphertext_returns_empty(self):
        ct = main.encrypt_secret("my-secret-pat-1234")
        # Tamper with a character inside the base64 payload
        mid = len(ct) // 2
        replacement = "A" if ct[mid] != "A" else "B"
        tampered = ct[:mid] + replacement + ct[mid + 1 :]

        self.assertEqual(main.decrypt_secret(tampered), "")
        self.assertEqual(main.decrypt_secret("not-a-valid-ciphertext"), "")
        self.assertEqual(main.decrypt_secret("enc:v1:!!!invalid_b64!!!"), "")
        self.assertEqual(main.decrypt_secret("enc:v1:AAAA"), "")

    def test_wrong_master_key_fails_gracefully(self):
        with patch.object(main, "AUTH_SECRET_KEY", "master-key-alpha"):
            ct = main.encrypt_secret("pat_secret_alpha")
        with patch.object(main, "AUTH_SECRET_KEY", "master-key-beta"):
            self.assertEqual(main.decrypt_secret(ct), "")


class ContainerRegistryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_registries.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.init_db()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_init_db_creates_container_registries_table(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(container_registries)")
            }
        finally:
            conn.close()

        expected_cols = {
            "id",
            "name",
            "url",
            "username",
            "encrypted_token",
            "is_default",
            "status",
            "last_tested_at",
            "last_error",
            "created_at",
            "updated_at",
        }
        self.assertTrue(expected_cols.issubset(cols))

    def test_crud_encryption_at_rest_and_token_masking(self):
        raw_token = "ghp_TopSecretPat5678"
        created = main.create_container_registry(
            {
                "name": "GitHub Container Registry",
                "url": "ghcr.io/",
                "username": "octocat",
                "token": raw_token,
                "is_default": True,
            }
        )
        reg_id = created["id"]

        # Masked response must never leak plaintext or ciphertext
        self.assertEqual(created["url"], "https://ghcr.io")
        self.assertEqual(created["token_masked"], "••••5678")
        self.assertTrue(created["token_configured"])
        self.assertTrue(created["is_default"])
        self.assertEqual(created["status"], "unverified")
        self.assertNotIn("token", created)
        self.assertNotIn("encrypted_token", created)
        self.assertNotIn(raw_token, json.dumps(created))

        # Verify raw DB row stores encrypted token, not plaintext
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT encrypted_token FROM container_registries WHERE id = ?",
                (reg_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].startswith("enc:v1:"))
        self.assertNotIn(raw_token, row[0])
        self.assertEqual(main.decrypt_secret(row[0]), raw_token)

        # Short token masking
        short_reg = main.create_container_registry(
            {
                "name": "Local Registry",
                "url": "http://localhost:5000",
                "token": "1234",
            }
        )
        self.assertEqual(short_reg["token_masked"], "••••")
        self.assertTrue(short_reg["token_configured"])

        # Empty token masking
        anon_reg = main.create_container_registry(
            {
                "name": "Public Quay",
                "url": "https://quay.io",
            }
        )
        self.assertEqual(anon_reg["token_masked"], "")
        self.assertFalse(anon_reg["token_configured"])

        # List and get (default masked vs include_decrypted_token=True)
        listed = main.list_container_registries()
        self.assertEqual(len(listed), 3)
        for item in listed:
            self.assertNotIn("token", item)
            self.assertNotIn("encrypted_token", item)
            self.assertNotIn(raw_token, json.dumps(item))

        fetched_decrypted = main.get_container_registry(
            reg_id, include_decrypted_token=True
        )
        self.assertEqual(fetched_decrypted["token"], raw_token)

        # Update preserving token via omission and via "********"
        upd1 = main.update_container_registry(reg_id, {"name": "GHCR Prod"})
        self.assertEqual(upd1["name"], "GHCR Prod")
        self.assertEqual(upd1["token_masked"], "••••5678")
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            raw_token,
        )

        upd2 = main.update_container_registry(reg_id, {"token": "********"})
        self.assertEqual(upd2["token_masked"], "••••5678")
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            raw_token,
        )

        # Update with new token re-encrypts
        upd3 = main.update_container_registry(reg_id, {"token": "ghp_Rotated9999"})
        self.assertEqual(upd3["token_masked"], "••••9999")
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            "ghp_Rotated9999",
        )

        # Update with empty string clears token
        upd4 = main.update_container_registry(reg_id, {"token": ""})
        self.assertEqual(upd4["token_masked"], "")
        self.assertFalse(upd4["token_configured"])
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            "",
        )

        # Setting another registry as default clears previous default
        main.update_container_registry(short_reg["id"], {"is_default": True})
        self.assertFalse(main.get_container_registry(reg_id)["is_default"])
        self.assertTrue(main.get_container_registry(short_reg["id"])["is_default"])

        # Delete
        self.assertTrue(main.delete_container_registry(reg_id))
        self.assertFalse(main.delete_container_registry(reg_id))
        self.assertIsNone(main.get_container_registry(reg_id))

    def test_connection_test_success_bearer_and_failure_branches(self):
        reg = main.create_container_registry(
            {
                "name": "Harbor",
                "url": "https://harbor.example.com",
                "username": "robot$vigil",
                "token": "harbor_pat_4321",
            }
        )

        # 1. Direct HTTP 200
        mock_session_ok = MagicMock()
        mock_session_ok.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(200))
        )
        with patch("aiohttp.ClientSession", return_value=_make_async_cm(mock_session_ok)):
            res_ok = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertTrue(res_ok["success"])
        self.assertEqual(res_ok["status"], "verified")
        self.assertEqual(res_ok["status_code"], 200)
        updated_ok = main.get_container_registry(reg["id"])
        self.assertEqual(updated_ok["status"], "verified")
        self.assertGreater(updated_ok["last_tested_at"], 0)
        self.assertEqual(updated_ok["last_error"], "")

        # 2. Bearer challenge -> token exchange -> 200
        resp_challenge = _make_mock_response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer realm="https://harbor.example.com/service/token",service="harbor-registry"'
            },
        )
        resp_token = _make_mock_response(200, json_data={"token": "jwt_token_ok"})
        resp_retry = _make_mock_response(200)
        mock_session_bearer = MagicMock()
        mock_session_bearer.get = MagicMock(
            side_effect=[
                _make_async_cm(resp_challenge),
                _make_async_cm(resp_token),
                _make_async_cm(resp_retry),
            ]
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_bearer)
        ):
            res_bearer = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertTrue(res_bearer["success"])
        self.assertEqual(res_bearer["status"], "verified")

        # 3. 401 unauthorized failure
        mock_session_401 = MagicMock()
        mock_session_401.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(401))
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_401)
        ):
            res_401 = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertFalse(res_401["success"])
        self.assertEqual(res_401["status"], "error")
        self.assertEqual(res_401["status_code"], 401)
        updated_err = main.get_container_registry(reg["id"])
        self.assertEqual(updated_err["status"], "error")
        self.assertIn("401", updated_err["last_error"])

        # 4. Network timeout / ClientError
        mock_session_err = MagicMock()
        mock_session_err.get = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_err)
        ):
            res_net = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertFalse(res_net["success"])
        self.assertEqual(res_net["status"], "error")
        self.assertIsNone(res_net["status_code"])
        self.assertIn("connection refused", res_net["message"].lower())

        # 5. Docker Hub URL maps to registry-1.docker.io/v2/
        mock_session_dh = MagicMock()
        mock_session_dh.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(200))
        )
        with patch("aiohttp.ClientSession", return_value=_make_async_cm(mock_session_dh)):
            res_dh = asyncio.run(
                main.test_container_registry_connection(url="docker.io")
            )
        self.assertTrue(res_dh["success"])
        called_url = mock_session_dh.get.call_args.args[0]
        self.assertEqual(called_url, "https://registry-1.docker.io/v2/")

    def test_connection_test_timeout_and_auth_challenge_edge_cases(self):
        reg = main.create_container_registry(
            {
                "name": "Quay Private",
                "url": "https://quay.io",
                "username": "ci-bot",
                "token": "quay_pat_secret_1234",
            }
        )

        # 1. Explicit asyncio.TimeoutError -> error persisted, status_code is None
        mock_session_timeout = MagicMock()
        mock_session_timeout.get = MagicMock(side_effect=asyncio.TimeoutError())
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_timeout)
        ):
            res_timeout = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertFalse(res_timeout["success"])
        self.assertEqual(res_timeout["status"], "error")
        self.assertIsNone(res_timeout["status_code"])
        self.assertIn("failed", res_timeout["message"].lower())
        persisted_timeout = main.get_container_registry(reg["id"])
        self.assertEqual(persisted_timeout["status"], "error")
        self.assertTrue(persisted_timeout["last_error"])

        # 2. Bearer challenge where token exchange endpoint returns 401 -> fails gracefully
        resp_challenge = _make_mock_response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer realm="https://quay.io/v2/auth",service="quay.io",scope="repository:org/app:pull"'
            },
        )
        resp_token_fail = _make_mock_response(401)
        mock_session_bearer_fail = MagicMock()
        mock_session_bearer_fail.get = MagicMock(
            side_effect=[
                _make_async_cm(resp_challenge),
                _make_async_cm(resp_token_fail),
            ]
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=_make_async_cm(mock_session_bearer_fail),
        ):
            res_bearer_fail = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertFalse(res_bearer_fail["success"])
        self.assertEqual(res_bearer_fail["status"], "error")
        self.assertEqual(res_bearer_fail["status_code"], 401)
        self.assertIn("authentication failed", res_bearer_fail["message"].lower())

        # 3. 401 without token -> message notes registry requires authentication
        mock_session_anon_401 = MagicMock()
        mock_session_anon_401.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(401))
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_anon_401)
        ):
            res_anon_401 = asyncio.run(
                main.test_container_registry_connection(url="https://ghcr.io")
            )
        self.assertFalse(res_anon_401["success"])
        self.assertEqual(res_anon_401["status_code"], 401)
        self.assertIn("requires authentication", res_anon_401["message"].lower())

        # 4. 403 with token -> authentication failed; 500 -> generic HTTP status error
        mock_session_403 = MagicMock()
        mock_session_403.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(403))
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_403)
        ):
            res_403 = asyncio.run(
                main.test_container_registry_connection(registry_id=reg["id"])
            )
        self.assertFalse(res_403["success"])
        self.assertEqual(res_403["status_code"], 403)
        self.assertIn("authentication failed", res_403["message"].lower())

        mock_session_500 = MagicMock()
        mock_session_500.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(500))
        )
        with patch(
            "aiohttp.ClientSession", return_value=_make_async_cm(mock_session_500)
        ):
            res_500 = asyncio.run(
                main.test_container_registry_connection(url="https://harbor.local")
            )
        self.assertFalse(res_500["success"])
        self.assertEqual(res_500["status_code"], 500)
        self.assertIn("500", res_500["message"])

    def test_update_preserves_verified_status_on_name_change_and_resets_on_credential_change(self):
        reg = main.create_container_registry(
            {
                "name": "GHCR",
                "url": "https://ghcr.io",
                "username": "octocat",
                "token": "ghp_InitialToken9999",
            }
        )
        mock_session_ok = MagicMock()
        mock_session_ok.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(200))
        )
        with patch("aiohttp.ClientSession", return_value=_make_async_cm(mock_session_ok)):
            asyncio.run(main.test_container_registry_connection(registry_id=reg["id"]))

        verified = main.get_container_registry(reg["id"])
        self.assertEqual(verified["status"], "verified")

        # Updating only name/is_default preserves verified status and encrypted token
        renamed = main.update_container_registry(
            reg["id"], {"name": "GHCR Team", "is_default": True}
        )
        self.assertEqual(renamed["status"], "verified")
        self.assertEqual(renamed["token_masked"], "••••9999")
        self.assertEqual(
            main.get_container_registry(reg["id"], include_decrypted_token=True)["token"],
            "ghp_InitialToken9999",
        )

        # Updating URL/username/token resets status to unverified
        repointed = main.update_container_registry(
            reg["id"], {"url": "https://registry.example.org"}
        )
        self.assertEqual(repointed["status"], "unverified")
        self.assertEqual(repointed["last_tested_at"], 0.0)
        self.assertEqual(repointed["last_error"], "")


class ContainerRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_registries.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_auth_required_on_all_registry_endpoints(self):
        self.assertEqual(asgi_get(api.app, "/api/docker/registries").status_code, 401)
        self.assertEqual(
            asgi_post(api.app, "/api/docker/registries", json_body={}).status_code, 401
        )
        self.assertEqual(
            asgi_post(api.app, "/api/docker/registries/test", json_body={}).status_code,
            401,
        )
        self.assertEqual(
            asgi_get(api.app, "/api/docker/registries/some-id").status_code, 401
        )
        self.assertEqual(
            asgi_put(api.app, "/api/docker/registries/some-id", json_body={}).status_code,
            401,
        )
        self.assertEqual(
            asgi_delete(api.app, "/api/docker/registries/some-id").status_code, 401
        )
        self.assertEqual(
            asgi_post(api.app, "/api/docker/registries/some-id/test").status_code, 401
        )

    def test_registry_api_crud_masking_test_and_audit_logs(self):
        # Empty list
        list_resp = asgi_get(
            api.app, "/api/docker/registries", cookies=self.auth_cookies
        )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json(), {"registries": []})

        # Validation error -> 400
        bad_create = asgi_post(
            api.app,
            "/api/docker/registries",
            json_body={"name": "", "url": "ghcr.io"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_create.status_code, 400)

        bad_url = asgi_post(
            api.app,
            "/api/docker/registries",
            json_body={"name": "Bad", "url": "ftp://invalid.local"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_url.status_code, 400)

        # Create -> 201
        secret_pat = "glpat-SecretToken7788"
        create_resp = asgi_post(
            api.app,
            "/api/docker/registries",
            json_body={
                "name": "GitLab Registry",
                "url": "registry.gitlab.com",
                "username": "deploy-bot",
                "token": secret_pat,
                "is_default": True,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(create_resp.status_code, 201)
        created = create_resp.json()["registry"]
        reg_id = created["id"]
        self.assertEqual(created["url"], "https://registry.gitlab.com")
        self.assertEqual(created["token_masked"], "••••7788")
        self.assertTrue(created["token_configured"])
        self.assertNotIn(secret_pat, create_resp.text)
        self.assertNotIn("encrypted_token", create_resp.text)

        # Get single -> 200, masked
        get_resp = asgi_get(
            api.app, f"/api/docker/registries/{reg_id}", cookies=self.auth_cookies
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["registry"]["token_masked"], "••••7788")
        self.assertNotIn(secret_pat, get_resp.text)
        self.assertNotIn("encrypted_token", get_resp.text)

        # Get nonexistent -> 404
        self.assertEqual(
            asgi_get(
                api.app, "/api/docker/registries/missing-id", cookies=self.auth_cookies
            ).status_code,
            404,
        )

        # Update preserving masked token -> 200
        put_keep = asgi_put(
            api.app,
            f"/api/docker/registries/{reg_id}",
            json_body={"name": "GitLab Prod", "token": "********"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(put_keep.status_code, 200)
        self.assertEqual(put_keep.json()["registry"]["name"], "GitLab Prod")
        self.assertEqual(put_keep.json()["registry"]["token_masked"], "••••7788")
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            secret_pat,
        )

        # Update omitting token key entirely -> preserves existing encrypted token
        put_omit = asgi_put(
            api.app,
            f"/api/docker/registries/{reg_id}",
            json_body={"name": "GitLab Prod v2"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(put_omit.status_code, 200)
        self.assertEqual(put_omit.json()["registry"]["token_masked"], "••••7788")
        self.assertNotIn(secret_pat, put_omit.text)
        self.assertNotIn("encrypted_token", put_omit.text)
        self.assertEqual(
            main.get_container_registry(reg_id, include_decrypted_token=True)["token"],
            secret_pat,
        )

        # List populated endpoint -> masked, never leaks secret or encrypted_token
        list_populated = asgi_get(
            api.app, "/api/docker/registries", cookies=self.auth_cookies
        )
        self.assertEqual(list_populated.status_code, 200)
        self.assertEqual(len(list_populated.json()["registries"]), 1)
        self.assertEqual(
            list_populated.json()["registries"][0]["token_masked"], "••••7788"
        )
        self.assertNotIn(secret_pat, list_populated.text)
        self.assertNotIn("encrypted_token", list_populated.text)

        # Test connection ad-hoc invalid URL -> 400
        self.assertEqual(
            asgi_post(
                api.app,
                "/api/docker/registries/test",
                json_body={"url": "https://invalid host with spaces"},
                cookies=self.auth_cookies,
            ).status_code,
            400,
        )

        # Update with invalid name -> 400; missing id -> 404
        self.assertEqual(
            asgi_put(
                api.app,
                f"/api/docker/registries/{reg_id}",
                json_body={"name": "   "},
                cookies=self.auth_cookies,
            ).status_code,
            400,
        )
        self.assertEqual(
            asgi_put(
                api.app,
                "/api/docker/registries/missing-id",
                json_body={"name": "Nope"},
                cookies=self.auth_cookies,
            ).status_code,
            404,
        )

        # Test connection ad-hoc missing params -> 400; missing registry_id -> 404
        self.assertEqual(
            asgi_post(
                api.app,
                "/api/docker/registries/test",
                json_body={},
                cookies=self.auth_cookies,
            ).status_code,
            400,
        )
        self.assertEqual(
            asgi_post(
                api.app,
                "/api/docker/registries/test",
                json_body={"registry_id": "missing-id"},
                cookies=self.auth_cookies,
            ).status_code,
            404,
        )

        # Test ad-hoc success (mocked aiohttp) -> 200
        mock_session_ok = MagicMock()
        mock_session_ok.get = MagicMock(
            return_value=_make_async_cm(_make_mock_response(200))
        )
        with patch("aiohttp.ClientSession", return_value=_make_async_cm(mock_session_ok)):
            adhoc_resp = asgi_post(
                api.app,
                "/api/docker/registries/test",
                json_body={
                    "url": "https://ghcr.io",
                    "username": "user",
                    "token": "ghp_adhoc",
                },
                cookies=self.auth_cookies,
            )
        self.assertEqual(adhoc_resp.status_code, 200)
        self.assertTrue(adhoc_resp.json()["success"])
        self.assertEqual(adhoc_resp.json()["status"], "verified")

        # Test specific registry by ID -> 200 and updates status
        with patch("aiohttp.ClientSession", return_value=_make_async_cm(mock_session_ok)):
            id_test_resp = asgi_post(
                api.app,
                f"/api/docker/registries/{reg_id}/test",
                cookies=self.auth_cookies,
            )
        self.assertEqual(id_test_resp.status_code, 200)
        self.assertTrue(id_test_resp.json()["success"])
        self.assertEqual(id_test_resp.json()["registry"]["status"], "verified")

        # Test missing registry by ID -> 404
        self.assertEqual(
            asgi_post(
                api.app,
                "/api/docker/registries/missing-id/test",
                cookies=self.auth_cookies,
            ).status_code,
            404,
        )

        # Delete -> 200, second delete -> 404
        del_resp = asgi_delete(
            api.app, f"/api/docker/registries/{reg_id}", cookies=self.auth_cookies
        )
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["success"])
        self.assertEqual(
            asgi_delete(
                api.app, f"/api/docker/registries/{reg_id}", cookies=self.auth_cookies
            ).status_code,
            404,
        )

        # Audit logs recorded for create, update, test, delete without leaking secret
        logs, total = main.get_audit_logs(limit=50)
        event_types = [entry["event_type"] for entry in logs]
        self.assertIn("docker.registry.create", event_types)
        self.assertIn("docker.registry.update", event_types)
        self.assertIn("docker.registry.test", event_types)
        self.assertIn("docker.registry.delete", event_types)
        logs_serialized = json.dumps(logs)
        self.assertNotIn(secret_pat, logs_serialized)
        self.assertNotIn("ghp_adhoc", logs_serialized)


class ContainerRegistryFrontendContractsTests(unittest.TestCase):
    def test_zero_duplicate_html_ids_and_required_registry_dom_contracts(self):
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        all_ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
        duplicates = {k: v for k, v in collections.Counter(all_ids).items() if v > 1}
        self.assertEqual(
            duplicates,
            {},
            f"Duplicate HTML IDs found in static/index.html: {duplicates}",
        )

        required_dom_ids = [
            "subtab-docker-registries",
            "subtab-badge-registries",
            "subview-docker-registries",
            "btn-add-registry",
            "docker-registries-feedback",
            "registries-empty-state",
            "btn-empty-add-registry",
            "registries-table-container",
            "docker-registries-table",
            "docker-registries-tbody",
            "modal-registry",
            "registry-modal-title",
            "registry-modal-subtitle",
            "btn-close-registry-modal",
            "btn-preset-dockerhub",
            "btn-preset-ghcr",
            "btn-preset-quay",
            "registry-form-test-feedback",
            "registry-form",
            "registry-form-name",
            "registry-form-url",
            "registry-form-username",
            "registry-form-token",
            "btn-toggle-registry-token",
            "registry-token-hint",
            "registry-form-is-default",
            "btn-test-registry-form",
            "btn-cancel-registry",
            "btn-save-registry",
            "docker-image-pull-input",
            "docker-image-pull-registry",
            "btn-pull-image",
            "docker-image-feedback",
            "docker-images-table",
            "docker-images-tbody",
        ]
        for dom_id in required_dom_ids:
            self.assertIn(
                f'id="{dom_id}"',
                html,
                f"Missing required DOM element id='{dom_id}' in static/index.html",
            )

        self.assertIn("<th>Registry</th>", html)
        self.assertIn('<td colspan="7" class="empty-state">No images found.</td>', html)

        required_css_selectors = [
            ".status-badge.status-verified",
            ".status-badge.status-unverified",
            ".registries-empty-state",
            ".registry-modal-card",
            ".registry-default-pill",
            ".registry-token-mask",
            ".btn-preset-chip",
            ".pull-registry-select",
            ".pull-auth-badge",
            ".registry-pill",
            ".registry-pill.authenticated",
        ]
        for sel in required_css_selectors:
            self.assertIn(sel, css, f"Missing CSS selector {sel} in static/styles.css")

        for fn_sig in [
            "function updateRegistriesSubtabBadge(",
            "function populateImagePullRegistrySelect(",
            "function extractImageRegistryHost(",
            "function registryMatchesImageHost(",
            "function resolveImageRegistryBadgeHtml(",
            "function renderDockerImages(",
            "function renderContainerRegistries(",
            "async function fetchContainerRegistries(",
            "function openRegistryModal(",
            "function closeRegistryModal(",
            "function applyRegistryPreset(",
            "async function handleTestRegistryForm(",
            "async function handleSaveRegistry(",
            "async function handleTestRegistryRow(",
            "async function handleDeleteRegistry(",
            "async function handlePullImage(",
        ]:
            self.assertIn(fn_sig, js, f"Missing JS function '{fn_sig}' in static/app.js")

    def test_subtab_switching_rendering_and_modal_preservation_in_node_vm(self):
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) idx = source.indexOf('function ' + name + '(');
  if (idx === -1) throw new Error('Function not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(idx, i + 1);
    }
    i++;
  }
  throw new Error('Unbalanced braces in ' + name);
}

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  };
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      innerHTML: '',
      value: '',
      placeholder: '',
      type: 'text',
      checked: false,
      disabled: false,
      attrs: {},
      className: '',
      classList: makeClassList(id.startsWith('subview-docker-') || id === 'modal-registry' ? ['hidden'] : []),
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k]; },
      focus() {},
      querySelectorAll() { return []; },
    };
  }
  return elements[id];
}

// Pre-initialize subtabs/subviews
for (const st of ['stacks', 'containers', 'images', 'volumes', 'networks', 'updates', 'registries']) {
  getEl('subtab-docker-' + st);
  getEl('subview-docker-' + st);
}

const fetchCalls = [];
const historyCalls = [];
const toasts = [];
let fetchRegistriesCalls = 0;

const context = vm.createContext({
  console,
  Set,
  Array,
  Number,
  String,
  Boolean,
  encodeURIComponent,
  Date,
  document: { getElementById: getEl },
  window: { location: { hash: '#containers/stacks' } },
  history: { replaceState: (_a, _b, url) => historyCalls.push(url) },
  currentViewTab: 'containers',
  currentDockerSubtab: 'stacks',
  isAuthenticated: true,
  isFetchingDockerStacks: false,
  isFetchingDockerContainers: false,
  isFetchingDockerImages: false,
  isFetchingDockerVolumes: false,
  isFetchingDockerNetworks: false,
  isFetchingDockerUpdates: false,
  isFetchingContainerRegistries: false,
  lastDockerSubtabFetchTime: 0,
  cachedDockerUpdates: [{ container_id: 'c1' }],
  containerRegistries: [],
  editingRegistryId: null,
  editingRegistryTokenConfigured: false,
  testingRegistryIds: new Set(),
  syncSidebarSubmenus: () => {},
  sendWsSubscription: () => {},
  fetchDockerStacks: () => {},
  fetchDockerContainers: () => {},
  fetchDockerImages: () => {},
  fetchDockerVolumes: () => {},
  fetchDockerNetworks: () => {},
  fetchDockerUpdatesConfig: () => {},
  fetchUpdateSchedule: () => {},
  fetchDockerUpdates: () => {},
  renderDockerUpdates: () => {},
  fetchContainerRegistries: async () => { fetchRegistriesCalls++; },
  updateEyeIcon: () => {},
  showToast: (t) => toasts.push(t),
  escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  formatRelativeTime: () => '2m ago',
  formatDateTime: () => '2026-11-28 00:00:00',
  apiFetch: async (url, opts) => {
    fetchCalls.push({ url, opts });
    return { ok: true, status: 200, json: async () => ({ success: true, registry: { id: 'r1' } }) };
  },
});

vm.runInContext(
  'const REGISTRY_PRESETS = { dockerhub: { name: "Docker Hub", url: "https://registry-1.docker.io" }, ghcr: { name: "GitHub GHCR", url: "https://ghcr.io" }, quay: { name: "Quay.io", url: "https://quay.io" } };',
  context
);

for (const fnName of [
  'switchDockerSubtab',
  'isDockerSubtabInFlight',
  'refreshDockerSubtab',
  'updateRegistriesSubtabBadge',
  'setRegistryAlertBox',
  'formatRegistryStatusBadge',
  'renderContainerRegistries',
  'openRegistryModal',
  'closeRegistryModal',
  'applyRegistryPreset',
  'handleSaveRegistry',
]) {
  vm.runInContext(extractFunc(code, fnName), context);
}

// 1. Subtab switching to 'registries' and back to 'containers'
context.switchDockerSubtab('registries', true);
if (context.currentDockerSubtab !== 'registries') throw new Error('Expected currentDockerSubtab=registries');
if (!getEl('subtab-docker-registries').classList.contains('active')) throw new Error('Registries subtab not active');
if (getEl('subtab-docker-registries').attrs['aria-selected'] !== 'true') throw new Error('aria-selected not true');
if (getEl('subview-docker-registries').classList.contains('hidden')) throw new Error('Registries subview still hidden');
if (!getEl('subview-docker-stacks').classList.contains('hidden')) throw new Error('Stacks subview should be hidden');
if (historyCalls[historyCalls.length - 1] !== '#containers/registries') throw new Error('Hash not updated to #containers/registries');
if (fetchRegistriesCalls !== 1) throw new Error('fetchContainerRegistries not invoked on switch');

context.isFetchingContainerRegistries = true;
if (!context.isDockerSubtabInFlight('registries')) throw new Error('isDockerSubtabInFlight(registries) should be true');
context.isFetchingContainerRegistries = false;

context.switchDockerSubtab('containers', true);
if (!getEl('subview-docker-registries').classList.contains('hidden')) throw new Error('Registries subview should hide when switching away');
if (getEl('subtab-docker-registries').classList.contains('active')) throw new Error('Registries subtab should lose active class');

// 2. Empty state vs populated state rendering & token masking
context.containerRegistries = [];
context.renderContainerRegistries();
if (getEl('registries-empty-state').classList.contains('hidden')) throw new Error('Empty state should be visible when list is empty');
if (!getEl('registries-table-container').classList.contains('hidden')) throw new Error('Table container should be hidden when list is empty');
if (getEl('subtab-badge-registries').textContent !== '0') throw new Error('Badge should be 0');

context.containerRegistries = [
  {
    id: 'reg-1',
    name: 'GitHub GHCR',
    url: 'https://ghcr.io',
    username: 'octocat',
    token_masked: '••••7788',
    token_configured: true,
    is_default: true,
    status: 'verified',
    last_tested_at: 1700000000,
    last_error: '',
  },
  {
    id: 'reg-2',
    name: 'Local Registry',
    url: 'http://localhost:5000',
    username: '',
    token_masked: '',
    token_configured: false,
    is_default: false,
    status: 'error',
    last_tested_at: 0,
    last_error: 'Connection refused',
  },
];
context.renderContainerRegistries();
if (!getEl('registries-empty-state').classList.contains('hidden')) throw new Error('Empty state should be hidden when populated');
if (getEl('registries-table-container').classList.contains('hidden')) throw new Error('Table container should be visible when populated');
if (getEl('subtab-badge-registries').textContent !== '2') throw new Error('Badge should be 2');
const tbodyHtml = getEl('docker-registries-tbody').innerHTML;
if (!tbodyHtml.includes('••••7788')) throw new Error('Masked token missing from rendered table');
if (!tbodyHtml.includes('Default')) throw new Error('Default pill missing');
if (!tbodyHtml.includes('Verified')) throw new Error('Verified badge missing');
if (!tbodyHtml.includes('Anonymous')) throw new Error('Anonymous fallback missing');
if (!tbodyHtml.includes('Connection refused')) throw new Error('Error snippet missing');

// 3. Preset application & modal edit preserving existing token when input left blank
context.openRegistryModal(null);
if (getEl('modal-registry').classList.contains('hidden')) throw new Error('Modal should open');
context.applyRegistryPreset('ghcr');
if (getEl('registry-form-url').value !== 'https://ghcr.io') throw new Error('Preset URL mismatch');
if (getEl('registry-form-name').value !== 'GitHub GHCR') throw new Error('Preset Name mismatch');
context.closeRegistryModal();
if (!getEl('modal-registry').classList.contains('hidden')) throw new Error('Modal should close');

context.openRegistryModal(context.containerRegistries[0]);
if (getEl('registry-form-token').value !== '') throw new Error('Edit modal must not prefill raw token');
if (getEl('registry-form-token').placeholder !== '••••••••') throw new Error('Edit modal should show masked placeholder');
(async () => {
  await context.handleSaveRegistry();
  if (fetchCalls.length !== 1) throw new Error('Expected 1 save API call');
  const sentBody = JSON.parse(fetchCalls[0].opts.body);
  if (fetchCalls[0].url !== '/api/docker/registries/reg-1' || fetchCalls[0].opts.method !== 'PUT') {
    throw new Error('Unexpected PUT endpoint/method: ' + JSON.stringify(fetchCalls[0]));
  }
  if (sentBody.token !== '********') {
    throw new Error('Expected sentinel ******** when blank token saved on configured registry, got: ' + sentBody.token);
  }
  console.log('NODE_VM_PASS');
})().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node VM test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
        self.assertIn("NODE_VM_PASS", proc.stdout)


class RegistryAuthenticatedPullTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pull_registries.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_extract_registry_host_from_image(self):
        self.assertEqual(
            main.extract_registry_host_from_image("ghcr.io/user/repo:tag"), "ghcr.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image("quay.io/org/app"), "quay.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image("reg.local:5000/app:v1"),
            "reg.local:5000",
        )
        self.assertEqual(
            main.extract_registry_host_from_image("localhost:5000/myimg"),
            "localhost:5000",
        )
        self.assertEqual(
            main.extract_registry_host_from_image("redis:latest"), "docker.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image("library/nginx"), "docker.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image("myorg/myrepo:1.0"), "docker.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image("docker.io/user/repo:v2"), "docker.io"
        )
        self.assertEqual(
            main.extract_registry_host_from_image(
                "ghcr.io/org/app@sha256:abcdef123456"
            ),
            "ghcr.io",
        )

    def test_resolve_registry_for_image_matching_default_and_no_match(self):
        # No registries configured yet
        self.assertIsNone(main.resolve_registry_for_image("ghcr.io/org/app:v1"))

        ghcr = main.create_container_registry(
            {
                "name": "GHCR",
                "url": "https://ghcr.io",
                "username": "octocat",
                "token": "ghp_secret_1234",
                "is_default": False,
            }
        )
        dhub = main.create_container_registry(
            {
                "name": "Docker Hub",
                "url": "https://index.docker.io/v1/",
                "username": "dhuser",
                "token": "dckr_pat_5678",
                "is_default": True,
            }
        )
        # Exact match for ghcr.io
        resolved_ghcr = main.resolve_registry_for_image("ghcr.io/octocat/app:latest")
        self.assertIsNotNone(resolved_ghcr)
        self.assertEqual(resolved_ghcr["id"], ghcr["id"])
        self.assertEqual(resolved_ghcr["username"], "octocat")
        self.assertEqual(resolved_ghcr["decrypted_token"], "ghp_secret_1234")
        self.assertEqual(resolved_ghcr["server_address"], "ghcr.io")

        # Docker Hub match for bare image tag
        resolved_hub = main.resolve_registry_for_image("redis:alpine")
        self.assertIsNotNone(resolved_hub)
        self.assertEqual(resolved_hub["id"], dhub["id"])
        self.assertEqual(resolved_hub["decrypted_token"], "dckr_pat_5678")
        self.assertEqual(resolved_hub["server_address"], "https://index.docker.io/v1/")

        # Unmatched host falls back to is_default=1
        resolved_fallback = main.resolve_registry_for_image("quay.io/org/app:v1")
        self.assertIsNotNone(resolved_fallback)
        self.assertEqual(resolved_fallback["id"], dhub["id"])

        # Remove default flag and check non-matching host returns None
        main.update_container_registry(dhub["id"], {"is_default": False})
        self.assertIsNone(main.resolve_registry_for_image("quay.io/org/app:v1"))

        # Registry without credentials returns None
        main.create_container_registry(
            {
                "name": "Public Quay",
                "url": "https://quay.io",
            }
        )
        self.assertIsNone(main.resolve_registry_for_image("quay.io/org/app:v1"))

    def test_pull_image_authenticated_config_json_cleanup_and_audit(self):
        ghcr = main.create_container_registry(
            {
                "name": "Company GHCR",
                "url": "https://ghcr.io",
                "username": "ci-bot",
                "token": "ghp_pull_secret_9999",
            }
        )

        observed_config_dir = None
        observed_config_json = None

        def fake_run_docker(args, timeout=300):
            nonlocal observed_config_dir, observed_config_json
            self.assertEqual(args[0], "--config")
            observed_config_dir = Path(args[1])
            self.assertEqual(os.stat(observed_config_dir).st_mode & 0o777, 0o700)
            self.assertEqual(args[2:], ["pull", "ghcr.io/myorg/app:v2"])
            cfg_file = observed_config_dir / "config.json"
            self.assertTrue(cfg_file.exists())
            self.assertEqual(os.stat(cfg_file).st_mode & 0o777, 0o600)
            observed_config_json = json.loads(cfg_file.read_text(encoding="utf-8"))
            return "Status: Downloaded newer image for ghcr.io/myorg/app:v2"

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", side_effect=fake_run_docker
            ),
        ):
            res = main.docker_manager.pull_image("ghcr.io/myorg/app:v2")

        self.assertTrue(res["success"])
        self.assertEqual(res["image"], "ghcr.io/myorg/app:v2")
        self.assertTrue(res["authenticated"])
        self.assertEqual(res["registry_name"], "Company GHCR")

        # Validate multi-alias auth config content and credentials
        expected_auth = base64.b64encode(b"ci-bot:ghp_pull_secret_9999").decode("ascii")
        expected_entry = {
            "auth": expected_auth,
            "username": "ci-bot",
            "password": "ghp_pull_secret_9999",
        }
        for key in (
            "ghcr.io",
            "https://ghcr.io",
            "https://ghcr.io/v1/",
            "https://ghcr.io/v2/",
        ):
            self.assertIn(key, observed_config_json["auths"])
            self.assertEqual(observed_config_json["auths"][key], expected_entry)

        # Temp directory must be cleaned up after pull
        self.assertIsNotNone(observed_config_dir)
        self.assertFalse(observed_config_dir.exists())

        # Audit log recorded without leaking token
        logs, _ = main.get_audit_logs(limit=20, event_type="docker.image.pull")
        self.assertEqual(len(logs), 1)
        self.assertIn("Company GHCR", logs[0]["description"])
        self.assertNotIn("ghp_pull_secret_9999", json.dumps(logs))

        # Test temp directory cleanup even when _run_docker raises an exception
        failing_dir = None

        def failing_run_docker(args, timeout=300):
            nonlocal failing_dir
            failing_dir = Path(args[1])
            self.assertTrue((failing_dir / "config.json").exists())
            raise RuntimeError("pull failed: unauthorized")

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", side_effect=failing_run_docker
            ),
        ):
            with self.assertRaises(RuntimeError):
                main.docker_manager.pull_image(
                    "custom.reg/app:v1", registry_id=ghcr["id"]
                )

        self.assertIsNotNone(failing_dir)
        self.assertFalse(failing_dir.exists())

    def test_pull_image_unauthenticated_fallback(self):
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", return_value="Pulled alpine"
            ) as mock_run,
        ):
            res = main.docker_manager.pull_image("alpine:latest")

        mock_run.assert_called_once_with(["pull", "alpine:latest"], timeout=300)
        self.assertTrue(res["success"])
        self.assertFalse(res["authenticated"])
        self.assertIsNone(res["registry_name"])

    def test_api_docker_images_pull_with_and_without_registry_id(self):
        reg = main.create_container_registry(
            {
                "name": "Private Harbor",
                "url": "https://harbor.internal:8443",
                "username": "robot",
                "token": "harbor_secret_0001",
            }
        )

        # 1. Explicit registry_id passed to POST /api/docker/images/pull
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", return_value="Pulled"
            ) as mock_run,
        ):
            resp = asgi_post(
                api.app,
                "/api/docker/images/pull",
                json_body={
                    "image": "harbor.internal:8443/team/svc:1.2",
                    "registry_id": reg["id"],
                },
                cookies=self.auth_cookies,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["registry_name"], "Private Harbor")
        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args[0], "--config")
        self.assertEqual(called_args[2:], ["pull", "harbor.internal:8443/team/svc:1.2"])

        # 2. Without registry_id (unauthenticated fallback for unmatched image)
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", return_value="Pulled"
            ) as mock_run_unauth,
        ):
            resp_unauth = asgi_post(
                api.app,
                "/api/docker/images/pull",
                json_body={"image": "busybox:latest"},
                cookies=self.auth_cookies,
            )

        self.assertEqual(resp_unauth.status_code, 200)
        self.assertFalse(resp_unauth.json()["authenticated"])
        self.assertIsNone(resp_unauth.json()["registry_name"])
        mock_run_unauth.assert_called_once_with(["pull", "busybox:latest"], timeout=300)

        # 3. Unknown registry_id -> 404
        with patch.object(main.docker_manager, "is_running", return_value=True):
            resp_404 = asgi_post(
                api.app,
                "/api/docker/images/pull",
                json_body={"image": "busybox:latest", "registry_id": "nonexistent-id"},
                cookies=self.auth_cookies,
            )
        self.assertEqual(resp_404.status_code, 404)

    def test_bare_domain_matching_without_scheme(self):
        self.assertTrue(main._registry_url_matches_host("ghcr.io", "ghcr.io"))
        self.assertTrue(main._registry_url_matches_host("quay.io", "quay.io"))
        self.assertTrue(
            main._registry_url_matches_host("reg.local:5000", "reg.local:5000")
        )
        self.assertTrue(main._registry_url_matches_host("docker.io", "docker.io"))
        self.assertEqual(
            main._get_docker_config_server_address("ghcr.io"), "ghcr.io"
        )
        self.assertEqual(
            main._get_docker_config_server_address("docker.io"),
            "https://index.docker.io/v1/",
        )

        # Simulate legacy/bare URL persisted directly in container_registries without https://
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = 1700000000.0
            conn.execute(
                """
                INSERT INTO container_registries (
                    id, name, url, username, encrypted_token, is_default,
                    status, last_tested_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 'verified', 0, '', ?, ?)
                """,
                (
                    "bare-ghcr-id",
                    "Bare GHCR",
                    "ghcr.io",
                    "octocat",
                    main.encrypt_secret("ghp_bare_1234"),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        resolved = main.resolve_registry_for_image("ghcr.io/octocat/hello:latest")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], "bare-ghcr-id")
        self.assertEqual(resolved["decrypted_token"], "ghp_bare_1234")
        self.assertIn("ghcr.io", resolved["server_aliases"])
        self.assertIn("https://ghcr.io", resolved["server_aliases"])

    def test_empty_username_falls_back_to_oauth2_and_docker_hub_multi_aliases(self):
        main.create_container_registry(
            {
                "name": "Token-Only GHCR",
                "url": "https://ghcr.io",
                "username": "",
                "token": "ghp_only_token_7777",
            }
        )
        observed_ghcr_cfg = None

        def fake_ghcr_pull(args, timeout=300):
            nonlocal observed_ghcr_cfg
            observed_ghcr_cfg = json.loads(
                (Path(args[1]) / "config.json").read_text(encoding="utf-8")
            )
            return "pulled"

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_docker", side_effect=fake_ghcr_pull
            ),
        ):
            res = main.docker_manager.pull_image("ghcr.io/org/private:v1")

        self.assertTrue(res["authenticated"])
        expected_oauth_auth = base64.b64encode(
            b"oauth2:ghp_only_token_7777"
        ).decode("ascii")
        self.assertEqual(
            observed_ghcr_cfg["auths"]["ghcr.io"],
            {
                "auth": expected_oauth_auth,
                "username": "oauth2",
                "password": "ghp_only_token_7777",
            },
        )
        self.assertIn("https://ghcr.io/v2/", observed_ghcr_cfg["auths"])

        # Docker Hub multi-aliases
        main.create_container_registry(
            {
                "name": "Docker Hub",
                "url": "https://docker.io",
                "username": "dhuser",
                "token": "dckr_pat_8888",
            }
        )
        observed_dh_cfg = None

        def fake_dh_pull(args, timeout=300):
            nonlocal observed_dh_cfg
            observed_dh_cfg = json.loads(
                (Path(args[1]) / "config.json").read_text(encoding="utf-8")
            )
            return "pulled"

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "_run_docker", side_effect=fake_dh_pull),
        ):
            main.docker_manager.pull_image("myorg/private-app:latest")

        for dh_key in (
            "https://index.docker.io/v1/",
            "index.docker.io",
            "docker.io",
            "https://docker.io",
        ):
            self.assertIn(dh_key, observed_dh_cfg["auths"])
            self.assertEqual(observed_dh_cfg["auths"][dh_key]["username"], "dhuser")
            self.assertEqual(
                observed_dh_cfg["auths"][dh_key]["password"], "dckr_pat_8888"
            )

    def test_stack_deploy_and_update_check_use_registry_auth(self):
        main.create_container_registry(
            {
                "name": "Private GHCR",
                "url": "https://ghcr.io",
                "username": "",
                "token": "ghp_stack_token_1122",
            }
        )

        # 1. Check updates passes --config <temp_dir> for ghcr.io image
        observed_inspect_cfg = None

        def fake_inspect_docker(args, timeout=30):
            nonlocal observed_inspect_cfg
            if args[0] == "--config":
                observed_inspect_cfg = json.loads(
                    (Path(args[1]) / "config.json").read_text(encoding="utf-8")
                )
                return json.dumps({"config": {"digest": "sha256:abc123"}})
            return json.dumps([{"Id": "sha256:abc123", "RepoDigests": []}])

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager,
                "list_containers",
                return_value=[
                    {"id": "c1", "name": "priv", "image": "ghcr.io/org/priv:latest", "stack": ""}
                ],
            ),
            patch.object(
                main.docker_manager, "_run_docker", side_effect=fake_inspect_docker
            ),
        ):
            upd = main.docker_manager.check_updates(force=True)

        self.assertEqual(len(upd["updates"]), 1)
        self.assertIsNotNone(observed_inspect_cfg)
        self.assertEqual(
            observed_inspect_cfg["auths"]["ghcr.io"]["username"], "oauth2"
        )

        # 2. Stack deploy and pull pass DOCKER_CONFIG in env
        observed_envs = []

        def fake_run_compose(args, timeout=60, cwd=None, env=None):
            if env and "DOCKER_CONFIG" in env:
                cfg_file = Path(env["DOCKER_CONFIG"]) / "config.json"
                observed_envs.append(json.loads(cfg_file.read_text(encoding="utf-8")))
            return "ok"

        stacks_dir = Path(self.temp_dir.name) / "stacks"
        with (
            patch.object(main.docker_manager, "stacks_dir", stacks_dir),
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager, "_run_compose", side_effect=fake_run_compose
            ),
        ):
            main.docker_manager.save_stack(
                "privstack",
                "services:\n  app:\n    image: ghcr.io/org/priv:latest\n",
                deploy=True,
            )
            main.docker_manager.execute_stack_action("privstack", "pull")

        self.assertEqual(len(observed_envs), 2)
        for cfg in observed_envs:
            self.assertIn("ghcr.io", cfg["auths"])
            self.assertEqual(cfg["auths"]["ghcr.io"]["password"], "ghp_stack_token_1122")

    def test_image_pull_registry_dropdown_and_payload_dispatch_in_node_vm(self):
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) idx = source.indexOf('function ' + name + '(');
  if (idx === -1) throw new Error('Function not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(idx, i + 1);
    }
    i++;
  }
  throw new Error('Unbalanced braces in ' + name);
}

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
  };
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      value: '',
      textContent: '',
      innerHTML: '',
      disabled: false,
      className: '',
      classList: makeClassList(['hidden']),
    };
  }
  return elements[id];
}

const pullRequests = [];
const toasts = [];
let fetchDockerImagesCount = 0;
let fetchDockerStatusCount = 0;
let nextApiResponse = null;

const context = vm.createContext({
  console,
  Array,
  String,
  Boolean,
  JSON,
  document: { getElementById: getEl },
  containerRegistries: [],
  escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  showToast: (t) => toasts.push(t),
  fetchDockerImages: () => { fetchDockerImagesCount++; },
  fetchDockerStatus: () => { fetchDockerStatusCount++; },
  alert: (msg) => { throw new Error('Unexpected alert: ' + msg); },
  apiFetch: async (url, opts) => {
    pullRequests.push({ url, opts, body: JSON.parse(opts.body) });
    return nextApiResponse;
  },
});

vm.runInContext(extractFunc(code, 'populateImagePullRegistrySelect'), context);
vm.runInContext(extractFunc(code, 'handlePullImage'), context);

const selectEl = getEl('docker-image-pull-registry');
const inputEl = getEl('docker-image-pull-input');
const btnEl = getEl('btn-pull-image');
const feedbackEl = getEl('docker-image-feedback');

// 1. Populate dropdown: only token_configured registries appear, default gets suffix
context.containerRegistries = [
  { id: 'reg-ghcr', name: 'Company GHCR', url: 'https://ghcr.io', token_configured: true, is_default: true },
  { id: 'reg-anon', name: 'Public Quay', url: 'https://quay.io', token_configured: false, is_default: false },
  { id: 'reg-harbor', name: 'Harbor <Prod>', url: 'https://harbor.local', token_configured: true, is_default: false },
];
context.populateImagePullRegistrySelect();
if (!selectEl.innerHTML.includes('value=""')) throw new Error('Missing Auto option');
if (!selectEl.innerHTML.includes('value="reg-ghcr"') || !selectEl.innerHTML.includes('(Default)')) {
  throw new Error('Missing GHCR default option: ' + selectEl.innerHTML);
}
if (selectEl.innerHTML.includes('reg-anon')) throw new Error('Tokenless registry should not appear in dropdown');
if (!selectEl.innerHTML.includes('Harbor &lt;Prod&gt;')) throw new Error('Registry name must be HTML-escaped');

// Preserve selected registry across re-population
selectEl.value = 'reg-harbor';
context.populateImagePullRegistrySelect();
if (selectEl.value !== 'reg-harbor') throw new Error('Selected registry was not preserved');

// Reset selection to Auto if previously selected registry is removed
context.containerRegistries = [
  { id: 'reg-ghcr', name: 'Company GHCR', url: 'https://ghcr.io', token_configured: true, is_default: true },
];
context.populateImagePullRegistrySelect();
if (selectEl.value !== '') throw new Error('Removed registry selection should reset to empty string');

(async () => {
  // 2. Pull with explicit registry selected -> sends registry_id and renders Authenticated badge
  selectEl.value = 'reg-ghcr';
  inputEl.value = 'ghcr.io/org/app:v1';
  nextApiResponse = {
    ok: true,
    status: 200,
    json: async () => ({
      success: true,
      image: 'ghcr.io/org/app:v1',
      authenticated: true,
      registry_name: 'Company GHCR',
    }),
  };
  await context.handlePullImage();
  if (pullRequests.length !== 1) throw new Error('Expected 1 pull request');
  if (pullRequests[0].url !== '/api/docker/images/pull') throw new Error('Wrong pull endpoint');
  if (pullRequests[0].body.image !== 'ghcr.io/org/app:v1' || pullRequests[0].body.registry_id !== 'reg-ghcr') {
    throw new Error('Unexpected explicit registry pull payload: ' + JSON.stringify(pullRequests[0].body));
  }
  if (!feedbackEl.innerHTML.includes('pull-auth-badge') || !feedbackEl.innerHTML.includes('Company GHCR')) {
    throw new Error('Missing authenticated badge or registry attribution: ' + feedbackEl.innerHTML);
  }
  if (inputEl.value !== '') throw new Error('Input should clear after successful pull');
  if (btnEl.disabled !== false || btnEl.textContent !== 'Pull Image') throw new Error('Button not restored');
  if (toasts[0].title !== 'Authenticated Image Pull') throw new Error('Expected Authenticated Image Pull toast');

  // 3. Pull with Auto ("") -> omits registry_id and supports unauthenticated fallback
  selectEl.value = '';
  inputEl.value = 'alpine:latest';
  nextApiResponse = {
    ok: true,
    status: 200,
    json: async () => ({
      success: true,
      image: 'alpine:latest',
      authenticated: false,
      registry_name: null,
    }),
  };
  await context.handlePullImage();
  if ('registry_id' in pullRequests[1].body) {
    throw new Error('Auto mode should omit registry_id, got: ' + JSON.stringify(pullRequests[1].body));
  }
  if (!feedbackEl.textContent.includes("Successfully pulled image 'alpine:latest'")) {
    throw new Error('Missing plain success feedback: ' + feedbackEl.textContent);
  }

  // 4. Error response -> feedback-error and button re-enabled
  inputEl.value = 'ghcr.io/org/private:latest';
  selectEl.value = 'reg-ghcr';
  nextApiResponse = {
    ok: false,
    status: 500,
    json: async () => ({ detail: 'pull access denied' }),
  };
  await context.handlePullImage();
  if (!feedbackEl.className.includes('feedback-error') || feedbackEl.textContent !== 'pull access denied') {
    throw new Error('Expected error detail in feedback: ' + feedbackEl.textContent);
  }
  if (btnEl.disabled !== false) throw new Error('Button should re-enable after error');
  if (fetchDockerImagesCount !== 2 || fetchDockerStatusCount !== 2) {
    throw new Error('Expected 2 image/status refreshes for 2 successful pulls');
  }
  console.log('PULL_VM_PASS');
})().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node VM pull test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
        self.assertIn("PULL_VM_PASS", proc.stdout)

    def test_images_table_registry_column_and_badges_in_node_vm(self):
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) idx = source.indexOf('function ' + name + '(');
  if (idx === -1) throw new Error('Function not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(idx, i + 1);
    }
    i++;
  }
  throw new Error('Unbalanced braces in ' + name);
}

const tbodyEl = {
  innerHTML: '',
  querySelectorAll: () => [],
};

const context = vm.createContext({
  console,
  Set,
  Array,
  String,
  Boolean,
  URL,
  document: {
    getElementById: (id) => (id === 'docker-images-tbody' ? tbodyEl : null),
  },
  containerRegistries: [
    { id: 'reg-ghcr', name: 'Company GHCR', url: 'https://ghcr.io', token_configured: true, is_default: false },
    { id: 'reg-quay', name: 'Public Quay', url: 'https://quay.io', token_configured: false, is_default: false },
  ],
  escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  handleRemoveImage: () => {},
});

vm.runInContext(
  'const DOCKER_HUB_HOSTS = new Set(["docker.io", "registry-1.docker.io", "index.docker.io", "registry.hub.docker.com"]);',
  context
);
for (const fnName of [
  'extractImageRegistryHost',
  'registryMatchesImageHost',
  'resolveImageRegistryBadgeHtml',
  'renderDockerImages',
]) {
  vm.runInContext(extractFunc(code, fnName), context);
}

// 1. Empty state renders colspan="7"
context.renderDockerImages([]);
if (!tbodyEl.innerHTML.includes('colspan="7"')) {
  throw new Error('Empty images state must have colspan="7", got: ' + tbodyEl.innerHTML);
}

// 2. Populated images render 7 cells & resolved .registry-pill badges
context.renderDockerImages([
  { id: 'sha256:1111', repository: 'ghcr.io/org/webapp', tag: 'v1.0', size: '120MB', created: '2h ago' },
  { id: 'sha256:2222', repository: 'quay.io/coreos/etcd', tag: 'v3.5', size: '90MB', created: '1d ago' },
  { id: 'sha256:3333', repository: 'nginx', tag: 'alpine', size: '40MB', created: '3d ago' },
  { id: 'sha256:4444', repository: '<none>', tag: '<none>', size: '10MB', created: '4d ago' },
]);

const html = tbodyEl.innerHTML;
if (!html.includes('class="registry-pill authenticated"') || !html.includes('Company GHCR')) {
  throw new Error('Missing authenticated GHCR registry pill in: ' + html);
}
if (!html.includes('class="registry-pill" title="https://quay.io">Public Quay</span>')) {
  throw new Error('Missing unauthenticated Quay registry pill in: ' + html);
}
if (!html.includes('class="registry-pill" title="docker.io">Docker Hub</span>')) {
  throw new Error('Missing Docker Hub fallback pill in: ' + html);
}
if (!html.includes('<span class="registry-pill">Local</span>')) {
  throw new Error('Missing Local pill for <none> repository in: ' + html);
}

console.log('IMAGES_COL_VM_PASS');
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node VM images column test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
        self.assertIn("IMAGES_COL_VM_PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
