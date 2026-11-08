import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import psutil
import re
import shutil
import socket
import sqlite3
from subprocess import CalledProcessError
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from urllib.error import URLError
from urllib.parse import urlsplit

import aiohttp
import discord

import api
import main


SAMPLE_DETAILS = """/dev/md128:
        Version : 1.2
        Creation Time : Thu Jan  1 00:00:00 1970
        Raid Level : raid1
        Array Size : 10000 (9.31 GiB)
        Used Dev Size : 10000
        Raid Devices : 2
        Total Devices : 2
        Persistence : Superblock is persistent

        Update Time : Wed Oct 28 00:00:00 2026
        State : clean
        Active Devices : 2
        Working Devices : 2
        Failed Devices : 0
        Spare Devices : 0
        Check Status : 100%
        Events : 42

        Layout : near=2
        Chunk Size : 64K
        Consistency Policy : resync

        Name : test:0
        UUID : abcdef
        Events : 42

        Number   Major   Minor   RaidDevice State
           0       8       0        0      active sync   /dev/sda
           1       8      16        1      active sync   /dev/sdb
"""

SAMPLE_DUF = """Filesystem      Size  Used Avail Use%
/dev/media      100G   60G   40G   60%
"""

SAMPLE_DUF_TABLE = """╭─────────┬─────────┬─────────┬────────╮
│    SIZE │    USED │   AVAIL │  USAGE │
├─────────┼─────────┼─────────┼────────┤
│  14.55T │   8.12T │   6.43T │  55.8% │
╰─────────┴─────────┴─────────┴────────╯"""


class AsgiResponse:
    """Minimal response wrapper for in-process ASGI requests."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        raw_headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.raw_headers = raw_headers
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def asgi_request(
    app,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiResponse:
    """Execute an HTTP request directly against an ASGI app without external HTTP client deps."""
    parsed = urlsplit(path)
    body_bytes = b""
    req_headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]

    if json_body is not None:
        body_bytes = json.dumps(json_body).encode("utf-8")
        req_headers.append((b"content-type", b"application/json"))
        req_headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))

    if headers:
        for k, v in headers.items():
            req_headers.append((k.lower().encode("latin1"), v.encode("latin1")))

    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req_headers.append((b"cookie", cookie_str.encode("latin1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path or "/",
        "raw_path": (parsed.path or "/").encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": req_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    status_code = 500
    resp_headers: dict[str, str] = {}
    raw_headers: list[tuple[str, str]] = []
    body_parts: list[bytes] = []

    for message in messages:
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", []):
                key_str = k.decode("latin1").lower()
                val_str = v.decode("latin1")
                resp_headers[key_str] = val_str
                raw_headers.append((key_str, val_str))
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    return AsgiResponse(status_code, resp_headers, raw_headers, b"".join(body_parts))


def asgi_get(
    app,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiResponse:
    return asgi_request(app, "GET", path, headers=headers, cookies=cookies)


def asgi_post(
    app,
    path: str,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiResponse:
    return asgi_request(
        app, "POST", path, json_body=json_body, headers=headers, cookies=cookies
    )


def asgi_put(
    app,
    path: str,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiResponse:
    return asgi_request(
        app, "PUT", path, json_body=json_body, headers=headers, cookies=cookies
    )


def asgi_delete(
    app,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiResponse:
    return asgi_request(app, "DELETE", path, headers=headers, cookies=cookies)


class AsgiWebSocketResult:
    """Result container for in-process ASGI WebSocket session."""

    def __init__(
        self,
        accepted: bool,
        close_code: int | None,
        messages: list[dict],
    ) -> None:
        self.accepted = accepted
        self.close_code = close_code
        self.messages = messages


def asgi_ws(
    app,
    path: str,
    *,
    client_messages: list[dict] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> AsgiWebSocketResult:
    """Execute a WebSocket session directly against an ASGI app."""
    parsed = urlsplit(path)
    req_headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if headers:
        for k, v in headers.items():
            req_headers.append((k.lower().encode("latin1"), v.encode("latin1")))
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req_headers.append((b"cookie", cookie_str.encode("latin1")))

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": parsed.path or "/api/ws",
        "raw_path": (parsed.path or "/api/ws").encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": req_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "subprotocols": [],
    }

    inbound_queue: list[dict] = [{"type": "websocket.connect"}]
    for msg in client_messages or []:
        if isinstance(msg, dict) and msg.get("type") == "websocket.receive":
            inbound_queue.append(msg)
        elif isinstance(msg, str):
            inbound_queue.append({"type": "websocket.receive", "text": msg})
        elif isinstance(msg, bytes):
            inbound_queue.append({"type": "websocket.receive", "bytes": msg})
        else:
            inbound_queue.append({"type": "websocket.receive", "text": json.dumps(msg)})
    inbound_queue.append({"type": "websocket.disconnect", "code": 1000})

    accepted = False
    close_code: int | None = None
    sent_messages: list[dict] = []

    async def receive():
        if inbound_queue:
            return inbound_queue.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        nonlocal accepted, close_code
        msg_type = message.get("type")
        if msg_type == "websocket.accept":
            accepted = True
        elif msg_type == "websocket.send":
            text = message.get("text")
            b_data = message.get("bytes")
            if b_data is not None:
                sent_messages.append({"bytes": b_data})
            elif text is not None:
                try:
                    sent_messages.append(json.loads(text))
                except Exception:
                    sent_messages.append({"text": text})
        elif msg_type == "websocket.close":
            close_code = message.get("code", 1000)

    asyncio.run(app(scope, receive, send))
    return AsgiWebSocketResult(accepted, close_code, sent_messages)


class RaidFormattingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_formatting.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_format_raid_summary_uses_clean_card_layout(self):
        text = main.format_raid_summary("clean", SAMPLE_DETAILS, SAMPLE_DUF)
        self.assertNotIn("╭", text)
        self.assertNotIn("│", text)
        self.assertIn("Status: clean", text)
        self.assertIn("Array Size: 9.31 GiB", text)
        self.assertIn("Devices: 2 active, 2 working, 0 failed, 0 spare", text)
        self.assertIn("- sda: active sync", text)
        self.assertIn("- sdb: active sync", text)

    def test_format_raid_summary_exception_fallback(self):
        with patch.object(
            main, "_extract_raid_summary_data", side_effect=RuntimeError("parse fail")
        ):
            text = main.format_raid_summary("degraded", SAMPLE_DETAILS, SAMPLE_DUF)
        self.assertEqual(text, "RAID Array Degraded\nError parsing details")

    def test_extract_raid_summary_data_parses_duf_table_and_fallback(self):
        data = main._extract_raid_summary_data("clean", SAMPLE_DETAILS, SAMPLE_DUF_TABLE)
        self.assertEqual(data["raid_device"], "/dev/md128")
        self.assertEqual(data["status"], "clean")
        self.assertEqual(data["raid_level"], "raid1")
        self.assertEqual(data["array_size"], "9.31 GiB")
        self.assertEqual(data["storage_info"], "8.12T/14.55T (55.8%) | 6.43T free")
        self.assertEqual(len(data["devices"]), 2)

        empty_data = main._extract_raid_summary_data("unknown", "", "")
        self.assertEqual(empty_data["raid_level"], "unknown")
        self.assertEqual(empty_data["storage_info"], "N/A")
        self.assertEqual(empty_data["devices"], [])

        summary_no_devices = main.format_raid_summary("unknown", "", "")
        self.assertIn("- No device information available", summary_no_devices)

    def test_extract_raid_summary_data_parses_nvme_vda_and_raid_device(self):
        nvme_details = """/dev/md0:
        Version : 1.2
        Raid Level : raid10
        Array Size : 20000 (19.07 GiB)
        State : active
        Active Devices : 2
        Working Devices : 2
        Failed Devices : 0
        Spare Devices : 0

        Number   Major   Minor   RaidDevice State
           0     259       0        0      active sync set-A   /dev/nvme0n1p1
           1     252       1        1      active sync set-B   /dev/vda1
"""
        with patch.object(main, "get_all_disks_smart_metrics", return_value=[]):
            data = main._extract_raid_summary_data("active", nvme_details, SAMPLE_DUF_TABLE)
        self.assertEqual(data["raid_device"], "/dev/md0")
        self.assertEqual(data["raid_level"], "raid10")
        self.assertEqual(
            data["devices"],
            [
                {"device": "nvme0n1p1", "state": "active sync set-A"},
                {"device": "vda1", "state": "active sync set-B"},
            ],
        )

    def test_discover_and_resolve_raid_devices(self):
        fake_mdstat = """Personalities : [raid1] [raid6] [raid5] [raid4]
md127 : active raid5 sdc1[0] sdd1[1] sde1[2]
      19532609536 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]

md0 : active raid1 nvme0n1p1[0] nvme1n1p1[1]
      999872 blocks super 1.2 [2/2] [UU]

unused devices: <none>
"""
        mdstat_file = Path(self.temp_dir.name) / "mdstat"
        mdstat_file.write_text(fake_mdstat, encoding="utf-8")

        orig_path = main.Path

        def fake_path_factory(p):
            if p == "/proc/mdstat":
                return mdstat_file
            return orig_path(p)

        with (
            patch.object(main, "Path", side_effect=fake_path_factory),
            patch.object(
                main.glob,
                "glob",
                side_effect=lambda pattern: (
                    ["/dev/md0", "/dev/md0p1"] if pattern == "/dev/md[0-9]*" else []
                ),
            ),
        ):
            devices = main.discover_raid_devices()
            self.assertEqual(devices, ["/dev/md0", "/dev/md127"])

            # 'auto' resolves to first discovered device (/dev/md0)
            self.assertEqual(main.resolve_raid_device("auto"), "/dev/md0")

            # Non-existent default '/dev/md128' falls back to discovered '/dev/md0'
            self.assertEqual(main.resolve_raid_device("/dev/md128"), "/dev/md0")

        # When no devices are discovered on disk or in /proc/mdstat, returns None
        with patch.object(main, "discover_raid_devices", return_value=[]):
            self.assertIsNone(main.resolve_raid_device("auto"))
            self.assertIsNone(main.resolve_raid_device(""))
            self.assertIsNone(main.resolve_raid_device("none"))
            self.assertIsNone(main.resolve_raid_device("/dev/md_nonexistent"))

    def test_build_raid_embed_is_compatible_across_statuses(self):
        embed = main.build_raid_embed("clean", SAMPLE_DETAILS, SAMPLE_DUF)
        self.assertEqual(embed.title, "RAID 1 | CLEAN")
        self.assertEqual(embed.color.value, 0x2ECC71)
        self.assertNotIn("╭", embed.description)
        self.assertIn("Status", embed.description)

        degraded_embed = main.build_raid_embed("degraded", SAMPLE_DETAILS, SAMPLE_DUF)
        self.assertEqual(degraded_embed.title, "RAID 1 | DEGRADED")
        self.assertEqual(degraded_embed.color.value, 0xF39C12)

        failed_embed = main.build_raid_embed("failed", SAMPLE_DETAILS, SAMPLE_DUF)
        self.assertEqual(failed_embed.title, "RAID 1 | FAILED")
        self.assertEqual(failed_embed.color.value, 0xE74C3C)

        unknown_embed = main.build_raid_embed("mystery", "", "")
        self.assertEqual(unknown_embed.title, "RAID UNKNOWN | MYSTERY")
        self.assertEqual(unknown_embed.color.value, 0x95A5A6)

    def test_should_send_weekly_summary_respects_due_date_and_empty_state(self):
        self.assertTrue(main.should_send_weekly_summary(None, now=150.0))
        future_state = (100.0, 200.0, "clean")
        self.assertFalse(main.should_send_weekly_summary(future_state, now=150.0))
        self.assertTrue(
            main.should_send_weekly_summary(future_state, force=True, now=150.0)
        )
        self.assertTrue(main.should_send_weekly_summary(future_state, now=200.0))
        self.assertTrue(main.should_send_weekly_summary(future_state, now=250.0))

    def test_enforce_discord_message_limit_preserves_short_and_truncates_large_messages(self):
        short_message = "RAID array is healthy."
        self.assertEqual(main.enforce_discord_message_limit(short_message), short_message)

        exact_limit_message = "B" * main.MAX_DISCORD_MESSAGE_LENGTH
        self.assertEqual(
            main.enforce_discord_message_limit(exact_limit_message), exact_limit_message
        )

        long_message = "A" * 5000
        truncated = main.enforce_discord_message_limit(long_message)
        self.assertLessEqual(len(truncated), main.MAX_DISCORD_MESSAGE_LENGTH)
        self.assertTrue(truncated.endswith("...\n[truncated]"))

    def test_check_raid_status_all_state_branches_and_errors(self):
        # When resolve_raid_device() returns None, check_raid_status returns ('none', 'No RAID array detected') without calling mdadm
        with (
            patch.object(main, "resolve_raid_device", return_value=None),
            patch("main.check_output") as mock_check_output,
        ):
            status, details = main.check_raid_status()
            self.assertEqual(status, "none")
            self.assertEqual(details, "No RAID array detected")
            mock_check_output.assert_not_called()

        with patch.object(main, "resolve_raid_device", return_value="/dev/md128"):
            for expected_state in [
                "clean",
                "active",
                "degraded",
                "recovering",
                "resyncing",
                "failed",
            ]:
                raw_output = f"/dev/md128:\n        State : {expected_state}\n".encode()
                with patch("main.check_output", return_value=raw_output):
                    status, details = main.check_raid_status()
                    self.assertEqual(status, expected_state)
                    self.assertIn(f"State : {expected_state}", details)

            with patch(
                "main.check_output", return_value=b"/dev/md128:\n        State : weird\n"
            ):
                status, details = main.check_raid_status()
                self.assertEqual(status, "unknown")
                self.assertIn("State : weird", details)

            with patch(
                "main.check_output",
                side_effect=CalledProcessError(1, ["mdadm"], output=b"mdadm failed"),
            ):
                status, details = main.check_raid_status()
                self.assertEqual(status, "error")
                self.assertEqual(details, "mdadm failed")

            with patch(
                "main.check_output",
                side_effect=FileNotFoundError("No such file or directory: 'mdadm'"),
            ):
                status, details = main.check_raid_status()
                self.assertEqual(status, "error")
                self.assertIn("mdadm", details)

    def test_get_duf_output_success_and_error_branches(self):
        with patch("main.check_output", return_value=b"  duf table output  \n"):
            self.assertEqual(main.get_duf_output(), "duf table output")

        with patch(
            "main.check_output",
            side_effect=CalledProcessError(1, ["duf"], output=b"duf error"),
        ):
            duf_out = main.get_duf_output()
            self.assertIn("Error getting disk usage: duf error", duf_out)

        with patch(
            "main.check_output",
            side_effect=FileNotFoundError("No such file or directory: 'duf'"),
        ):
            duf_out = main.get_duf_output()
            self.assertIn("Error getting disk usage:", duf_out)
            self.assertIn("duf", duf_out)


class DiscordAndRaidCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_raid_check.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.update_app_settings(
            {
                "discord_enabled": "true",
                "discord_token": "test_token",
                "channel_id": "12345",
            }
        )

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_send_message_handles_string_embed_missing_channel_and_exception(self):
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        with (
            patch.object(main, "CHANNEL_ID", "12345"),
            patch.object(main.client, "get_channel", return_value=mock_channel),
        ):
            result_str = asyncio.run(main.send_message("hello world"))
            self.assertTrue(result_str)
            mock_channel.send.assert_awaited_once_with("hello world")

            mock_channel.send.reset_mock()
            embed = discord.Embed(title="Test Embed")
            result_embed = asyncio.run(main.send_message(embed))
            self.assertTrue(result_embed)
            mock_channel.send.assert_awaited_once_with(embed=embed)

        with (
            patch.object(main, "CHANNEL_ID", "99999"),
            patch.object(main.client, "get_channel", return_value=None),
        ):
            result_missing = asyncio.run(main.send_message("no channel"))
            self.assertFalse(result_missing)

        mock_failing_channel = MagicMock()
        mock_failing_channel.send = AsyncMock(side_effect=RuntimeError("network down"))
        with (
            patch.object(main, "CHANNEL_ID", "12345"),
            patch.object(main.client, "get_channel", return_value=mock_failing_channel),
        ):
            result_error = asyncio.run(main.send_message("boom"))
            self.assertFalse(result_error)

    def test_run_raid_check_clean_sent_vs_throttled_and_degraded_alert(self):
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=False))
            self.assertEqual(mock_send.await_count, 1)
            self.assertIsNotNone(main.get_weekly_notification_state())

            mock_send.reset_mock()
            asyncio.run(main.run_raid_check(force=False))
            self.assertEqual(mock_send.await_count, 0)

            asyncio.run(main.run_raid_check(force=True))
            self.assertEqual(mock_send.await_count, 1)

        with (
            patch.object(
                main, "check_raid_status", return_value=("degraded", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=False))
            self.assertEqual(mock_send.await_count, 1)
            history = main.get_notification_history(5)
            self.assertEqual(history[0][0], "degraded")
            self.assertEqual(history[0][2], 0)

        # When check_raid_status returns ('none', 'No RAID array detected'), no Discord alert is sent
        with (
            patch.object(
                main,
                "check_raid_status",
                return_value=("none", "No RAID array detected"),
            ),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=True))
            self.assertEqual(mock_send.await_count, 0)

    def test_run_raid_check_handles_sqlite_errors_gracefully(self):
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(
                main,
                "get_weekly_notification_state",
                side_effect=sqlite3.Error("read failure"),
            ),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=False))
            self.assertEqual(mock_send.await_count, 0)

        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(
                main, "record_notification", side_effect=sqlite3.Error("write failure")
            ),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=True))
            self.assertEqual(mock_send.await_count, 1)

        with (
            patch.object(
                main, "check_raid_status", return_value=("failed", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(
                main, "record_notification", side_effect=sqlite3.Error("write failure")
            ),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            asyncio.run(main.run_raid_check(force=False))
            self.assertEqual(mock_send.await_count, 1)

    def test_discord_enabled_setting_controls_sending_and_logging(self):
        # 1. When discord_enabled is explicitly 'false', send_message skips sending and logs
        main.update_app_settings({"discord_enabled": "false", "channel_id": "12345"})
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()
        with (
            patch.object(main.client, "get_channel", return_value=mock_channel),
            self.assertLogs(level="INFO") as log_ctx,
        ):
            sent = asyncio.run(main.send_message("test message"))
            self.assertFalse(sent)
            mock_channel.send.assert_not_called()
            self.assertTrue(
                any("Discord notifications are disabled" in entry for entry in log_ctx.output)
            )

        # 2. When discord_enabled is 'false', run_raid_check skips checks/sending
        with (
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
            patch.object(main, "check_raid_status") as mock_raid_status,
            self.assertLogs(level="INFO") as log_ctx_raid,
        ):
            asyncio.run(main.run_raid_check(force=True))
            mock_raid_status.assert_not_called()
            self.assertEqual(mock_send.await_count, 0)
            self.assertTrue(
                any("Discord notifications are disabled" in entry for entry in log_ctx_raid.output)
            )

        # 3. When discord_enabled is set to 'true', sending works
        main.update_app_settings({"discord_enabled": "true"})
        with patch.object(main.client, "get_channel", return_value=mock_channel):
            sent_enabled = asyncio.run(main.send_message("enabled message"))
            self.assertTrue(sent_enabled)
            mock_channel.send.assert_awaited_once_with("enabled message")

    def test_send_test_notification_success_and_failure_branches(self):
        # Disabled in settings
        main.update_app_settings(
            {"discord_enabled": "false", "discord_token": "test_token", "channel_id": "12345"}
        )
        success, msg = asyncio.run(main.send_test_notification())
        self.assertFalse(success)
        self.assertIn("disabled", msg.lower())

        # Enabled but channel ID missing
        with (
            patch.object(main, "CHANNEL_ID", ""),
            patch.object(
                main,
                "get_app_settings",
                return_value={
                    "discord_enabled": "true",
                    "discord_token": "test_token",
                    "channel_id": "",
                },
            ),
        ):
            success, msg = asyncio.run(main.send_test_notification())
            self.assertFalse(success)
            self.assertIn("channel", msg.lower())

        # Enabled and channel ID configured, send_message succeeds
        main.update_app_settings(
            {"discord_enabled": "true", "discord_token": "test_token", "channel_id": "99999"}
        )
        with patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send:
            success, msg = asyncio.run(main.send_test_notification())
            self.assertTrue(success)
            self.assertEqual(mock_send.await_count, 1)
            self.assertIn("success", msg.lower())

            history = main.get_notification_history(5)
            self.assertEqual(history[0][0], "test")
            self.assertEqual(history[0][2], 0)

        # Enabled, but send_message fails
        with patch.object(main, "send_message", new=AsyncMock(return_value=False)) as mock_send_fail:
            success, msg = asyncio.run(main.send_test_notification())
            self.assertFalse(success)
            self.assertEqual(mock_send_fail.await_count, 1)
            self.assertIn("failed", msg.lower())

    def test_discord_rest_fallback_and_detailed_error_reporting(self):
        # 1. Missing token
        main.update_app_settings({"discord_enabled": "true", "channel_id": "123456", "discord_token": ""})
        with (
            patch.object(main, "CHANNEL_ID", "123456"),
            patch.object(main, "config", side_effect=lambda k, default="": ""),
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
        ):
            success, msg = asyncio.run(main.send_test_notification())
            self.assertFalse(success)
            self.assertIn("token", msg.lower())

        # 2. REST 200 success
        main.update_app_settings({"discord_enabled": "true", "channel_id": "123456", "discord_token": "valid_token"})
        mock_resp_200 = MagicMock()
        mock_resp_200.status = 200
        mock_resp_200.text = AsyncMock(return_value='{"id": "msg123"}')
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp_200), __aexit__=AsyncMock()))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock())),
        ):
            # Test string message
            sent_str = asyncio.run(main.send_message("rest text message"))
            self.assertTrue(sent_str)
            mock_session.post.assert_called_once()
            call_kwargs = mock_session.post.call_args.kwargs
            self.assertEqual(call_kwargs["json"], {"content": "rest text message"})
            self.assertEqual(call_kwargs["headers"]["Authorization"], "Bot valid_token")

            # Test send_test_notification with REST 200
            mock_session.post.reset_mock()
            success_test, msg_test = asyncio.run(main.send_test_notification())
            self.assertTrue(success_test)
            self.assertIn("success", msg_test.lower())

        # 3. REST 401 Unauthorized
        mock_resp_401 = MagicMock()
        mock_resp_401.status = 401
        mock_resp_401.text = AsyncMock(return_value='{"message": "401: Unauthorized", "code": 0}')
        mock_session_401 = MagicMock()
        mock_session_401.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp_401), __aexit__=AsyncMock()))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session_401), __aexit__=AsyncMock())),
        ):
            success_401, msg_401 = asyncio.run(main.send_test_notification())
            self.assertFalse(success_401)
            self.assertIn("401", msg_401)
            self.assertIn("unauthorized", msg_401.lower())

        # 4. REST 404 Unknown Channel
        mock_resp_404 = MagicMock()
        mock_resp_404.status = 404
        mock_resp_404.text = AsyncMock(return_value='{"message": "Unknown Channel", "code": 10003}')
        mock_session_404 = MagicMock()
        mock_session_404.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp_404), __aexit__=AsyncMock()))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session_404), __aexit__=AsyncMock())),
        ):
            success_404, msg_404 = asyncio.run(main.send_test_notification())
            self.assertFalse(success_404)
            self.assertIn("404", msg_404)
            self.assertIn("unknown channel", msg_404.lower())

        # 5. REST 403 Forbidden
        mock_resp_403 = MagicMock()
        mock_resp_403.status = 403
        mock_resp_403.text = AsyncMock(return_value='{"message": "Missing Permissions", "code": 50013}')
        mock_session_403 = MagicMock()
        mock_session_403.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp_403), __aexit__=AsyncMock()))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session_403), __aexit__=AsyncMock())),
        ):
            success_403, msg_403 = asyncio.run(main.send_test_notification())
            self.assertFalse(success_403)
            self.assertIn("403", msg_403)
            self.assertIn("forbidden", msg_403.lower())
            self.assertIn("Missing Permissions", msg_403)

        # 6. REST network exception handling and descriptive message
        mock_session_exc = MagicMock()
        mock_session_exc.post = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session_exc), __aexit__=AsyncMock(return_value=False))),
        ):
            success_exc, msg_exc = asyncio.run(main.send_test_notification())
            self.assertFalse(success_exc)
            self.assertIn("request failed", msg_exc.lower())
            self.assertIn("connection refused", msg_exc)

            sent_exc = asyncio.run(main.send_message("rest fail"))
            self.assertFalse(sent_exc)
            self.assertIn("request failed", main._last_send_error.lower())

        # 7. REST 200 with discord.Embed
        test_embed = discord.Embed(title="Embed Title", description="Embed Description")
        mock_resp_embed = MagicMock()
        mock_resp_embed.status = 200
        mock_resp_embed.text = AsyncMock(return_value='{"id": "msg_embed"}')
        mock_session_embed = MagicMock()
        mock_session_embed.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp_embed), __aexit__=AsyncMock()))

        with (
            patch.object(main.client, "is_ready", return_value=False),
            patch.object(main.client, "get_channel", return_value=None),
            patch("aiohttp.ClientSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_session_embed), __aexit__=AsyncMock())),
        ):
            sent_embed = asyncio.run(main.send_message(test_embed))
            self.assertTrue(sent_embed)
            mock_session_embed.post.assert_called_once()
            call_kwargs = mock_session_embed.post.call_args.kwargs
            self.assertIn("embeds", call_kwargs["json"])
            self.assertEqual(call_kwargs["json"]["embeds"][0]["title"], "Embed Title")

    def test_on_ready_starts_tasks_without_startup_notification(self):
        with (
            patch.object(main, "run_raid_check", new_callable=AsyncMock) as mock_raid_check,
            patch.object(main.monitor_raid, "start") as mock_monitor_raid,
            patch.object(main.monitor_docker_schedule, "start") as mock_monitor_docker,
        ):
            asyncio.run(main.on_ready())
            mock_raid_check.assert_not_called()
            mock_monitor_raid.assert_called_once()
            mock_monitor_docker.assert_called_once()


class CliAndMainTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cli.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_show_notification_status_empty_future_and_overdue(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.show_notification_status()
        self.assertIn("No successful weekly summary has been recorded yet.", buf.getvalue())

        now = time.time()
        main.record_notification("clean", now, weekly=True)
        buf_future = io.StringIO()
        with patch("sys.stdout", buf_future):
            main.show_notification_status()
        self.assertIn("Last successful weekly summary:", buf_future.getvalue())
        self.assertIn("Time remaining:", buf_future.getvalue())

        past = now - main.WEEK_SECONDS - 100
        main.record_notification("active", past, weekly=True)
        buf_overdue = io.StringIO()
        with patch("sys.stdout", buf_overdue):
            main.show_notification_status()
        self.assertIn(
            "Refresh status:                due on the next clean/active RAID check",
            buf_overdue.getvalue(),
        )

    def test_show_notification_logs_empty_and_populated(self):
        buf_empty = io.StringIO()
        with patch("sys.stdout", buf_empty):
            main.show_notification_logs(10)
        self.assertIn("No successful notifications have been recorded yet.", buf_empty.getvalue())

        main.record_notification("clean", 1700000000.0, weekly=True)
        main.record_notification("degraded", 1700000500.0, weekly=False)
        main.record_notification("test", 1700000600.0, weekly=False)
        buf_logs = io.StringIO()
        with patch("sys.stdout", buf_logs):
            main.show_notification_logs(10)
        output = buf_logs.getvalue()
        self.assertIn("weekly summary", output)
        self.assertIn("alert", output)
        self.assertIn("test notification", output)
        self.assertIn("clean", output)
        self.assertIn("degraded", output)
        self.assertIn("test", output)

    def test_main_cli_subcommands_and_validation(self):
        with (
            patch("sys.argv", ["main.py", "status"]),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(main.main(), 0)

        with (
            patch("sys.argv", ["main.py", "logs", "--limit", "5"]),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(main.main(), 0)

        with (
            patch("sys.argv", ["main.py", "logs", "--limit", "0"]),
            patch("sys.stderr", io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                main.main()

        with (
            patch("sys.argv", ["main.py", "status"]),
            patch.object(
                main,
                "show_notification_status",
                side_effect=sqlite3.Error("corrupt db"),
            ),
            patch("sys.stderr", io.StringIO()) as err_buf,
        ):
            self.assertEqual(main.main(), 1)
            self.assertIn("Could not read RAID notification data", err_buf.getvalue())

        with (
            patch("sys.argv", ["main.py", "web", "--host", "127.0.0.1", "--port", "9000"]),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            self.assertEqual(main.main(), 0)
            mock_uvicorn.assert_called_once_with("api:app", host="127.0.0.1", port=9000)

        with (
            patch("sys.argv", ["main.py"]),
            patch.object(main, "get_app_settings", return_value={}),
            patch.object(main, "config", side_effect=lambda k, **kw: ""),
            patch("sys.stderr", io.StringIO()) as err_buf,
        ):
            self.assertEqual(main.main(), 1)
            self.assertIn(
                "DISCORD_TOKEN and CHANNEL_ID must be set to run the bot.",
                err_buf.getvalue(),
            )

        def fake_config_invalid_log(key, default=None, cast=None):
            values = {
                "DISCORD_TOKEN": "token123",
                "CHANNEL_ID": "456",
                "RAID_LOG_MAX_BYTES": 0,
                "RAID_LOG_BACKUP_COUNT": -1,
            }
            return values.get(key, default)

        with (
            patch("sys.argv", ["main.py"]),
            patch.object(main, "get_app_settings", return_value={}),
            patch.object(main, "config", side_effect=fake_config_invalid_log),
            patch("sys.stderr", io.StringIO()) as err_buf,
        ):
            self.assertEqual(main.main(), 1)
            self.assertIn("RAID_LOG_MAX_BYTES must be positive", err_buf.getvalue())


class SqlitePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_raid_monitor.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_initial_state_and_history_are_empty(self):
        self.assertIsNone(main.get_weekly_notification_state())
        self.assertEqual(main.get_notification_history(10), [])
        self.assertTrue(self.db_path.exists())

    def test_record_weekly_and_alert_notifications(self):
        t1 = 1700000000.0
        main.record_notification("clean", t1, weekly=True)

        state = main.get_weekly_notification_state()
        self.assertIsNotNone(state)
        sent_at, due_at, status = state
        self.assertEqual(sent_at, t1)
        self.assertEqual(due_at, t1 + main.WEEK_SECONDS)
        self.assertEqual(status, "clean")

        t2 = t1 + 3600.0
        main.record_notification("degraded", t2, weekly=False)

        state_after_alert = main.get_weekly_notification_state()
        self.assertEqual(state_after_alert, (t1, t1 + main.WEEK_SECONDS, "clean"))

        t3 = t1 + main.WEEK_SECONDS + 60.0
        main.record_notification("active", t3, weekly=True)
        state_after_second_weekly = main.get_weekly_notification_state()
        self.assertEqual(
            state_after_second_weekly, (t3, t3 + main.WEEK_SECONDS, "active")
        )

        full_history = main.get_notification_history(10)
        self.assertEqual(
            full_history,
            [
                ("active", t3, 1),
                ("degraded", t2, 0),
                ("clean", t1, 1),
            ],
        )

        limited_history = main.get_notification_history(2)
        self.assertEqual(len(limited_history), 2)
        self.assertEqual(limited_history[0][0], "active")
        self.assertEqual(limited_history[1][0], "degraded")

    def test_users_and_sessions_crud_and_expiration(self):
        self.assertFalse(main.has_any_user())

        with self.assertRaises(ValueError):
            main.create_user("", "secret")
        with self.assertRaises(ValueError):
            main.create_user("admin", "")

        created = main.create_user("admin", "CorrectHorseBatteryStaple", email="admin@example.com")
        self.assertEqual(created["username"], "admin")
        self.assertEqual(created["email"], "admin@example.com")
        self.assertTrue(main.has_any_user())

        self.assertTrue(main.verify_user("admin", "CorrectHorseBatteryStaple"))
        self.assertFalse(main.verify_user("admin", "WrongPassword"))
        self.assertFalse(main.verify_user("nonexistent", "CorrectHorseBatteryStaple"))
        self.assertFalse(main.verify_user("", "CorrectHorseBatteryStaple"))

        # Sessions: create, verify, expire, delete
        token = main.create_session("admin")
        self.assertEqual(main.verify_session(token), "admin")
        self.assertIsNone(main.verify_session(None))
        self.assertIsNone(main.verify_session("unknown-token"))

        # Expire session in the future using time.time mock
        future_time = time.time() + main.SESSION_TTL_SECONDS + 10
        with patch("main.time.time", return_value=future_time):
            self.assertIsNone(main.verify_session(token))
        # Expired session should have been deleted from DB
        self.assertIsNone(main.verify_session(token))

        # Delete active session
        token2 = main.create_session("admin")
        self.assertEqual(main.verify_session(token2), "admin")
        main.delete_session(token2)
        self.assertIsNone(main.verify_session(token2))
        main.delete_session(None)

    def test_refresh_intervals_defaults_and_custom_settings(self):
        # 1. Default intervals
        intervals = main.get_refresh_intervals()
        self.assertEqual(
            intervals,
            {"system": 1, "raid": 15, "notifications": 60, "docker": 5},
        )

        # 2. Custom SQLite settings including 0 for paused
        main.update_app_settings(
            {
                "refresh_system_sec": "5",
                "refresh_raid_sec": "0",
                "refresh_notifications_sec": "120",
                "refresh_docker_sec": "10",
            }
        )
        custom = main.get_refresh_intervals()
        self.assertEqual(
            custom,
            {"system": 5, "raid": 0, "notifications": 120, "docker": 10},
        )

        # 3. Direct dictionary argument support and fallback for invalid values
        override = main.get_refresh_intervals(
            {
                "refresh_system_sec": "invalid",
                "refresh_raid_sec": "30",
                "refresh_notifications_sec": "-5",
                "refresh_docker_sec": "abc",
            }
        )
        self.assertEqual(override["system"], 1)  # fallback to default
        self.assertEqual(override["raid"], 30)
        self.assertEqual(override["notifications"], 0)  # max(0, -5) -> 0
        self.assertEqual(override["docker"], 5)  # fallback to default

    def test_app_settings_env_seed_update_and_fallback(self):
        env_values = {
            "DISCORD_TOKEN": "env_discord_token_1234",
            "CHANNEL_ID": "987654321",
            "RAID_DEVICE": "/dev/md128",
            "MOUNT_PATH": "/mnt/media/",
            "GOOGLE_CLIENT_ID": "env-google-client.apps.googleusercontent.com",
            "ALLOWED_GOOGLE_EMAILS": "owner@example.com",
        }
        with patch.object(
            main, "config", side_effect=lambda k, default="": env_values.get(k, default)
        ):
            seeded = main.get_app_settings()
            self.assertEqual(seeded["discord_token"], "env_discord_token_1234")
            self.assertEqual(seeded["channel_id"], "987654321")
            self.assertEqual(seeded["raid_device"], "/dev/md128")
            self.assertEqual(seeded["mount_path"], "/mnt/media/")
            self.assertEqual(
                seeded["google_client_id"], "env-google-client.apps.googleusercontent.com"
            )
            self.assertEqual(seeded["allowed_google_emails"], "owner@example.com")

            # Update settings in SQLite
            updated = main.update_app_settings(
                {
                    "channel_id": "111222333",
                    "raid_device": "/dev/md0",
                    "mount_path": "/mnt/storage/",
                    "allowed_google_emails": "owner@example.com, second@example.com",
                }
            )
            self.assertEqual(updated["channel_id"], "111222333")
            self.assertEqual(main.CHANNEL_ID, "111222333")
            self.assertEqual(updated["raid_device"], "/dev/md0")
            self.assertEqual(updated["mount_path"], "/mnt/storage/")
            self.assertEqual(updated["discord_token"], "env_discord_token_1234")

            # Verify fallback to .env defaults if SQLite raises an error
            with patch.object(
                main, "_connect_state_db", side_effect=sqlite3.Error("db locked")
            ):
                fallback = main.get_app_settings()
                self.assertEqual(fallback["channel_id"], "987654321")
                self.assertEqual(fallback["discord_token"], "env_discord_token_1234")

    def test_discord_enabled_env_seed_and_db_persistence(self):
        # 1. When DISCORD_TOKEN is present in env, discord_enabled defaults to 'true'
        with patch.object(
            main, "config", side_effect=lambda k, default=None: "my-token" if k == "DISCORD_TOKEN" else default
        ):
            defaults = main._default_app_settings_from_env()
            self.assertEqual(defaults["discord_enabled"], "true")

        # 2. When DISCORD_TOKEN is missing in env, discord_enabled defaults to 'false'
        with patch.object(
            main, "config", side_effect=lambda k, default=None: "" if k == "DISCORD_TOKEN" else default
        ):
            defaults_empty = main._default_app_settings_from_env()
            self.assertEqual(defaults_empty["discord_enabled"], "false")

        # 3. Explicit DISCORD_ENABLED env var overrides token presence
        with patch.object(
            main, "config", side_effect=lambda k, default=None: "my-token" if k == "DISCORD_TOKEN" else ("false" if k == "DISCORD_ENABLED" else default)
        ):
            defaults_disabled = main._default_app_settings_from_env()
            self.assertEqual(defaults_disabled["discord_enabled"], "false")

        with patch.object(
            main, "config", side_effect=lambda k, default=None: "" if k == "DISCORD_TOKEN" else ("true" if k == "DISCORD_ENABLED" else default)
        ):
            defaults_override_true = main._default_app_settings_from_env()
            self.assertEqual(defaults_override_true["discord_enabled"], "true")

        # 4. update_app_settings persists discord_enabled to app_settings in DB
        main.update_app_settings({"discord_enabled": "true"})
        self.assertEqual(main.get_app_settings()["discord_enabled"], "true")

        main.update_app_settings({"discord_enabled": "false"})
        self.assertEqual(main.get_app_settings()["discord_enabled"], "false")

    def test_app_settings_zero_env_sqlite_secrets(self):
        # 1. Ensure sensible defaults when no env vars or .env exist
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(main, "_decouple_config", None),
        ):
            # Test safe config helper directly
            self.assertEqual(main.config("NONEXISTENT_KEY", default="default_val"), "default_val")
            self.assertEqual(main.config("INT_KEY", default=42, cast=int), 42)

            defaults = main._default_app_settings_from_env()
            self.assertEqual(defaults["discord_token"], "")
            self.assertEqual(defaults["channel_id"], "")
            self.assertEqual(defaults["discord_enabled"], "false")
            self.assertEqual(defaults["raid_device"], "auto")
            self.assertEqual(defaults["timezone"], "UTC")

            # 2. Update and retrieve secrets directly via SQLite
            main.update_app_settings(
                {
                    "discord_token": "sqlite_bot_token_999",
                    "channel_id": "888777666",
                    "google_client_id": "sqlite_client_id.apps.googleusercontent.com",
                    "allowed_google_emails": "admin@local.host",
                    "timezone": "America/New_York",
                }
            )
            settings = main.get_app_settings()
            self.assertEqual(settings["discord_token"], "sqlite_bot_token_999")
            self.assertEqual(settings["channel_id"], "888777666")
            self.assertEqual(settings["google_client_id"], "sqlite_client_id.apps.googleusercontent.com")
            self.assertEqual(settings["allowed_google_emails"], "admin@local.host")
            self.assertEqual(settings["timezone"], "America/New_York")


class FastAPIEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_raid.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}
        self.auth_headers = {"Authorization": f"Bearer {self.session_token}"}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_protected_endpoints_enforce_authentication(self):
        protected_get_paths = [
            "/api/status",
            "/api/system/storage",
            "/api/history",
            "/api/settings",
            "/api/update/check",
            "/api/changelog",
        ]
        for path in protected_get_paths:
            resp = asgi_get(api.app, path)
            self.assertEqual(resp.status_code, 401, f"Expected 401 for unauthenticated GET {path}")

        for path in ["/api/settings", "/api/update/apply", "/api/notifications/test"]:
            resp = asgi_post(api.app, path, json_body={})
            self.assertEqual(resp.status_code, 401, f"Expected 401 for unauthenticated POST {path}")

        # Verify both session cookie and Bearer token grant 200 access
        with (
            patch.object(main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
        ):
            cookie_resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
            self.assertEqual(cookie_resp.status_code, 200)

            bearer_resp = asgi_get(api.app, "/api/status", headers=self.auth_headers)
            self.assertEqual(bearer_resp.status_code, 200)

    def test_auth_setup_login_google_and_logout_flows(self):
        # Use a fresh empty DB to test initial setup flow
        fresh_db = Path(self.temp_dir.name) / "fresh_auth.sqlite3"
        with patch.object(main, "RAID_STATE_DB", fresh_db):
            status_before = asgi_get(api.app, "/api/auth/status").json()
            self.assertFalse(status_before["authenticated"])
            self.assertTrue(status_before["setup_required"])

            # Password too short returns 400
            bad_setup = asgi_post(
                api.app,
                "/api/auth/setup",
                json_body={"username": "firstadmin", "password": "123"},
            )
            self.assertEqual(bad_setup.status_code, 400)

            # Valid initial setup creates user and returns token + cookie
            setup_resp = asgi_post(
                api.app,
                "/api/auth/setup",
                json_body={
                    "username": "firstadmin",
                    "password": "strongpassword",
                    "email": "first@example.com",
                },
            )
            self.assertEqual(setup_resp.status_code, 200)
            setup_data = setup_resp.json()
            self.assertTrue(setup_data["authenticated"])
            self.assertEqual(setup_data["username"], "firstadmin")
            self.assertIn("session_token=", setup_resp.headers.get("set-cookie", ""))

            # Second setup attempt is rejected with 400
            dup_setup = asgi_post(
                api.app,
                "/api/auth/setup",
                json_body={"username": "second", "password": "strongpassword"},
            )
            self.assertEqual(dup_setup.status_code, 400)

            # Login with invalid password returns 401
            bad_login = asgi_post(
                api.app,
                "/api/auth/login",
                json_body={"username": "firstadmin", "password": "wrongpassword"},
            )
            self.assertEqual(bad_login.status_code, 401)

            # Login with valid password returns 200 and session token
            login_resp = asgi_post(
                api.app,
                "/api/auth/login",
                json_body={"username": "firstadmin", "password": "strongpassword"},
            )
            self.assertEqual(login_resp.status_code, 200)
            login_token = login_resp.json()["token"]

            # Auth status with token shows authenticated
            status_after = asgi_get(
                api.app,
                "/api/auth/status",
                cookies={"session_token": login_token},
            ).json()
            self.assertTrue(status_after["authenticated"])
            self.assertEqual(status_after["username"], "firstadmin")
            self.assertFalse(status_after["setup_required"])

            # Google OAuth login test (mocking urlopen to Google tokeninfo endpoint)
            main.update_app_settings(
                {
                    "google_client_id": "my-google-client-id",
                    "allowed_google_emails": "allowed@example.com",
                }
            )

            missing_token_resp = asgi_post(api.app, "/api/auth/google", json_body={})
            self.assertEqual(missing_token_resp.status_code, 400)

            def mock_urlopen_factory(payload_dict):
                mock_cm = MagicMock()
                mock_cm.__enter__.return_value.read.return_value = json.dumps(
                    payload_dict
                ).encode("utf-8")
                return mock_cm

            # Allowed Google email succeeds
            with patch(
                "api.urlopen",
                return_value=mock_urlopen_factory(
                    {
                        "email": "allowed@example.com",
                        "email_verified": "true",
                        "aud": "my-google-client-id",
                    }
                ),
            ):
                google_ok = asgi_post(
                    api.app,
                    "/api/auth/google",
                    json_body={"credential": "fake-google-jwt"},
                )
                self.assertEqual(google_ok.status_code, 200)
                self.assertTrue(google_ok.json()["authenticated"])
                self.assertEqual(google_ok.json()["username"], "allowed@example.com")

            # Unauthorized Google email returns 403
            with patch(
                "api.urlopen",
                return_value=mock_urlopen_factory(
                    {
                        "email": "intruder@example.com",
                        "email_verified": "true",
                        "aud": "my-google-client-id",
                    }
                ),
            ):
                google_forbidden = asgi_post(
                    api.app,
                    "/api/auth/google",
                    json_body={"credential": "fake-google-jwt"},
                )
                self.assertEqual(google_forbidden.status_code, 403)

            # Audience mismatch returns 401
            with patch(
                "api.urlopen",
                return_value=mock_urlopen_factory(
                    {
                        "email": "allowed@example.com",
                        "email_verified": "true",
                        "aud": "wrong-client-id",
                    }
                ),
            ):
                google_bad_aud = asgi_post(
                    api.app,
                    "/api/auth/google",
                    json_body={"credential": "fake-google-jwt"},
                )
                self.assertEqual(google_bad_aud.status_code, 401)

            # URLError during tokeninfo verification returns 401
            with patch("api.urlopen", side_effect=URLError("network error")):
                google_net_err = asgi_post(
                    api.app,
                    "/api/auth/google",
                    json_body={"credential": "fake-google-jwt"},
                )
                self.assertEqual(google_net_err.status_code, 401)

            # Logout invalidates session token
            logout_resp = asgi_post(
                api.app,
                "/api/auth/logout",
                cookies={"session_token": login_token},
            )
            self.assertEqual(logout_resp.status_code, 200)
            self.assertFalse(logout_resp.json()["authenticated"])
            self.assertIsNone(main.verify_session(login_token))

    def test_settings_api_masks_discord_token_and_updates_sqlite(self):
        main.update_app_settings(
            {
                "discord_token": "super_secret_token_9876",
                "channel_id": "123456789",
                "raid_device": "/dev/md128",
                "mount_path": "/mnt/media/",
            }
        )

        with patch.object(
            main, "discover_raid_devices", return_value=["/dev/md0", "/dev/md127"]
        ):
            get_resp = asgi_get(api.app, "/api/settings", cookies=self.auth_cookies)
            self.assertEqual(get_resp.status_code, 200)
            settings_data = get_resp.json()
            self.assertTrue(settings_data["discord_token_configured"])
            self.assertEqual(settings_data["discord_token_masked"], "••••9876")
            self.assertNotIn("super_secret", json.dumps(settings_data))
            self.assertEqual(settings_data["channel_id"], "123456789")
            self.assertEqual(
                settings_data["available_raid_devices"], ["/dev/md0", "/dev/md127"]
            )
            self.assertEqual(settings_data["resolved_raid_device"], "/dev/md0")

        # Update settings without sending discord_token preserves existing token
        post_resp = asgi_post(
            api.app,
            "/api/settings",
            cookies=self.auth_cookies,
            json_body={
                "channel_id": "555666777",
                "raid_device": "/dev/md1",
                "mount_path": "/mnt/raid/",
                "google_client_id": "new-client-id",
                "allowed_google_emails": "user1@example.com",
            },
        )
        self.assertEqual(post_resp.status_code, 200)
        updated_data = post_resp.json()
        self.assertTrue(updated_data["saved"])
        self.assertEqual(updated_data["channel_id"], "555666777")
        self.assertEqual(updated_data["raid_device"], "/dev/md1")
        self.assertEqual(updated_data["mount_path"], "/mnt/raid/")
        self.assertEqual(updated_data["discord_token_masked"], "••••9876")

        # Updating discord_token changes the stored and masked value
        post_token_resp = asgi_post(
            api.app,
            "/api/settings",
            cookies=self.auth_cookies,
            json_body={"discord_token": "new_discord_token_4321"},
        )
        self.assertEqual(post_token_resp.status_code, 200)
        self.assertEqual(post_token_resp.json()["discord_token_masked"], "••••4321")
        self.assertEqual(main.get_app_settings()["discord_token"], "new_discord_token_4321")

    def test_notifications_test_endpoint_and_discord_enabled_toggle(self):
        # 1. Toggle discord_enabled to false via POST /api/settings
        toggle_false_resp = asgi_post(
            api.app,
            "/api/settings",
            cookies=self.auth_cookies,
            json_body={"discord_enabled": False},
        )
        self.assertEqual(toggle_false_resp.status_code, 200)
        self.assertFalse(toggle_false_resp.json()["discord_enabled"])
        self.assertEqual(main.get_app_settings()["discord_enabled"], "false")

        # 2. Toggle discord_enabled back to true
        toggle_true_resp = asgi_post(
            api.app,
            "/api/settings",
            cookies=self.auth_cookies,
            json_body={"discord_enabled": True},
        )
        self.assertEqual(toggle_true_resp.status_code, 200)
        self.assertTrue(toggle_true_resp.json()["discord_enabled"])
        self.assertEqual(main.get_app_settings()["discord_enabled"], "true")

        # 3. POST /api/notifications/test with mocked send_test_notification
        with patch.object(
            main,
            "send_test_notification",
            new=AsyncMock(return_value=(True, "Test notification sent successfully.")),
        ) as mock_send_test:
            test_resp = asgi_post(
                api.app,
                "/api/notifications/test",
                cookies=self.auth_cookies,
            )
            self.assertEqual(test_resp.status_code, 200)
            self.assertEqual(
                test_resp.json(),
                {"success": True, "message": "Test notification sent successfully."},
            )
            mock_send_test.assert_awaited_once()

        # 4. POST /api/notifications/test failure result
        with patch.object(
            main,
            "send_test_notification",
            new=AsyncMock(
                return_value=(False, "Discord notifications are disabled in settings.")
            ),
        ):
            test_fail_resp = asgi_post(
                api.app,
                "/api/notifications/test",
                cookies=self.auth_cookies,
            )
            self.assertEqual(test_fail_resp.status_code, 200)
            self.assertEqual(
                test_fail_resp.json(),
                {
                    "success": False,
                    "message": "Discord notifications are disabled in settings.",
                },
            )

    def test_release_update_check_and_apply_endpoints(self):
        executed_cmds = []

        def fake_check_output(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            if cmd[:3] == ["git", "rev-parse", "--short"] and cmd[3] == "HEAD":
                return b"abc1234\n"
            if cmd[:3] == ["git", "fetch", "origin"]:
                return b""
            if cmd[:3] == ["git", "tag", "-l"] and any("v*" in str(c) for c in cmd):
                return b"v0.1.0\nv0.2.0\n"
            if cmd[:3] == ["git", "rev-parse", "--short"] and "v0.2.0" in cmd[3]:
                return b"def5678\n"
            if cmd[:3] == ["git", "log", "--oneline"]:
                return b"def5678 Add settings and update manager\n"
            if cmd[:2] == ["git", "show"] and "v0.2.0:RELEASE_NOTES.md" in cmd[2]:
                return b"# Release v0.2.0 \xf0\x9f\x9a\x80\n\n- Add settings and update manager\n"
            if cmd[:3] == ["git", "checkout", "-f"]:
                return b"Previous HEAD position was abc1234\nHEAD is now at def5678\n"
            if len(cmd) == 2 and cmd[1] == "sync":
                return b"Resolved 20 packages\n"
            return b""

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "0.1.0", "commit": "abc1234"},
            ),
            patch.object(main, "check_output", side_effect=fake_check_output),
        ):
            check_resp = asgi_get(api.app, "/api/update/check", cookies=self.auth_cookies)
            self.assertEqual(check_resp.status_code, 200)
            check_data = check_resp.json()
            self.assertEqual(check_data["current_version"], "0.1.0")
            self.assertEqual(check_data["current_commit"], "abc1234")
            self.assertEqual(check_data["latest_version"], "0.2.0")
            self.assertEqual(check_data["latest_commit"], "def5678")
            self.assertTrue(check_data["update_available"])
            self.assertIn("Add settings and update manager", check_data["changelog"])
            self.assertEqual(check_data["release_notes"], "# Release v0.2.0\n\n- Add settings and update manager")

            with patch("main.subprocess.Popen") as mock_popen:
                apply_resp = asgi_post(
                    api.app, "/api/update/apply", cookies=self.auth_cookies
                )
                self.assertEqual(apply_resp.status_code, 200)
                apply_data = apply_resp.json()
                self.assertEqual(apply_data["status"], "updating")
                self.assertEqual(apply_data["target_version"], "0.2.0")
                self.assertIn(["git", "checkout", "-f", "v0.2.0"], executed_cmds)
                mock_popen.assert_called_once()

            changelog_resp = asgi_get(api.app, "/api/changelog", cookies=self.auth_cookies)
            self.assertEqual(changelog_resp.status_code, 200)
            changelog_data = changelog_resp.json()
            self.assertIn("changelog", changelog_data)
            self.assertTrue(isinstance(changelog_data["changelog"], str))

        # Verify apply_release_update succeeds even when uv binary raises FileNotFoundError
        def fake_check_output_no_uv(cmd, *args, **kwargs):
            if len(cmd) == 2 and cmd[1] == "sync":
                raise FileNotFoundError(2, "No such file or directory", "uv")
            return fake_check_output(cmd, *args, **kwargs)

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "0.1.0", "commit": "abc1234"},
            ),
            patch.object(main, "check_output", side_effect=fake_check_output_no_uv),
            patch("main.subprocess.Popen") as mock_popen,
        ):
            no_uv_resp = asgi_post(
                api.app, "/api/update/apply", cookies=self.auth_cookies
            )
            self.assertEqual(no_uv_resp.status_code, 200)
            self.assertEqual(no_uv_resp.json()["status"], "updating")
            mock_popen.assert_called_once()

        # Verify 500 response when git checkout / uv sync fails during apply
        with patch.object(
            main,
            "apply_release_update",
            side_effect=CalledProcessError(1, ["git", "checkout"], output=b"checkout conflict"),
        ):
            fail_resp = asgi_post(
                api.app, "/api/update/apply", cookies=self.auth_cookies
            )
            self.assertEqual(fail_resp.status_code, 500)
            self.assertIn("checkout conflict", fail_resp.json()["detail"])

    def test_api_changelog_endpoint(self):
        # Authenticated access when CHANGELOG.md exists
        with patch.object(main, "get_full_changelog", return_value="# Changelog\n\n- v0.8.2 Initial"):
            resp = asgi_get(api.app, "/api/changelog", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"changelog": "# Changelog\n\n- v0.8.2 Initial"})

        # Fallback when CHANGELOG.md is missing or raises OSError
        with patch.object(main, "get_full_changelog", return_value=""):
            resp = asgi_get(api.app, "/api/changelog", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"changelog": ""})

    def test_check_for_release_update_with_curated_notes(self):
        def fake_git(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "fetch", "origin"]:
                return b""
            if cmd[:3] == ["git", "tag", "-l"] and any("v*" in str(c) for c in cmd):
                return b"v0.1.0\nv0.9.0\n"
            if cmd[:3] == ["git", "rev-parse", "--short"]:
                return b"abc999\n"
            if cmd[:3] == ["git", "log", "--oneline"]:
                return b"abc999 Commit log\n"
            if cmd[:2] == ["git", "show"] and "v0.9.0:RELEASE_NOTES.md" in cmd[2]:
                return (
                    b"# Release v0.9.0 \xf0\x9f\x9a\x80\n\n"
                    b"## Highlights \xe2\x9c\xa8\n"
                    b"- \xf0\x9f\x9a\x80 Fast streaming\n"
                    b"- \xe2\x9a\xa1 Low overhead\n"
                )
            return b""

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "0.1.0", "commit": "old000"},
            ),
            patch.object(main, "check_output", side_effect=fake_git),
        ):
            res = main.check_for_release_update()
            self.assertTrue(res["update_available"])
            self.assertEqual(res["latest_version"], "0.9.0")
            self.assertEqual(
                res["release_notes"],
                "# Release v0.9.0\n\n## Highlights\n- Fast streaming\n- Low overhead",
            )
            self.assertEqual(res["changelog"], "abc999 Commit log")

    def test_check_for_release_update_fallback_to_tag_message(self):
        def fake_git(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "fetch", "origin"]:
                return b""
            if cmd[:3] == ["git", "tag", "-l"] and any("v*" in str(c) for c in cmd):
                return b"v0.1.0\nv0.9.0\n"
            if cmd[:3] == ["git", "rev-parse", "--short"]:
                return b"abc999\n"
            if cmd[:3] == ["git", "log", "--oneline"]:
                return b"abc999 Commit log\n"
            if cmd[:2] == ["git", "show"]:
                raise CalledProcessError(1, cmd, output=b"fatal: path not found")
            if cmd[:3] == ["git", "tag", "-l"] and any("%(contents)" in str(c) for c in cmd):
                return b"Release v0.9.0 \xf0\x9f\x8e\x89\nCurated tag message\n"
            return b""

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "0.1.0", "commit": "old000"},
            ),
            patch.object(main, "check_output", side_effect=fake_git),
        ):
            res = main.check_for_release_update()
            self.assertTrue(res["update_available"])
            self.assertEqual(
                res["release_notes"],
                "Release v0.9.0\nCurated tag message",
            )

    def test_check_for_release_update_fallback_to_git_log(self):
        def fake_git(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "fetch", "origin"]:
                return b""
            if cmd[:3] == ["git", "tag", "-l"] and any("v*" in str(c) for c in cmd):
                return b"v0.1.0\nv0.9.0\n"
            if cmd[:3] == ["git", "rev-parse", "--short"]:
                return b"abc999\n"
            if cmd[:3] == ["git", "log", "--oneline"]:
                return b"abc999 Direct log fallback \xf0\x9f\x90\x9b\n"
            if cmd[:2] == ["git", "show"]:
                raise CalledProcessError(1, cmd, output=b"fatal: not found")
            if cmd[:3] == ["git", "tag", "-l"] and any("%(contents)" in str(c) for c in cmd):
                return b""
            return b""

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "0.1.0", "commit": "old000"},
            ),
            patch.object(main, "check_output", side_effect=fake_git),
        ):
            res = main.check_for_release_update()
            self.assertTrue(res["update_available"])
            self.assertEqual(res["release_notes"], "abc999 Direct log fallback")
            self.assertEqual(res["changelog"], "abc999 Direct log fallback \U0001f41b")

    def test_check_for_release_update_no_update_and_no_tags(self):
        # Case 1: No tags
        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "1.0.0", "commit": "cur123"},
            ),
            patch.object(main, "check_output", return_value=b""),
        ):
            res = main.check_for_release_update()
            self.assertFalse(res["update_available"])
            self.assertEqual(res["release_notes"], "")
            self.assertEqual(res["changelog"], "")

        # Case 2: Latest version <= current version
        def fake_git_uptodate(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "tag", "-l"] and any("v*" in str(c) for c in cmd):
                return b"v1.0.0\nv0.9.0\n"
            return b""

        with (
            patch.object(
                main,
                "get_current_version_info",
                return_value={"version": "1.0.0", "commit": "cur123"},
            ),
            patch.object(main, "check_output", side_effect=fake_git_uptodate),
        ):
            res = main.check_for_release_update()
            self.assertFalse(res["update_available"])
            self.assertEqual(res["release_notes"], "")
            self.assertEqual(res["changelog"], "")

    def test_strip_emojis_utility(self):
        self.assertEqual(main.strip_emojis(""), "")
        self.assertEqual(main.strip_emojis(None), "")
        raw = (
            "# Release v1.0.0 \U0001f680\n\n"
            "## Highlights \u2728\n"
            "- \U0001f680 High-performance direct event streaming\n"
            "    - \u26a1 Debian-native low-overhead monitoring\n"
            "- \U0001f41b Fix concurrency issue\n"
        )
        expected = (
            "# Release v1.0.0\n\n"
            "## Highlights\n"
            "- High-performance direct event streaming\n"
            "    - Debian-native low-overhead monitoring\n"
            "- Fix concurrency issue"
        )
        self.assertEqual(main.strip_emojis(raw), expected)

    def test_get_api_status_returns_raid_system_and_notification_state(self):
        ts = 1700001000.0
        main.record_notification("clean", ts, weekly=True)

        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ) as mock_raid,
            patch.object(
                main, "get_duf_output", return_value=SAMPLE_DUF_TABLE
            ) as mock_duf,
        ):
            response = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)

        self.assertEqual(response.status_code, 200)
        mock_raid.assert_called_once()
        mock_duf.assert_called_once()

        payload = response.json()
        self.assertIn("raid", payload)
        self.assertIn("system", payload)
        self.assertIn("notification_state", payload)
        self.assertIn("version", payload)

        self.assertEqual(payload["raid"]["status"], "clean")
        self.assertEqual(payload["raid"]["raid_level"], "raid1")
        self.assertEqual(payload["raid"]["array_size"], "9.31 GiB")
        self.assertEqual(
            payload["raid"]["storage_info"], "8.12T/14.55T (55.8%) | 6.43T free"
        )
        self.assertEqual(len(payload["raid"]["devices"]), 2)

        system = payload["system"]
        self.assertIn("cpu_percent", system)
        self.assertIn("memory", system)
        self.assertIn("disk", system)
        self.assertIn("boot_time", system)

        notif = payload["notification_state"]
        self.assertIsNotNone(notif)
        self.assertEqual(notif["sent_at"], ts)
        self.assertEqual(notif["due_at"], ts + main.WEEK_SECONDS)
        self.assertEqual(notif["status"], "clean")

    def test_get_api_status_when_no_notification_state_recorded(self):
        with (
            patch.object(
                main, "check_raid_status", return_value=("degraded", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
        ):
            response = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["raid"]["status"], "degraded")
        self.assertIsNone(payload["notification_state"])

    def test_get_system_metrics_and_storage_endpoint(self):
        metrics = api.get_system_metrics()
        self.assertIn("cpu_cores", metrics)
        self.assertIsInstance(metrics["cpu_cores"], list)
        self.assertIn("cpu_count_logical", metrics)
        self.assertGreaterEqual(metrics["cpu_count_logical"], 1)
        self.assertIn("cpu_count_physical", metrics)
        self.assertGreaterEqual(metrics["cpu_count_physical"], 1)
        self.assertIn("load_avg", metrics)
        self.assertIsInstance(metrics["load_avg"], list)
        self.assertIn("memory", metrics)
        mem = metrics["memory"]
        self.assertIn("total", mem)
        self.assertIn("used", mem)
        self.assertIn("available", mem)
        self.assertIn("percent", mem)
        self.assertIn("free", mem)
        self.assertIn("cached", mem)
        self.assertIn("buffers", mem)
        self.assertIn("disks", metrics)
        self.assertIsInstance(metrics["disks"], list)

        # GET /api/system/storage
        resp = asgi_get(api.app, "/api/system/storage", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("disks", data)
        self.assertIsInstance(data["disks"], list)
        if data["disks"]:
            disk_item = data["disks"][0]
            for key in ["mountpoint", "device", "fstype", "opts", "total", "used", "free", "percent"]:
                self.assertIn(key, disk_item)

    def test_get_api_history_returns_formatted_notifications_and_respects_limit(self):
        main.record_notification("clean", 1700000100.0, weekly=True)
        main.record_notification("degraded", 1700000200.0, weekly=False)
        main.record_notification("recovering", 1700000300.0, weekly=False)
        main.record_notification("test", 1700000400.0, weekly=False)

        response = asgi_get(api.app, "/api/history", cookies=self.auth_cookies)
        self.assertEqual(response.status_code, 200)
        history = response.json()["history"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["status"], "test")
        self.assertFalse(history[0]["weekly"])
        self.assertEqual(history[0]["kind"], "test notification")
        self.assertEqual(history[1]["status"], "recovering")
        self.assertFalse(history[1]["weekly"])
        self.assertEqual(history[1]["kind"], "alert")
        self.assertEqual(history[3]["status"], "clean")
        self.assertTrue(history[3]["weekly"])
        self.assertEqual(history[3]["kind"], "weekly summary")

        limited_resp = asgi_get(
            api.app, "/api/history?limit=1", cookies=self.auth_cookies
        )
        self.assertEqual(limited_resp.status_code, 200)
        limited_history = limited_resp.json()["history"]
        self.assertEqual(len(limited_history), 1)
        self.assertEqual(limited_history[0]["status"], "test")

        invalid_resp = asgi_get(
            api.app, "/api/history?limit=0", cookies=self.auth_cookies
        )
        self.assertEqual(invalid_resp.status_code, 422)

    def test_api_sqlite_error_and_missing_index_fallbacks(self):
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF),
            patch.object(
                main,
                "get_weekly_notification_state",
                side_effect=sqlite3.Error("db unavailable"),
            ),
        ):
            status_resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
            self.assertEqual(status_resp.status_code, 200)
            self.assertIsNone(status_resp.json()["notification_state"])

        with patch.object(
            main,
            "get_notification_history",
            side_effect=sqlite3.Error("db unavailable"),
        ):
            history_resp = asgi_get(api.app, "/api/history", cookies=self.auth_cookies)
            self.assertEqual(history_resp.status_code, 200)
            self.assertEqual(history_resp.json(), {"history": []})

        empty_static_dir = Path(self.temp_dir.name) / "empty_static"
        empty_static_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(api, "STATIC_DIR", empty_static_dir):
            fallback_resp = asgi_get(api.app, "/")
            self.assertEqual(fallback_resp.status_code, 200)
            self.assertIn("<h1>Vigil</h1>", fallback_resp.text)

    def test_get_root_serves_dashboard_html(self):
        response = asgi_get(api.app, "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("<html", response.text.lower())
        self.assertEqual(
            response.headers.get("cache-control"), "no-cache, no-store, must-revalidate"
        )
        self.assertEqual(response.headers.get("pragma"), "no-cache")
        self.assertEqual(response.headers.get("expires"), "0")

        current_ver = main.get_current_version_info()["current_version"]
        self.assertIn(f"/static/styles.css?v={current_ver}", response.text)
        self.assertIn(f"/static/app.js?v={current_ver}", response.text)

    def test_spa_fallback_routes_and_api_static_404(self):
        """Test full SPA fallback routing for client-side navigation and proper 404s for API/static paths."""
        spa_paths = [
            "/audit",
            "/services",
            "/processes",
            "/users",
            "/storage",
            "/disks",
            "/overview",
            "/notifications",
            "/containers/stacks",
            "/raid",
            "/containers",
            "/containers/logs/web-service",
            "/users/manage/admin",
            "/login",
            "/logs",
            "/deep/nested/view",
        ]
        for path in spa_paths:
            resp = asgi_get(api.app, path)
            self.assertEqual(
                resp.status_code,
                200,
                f"SPA route {path} should serve dashboard HTML with status 200",
            )
            self.assertIn("text/html", resp.headers.get("content-type", ""))
            self.assertIn("<html", resp.text.lower())
            self.assertIn("vigil", resp.text.lower())

        # Unknown API endpoints must return 404 (JSON {"detail": "Not Found"}), not fallback to HTML
        for api_path in ["/api/unknown_endpoint", "/api/nonexistent", "/api", "/api/unknown/subroute"]:
            api_resp = asgi_get(api.app, api_path, cookies=self.auth_cookies)
            self.assertEqual(api_resp.status_code, 404)
            self.assertIn("application/json", api_resp.headers.get("content-type", ""))
            self.assertEqual(api_resp.json(), {"detail": "Not Found"})

        # Non-existent static files must return 404, not HTML
        for static_path in ["/static/unknown_file.xyz", "/static/does_not_exist.css"]:
            static_resp = asgi_get(api.app, static_path)
            self.assertEqual(static_resp.status_code, 404)

        # Favicon routes must serve icon assets
        fav_ico_resp = asgi_get(api.app, "/favicon.ico")
        self.assertEqual(fav_ico_resp.status_code, 200)
        self.assertIn("image/x-icon", fav_ico_resp.headers.get("content-type", ""))

        fav_svg_resp = asgi_get(api.app, "/static/favicon.svg")
        self.assertEqual(fav_svg_resp.status_code, 200)
        self.assertIn("image/svg+xml", fav_svg_resp.headers.get("content-type", ""))

    def test_storage_ui_renaming_and_overview_card_simplification(self):
        """Verify storage UI renaming in sidebar and view heading, plus overview storage metric card simplification."""
        resp = asgi_get(api.app, "/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        # 1. Sidebar tab #nav-tab-raid has title "Storage" and text "Storage"
        self.assertRegex(
            html,
            r'<button[^>]*id="nav-tab-raid"[^>]*title="Storage"[^>]*>[\s\S]*?<span>Storage</span>[\s\S]*?</button>',
            "Sidebar tab #nav-tab-raid must have title 'Storage' and display 'Storage'",
        )

        # 2. #view-raid section heading is "Storage"
        view_raid_start = html.find('id="view-raid"')
        self.assertGreater(view_raid_start, -1)
        view_containers_start = html.find('id="view-containers"')
        self.assertGreater(view_containers_start, view_raid_start)
        view_raid_html = html[view_raid_start:view_containers_start]

        self.assertRegex(
            view_raid_html,
            r'<h2[^>]*id="raid-details-heading"[^>]*>\s*Storage\s*</h2>',
            "#view-raid section heading must be 'Storage'",
        )

        # 3. #storage-metric-card on overview page includes required elements
        overview_start = html.find('id="view-overview"')
        self.assertGreater(overview_start, -1)
        overview_end = html.find('id="view-raid"')
        self.assertGreater(overview_end, overview_start)
        overview_html = html[overview_start:overview_end]

        card_match = re.search(r'<article[^>]*id="storage-metric-card"[^>]*>([\s\S]*?)</article>', overview_html)
        self.assertIsNotNone(card_match, "#storage-metric-card not found in overview panel")
        card_html = card_match.group(0)
        self.assertIn('id="btn-goto-storage"', card_html)
        self.assertIn('id="disk-progress-bar"', card_html)
        self.assertIn('id="disk-percent-value"', card_html)
        self.assertIn('id="disk-detail"', card_html)

        # 4. #overview-mounts-container has class "hidden"
        self.assertRegex(
            card_html,
            r'<div[^>]*id="overview-mounts-container"[^>]*class="[^"]*\bhidden\b[^"]*"',
            "#overview-mounts-container must have 'hidden' class",
        )

    def test_static_assets_and_dashboard_elements_served(self):
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        self.assertNotIn('id="header-raid-pill"', index_resp.text)
        self.assertIn('id="overview-raid-status-badge"', index_resp.text)
        self.assertIn('id="active-disks-table"', index_resp.text)
        self.assertIn('id="notification-history-table"', index_resp.text)
        self.assertIn('id="auth-overlay"', index_resp.text)
        self.assertIn('id="settings-modal"', index_resp.text)
        self.assertNotIn('id="system-interval-select"', index_resp.text)
        self.assertNotIn('id="raid-interval-select"', index_resp.text)
        self.assertIn('id="notifications-interval-select"', index_resp.text)

        # Navigation tabs and view panels (verifying System Level: Overview, RAID & Storage, then Containers, Notifications)
        self.assertIn('id="nav-tab-overview"', index_resp.text)
        self.assertIn('id="nav-tab-raid"', index_resp.text)
        self.assertIn('id="nav-tab-containers"', index_resp.text)
        self.assertIn('id="nav-tab-notifications"', index_resp.text)
        self.assertIn(">Overview</span>", index_resp.text)
        self.assertTrue(">Storage</span>" in index_resp.text or ">RAID &amp; Storage</span>" in index_resp.text)
        self.assertIn(">Containers</span>", index_resp.text)
        self.assertIn(">Notifications</span>", index_resp.text)
        self.assertIn('id="view-overview"', index_resp.text)
        self.assertIn('id="view-raid"', index_resp.text)
        self.assertIn('id="view-containers"', index_resp.text)
        self.assertIn('id="view-notifications"', index_resp.text)

        # Verify nav order: Overview -> RAID & Storage -> Containers -> Notifications
        idx_nav_overview = index_resp.text.find('id="nav-tab-overview"')
        idx_nav_raid = index_resp.text.find('id="nav-tab-raid"')
        idx_nav_containers = index_resp.text.find('id="nav-tab-containers"')
        idx_nav_notifs = index_resp.text.find('id="nav-tab-notifications"')
        self.assertTrue(
            -1 < idx_nav_overview < idx_nav_raid < idx_nav_containers < idx_nav_notifs,
            "Nav order must be Overview, RAID & Storage, Containers, Notifications",
        )

        # Docker UI elements in Containers view
        self.assertIn('id="docker-install-banner"', index_resp.text)
        self.assertIn('id="btn-install-docker"', index_resp.text)
        self.assertIn('id="docker-stopped-banner"', index_resp.text)
        self.assertIn('id="docker-daemon-badge"', index_resp.text)
        self.assertIn('id="docker-version-info"', index_resp.text)
        self.assertIn('id="docker-interval-select"', index_resp.text)
        self.assertIn('id="docker-status-dot"', index_resp.text)
        self.assertIn('id="nav-tab-containers-badge"', index_resp.text)
        self.assertIn('id="refresh-docker-btn"', index_resp.text)
        self.assertIn('id="docker-stat-stacks"', index_resp.text)
        self.assertIn('id="docker-stat-containers"', index_resp.text)
        self.assertIn('id="docker-stat-images"', index_resp.text)
        self.assertIn('id="docker-stat-volumes"', index_resp.text)
        self.assertIn('id="docker-stat-networks"', index_resp.text)

        # Container filters
        self.assertIn('id="containers-search-input"', index_resp.text)
        self.assertIn('id="containers-filter-stack"', index_resp.text)
        self.assertIn('id="containers-filter-status"', index_resp.text)

        # Automated Update Schedule UI
        self.assertIn('update-schedule-card', index_resp.text)
        self.assertIn('id="update-schedule-form"', index_resp.text)
        self.assertIn('id="update-schedule-badge"', index_resp.text)
        self.assertIn('id="schedule-enabled-toggle"', index_resp.text)
        self.assertIn('id="schedule-day-select"', index_resp.text)
        self.assertIn('id="schedule-time-input"', index_resp.text)
        self.assertIn('id="btn-save-schedule"', index_resp.text)
        self.assertIn('id="schedule-next-run"', index_resp.text)

        # Docker sub-tabs and tables
        self.assertIn('id="subtab-docker-stacks"', index_resp.text)
        self.assertIn('id="subtab-docker-containers"', index_resp.text)
        self.assertIn('id="subtab-docker-images"', index_resp.text)
        self.assertIn('id="subtab-docker-volumes"', index_resp.text)
        self.assertIn('id="subtab-docker-networks"', index_resp.text)
        self.assertIn('id="subtab-docker-updates"', index_resp.text)
        self.assertIn('id="docker-stacks-table"', index_resp.text)
        self.assertIn('id="docker-containers-table"', index_resp.text)
        self.assertIn('id="docker-images-table"', index_resp.text)
        self.assertIn('id="docker-volumes-table"', index_resp.text)
        self.assertIn('id="docker-networks-table"', index_resp.text)
        self.assertIn('id="docker-updates-table"', index_resp.text)

        # Modals for Docker management
        self.assertIn('id="container-logs-modal"', index_resp.text)
        self.assertIn('id="container-logs-viewer"', index_resp.text)
        self.assertIn('id="stack-editor-modal"', index_resp.text)
        self.assertIn('id="stack-editor-form"', index_resp.text)
        self.assertIn('id="volume-details-modal"', index_resp.text)
        self.assertIn('id="vol-detail-name"', index_resp.text)
        self.assertIn('id="vol-detail-driver"', index_resp.text)
        self.assertIn('id="vol-detail-mountpoint"', index_resp.text)
        self.assertIn('id="vol-detail-containers"', index_resp.text)
        self.assertIn('id="close-volume-details-btn"', index_resp.text)

        # Timezone selector in settings modal
        self.assertIn('id="setting-timezone"', index_resp.text)

        # Notifications tab ('Notifications'), view panels, and Discord setup card
        self.assertIn('id="discord-config-form"', index_resp.text)
        self.assertIn('id="save-discord-btn"', index_resp.text)
        self.assertIn('id="send-test-notification-btn"', index_resp.text)
        self.assertIn('id="discord-feedback"', index_resp.text)
        self.assertIn('id="discord-status-badge"', index_resp.text)
        self.assertIn('id="discord-channel-input"', index_resp.text)
        self.assertIn('id="discord-token-input"', index_resp.text)
        self.assertIn('id="discord-enabled-toggle"', index_resp.text)
        self.assertIn('name="discord_enabled"', index_resp.text)

        # Sysfs RAID rebuild progress, direct stream indicator, and toast notifications
        self.assertIn('id="raid-sync-container"', index_resp.text)
        self.assertIn('id="raid-sync-label"', index_resp.text)
        self.assertIn('id="raid-sync-progress-fill"', index_resp.text)
        self.assertNotIn('id="live-stream-indicator"', index_resp.text)
        self.assertIn('id="toast-container"', index_resp.text)

        # Software update header notice button and update modal
        self.assertIn('id="nav-update-notice-btn"', index_resp.text)
        self.assertIn('class="update-pulse-dot"', index_resp.text)
        self.assertIn('id="nav-update-version-label"', index_resp.text)
        self.assertIn('id="update-modal"', index_resp.text)
        self.assertIn('id="close-update-modal-btn"', index_resp.text)
        self.assertIn('id="modal-current-version"', index_resp.text)
        self.assertIn('id="modal-latest-version"', index_resp.text)
        self.assertIn('id="modal-release-notes"', index_resp.text)
        self.assertIn('id="modal-update-status"', index_resp.text)
        self.assertIn('id="modal-dismiss-btn"', index_resp.text)
        self.assertIn('id="modal-apply-update-btn"', index_resp.text)
        self.assertIn('id="modal-toggle-changelog-btn"', index_resp.text)

        # Services, Processes, Users, and Audit Navigation Tabs & Views
        self.assertIn('id="nav-tab-services"', index_resp.text)
        self.assertIn('id="nav-tab-processes"', index_resp.text)
        self.assertIn('id="nav-tab-users"', index_resp.text)
        self.assertIn('id="nav-tab-audit"', index_resp.text)

        self.assertIn('id="view-services"', index_resp.text)
        self.assertIn('id="view-processes"', index_resp.text)
        self.assertIn('id="view-users"', index_resp.text)
        self.assertIn('id="view-audit"', index_resp.text)

        # Overview Ribbon & Telemetry Enhancements
        self.assertIn('id="overview-services-badge"', index_resp.text)
        self.assertIn('id="overview-services-summary"', index_resp.text)
        self.assertIn('id="btn-goto-services"', index_resp.text)
        self.assertIn('id="overview-mounts-list"', index_resp.text)
        self.assertIn('id="host-os-detail"', index_resp.text)
        self.assertIn('id="host-kernel-detail"', index_resp.text)

        # Services View Elements
        self.assertIn('id="services-total-count"', index_resp.text)
        self.assertIn('id="services-running-badge"', index_resp.text)
        self.assertIn('id="services-failed-badge"', index_resp.text)
        self.assertIn('id="services-search-input"', index_resp.text)
        self.assertIn('id="services-state-filter"', index_resp.text)
        self.assertIn('id="services-table-body"', index_resp.text)
        self.assertIn('id="service-logs-modal"', index_resp.text)

        # Process Explorer View Elements
        self.assertIn('id="proc-total-count"', index_resp.text)
        self.assertIn('id="proc-running-count"', index_resp.text)
        self.assertIn('id="processes-search-input"', index_resp.text)
        self.assertIn('id="processes-sort-select"', index_resp.text)
        self.assertIn('id="processes-table-body"', index_resp.text)
        self.assertIn('id="process-kill-modal"', index_resp.text)

        # Linux Users & Groups View Elements
        self.assertIn('id="users-total-count"', index_resp.text)
        self.assertIn('id="users-regular-count"', index_resp.text)
        self.assertIn('id="users-regular-only-toggle"', index_resp.text)
        self.assertIn('id="modal-add-user"', index_resp.text)
        self.assertIn('id="modal-add-group"', index_resp.text)

        # Security & Audit Logs View Elements
        self.assertIn('id="audit-total-count"', index_resp.text)
        self.assertIn('id="audit-security-count"', index_resp.text)
        self.assertIn('id="audit-search-input"', index_resp.text)
        self.assertIn('id="audit-severity-filter"', index_resp.text)
        self.assertIn('id="audit-table-body"', index_resp.text)

        # Settings modal should NOT contain Discord fields
        modal_start = index_resp.text.find('id="settings-modal"')
        self.assertGreater(modal_start, -1)
        modal_end = index_resp.text.find('</form>', modal_start)
        self.assertGreater(modal_end, modal_start)
        settings_modal_html = index_resp.text[modal_start:modal_end]
        self.assertNotIn("setting-discord-token", settings_modal_html)
        self.assertNotIn("setting-channel-id", settings_modal_html)
        self.assertNotIn("setting-discord-enabled", settings_modal_html)
        self.assertNotIn('name="discord_token"', settings_modal_html)
        self.assertNotIn('name="discord_enabled"', settings_modal_html)

        css_resp = asgi_get(api.app, "/static/styles.css")
        self.assertEqual(css_resp.status_code, 200)
        self.assertIn("text/css", css_resp.headers.get("content-type", ""))
        self.assertEqual(css_resp.headers.get("cache-control"), "no-cache, must-revalidate")
        self.assertTrue(len(css_resp.text) > 0)

        js_resp = asgi_get(api.app, "/static/app.js")
        self.assertEqual(js_resp.status_code, 200)
        self.assertIn("javascript", js_resp.headers.get("content-type", ""))
        self.assertEqual(js_resp.headers.get("cache-control"), "no-cache, must-revalidate")
        self.assertTrue(len(js_resp.text) > 0)

    def test_cross_tier_qa_contract_integrity(self):
        """Verify frontend API route calls and DOM element references match backend and HTML."""
        import re

        app_js = Path("static/app.js").read_text(encoding="utf-8")
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        api_py = Path("api.py").read_text(encoding="utf-8")

        # 1. API routes called by frontend must exist in api.py
        api_calls = set(re.findall(r'apiFetch\(\s*["\']([^"\'\?]+)', app_js))
        self.assertGreater(len(api_calls), 10, "Expected frontend API calls to be detected")
        for route in api_calls:
            clean_route = route.rstrip("/")
            found = clean_route in api_py or route.split("/")[-1] in api_py
            self.assertTrue(found, f"Frontend calls route '{route}' but it was not found in api.py")

        # 2. Key DOM IDs accessed by app.js must exist in index.html
        direct_ids = set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', app_js))
        permissible_fallbacks = {
            "docker-container-search",
            "setting-raid-device",
            "docker-containers-all-toggle",
            "test-notification-feedback",
            "header-raid-pill",
            "raid-status-badge",
            "raid-status-text",
            "live-stream-indicator",
            "live-stream-text",
            "auto-refresh-indicator",
            "system-interval-select",
            "raid-interval-select",
            "raid-adaptive-badge",
        }
        for elem_id in direct_ids:
            if elem_id not in permissible_fallbacks:
                has_id = f'id="{elem_id}"' in index_html or f"id='{elem_id}'" in index_html
                self.assertTrue(has_id, f"Frontend accesses DOM id '{elem_id}' missing in index.html")

    def test_dedicated_login_page_contract_and_behavior(self):
        """Test full-page login view #view-login structure and authentication transitions."""
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        html = index_resp.text

        # Verify dedicated login view exists with login-page-container class
        self.assertIn('id="view-login"', html)
        self.assertIn('class="login-page-container', html)
        self.assertIn('id="auth-overlay"', html)
        self.assertIn('class="auth-brand"', html)
        self.assertIn('id="auth-modal-title"', html)
        self.assertIn('id="auth-modal-subtitle"', html)
        self.assertIn('id="auth-setup-form"', html)
        self.assertIn('id="auth-login-form"', html)
        self.assertIn('id="app-shell"', html)

        # Verify JS visibility toggling logic between view-login and app-shell
        app_js = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn('viewLogin.classList.remove("hidden")', app_js)
        self.assertIn('appShell.classList.add("hidden")', app_js)
        self.assertIn('viewLogin.classList.add("hidden")', app_js)
        self.assertIn('appShell.classList.remove("hidden")', app_js)

        # Verify CSS rules for login-page-container
        styles_css = Path("static/styles.css").read_text(encoding="utf-8")
        self.assertIn(".login-page-container", styles_css)
        self.assertIn("min-height: 100vh", styles_css)

    def test_sidebar_navigation_hierarchy_and_view_switching(self):
        """Test 4-pillar sidebar navigation hierarchy: Monitoring & Health, Workloads & Runtime, System & Security, Alerts & Auditing."""
        import re
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        html = index_resp.text

        self.assertIn('id="app-sidebar"', html)
        self.assertIn('class="app-sidebar"', html)

        # Extract the <nav class="sidebar-nav" ...> block
        nav_start = html.find('<nav class="sidebar-nav"')
        self.assertGreater(nav_start, -1)
        nav_end = html.find('</nav>', nav_start)
        self.assertGreater(nav_end, nav_start)
        nav_html = html[nav_start:nav_end]

        # 1. Verify the 4 .sidebar-nav-heading labels in exact order
        headings = re.findall(r'<span class="sidebar-nav-heading">(.*?)</span>', nav_html)
        expected_headings = [
            "Monitoring &amp; Health",
            "Workloads &amp; Runtime",
            "System &amp; Security",
            "Alerts &amp; Auditing",
        ]
        self.assertEqual(headings, expected_headings)

        # 2. Verify exact relative DOM order of all 12 navigation buttons
        expected_nav_buttons = [
            ("nav-tab-overview", "view-overview"),
            ("nav-tab-raid", "view-raid"),
            ("nav-tab-probes", "view-probes"),
            ("nav-tab-containers", "view-containers"),
            ("nav-tab-services", "view-services"),
            ("nav-tab-processes", "view-processes"),
            ("nav-tab-packages", "view-packages"),
            ("nav-tab-security", "view-security"),
            ("nav-tab-users", "view-users"),
            ("nav-tab-backups", "view-backups"),
            ("nav-tab-notifications", "view-notifications"),
            ("nav-tab-audit", "view-audit"),
        ]
        nav_button_ids = re.findall(r'<button\s+[^>]*id="(nav-tab-[a-z-]+)"', nav_html)
        self.assertEqual(nav_button_ids, [btn_id for btn_id, _ in expected_nav_buttons])

        # Verify each section groups the expected buttons
        sections = nav_html.split('<div class="sidebar-nav-section">')[1:]
        self.assertEqual(len(sections), 4)
        self.assertEqual(
            re.findall(r'<button\s+[^>]*id="(nav-tab-[a-z-]+)"', sections[0]),
            ["nav-tab-overview", "nav-tab-raid", "nav-tab-probes"],
        )
        self.assertEqual(
            re.findall(r'<button\s+[^>]*id="(nav-tab-[a-z-]+)"', sections[1]),
            ["nav-tab-containers", "nav-tab-services", "nav-tab-processes"],
        )
        self.assertEqual(
            re.findall(r'<button\s+[^>]*id="(nav-tab-[a-z-]+)"', sections[2]),
            ["nav-tab-packages", "nav-tab-security", "nav-tab-users", "nav-tab-backups"],
        )
        self.assertEqual(
            re.findall(r'<button\s+[^>]*id="(nav-tab-[a-z-]+)"', sections[3]),
            ["nav-tab-notifications", "nav-tab-audit"],
        )

        # 3. Verify all aria-controls and badge IDs remain intact
        for btn_id, view_id in expected_nav_buttons:
            self.assertIn(f'id="{btn_id}"', nav_html)
            self.assertIn(f'aria-controls="{view_id}"', nav_html)

        for badge_id in [
            "nav-tab-probes-badge",
            "nav-tab-containers-badge",
            "nav-tab-services-badge",
            "nav-badge-packages",
        ]:
            self.assertIn(f'id="{badge_id}"', nav_html)

        # Verify mobile toggle and backdrop controls
        self.assertIn('id="sidebar-toggle-btn"', html)
        self.assertIn('class="btn-sidebar-toggle"', html)
        self.assertIn('id="sidebar-backdrop"', html)

        # Check view switching handlers in app.js
        app_js = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("switchViewTab(", app_js)
        for btn_id, view_id in expected_nav_buttons:
            self.assertIn(f'"{btn_id}"', app_js)
            self.assertIn(f'"{view_id}"', app_js)

    def test_dedicated_raid_storage_view_and_overview_cockpit(self):
        """Test #view-raid isolation and #view-overview cockpit status cards."""
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        html = index_resp.text

        # Verify view-raid has dedicated role and contains full RAID details
        raid_start = html.find('id="view-raid"')
        self.assertGreater(raid_start, -1)
        raid_end = html.find('id="view-containers"')
        self.assertGreater(raid_end, raid_start)
        raid_view_html = html[raid_start:raid_end]

        self.assertIn('raid-details-card', raid_view_html)
        self.assertIn('id="raid-card-badge"', raid_view_html)
        self.assertIn('id="active-disks-table"', raid_view_html)
        self.assertIn('id="raid-sync-container"', raid_view_html)
        self.assertNotIn('id="raid-interval-select"', raid_view_html)
        self.assertIn('id="refresh-raid-btn"', raid_view_html)

        # Verify Storage Volumes & Filesystems card is positioned above the RAID Array Details grid
        mounts_pos = raid_view_html.find('id="storage-mounts-card"')
        raid_grid_pos = raid_view_html.find('class="raid-grid"')
        self.assertGreater(mounts_pos, -1)
        self.assertGreater(raid_grid_pos, -1)
        self.assertLess(mounts_pos, raid_grid_pos)

        # Verify Vigil Logs button exists on sidebar nav and is removed from topbar
        self.assertNotIn('id="btn-topbar-vigil-logs"', html)
        self.assertIn('id="btn-sidebar-vigil-logs"', html)

        # Verify overview panel contains the cockpit summary cards and quick navigation buttons
        overview_start = html.find('id="view-overview"')
        self.assertGreater(overview_start, -1)
        overview_end = html.find('id="view-raid"')
        overview_view_html = html[overview_start:overview_end]

        self.assertIn("overview-cockpit-grid", overview_view_html)
        self.assertIn('id="overview-raid-status-badge"', overview_view_html)
        self.assertIn('id="btn-goto-raid"', overview_view_html)
        self.assertIn('id="overview-docker-badge"', overview_view_html)
        self.assertIn('id="btn-goto-containers"', overview_view_html)
        self.assertIn('id="overview-discord-badge"', overview_view_html)
        self.assertIn('id="btn-goto-notifications"', overview_view_html)

        # Ensure active-disks-table is NOT placed in the high-level overview
        self.assertNotIn('id="active-disks-table"', overview_view_html)

    def test_system_overview_drilldowns_storage_mounts_and_update_tags(self):
        """Verify CPU drilldowns, memory SVG trend chart, storage mounts table, and docker tag badges."""
        import subprocess
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        html = index_resp.text

        # 1. CPU Card & Drill-downs
        self.assertIn('id="btn-toggle-cpu-cores"', html)
        self.assertIn('id="cpu-cores-drilldown"', html)
        self.assertIn('id="cpu-cores-grid"', html)
        self.assertIn('id="cpu-cores-badge"', html)
        self.assertIn('id="cpu-load-detail"', html)

        # 2. Memory Card Trend Chart & Breakdown Chips
        self.assertIn('id="memory-chart-container"', html)
        self.assertIn('id="memory-history-chart"', html)
        self.assertIn('id="memory-chart-area"', html)
        self.assertIn('id="memory-chart-line"', html)
        self.assertIn('id="memory-breakdown-chips"', html)
        self.assertIn('id="memory-chip-used"', html)
        self.assertIn('id="memory-chip-cached"', html)
        self.assertIn('id="memory-chip-free"', html)

        # 3. System Uptime Card Load Average Badge
        self.assertIn('id="uptime-load-badge"', html)

        # 4. Storage Volumes & Filesystems Card in RAID view
        raid_start = html.find('id="view-raid"')
        self.assertGreater(raid_start, -1)
        raid_end = html.find('id="view-containers"')
        raid_view_html = html[raid_start:raid_end]
        self.assertIn('id="storage-mounts-card"', raid_view_html)
        self.assertIn('id="storage-mounts-table"', raid_view_html)
        self.assertIn('id="storage-mounts-tbody"', raid_view_html)
        self.assertIn('id="storage-mounts-count-badge"', raid_view_html)
        self.assertIn("Device", raid_view_html)
        self.assertIn("Mount Point", raid_view_html)
        self.assertIn("Filesystem", raid_view_html)
        self.assertIn("Mount Options", raid_view_html)

        # 5. Docker Updates Table Tag / Version column
        self.assertIn("<th>Tag / Version</th>", html)
        self.assertIn('colspan="8"', html)

        # 6. CSS styles verification
        css_text = Path("static/styles.css").read_text(encoding="utf-8")
        self.assertIn(".cpu-cores-grid", css_text)
        self.assertIn(".memory-chart-container", css_text)
        self.assertIn(".storage-mounts-card", css_text)
        self.assertIn(".kind-tag", css_text)

        # 7. Node unit test for generateUpdateRowHtml tag/version output
        node_script = """
        const fs = require('fs');
        const vm = require('vm');
        const code = fs.readFileSync('static/app.js', 'utf8');

        const context = vm.createContext({
          document: {},
          window: {},
          console: console,
          escapeHtml: (s) => String(s || ''),
          lastDockerUpdatesRenderKey: ''
        });

        const start = code.indexOf('function generateUpdateRowHtml');
        let depth = 0;
        let end = start;
        let foundBrace = false;
        for (let i = start; i < code.length; i++) {
          if (code[i] === '{') {
            depth++;
            foundBrace = true;
          } else if (code[i] === '}') {
            depth--;
            if (foundBrace && depth === 0) {
              end = i + 1;
              break;
            }
          }
        }
        vm.runInContext(code.slice(start, end), context);

        const html1 = context.generateUpdateRowHtml({
          container_id: 'c1',
          container_name: 'webapp',
          image: 'webapp:v1.2.0',
          image_tag: 'v1.2.0',
          image_version: '1.2.0',
          update_available: true
        });

        if (!html1.includes('kind-tag')) throw new Error('Missing kind-tag in row HTML');
        if (!html1.includes('v1.2.0')) throw new Error('Missing tag text');
        if (!html1.includes('1.2.0')) throw new Error('Missing version text');

        console.log('SUCCESS');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node test failed: {proc.stderr}")
        self.assertIn("SUCCESS", proc.stdout)

    def test_ci_workflow_packaging_and_systemd_services(self):
        repo_root = Path(__file__).resolve().parent
        pyproject_file = repo_root / "pyproject.toml"
        workflow_file = repo_root / ".github" / "workflows" / "ci-release.yml"

        # Check pyproject build-system packaging metadata
        self.assertTrue(pyproject_file.is_file())
        pyproject_content = pyproject_file.read_text(encoding="utf-8")
        self.assertIn("[build-system]", pyproject_content)
        self.assertIn('requires = ["setuptools>=61.0"]', pyproject_content)
        self.assertIn('build-backend = "setuptools.build_meta"', pyproject_content)
        self.assertIn("[tool.setuptools]", pyproject_content)
        self.assertIn('py-modules = ["main", "api"]', pyproject_content)

        # Check CI/CD release workflow
        self.assertTrue(workflow_file.is_file())
        workflow_content = workflow_file.read_text(encoding="utf-8")
        self.assertIn("uv run pytest -v", workflow_content)
        self.assertIn("astral-sh/setup-uv", workflow_content)
        self.assertIn("uv build", workflow_content)
        self.assertIn("TAG_EXISTS", workflow_content)
        self.assertIn("chore(release): bump version", workflow_content)
        self.assertIn("gh release create", workflow_content)
        self.assertIn("dist/*", workflow_content)
        self.assertIn(".github/scripts/release_helper.py", workflow_content)

        # Verify release_helper.py script functionality
        release_helper_script = repo_root / ".github" / "scripts" / "release_helper.py"
        self.assertTrue(release_helper_script.is_file())
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_helper", str(release_helper_script))
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(bool(mod.get_current_version()))
        self.assertEqual(mod.calculate_next_patch("0.9.1"), "0.9.2")
        self.assertEqual(mod.calculate_next_patch("v1.2.3"), "1.2.4")

        vigil_service = repo_root / "systemd" / "vigil.service"
        self.assertTrue(vigil_service.is_file())
        vigil_content = vigil_service.read_text(encoding="utf-8")
        self.assertIn("WantedBy=multi-user.target", vigil_content)
        self.assertIn("main.py web", vigil_content)

        install_script = repo_root / "install.sh"
        self.assertTrue(install_script.is_file())
        install_content = install_script.read_text(encoding="utf-8")
        self.assertIn("vigil.service", install_content)
        self.assertIn("/usr/local/bin/vigil", install_content)

    def test_websocket_auth_snapshot_and_refresh_actions(self):
        # 1. Unauthenticated connection attempt is closed with code 1008 (policy violation)
        unauth_ws = asgi_ws(api.app, "/api/ws")
        self.assertEqual(unauth_ws.close_code, 1008)
        self.assertEqual(unauth_ws.messages, [])

        # 2. Authenticated connection via session_token cookie receives initial snapshot
        #    and responds to {'action': 'refresh'} and {'action': 'refresh_module', 'module': 'system'}
        main.record_notification("clean", 1700001000.0, weekly=True)
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
        ):
            ws_cookie = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {"action": "refresh"},
                    {"action": "refresh_module", "module": "system"},
                ],
            )
            self.assertTrue(ws_cookie.accepted)
            self.assertEqual(len(ws_cookie.messages), 3)

            # First message: initial snapshot
            initial_snapshot = ws_cookie.messages[0]
            self.assertEqual(initial_snapshot["type"], "snapshot")
            self.assertEqual(initial_snapshot["raid"]["status"], "clean")
            self.assertIn("system", initial_snapshot)
            self.assertIsNotNone(initial_snapshot["notification_state"])
            self.assertEqual(len(initial_snapshot["history"]), 1)
            self.assertIn("version", initial_snapshot)

            # Second message: response to {'action': 'refresh'}
            refreshed_snapshot = ws_cookie.messages[1]
            self.assertEqual(refreshed_snapshot["type"], "snapshot")
            self.assertEqual(refreshed_snapshot["raid"]["array_size"], "9.31 GiB")

            # Third message: response to {'action': 'refresh_module', 'module': 'system'}
            module_msg = ws_cookie.messages[2]
            self.assertEqual(module_msg["type"], "module_update")
            self.assertEqual(module_msg["module"], "system")
            self.assertIn("cpu_percent", module_msg["system"])

            # 3. Authenticated connection via ?token= query parameter also receives snapshot
            ws_query = asgi_ws(
                api.app,
                f"/api/ws?token={self.session_token}",
            )
            self.assertTrue(ws_query.accepted)
            self.assertEqual(len(ws_query.messages), 1)
            self.assertEqual(ws_query.messages[0]["type"], "snapshot")

    def test_websocket_multi_rate_intervals_and_adaptive_raid(self):
        # 1. Test WebSocket handling 'set_intervals' and persisting to SQLite
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
        ):
            ws_set = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {
                        "action": "set_intervals",
                        "intervals": {"system": 2, "raid": 30, "notifications": 120},
                    }
                ],
            )
            self.assertTrue(ws_set.accepted)
            self.assertEqual(len(ws_set.messages), 2)  # 1 snapshot, 1 intervals_updated

            intervals_msg = ws_set.messages[1]
            self.assertEqual(intervals_msg["type"], "intervals_updated")
            self.assertEqual(
                intervals_msg["refresh_intervals"],
                {"system": 2, "raid": 30, "notifications": 120, "docker": 5},
            )

            # Verify persisted in SQLite
            persisted = main.get_refresh_intervals()
            self.assertEqual(
                persisted,
                {"system": 2, "raid": 30, "notifications": 120, "docker": 5},
            )

        # 2. Test WebSocket refresh_module 'raid' when status is 'degraded' (adaptive_fast=True, effective_interval=5)
        with (
            patch.object(
                main, "check_raid_status", return_value=("degraded", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
        ):
            ws_raid = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {"action": "refresh_module", "module": "raid"},
                ],
            )
            self.assertTrue(ws_raid.accepted)
            self.assertEqual(len(ws_raid.messages), 2)  # snapshot, module_update

            raid_module_msg = ws_raid.messages[1]
            self.assertEqual(raid_module_msg["type"], "module_update")
            self.assertEqual(raid_module_msg["module"], "raid")
            self.assertEqual(raid_module_msg["raid"]["status"], "degraded")
            self.assertTrue(raid_module_msg["adaptive_fast"])
            self.assertEqual(raid_module_msg["effective_interval"], 5)

        # 3. Test WebSocket refresh_module 'notifications'
        main.record_notification("degraded", 1700002000.0, weekly=False)
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
        ):
            ws_notif = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {"action": "refresh_module", "module": "notifications"},
                ],
            )
            self.assertTrue(ws_notif.accepted)
            self.assertEqual(len(ws_notif.messages), 2)  # snapshot, module_update

            notif_module_msg = ws_notif.messages[1]
            self.assertEqual(notif_module_msg["type"], "module_update")
            self.assertEqual(notif_module_msg["module"], "notifications")
            self.assertIn("notification_state", notif_module_msg)
            self.assertIn("history", notif_module_msg)
            self.assertGreaterEqual(len(notif_module_msg["history"]), 1)

        # 4. Test WebSocket refresh_module 'docker'
        with (
            patch.object(
                main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)
            ),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
            patch.object(
                main.docker_mgr,
                "get_status",
                return_value={"installed": True, "running": True, "version": "29.0.0"},
            ),
        ):
            ws_docker = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {"action": "refresh_module", "module": "docker"},
                ],
            )
            self.assertTrue(ws_docker.accepted)
            self.assertEqual(len(ws_docker.messages), 2)  # snapshot, module_update
            snapshot_msg = ws_docker.messages[0]
            self.assertIn("docker", snapshot_msg)
            self.assertEqual(snapshot_msg["docker"]["version"], "29.0.0")

            docker_mod_msg = ws_docker.messages[1]
            self.assertEqual(docker_mod_msg["type"], "module_update")
            self.assertEqual(docker_mod_msg["module"], "docker")
            self.assertEqual(docker_mod_msg["docker"]["version"], "29.0.0")



class TestDockerBackend(unittest.TestCase):
    def setUp(self):
        main.docker_manager.invalidate_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_docker.sqlite3"
        self.stacks_path = Path(self.temp_dir.name) / "stacks"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.stacks_patcher = patch.object(main, "STACKS_DIR", self.stacks_path)
        self.dm_stacks_patcher = patch.object(
            main.docker_manager, "stacks_dir", self.stacks_path
        )
        self.db_patcher.start()
        self.stacks_patcher.start()
        self.dm_stacks_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        main.docker_manager.invalidate_cache()
        self.dm_stacks_patcher.stop()
        self.stacks_patcher.stop()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_docker_endpoints_require_authentication(self):
        endpoints = [
            ("GET", "/api/docker/status"),
            ("POST", "/api/docker/install"),
            ("GET", "/api/docker/stacks"),
            ("POST", "/api/docker/stacks"),
            ("GET", "/api/docker/stacks/mystack"),
            ("POST", "/api/docker/stacks/mystack/action"),
            ("DELETE", "/api/docker/stacks/mystack"),
            ("GET", "/api/docker/containers"),
            ("POST", "/api/docker/containers/c123/action"),
            ("GET", "/api/docker/containers/c123/logs"),
            ("GET", "/api/docker/images"),
            ("POST", "/api/docker/images/pull"),
            ("DELETE", "/api/docker/images/img123"),
            ("POST", "/api/docker/images/prune"),
            ("GET", "/api/docker/volumes"),
            ("DELETE", "/api/docker/volumes/vol123"),
            ("GET", "/api/docker/networks"),
            ("DELETE", "/api/docker/networks/net123"),
            ("GET", "/api/docker/updates"),
            ("POST", "/api/docker/updates/apply"),
            ("GET", "/api/docker/updates/config"),
            ("POST", "/api/docker/updates/config"),
            ("GET", "/api/docker/updates/schedule"),
            ("POST", "/api/docker/updates/schedule"),
            ("GET", "/api/docker/volumes/testvol"),
            ("GET", "/api/docker/templates"),
            ("POST", "/api/docker/stacks/validate"),
            ("POST", "/api/docker/stacks/format"),
        ]
        for method, path in endpoints:
            resp = asgi_request(api.app, method, path)
            self.assertEqual(
                resp.status_code, 401, f"Expected 401 for unauthenticated {method} {path}"
            )

    def test_docker_status_endpoint(self):
        with (
            patch.object(main.docker_manager, "is_installed", return_value=True),
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "get_version", return_value="29.0.0"),
            patch.object(main.docker_manager, "get_compose_version", return_value="v2.20.0"),
            patch.object(
                main.docker_manager,
                "get_stats",
                return_value={
                    "containers": 5,
                    "running": 3,
                    "stopped": 2,
                    "images": 10,
                    "volumes": 4,
                    "networks": 3,
                    "stacks": 2,
                },
            ),
        ):
            resp = asgi_get(api.app, "/api/docker/status", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["installed"])
            self.assertTrue(data["running"])
            self.assertEqual(data["version"], "29.0.0")
            self.assertEqual(data["compose_version"], "v2.20.0")
            self.assertEqual(data["stats"]["containers"], 5)
            self.assertEqual(data["stats"]["running"], 3)
            self.assertEqual(data["stats"]["stacks"], 2)

    def test_docker_install_trigger(self):
        with patch.object(
            main.docker_manager,
            "install_docker",
            return_value={"status": "installing", "message": "Docker installation triggered"},
        ):
            resp = asgi_post(api.app, "/api/docker/install", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "installing")

    def test_docker_stacks_crud_and_actions(self):
        sample_yaml = "services:\n  web:\n    image: nginx:alpine\n"
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "_run_compose", return_value=""),
        ):
            # 1. Create stack
            create_resp = asgi_post(
                api.app,
                "/api/docker/stacks",
                cookies=self.auth_cookies,
                json_body={"name": "test_stack", "compose_yaml": sample_yaml, "deploy": True},
            )
            self.assertEqual(create_resp.status_code, 200)
            self.assertTrue(create_resp.json()["success"])
            self.assertEqual(create_resp.json()["stack"]["name"], "test_stack")

            # Verify saved file
            saved_file = self.stacks_path / "test_stack" / "docker-compose.yml"
            self.assertTrue(saved_file.is_file())
            self.assertEqual(saved_file.read_text(), sample_yaml)

            # 2. List stacks
            list_resp = asgi_get(api.app, "/api/docker/stacks", cookies=self.auth_cookies)
            self.assertEqual(list_resp.status_code, 200)
            stacks = list_resp.json()["stacks"]
            self.assertEqual(len(stacks), 1)
            self.assertEqual(stacks[0]["name"], "test_stack")

            # 3. Get stack
            get_resp = asgi_get(
                api.app, "/api/docker/stacks/test_stack", cookies=self.auth_cookies
            )
            self.assertEqual(get_resp.status_code, 200)
            stack_data = get_resp.json()["stack"]
            self.assertEqual(stack_data["name"], "test_stack")
            self.assertIn("services", stack_data["compose_yaml"])

            # 4. Stack action
            action_resp = asgi_post(
                api.app,
                "/api/docker/stacks/test_stack/action",
                cookies=self.auth_cookies,
                json_body={"action": "restart"},
            )
            self.assertEqual(action_resp.status_code, 200)
            self.assertEqual(action_resp.json()["status"], "running")

            # 5. Delete stack
            del_resp = asgi_request(
                api.app,
                "DELETE",
                "/api/docker/stacks/test_stack?down=true",
                cookies=self.auth_cookies,
            )
            self.assertEqual(del_resp.status_code, 200)
            self.assertTrue(del_resp.json()["success"])

            # Verify deleted from list
            after_del_resp = asgi_get(
                api.app, "/api/docker/stacks", cookies=self.auth_cookies
            )
            self.assertEqual(len(after_del_resp.json()["stacks"]), 0)

    def test_docker_containers_list_action_logs(self):
        mock_containers = [
            {
                "id": "c123456789ab",
                "name": "web_app",
                "image": "nginx:alpine",
                "state": "running",
                "status": "Up 2 hours",
                "ports": "0.0.0.0:80->80/tcp",
                "created": "2 hours ago",
                "stack": "my_stack",
            }
        ]
        with (
            patch.object(main.docker_manager, "list_containers", return_value=mock_containers),
            patch.object(
                main.docker_manager,
                "execute_container_action",
                return_value={"success": True, "action": "stop", "id": "c123456789ab"},
            ),
            patch.object(
                main.docker_manager,
                "get_container_logs",
                return_value="[INFO] Server started\n[INFO] Ready for connections\n",
            ),
        ):
            # List containers
            c_resp = asgi_get(api.app, "/api/docker/containers", cookies=self.auth_cookies)
            self.assertEqual(c_resp.status_code, 200)
            self.assertEqual(len(c_resp.json()["containers"]), 1)
            self.assertEqual(c_resp.json()["containers"][0]["name"], "web_app")

            # Container action
            act_resp = asgi_post(
                api.app,
                "/api/docker/containers/c123456789ab/action",
                cookies=self.auth_cookies,
                json_body={"action": "stop"},
            )
            self.assertEqual(act_resp.status_code, 200)
            self.assertTrue(act_resp.json()["success"])

            # Container logs
            log_resp = asgi_get(
                api.app,
                "/api/docker/containers/c123456789ab/logs?tail=50",
                cookies=self.auth_cookies,
            )
            self.assertEqual(log_resp.status_code, 200)
            self.assertIn("Server started", log_resp.json()["logs"])

    def test_docker_images_volumes_networks(self):
        with (
            patch.object(
                main.docker_manager,
                "list_images",
                return_value=[
                    {"id": "img1", "repository": "nginx", "tag": "alpine", "size": "20MB", "created": "1d"}
                ],
            ),
            patch.object(
                main.docker_manager,
                "pull_image",
                return_value={"success": True, "image": "redis:alpine", "output": "Downloaded"},
            ),
            patch.object(
                main.docker_manager,
                "remove_image",
                return_value={"success": True, "id": "img1", "output": "Deleted"},
            ),
            patch.object(
                main.docker_manager,
                "prune_images",
                return_value={"success": True, "output": "Total reclaimed: 10MB"},
            ),
            patch.object(
                main.docker_manager,
                "list_volumes",
                return_value=[
                    {"name": "vol1", "driver": "local", "scope": "local", "mountpoint": "/var/vol1", "created_at": "1d", "size": "100MB"}
                ],
            ),
            patch.object(
                main.docker_manager,
                "remove_volume",
                return_value={"success": True, "name": "vol1", "output": "Deleted"},
            ),
            patch.object(
                main.docker_manager,
                "list_networks",
                return_value=[
                    {"id": "net1", "name": "bridge", "driver": "bridge", "scope": "local", "internal": "false", "created_at": "1d"}
                ],
            ),
            patch.object(
                main.docker_manager,
                "remove_network",
                return_value={"success": True, "id": "net1", "output": "Deleted"},
            ),
        ):
            # Images
            imgs_resp = asgi_get(api.app, "/api/docker/images", cookies=self.auth_cookies)
            self.assertEqual(imgs_resp.status_code, 200)
            self.assertEqual(len(imgs_resp.json()["images"]), 1)

            pull_resp = asgi_post(
                api.app,
                "/api/docker/images/pull",
                cookies=self.auth_cookies,
                json_body={"image": "redis:alpine"},
            )
            self.assertEqual(pull_resp.status_code, 200)
            self.assertTrue(pull_resp.json()["success"])

            del_img_resp = asgi_request(
                api.app, "DELETE", "/api/docker/images/img1", cookies=self.auth_cookies
            )
            self.assertEqual(del_img_resp.status_code, 200)

            prune_resp = asgi_post(
                api.app, "/api/docker/images/prune", cookies=self.auth_cookies
            )
            self.assertEqual(prune_resp.status_code, 200)

            # Volumes
            vols_resp = asgi_get(api.app, "/api/docker/volumes", cookies=self.auth_cookies)
            self.assertEqual(vols_resp.status_code, 200)
            self.assertEqual(len(vols_resp.json()["volumes"]), 1)

            del_vol_resp = asgi_request(
                api.app, "DELETE", "/api/docker/volumes/vol1", cookies=self.auth_cookies
            )
            self.assertEqual(del_vol_resp.status_code, 200)

            # Networks
            nets_resp = asgi_get(api.app, "/api/docker/networks", cookies=self.auth_cookies)
            self.assertEqual(nets_resp.status_code, 200)
            self.assertEqual(len(nets_resp.json()["networks"]), 1)

            del_net_resp = asgi_request(
                api.app, "DELETE", "/api/docker/networks/net1", cookies=self.auth_cookies
            )
            self.assertEqual(del_net_resp.status_code, 200)

    def test_docker_updates_flow(self):
        with (
            patch.object(
                main.docker_manager,
                "check_updates",
                return_value={
                    "updates": [
                        {
                            "container_id": "c1",
                            "container_name": "app",
                            "image": "nginx:alpine",
                            "stack": "",
                            "update_available": True,
                            "current_id": "aaa",
                            "latest_id": "bbb",
                        }
                    ],
                    "config": {"frequency": "daily"},
                },
            ),
            patch.object(
                main.docker_manager,
                "apply_updates",
                return_value={"success": True, "updated": [{"type": "container", "id": "c1", "name": "app", "success": True}]},
            ),
        ):
            # Check updates
            chk_resp = asgi_get(api.app, "/api/docker/updates", cookies=self.auth_cookies)
            self.assertEqual(chk_resp.status_code, 200)
            self.assertEqual(len(chk_resp.json()["updates"]), 1)
            self.assertTrue(chk_resp.json()["updates"][0]["update_available"])

            # Apply updates
            apply_resp = asgi_post(
                api.app,
                "/api/docker/updates/apply",
                cookies=self.auth_cookies,
                json_body={"containers": ["c1"]},
            )
            self.assertEqual(apply_resp.status_code, 200)
            self.assertTrue(apply_resp.json()["success"])

            # Get auto updates config before setting
            get_cfg_init = asgi_get(
                api.app,
                "/api/docker/updates/config",
                cookies=self.auth_cookies,
            )
            self.assertEqual(get_cfg_init.status_code, 200)

            # Configure auto updates
            cfg_resp = asgi_post(
                api.app,
                "/api/docker/updates/config",
                cookies=self.auth_cookies,
                json_body={"frequency": "hourly"},
            )
            self.assertEqual(cfg_resp.status_code, 200)
            self.assertEqual(cfg_resp.json()["frequency"], "hourly")
            self.assertEqual(
                main.get_app_settings().get("docker_auto_update_frequency"), "hourly"
            )

            # Verify GET returns the newly updated frequency
            get_cfg_after = asgi_get(
                api.app,
                "/api/docker/updates/config",
                cookies=self.auth_cookies,
            )
            self.assertEqual(get_cfg_after.status_code, 200)
            self.assertEqual(get_cfg_after.json()["frequency"], "hourly")

    def test_docker_auto_update_config_persistence_and_api(self):
        # 1. Ensure initial config returns a valid dict with frequency
        resp = asgi_get(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("frequency", resp.json())

        # 2. Update to "daily" and verify persistence in SQLite and GET endpoint
        post_daily = asgi_post(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
            json_body={"frequency": "daily"},
        )
        self.assertEqual(post_daily.status_code, 200)
        self.assertEqual(post_daily.json(), {"frequency": "daily"})
        self.assertEqual(
            main.get_app_settings().get("docker_auto_update_frequency"), "daily"
        )

        get_daily = asgi_get(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
        )
        self.assertEqual(get_daily.status_code, 200)
        self.assertEqual(get_daily.json(), {"frequency": "daily"})

        # 3. Update to "disabled" and verify persistence
        post_dis = asgi_post(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
            json_body={"frequency": "disabled"},
        )
        self.assertEqual(post_dis.status_code, 200)
        self.assertEqual(post_dis.json(), {"frequency": "disabled"})
        self.assertEqual(
            main.get_app_settings().get("docker_auto_update_frequency"), "disabled"
        )

        get_dis = asgi_get(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
        )
        self.assertEqual(get_dis.status_code, 200)
        self.assertEqual(get_dis.json(), {"frequency": "disabled"})

    def test_docker_check_updates_caching_and_concurrency(self):
        dm = main.DockerManager()
        dm.invalidate_cache()

        sample_containers = [
            {"id": "c1", "name": "web", "image": "nginx:alpine", "stack": "frontend"},
            {"id": "c2", "name": "db", "image": "postgres:15", "stack": "backend"},
            {"id": "c3", "name": "cache", "image": "redis:7", "stack": "backend"},
        ]

        run_docker_calls = []

        def fake_run_docker(cmd, *args, **kwargs):
            run_docker_calls.append(cmd)
            if cmd[:2] == ["inspect", "--format"]:
                return "sha256:1234567890abcdef"
            if cmd[:2] == ["manifest", "inspect"]:
                return "{}"
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=sample_containers) as mock_list,
            patch.object(dm, "_run_docker", side_effect=fake_run_docker),
        ):
            # 1. First call: cache miss, runs checks concurrently
            res1 = dm.check_updates(force=False)
            self.assertEqual(len(res1["updates"]), 3)
            self.assertEqual(mock_list.call_count, 1)
            initial_docker_calls = len(run_docker_calls)
            self.assertGreaterEqual(initial_docker_calls, 3)

            # 2. Second call without force: cache hit, 0ms, list_containers not called again
            res2 = dm.check_updates(force=False)
            self.assertEqual(res1["updates"], res2["updates"])
            self.assertEqual(mock_list.call_count, 1)
            self.assertEqual(len(run_docker_calls), initial_docker_calls)

            # 3. Third call with force=True: bypasses cache
            res3 = dm.check_updates(force=True)
            self.assertEqual(len(res3["updates"]), 3)
            self.assertEqual(mock_list.call_count, 2)
            self.assertGreater(len(run_docker_calls), initial_docker_calls)

            # 4. Invalidate cache: forces next call to re-run
            dm.invalidate_cache()
            res4 = dm.check_updates(force=False)
            self.assertEqual(mock_list.call_count, 3)

        # 5. Verify API endpoint accepts ?force=true and ?force=false
        with patch.object(main.docker_manager, "check_updates", return_value={"updates": [], "config": {}}) as mock_chk:
            resp_no_force = asgi_get(api.app, "/api/docker/updates", cookies=self.auth_cookies)
            self.assertEqual(resp_no_force.status_code, 200)
            mock_chk.assert_called_with(force=False)

            resp_force = asgi_get(api.app, "/api/docker/updates?force=true", cookies=self.auth_cookies)
            self.assertEqual(resp_force.status_code, 200)
            mock_chk.assert_called_with(force=True)

    def test_docker_check_updates_remote_digest_detection(self):
        dm = main.DockerManager()
        dm.invalidate_cache()

        sample_container = [
            {"id": "c1", "name": "web", "image": "nginx:latest", "stack": "frontend"}
        ]

        local_inspect = json.dumps([
            {
                "Id": "sha256:111111111111222222222222",
                "RepoDigests": ["nginx@sha256:aaaa1111aaaa1111aaaa1111"],
            }
        ])

        # Case 1: Remote digest does NOT match local RepoDigests -> update_available is True
        remote_multi_arch_new = json.dumps({
            "manifests": [
                {
                    "digest": "sha256:bbbb2222bbbb2222bbbb2222",
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ]
        })

        def fake_docker_update_avail(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return remote_multi_arch_new
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=sample_container),
            patch.object(dm, "_run_docker", side_effect=fake_docker_update_avail),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertTrue(upd["update_available"])
            self.assertEqual(upd["current_id"], "111111111111")
            self.assertEqual(upd["latest_id"], "bbbb2222bbbb")

        # Case 2: Remote digest matches local RepoDigests -> update_available is False
        remote_matching = json.dumps({
            "manifests": [
                {
                    "digest": "sha256:aaaa1111aaaa1111aaaa1111",
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ]
        })

        def fake_docker_up_to_date(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return remote_matching
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=sample_container),
            patch.object(dm, "_run_docker", side_effect=fake_docker_up_to_date),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertFalse(upd["update_available"])
            self.assertEqual(upd["current_id"], "111111111111")
            self.assertEqual(upd["latest_id"], "111111111111")

        # Case 3: Remote inspect fails -> graceful handling, update_available is False
        def fake_docker_remote_fails(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_inspect
            raise RuntimeError("Network error reaching registry")

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=sample_container),
            patch.object(dm, "_run_docker", side_effect=fake_docker_remote_fails),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertFalse(upd["update_available"])
            self.assertEqual(upd["current_id"], "111111111111")
            self.assertEqual(upd["latest_id"], "111111111111")

    def test_docker_check_updates_multiarch_digest_matching(self):
        dm = main.DockerManager()
        dm.invalidate_cache()

        # Scenario 1: Multi-arch image up-to-date with parent index in RepoDigests (Portainer bug fix)
        # Parent raw manifest list hashes to 8166e2376756d1ad3194e336cd1707c28a5764af7acc8bb9f21a5f28d1903566.
        # Remote buildx returns multi-arch manifest list with child manifests including sha256:a1d45613d38b...
        # Local inspect has parent index digest in RepoDigests and Id.
        target_parent_hash = "8166e2376756d1ad3194e336cd1707c28a5764af7acc8bb9f21a5f28d1903566"
        raw_manifest_list = json.dumps({
            "manifests": [
                {
                    "digest": "sha256:a1d45613d38b14521b6a0d06d735aec5276c2b4f896a7b62ba34b2288a477b38",
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "digest": "sha256:9636b9f1d55e95ad4c149071e2726340d0646c55f8ed38fa77958f9e10176052",
                    "platform": {"architecture": "arm64", "os": "linux"},
                },
            ]
        })

        class MockHash8166:
            def hexdigest(self):
                return target_parent_hash

        orig_sha256 = hashlib.sha256

        def mock_sha256_portainer(data=b""):
            if data == raw_manifest_list.encode("utf-8"):
                return MockHash8166()
            return orig_sha256(data)

        local_portainer_inspect = json.dumps([
            {
                "Id": f"sha256:{target_parent_hash}",
                "RepoDigests": [f"portainer/portainer-ee@sha256:{target_parent_hash}"],
            }
        ])

        container_portainer = [
            {"id": "p1", "name": "portainer", "image": "portainer/portainer-ee:latest", "stack": ""}
        ]

        def fake_docker_portainer_match(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_portainer_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return raw_manifest_list
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=container_portainer),
            patch.object(dm, "_run_docker", side_effect=fake_docker_portainer_match),
            patch("hashlib.sha256", side_effect=mock_sha256_portainer),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertFalse(upd["update_available"])
            self.assertEqual(upd["current_id"], "8166e2376756")
            self.assertEqual(upd["latest_id"], "8166e2376756")

        # Scenario 2: Multi-arch image with real update available
        # Remote buildx returns a new index with sha256 starting with deadbeef9999
        new_manifest_digest = "sha256:deadbeef99991111222233334444555566667777888899990000aaaabbbbcccc"
        new_raw_manifest_list = json.dumps({
            "manifests": [
                {
                    "digest": new_manifest_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
            "nonce": "force_new_hash_9999",
        })

        def fake_docker_portainer_update(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_portainer_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return new_raw_manifest_list
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=container_portainer),
            patch.object(dm, "_run_docker", side_effect=fake_docker_portainer_update),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertTrue(upd["update_available"])
            self.assertEqual(upd["current_id"], "8166e2376756")
            self.assertEqual(upd["latest_id"], "deadbeef9999")
            self.assertTrue(upd["can_update"])

        # Scenario 3: Local RepoDigests matching child manifest digest directly
        local_child_inspect = json.dumps([
            {
                "Id": "sha256:childlayerlocal123456",
                "RepoDigests": ["portainer/portainer-ee@sha256:a1d45613d38b14521b6a0d06d735aec5276c2b4f896a7b62ba34b2288a477b38"],
            }
        ])

        def fake_docker_child_match(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                return local_child_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return raw_manifest_list
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=container_portainer),
            patch.object(dm, "_run_docker", side_effect=fake_docker_child_match),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertFalse(upd["update_available"])
            self.assertEqual(upd["current_id"], "childlayerlo")
            self.assertEqual(upd["latest_id"], "childlayerlo")

        # Scenario 4: Container running with digest-pinned image string (e.g. my-app:v1.0@sha256:somehash...)
        pinned_container = [
            {
                "id": "p2",
                "name": "pinned_app",
                "image": "my-app:v1.0@sha256:somehash1234567890abcdef1234567890abcdef1234567890abcdef12345678",
                "stack": "",
            }
        ]
        local_pinned_inspect = json.dumps([
            {
                "Id": "sha256:localapplayer123456",
                "RepoDigests": ["my-app@sha256:remote_match_digest_1111111111111111111111111111111111111111"],
            }
        ])
        resolved_docker_calls = []

        def fake_docker_pinned(cmd, *args, **kwargs):
            resolved_docker_calls.append(list(cmd))
            if cmd[:2] == ["image", "inspect"]:
                return local_pinned_inspect
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return json.dumps({
                    "manifests": [
                        {
                            "digest": "sha256:remote_match_digest_1111111111111111111111111111111111111111",
                            "platform": {"architecture": "amd64", "os": "linux"},
                        }
                    ]
                })
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=pinned_container),
            patch.object(dm, "_run_docker", side_effect=fake_docker_pinned),
            patch("platform.machine", return_value="x86_64"),
        ):
            dm.invalidate_cache()
            res = dm.check_updates(force=True)
            self.assertEqual(len(res["updates"]), 1)
            upd = res["updates"][0]
            self.assertFalse(upd["update_available"])
            self.assertEqual(upd["image_repo"], "my-app")
            self.assertEqual(upd["image_tag"], "v1.0")
            # Verify buildx imagetools inspect resolved against "my-app:v1.0" with digest stripped
            inspect_calls = [c for c in resolved_docker_calls if c[:3] == ["buildx", "imagetools", "inspect"]]
            self.assertTrue(len(inspect_calls) > 0)
            self.assertEqual(inspect_calls[0][-1], "my-app:v1.0")

    def test_docker_check_container_image_tag_repo_and_version(self):
        dm = main.DockerManager()
        dm.invalidate_cache()

        sample_containers = [
            {"id": "c1", "name": "grafana", "image": "grafana/grafana:10.1.0", "stack": "monitoring"},
            {"id": "c2", "name": "web", "image": "library/nginx", "stack": ""},
            {"id": "c3", "name": "app", "image": "localhost:5000/app:v2.0.1", "stack": "custom"},
            {"id": "c4", "name": "oci_labeled", "image": "myregistry.local:8080/service:latest", "stack": ""},
            {"id": "c5", "name": "ver_labeled", "image": "db:latest", "stack": "database"},
        ]

        def fake_run_docker(cmd, *args, **kwargs):
            if cmd[:2] == ["image", "inspect"]:
                target_img = cmd[2] if len(cmd) > 2 else ""
                if "myregistry.local:8080/service:latest" in target_img:
                    return json.dumps([{"Id": "sha256:444444444444", "Config": {"Labels": {"org.opencontainers.image.version": "3.8.4"}}}])
                if "db:latest" in target_img:
                    return json.dumps([{"Id": "sha256:555555555555", "Config": {"Labels": {"version": "15.2"}}}])
                return json.dumps([{"Id": "sha256:000000000000"}])
            return ""

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(dm, "list_containers", return_value=sample_containers),
            patch.object(dm, "_run_docker", side_effect=fake_run_docker),
            patch.object(dm, "_get_remote_image_digest", return_value=None),
        ):
            res = dm.check_updates(force=True)
            updates_by_id = {u["container_id"]: u for u in res["updates"]}
            self.assertEqual(len(updates_by_id), 5)

            # c1: grafana/grafana:10.1.0
            c1 = updates_by_id["c1"]
            self.assertEqual(c1["image_repo"], "grafana/grafana")
            self.assertEqual(c1["image_tag"], "10.1.0")
            self.assertEqual(c1["image_version"], "10.1.0")

            # c2: library/nginx (no tag -> latest)
            c2 = updates_by_id["c2"]
            self.assertEqual(c2["image_repo"], "library/nginx")
            self.assertEqual(c2["image_tag"], "latest")
            self.assertEqual(c2["image_version"], "latest")

            # c3: localhost:5000/app:v2.0.1
            c3 = updates_by_id["c3"]
            self.assertEqual(c3["image_repo"], "localhost:5000/app")
            self.assertEqual(c3["image_tag"], "v2.0.1")
            self.assertEqual(c3["image_version"], "v2.0.1")

            # c4: myregistry.local:8080/service:latest with org.opencontainers.image.version
            c4 = updates_by_id["c4"]
            self.assertEqual(c4["image_repo"], "myregistry.local:8080/service")
            self.assertEqual(c4["image_tag"], "latest")
            self.assertEqual(c4["image_version"], "3.8.4")

            # c5: db:latest with version label
            c5 = updates_by_id["c5"]
            self.assertEqual(c5["image_repo"], "db")
            self.assertEqual(c5["image_tag"], "latest")
            self.assertEqual(c5["image_version"], "15.2")

    def test_docker_get_remote_image_digest_fallback_chain(self):
        dm = main.DockerManager()

        # 1. Empty image input
        self.assertIsNone(dm._get_remote_image_digest(""))
        self.assertIsNone(dm._get_remote_image_digest("   "))

        # 2. Buildx single manifest format with config.digest
        single_manifest = json.dumps({"config": {"digest": "sha256:singleconfig1234"}})
        with patch.object(dm, "_run_docker", return_value=single_manifest):
            self.assertEqual(dm._get_remote_image_digest("app:v1"), "sha256:singleconfig1234")

        # 3. Fallback to manifest inspect -v with list of platform descriptors
        def fake_manifest_v(cmd, *args, **kwargs):
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                raise RuntimeError("buildx not supported")
            if cmd[:3] == ["manifest", "inspect", "-v"]:
                return json.dumps([
                    {
                        "Descriptor": {
                            "digest": "sha256:armdigest5678",
                            "platform": {"architecture": "arm64", "os": "linux"},
                        }
                    }
                ])
            return ""

        with (
            patch.object(dm, "_run_docker", side_effect=fake_manifest_v),
            patch("platform.machine", return_value="aarch64"),
        ):
            self.assertEqual(dm._get_remote_image_digest("app:v1"), "sha256:armdigest5678")

        # 4. Fallback to manifest inspect -v with dict Descriptor
        def fake_manifest_v_dict(cmd, *args, **kwargs):
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                raise RuntimeError("buildx not supported")
            if cmd[:3] == ["manifest", "inspect", "-v"]:
                return json.dumps({
                    "Descriptor": {
                        "digest": "sha256:dictdesc9999",
                    }
                })
            return ""

        with patch.object(dm, "_run_docker", side_effect=fake_manifest_v_dict):
            self.assertEqual(dm._get_remote_image_digest("app:v1"), "sha256:dictdesc9999")

        # 5. Fallback to manifest inspect standard
        def fake_manifest_std(cmd, *args, **kwargs):
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                raise RuntimeError("buildx not supported")
            if cmd[:3] == ["manifest", "inspect", "-v"]:
                raise RuntimeError("-v not supported")
            if cmd[:2] == ["manifest", "inspect"]:
                return json.dumps({
                    "manifests": [
                        {
                            "digest": "sha256:stddigest0000",
                            "platform": {"architecture": "amd64", "os": "linux"},
                        }
                    ]
                })
            return ""

        with (
            patch.object(dm, "_run_docker", side_effect=fake_manifest_std),
            patch("platform.machine", return_value="x86_64"),
        ):
            self.assertEqual(dm._get_remote_image_digest("app:v1"), "sha256:stddigest0000")

        # 6. All fail returns None
        with patch.object(dm, "_run_docker", side_effect=RuntimeError("all fail")):
            self.assertIsNone(dm._get_remote_image_digest("app:v1"))

    def test_docker_error_handling_and_validation(self):
        # 1. Invalid stack action
        bad_stack_act = asgi_post(
            api.app,
            "/api/docker/stacks/mystack/action",
            cookies=self.auth_cookies,
            json_body={"action": "invalid_action"},
        )
        self.assertEqual(bad_stack_act.status_code, 400)

        # 2. Invalid container action
        bad_c_act = asgi_post(
            api.app,
            "/api/docker/containers/c123/action",
            cookies=self.auth_cookies,
            json_body={"action": "explode"},
        )
        self.assertEqual(bad_c_act.status_code, 400)

        # 3. Invalid auto update config frequency
        bad_freq = asgi_post(
            api.app,
            "/api/docker/updates/config",
            cookies=self.auth_cookies,
            json_body={"frequency": "every_minute"},
        )
        self.assertEqual(bad_freq.status_code, 400)

        # 4. Empty image pull
        bad_pull = asgi_post(
            api.app,
            "/api/docker/images/pull",
            cookies=self.auth_cookies,
            json_body={"image": ""},
        )
        self.assertEqual(bad_pull.status_code, 400)

        # 5. Nonexistent stack returns 404
        missing_stack = asgi_get(
            api.app,
            "/api/docker/stacks/nonexistent_stack",
            cookies=self.auth_cookies,
        )
        self.assertEqual(missing_stack.status_code, 404)

        # 6. Docker daemon stopped returns 500 on actions and empty list on container queries
        with patch.object(main.docker_manager, "is_running", return_value=False):
            c_resp = asgi_get(api.app, "/api/docker/containers", cookies=self.auth_cookies)
            self.assertEqual(c_resp.status_code, 200)
            self.assertEqual(c_resp.json()["containers"], [])

            stopped_act = asgi_post(
                api.app,
                "/api/docker/containers/c1/action",
                cookies=self.auth_cookies,
                json_body={"action": "start"},
            )
            self.assertEqual(stopped_act.status_code, 500)

            # Docker daemon stopped on image pull
            stopped_pull = asgi_post(
                api.app,
                "/api/docker/images/pull",
                cookies=self.auth_cookies,
                json_body={"image": "redis:latest"},
            )
            self.assertEqual(stopped_pull.status_code, 500)

            # Docker daemon stopped on image remove
            stopped_del_img = asgi_request(
                api.app,
                "DELETE",
                "/api/docker/images/img123",
                cookies=self.auth_cookies,
            )
            self.assertEqual(stopped_del_img.status_code, 500)

            # Docker daemon stopped on volume remove
            stopped_del_vol = asgi_request(
                api.app,
                "DELETE",
                "/api/docker/volumes/vol123",
                cookies=self.auth_cookies,
            )
            self.assertEqual(stopped_del_vol.status_code, 500)

            # Docker daemon stopped on network remove
            stopped_del_net = asgi_request(
                api.app,
                "DELETE",
                "/api/docker/networks/net123",
                cookies=self.auth_cookies,
            )
            self.assertEqual(stopped_del_net.status_code, 500)

            # Docker daemon stopped on stack create with deploy=True
            stopped_deploy_stack = asgi_post(
                api.app,
                "/api/docker/stacks",
                cookies=self.auth_cookies,
                json_body={"name": "stopstack", "compose_yaml": "services:\n  a:\n    image: b\n", "deploy": True},
            )
            self.assertEqual(stopped_deploy_stack.status_code, 500)

            # Docker daemon stopped on stack action
            stopped_stack_act = asgi_post(
                api.app,
                "/api/docker/stacks/somestack/action",
                cookies=self.auth_cookies,
                json_body={"action": "start"},
            )
            self.assertEqual(stopped_stack_act.status_code, 500)

            # Docker daemon stopped on container logs
            stopped_logs = asgi_get(
                api.app,
                "/api/docker/containers/c123/logs",
                cookies=self.auth_cookies,
            )
            self.assertEqual(stopped_logs.status_code, 500)

            # Docker daemon stopped on apply updates
            stopped_apply = asgi_post(
                api.app,
                "/api/docker/updates/apply",
                cookies=self.auth_cookies,
                json_body={"containers": ["c1"]},
            )
            self.assertEqual(stopped_apply.status_code, 500)

            # Queries return empty lists when stopped
            self.assertEqual(asgi_get(api.app, "/api/docker/images", cookies=self.auth_cookies).json()["images"], [])
            self.assertEqual(asgi_get(api.app, "/api/docker/volumes", cookies=self.auth_cookies).json()["volumes"], [])
            self.assertEqual(asgi_get(api.app, "/api/docker/networks", cookies=self.auth_cookies).json()["networks"], [])
            self.assertEqual(asgi_get(api.app, "/api/docker/updates", cookies=self.auth_cookies).json()["updates"], [])

    def test_docker_install_failure_handling(self):
        with patch.object(
            main.docker_manager,
            "install_docker",
            side_effect=RuntimeError("Apt repository unreachable"),
        ):
            resp = asgi_post(api.app, "/api/docker/install", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Apt repository unreachable", resp.json()["detail"])

    def test_docker_stack_name_validation_errors(self):
        # Invalid stack names: spaces, special characters, empty
        for bad_name in ["", "   ", "my stack", "stack!@#", "stack/slash"]:
            resp = asgi_post(
                api.app,
                "/api/docker/stacks",
                cookies=self.auth_cookies,
                json_body={"name": bad_name, "compose_yaml": "services:\n  a:\n    image: b", "deploy": False},
            )
            self.assertEqual(resp.status_code, 400, f"Expected 400 for bad name: {bad_name!r}")

        # Empty compose yaml
        resp_empty_yaml = asgi_post(
            api.app,
            "/api/docker/stacks",
            cookies=self.auth_cookies,
            json_body={"name": "validname", "compose_yaml": "   ", "deploy": False},
        )
        self.assertEqual(resp_empty_yaml.status_code, 400)

    def test_docker_stack_not_found_on_action(self):
        with patch.object(main.docker_manager, "is_running", return_value=True):
            resp = asgi_post(
                api.app,
                "/api/docker/stacks/nonexistent/action",
                cookies=self.auth_cookies,
                json_body={"action": "restart"},
            )
            self.assertEqual(resp.status_code, 404)

    def test_docker_apply_updates_with_stacks_and_containers(self):
        main.docker_manager.save_stack(
            "mystack", "services:\n  redis:\n    image: redis:alpine\n", deploy=False
        )
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "execute_stack_action", return_value={"success": True}),
            patch.object(main.docker_manager, "list_containers", return_value=[
                {"id": "c1", "name": "my_container", "image": "redis:alpine", "stack": "mystack"}
            ]),
            patch.object(main.docker_manager, "pull_image", return_value={"success": True}),
        ):
            resp = asgi_post(
                api.app,
                "/api/docker/updates/apply",
                cookies=self.auth_cookies,
                json_body={"containers": ["c1"], "stacks": ["mystack"]},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(len(data["updated"]), 2)
            self.assertEqual(data["updated"][0]["type"], "stack")
            self.assertEqual(data["updated"][1]["type"], "container")

    def test_docker_apply_updates_handles_failures(self):
        main.docker_manager.save_stack(
            "failing_stack", "services:\n  f:\n    image: f:latest\n", deploy=False
        )
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager,
                "execute_stack_action",
                side_effect=RuntimeError("Compose failed"),
            ),
        ):
            resp = asgi_post(
                api.app,
                "/api/docker/updates/apply",
                cookies=self.auth_cookies,
                json_body={"stacks": ["failing_stack"]},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertFalse(data["updated"][0]["success"])
            self.assertIn("Compose failed", data["updated"][0]["error"])

    def test_external_stack_protection(self):
        external_stack_info = {
            "name": "ext_stack",
            "compose_yaml": "services:\n  ext:\n    image: ext:latest\n",
            "status": "running",
            "managed": False,
            "created_at": 0.0,
            "updated_at": 0.0,
            "containers": [],
        }

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager,
                "get_stack",
                side_effect=lambda name: external_stack_info if name == "ext_stack" else None,
            ),
        ):
            # 1. Direct method calls raise ValueError with exact required messages
            with self.assertRaises(ValueError) as ctx_save:
                main.docker_manager.save_stack("ext_stack", "services:\n  x:\n    image: y\n")
            self.assertEqual(
                str(ctx_save.exception),
                "Stack is managed externally and cannot be edited in SystemAlerts",
            )

            with self.assertRaises(ValueError) as ctx_del:
                main.docker_manager.delete_stack("ext_stack")
            self.assertEqual(
                str(ctx_del.exception),
                "Stack is managed externally and cannot be deleted",
            )

            with self.assertRaises(ValueError) as ctx_act:
                main.docker_manager.execute_stack_action("ext_stack", "restart")
            self.assertEqual(
                str(ctx_act.exception),
                "Stack is managed externally. Stack actions cannot be executed from SystemAlerts; manage constituent containers directly",
            )

            # 2. HTTP POST /api/docker/stacks returns 400
            post_resp = asgi_post(
                api.app,
                "/api/docker/stacks",
                cookies=self.auth_cookies,
                json_body={
                    "name": "ext_stack",
                    "compose_yaml": "services:\n  x:\n    image: y\n",
                    "deploy": False,
                },
            )
            self.assertEqual(post_resp.status_code, 400)
            self.assertEqual(
                post_resp.json()["detail"],
                "Stack is managed externally and cannot be edited in SystemAlerts",
            )

            # 3. HTTP PUT /api/docker/stacks/ext_stack returns 400
            put_resp = asgi_request(
                api.app,
                "PUT",
                "/api/docker/stacks/ext_stack",
                cookies=self.auth_cookies,
                json_body={
                    "name": "ext_stack",
                    "compose_yaml": "services:\n  x:\n    image: y\n",
                    "deploy": False,
                },
            )
            self.assertEqual(put_resp.status_code, 400)
            self.assertEqual(
                put_resp.json()["detail"],
                "Stack is managed externally and cannot be edited in SystemAlerts",
            )

            # 4. HTTP DELETE /api/docker/stacks/ext_stack returns 400
            del_resp = asgi_request(
                api.app,
                "DELETE",
                "/api/docker/stacks/ext_stack",
                cookies=self.auth_cookies,
            )
            self.assertEqual(del_resp.status_code, 400)
            self.assertEqual(
                del_resp.json()["detail"],
                "Stack is managed externally and cannot be deleted",
            )

            # 5. HTTP POST /api/docker/stacks/ext_stack/action returns 400
            act_resp = asgi_post(
                api.app,
                "/api/docker/stacks/ext_stack/action",
                cookies=self.auth_cookies,
                json_body={"action": "start"},
            )
            self.assertEqual(act_resp.status_code, 400)
            self.assertEqual(
                act_resp.json()["detail"],
                "Stack is managed externally. Stack actions cannot be executed from SystemAlerts; manage constituent containers directly",
            )

    def test_external_stack_update_protection(self):
        dm = main.docker_manager
        dm.invalidate_cache()

        # 1. Test is_stack_managed
        dm.save_stack("managed_stack", "services:\n  app:\n    image: app:v1\n", deploy=False)
        self.assertTrue(dm.is_stack_managed("managed_stack"))
        self.assertTrue(dm.is_stack_managed(""))
        self.assertTrue(dm.is_stack_managed(None))

        external_stack_info = {
            "name": "ext_stack",
            "compose_yaml": "services:\n  ext:\n    image: ext:latest\n",
            "status": "running",
            "managed": False,
            "created_at": 0.0,
            "updated_at": 0.0,
            "containers": [],
        }

        with (
            patch.object(dm, "is_running", return_value=True),
            patch.object(
                dm,
                "get_stack",
                side_effect=lambda name: external_stack_info if name == "ext_stack" else None,
            ),
        ):
            self.assertFalse(dm.is_stack_managed("ext_stack"))

            # 2. Test check_updates flags
            sample_containers = [
                {"id": "ext_c", "name": "ext_worker", "image": "redis:alpine", "stack": "ext_stack"},
                {"id": "mng_c", "name": "mng_worker", "image": "nginx:alpine", "stack": "managed_stack"},
                {"id": "std_c", "name": "standalone_worker", "image": "busybox:latest", "stack": ""},
            ]

            with (
                patch.object(dm, "list_containers", return_value=sample_containers),
                patch.object(dm, "_get_remote_image_digest", return_value="sha256:newdigest123456"),
                patch.object(dm, "_run_docker", return_value='{"RepoDigests": ["olddigest"]}'),
            ):
                res = dm.check_updates(force=True)
                updates = {u["container_id"]: u for u in res["updates"]}

                # External stack container: update available but cannot update because external
                ext = updates["ext_c"]
                self.assertTrue(ext["update_available"])
                self.assertFalse(ext["managed_stack"])
                self.assertFalse(ext["can_update"])

                # Managed stack container: update available and can update
                mng = updates["mng_c"]
                self.assertTrue(mng["update_available"])
                self.assertTrue(mng["managed_stack"])
                self.assertTrue(mng["can_update"])

                # Standalone container: update available and can update
                std = updates["std_c"]
                self.assertTrue(std["update_available"])
                self.assertTrue(std["managed_stack"])
                self.assertTrue(std["can_update"])

            # 3. Test apply_updates with explicit external stack
            res_stack = dm.apply_updates(stacks=["ext_stack"])
            self.assertTrue(res_stack["success"])
            self.assertEqual(len(res_stack["updated"]), 1)
            self.assertFalse(res_stack["updated"][0]["success"])
            self.assertIn(
                "Stack 'ext_stack' is managed externally and cannot be updated by SystemAlerts",
                res_stack["updated"][0]["error"],
            )

            # 4. Test apply_updates with explicit external container
            pull_mock = patch.object(dm, "pull_image", return_value={"success": True})
            with (
                pull_mock as mock_pull,
                patch.object(dm, "list_containers", return_value=sample_containers),
            ):
                res_cont = dm.apply_updates(containers=["ext_c"])
                self.assertTrue(res_cont["success"])
                self.assertEqual(len(res_cont["updated"]), 1)
                self.assertFalse(res_cont["updated"][0]["success"])
                self.assertIn(
                    "Container belongs to external stack 'ext_stack' and cannot be updated",
                    res_cont["updated"][0]["error"],
                )
                mock_pull.assert_not_called()

            # 5. Test apply_updates apply-all (neither containers nor stacks passed)
            with (
                patch.object(dm, "pull_image", return_value={"success": True}) as mock_pull,
                patch.object(dm, "execute_stack_action", return_value={"success": True}),
                patch.object(dm, "execute_container_action", return_value={"success": True}),
                patch.object(dm, "list_containers", return_value=sample_containers),
            ):
                res_all = dm.apply_updates()
                self.assertTrue(res_all["success"])
                updated_names = [u["name"] for u in res_all["updated"]]
                self.assertNotIn("ext_worker", updated_names)
                self.assertIn("mng_worker", updated_names)
                self.assertIn("standalone_worker", updated_names)
                self.assertEqual(mock_pull.call_count, 2)

            # 6. Test HTTP API endpoint POST /api/docker/updates/apply rejects external stack & container
            with (
                patch.object(dm, "list_containers", return_value=sample_containers),
                patch.object(dm, "pull_image", return_value={"success": True}) as mock_pull,
            ):
                # Target external stack via HTTP
                api_stack_resp = asgi_post(
                    api.app,
                    "/api/docker/updates/apply",
                    cookies=self.auth_cookies,
                    json_body={"stacks": ["ext_stack"]},
                )
                self.assertEqual(api_stack_resp.status_code, 200)
                stack_data = api_stack_resp.json()
                self.assertTrue(stack_data["success"])
                self.assertFalse(stack_data["updated"][0]["success"])
                self.assertIn("managed externally", stack_data["updated"][0]["error"])

                # Target external container via HTTP
                api_cont_resp = asgi_post(
                    api.app,
                    "/api/docker/updates/apply",
                    cookies=self.auth_cookies,
                    json_body={"containers": ["ext_c"]},
                )
                self.assertEqual(api_cont_resp.status_code, 200)
                cont_data = api_cont_resp.json()
                self.assertTrue(cont_data["success"])
                self.assertFalse(cont_data["updated"][0]["success"])
                self.assertIn("belongs to external stack", cont_data["updated"][0]["error"])
                mock_pull.assert_not_called()

    def test_clean_docker_error_formatting(self):
        raw_error = (
            "[+] Pulling 3/3\n"
            " 7b229517ed22: Pulling fs layer\n"
            " 3da1513d2124: Downloading [===>     ]  1.049MB/3.853MB\n"
            " 3ec36f3670df: Extracting 100%\n"
            " 7b229517ed22: Download complete\n"
            " 7b229517ed22: Pull complete\n"
            "Digest: sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a\n"
            "Status: Downloaded newer image for nginx:alpine\n"
            "WARNG[0000] the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion\n"
            "Error response from daemon: Bind for 0.0.0.0:80 failed: port is already allocated\n"
        )
        cleaned = main._clean_docker_error(raw_error)
        self.assertEqual(
            cleaned,
            "Error response from daemon: Bind for 0.0.0.0:80 failed: port is already allocated",
        )

        # Test _run_cmd surfaces cleaned error when CalledProcessError occurs
        cpe = CalledProcessError(1, ["docker", "compose", "up"], stderr=raw_error)
        with patch("subprocess.run", side_effect=cpe):
            with self.assertRaises(RuntimeError) as ctx:
                main.docker_manager._run_cmd(["docker", "compose", "up"])
            self.assertEqual(
                str(ctx.exception),
                "Error response from daemon: Bind for 0.0.0.0:80 failed: port is already allocated",
            )

    def test_volume_enhanced_listing_and_inspection(self):
        def fake_run_docker(args, timeout=30):
            if args[:2] == ["volume", "ls"]:
                return (
                    '{"Name": "data_vol", "Driver": "local", "Scope": "local", '
                    '"Mountpoint": "/var/lib/docker/volumes/data_vol/_data", "CreatedAt": "2026-10-28"}\n'
                )
            if args[:3] == ["system", "df", "-v"]:
                return json.dumps(
                    {
                        "Volumes": [
                            {
                                "Name": "data_vol",
                                "Size": "145MB",
                                "Mountpoint": "/var/lib/docker/volumes/data_vol/_data",
                            }
                        ]
                    }
                )
            if args[:3] == ["ps", "-a", "-q"]:
                return "cid1\n"
            if args[:2] == ["inspect", "--format"] and "cid1" in args:
                return '{"name": "/my_web_app", "mounts": [{"Type": "volume", "Name": "data_vol"}]}\n'
            if args[:2] == ["volume", "inspect"] and "data_vol" in args:
                return json.dumps(
                    [
                        {
                            "Name": "data_vol",
                            "Driver": "local",
                            "Scope": "local",
                            "Mountpoint": "/var/lib/docker/volumes/data_vol/_data",
                            "CreatedAt": "2026-10-28T00:00:00Z",
                            "Labels": {"env": "prod"},
                            "Options": None,
                        }
                    ]
                )
            if args[:2] == ["volume", "inspect"] and "nonexistent" in args:
                raise RuntimeError("Error response from daemon: get nonexistent: no such volume")
            return ""

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "_run_docker", side_effect=fake_run_docker),
        ):
            # 1. Enhanced list_volumes
            vols_resp = asgi_get(api.app, "/api/docker/volumes", cookies=self.auth_cookies)
            self.assertEqual(vols_resp.status_code, 200)
            vols = vols_resp.json()["volumes"]
            self.assertEqual(len(vols), 1)
            v0 = vols[0]
            self.assertEqual(v0["name"], "data_vol")
            self.assertEqual(v0["size"], "145MB")
            self.assertEqual(v0["mountpoint"], "/var/lib/docker/volumes/data_vol/_data")
            self.assertEqual(v0["containers"], ["my_web_app"])

            # 2. Volume inspect endpoint
            inspect_resp = asgi_get(
                api.app, "/api/docker/volumes/data_vol", cookies=self.auth_cookies
            )
            self.assertEqual(inspect_resp.status_code, 200)
            inv = inspect_resp.json()
            self.assertEqual(inv["name"], "data_vol")
            self.assertEqual(inv["size"], "145MB")
            self.assertEqual(inv["driver"], "local")
            self.assertEqual(inv["scope"], "local")
            self.assertEqual(inv["mountpoint"], "/var/lib/docker/volumes/data_vol/_data")
            self.assertEqual(inv["containers"], ["my_web_app"])
            self.assertEqual(inv["labels"], {"env": "prod"})

            # 3. Nonexistent volume inspect returns 404
            missing_resp = asgi_get(
                api.app, "/api/docker/volumes/nonexistent", cookies=self.auth_cookies
            )
            self.assertEqual(missing_resp.status_code, 404)

    def test_volume_remove_and_prune_endpoints_and_audit(self):
        # 1. Unauthenticated requests return 401
        self.assertEqual(asgi_post(api.app, "/api/docker/volumes/prune").status_code, 401)
        self.assertEqual(asgi_delete(api.app, "/api/docker/volumes/data_vol").status_code, 401)

        # 2. DockerManager.prune_volumes() unit test
        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager,
                "_run_docker",
                return_value="Deleted Volumes:\nvol_old\nTotal reclaimed space: 120MB\n",
            ) as mock_run,
        ):
            main.docker_manager._set_cache("status", {"cached": True})
            res = main.docker_manager.prune_volumes()
            self.assertTrue(res["success"])
            self.assertIn("Total reclaimed space", res["output"])
            mock_run.assert_called_once_with(["volume", "prune", "-f"], timeout=60)
            self.assertIsNone(main.docker_manager._get_cached("status"))

        # 3. POST /api/docker/volumes/prune and audit event
        with patch.object(
            main.docker_manager,
            "prune_volumes",
            return_value={"success": True, "output": "Pruned 2 volumes"},
        ):
            prune_resp = asgi_post(
                api.app, "/api/docker/volumes/prune", cookies=self.auth_cookies
            )
            self.assertEqual(prune_resp.status_code, 200)
            self.assertTrue(prune_resp.json()["success"])

            logs, total = main.get_audit_logs(event_type="docker.volume.prune")
            self.assertGreaterEqual(total, 1)
            self.assertEqual(logs[0]["actor"], "admin")
            self.assertIn("Pruned unused Docker volumes", logs[0]["description"])

        # 4. DELETE /api/docker/volumes/{name} and audit event
        with patch.object(
            main.docker_manager,
            "remove_volume",
            return_value={"success": True, "name": "data_vol", "output": "data_vol\n"},
        ) as mock_rm:
            del_resp = asgi_delete(
                api.app,
                "/api/docker/volumes/data_vol?force=true",
                cookies=self.auth_cookies,
            )
            self.assertEqual(del_resp.status_code, 200)
            self.assertTrue(del_resp.json()["success"])
            mock_rm.assert_called_once_with("data_vol", force=True)

            rm_logs, rm_total = main.get_audit_logs(event_type="docker.volume.remove")
            self.assertGreaterEqual(rm_total, 1)
            self.assertEqual(rm_logs[0]["actor"], "admin")
            self.assertIn("Removed Docker volume 'data_vol'", rm_logs[0]["description"])

    def test_docker_update_schedule_flow_and_checker(self):
        # 1. GET schedule returns defaults
        get_resp = asgi_get(
            api.app, "/api/docker/updates/schedule", cookies=self.auth_cookies
        )
        self.assertEqual(get_resp.status_code, 200)
        sched = get_resp.json()["schedule"]
        self.assertFalse(sched["enabled"])
        self.assertEqual(sched["day"], "daily")
        self.assertEqual(sched["time"], "03:00")
        self.assertTrue(sched["notify_discord"])

        # 2. POST with invalid day / time returns 400
        bad_day = asgi_post(
            api.app,
            "/api/docker/updates/schedule",
            cookies=self.auth_cookies,
            json_body={"day": "someday", "time": "03:00"},
        )
        self.assertEqual(bad_day.status_code, 400)

        bad_time = asgi_post(
            api.app,
            "/api/docker/updates/schedule",
            cookies=self.auth_cookies,
            json_body={"day": "daily", "time": "25:61"},
        )
        self.assertEqual(bad_time.status_code, 400)

        # 3. POST valid schedule
        ok_resp = asgi_post(
            api.app,
            "/api/docker/updates/schedule",
            cookies=self.auth_cookies,
            json_body={
                "enabled": True,
                "day": "monday",
                "time": "04:15",
                "notify_discord": False,
            },
        )
        self.assertEqual(ok_resp.status_code, 200)
        saved = ok_resp.json()["schedule"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["day"], "monday")
        self.assertEqual(saved["time"], "04:15")
        self.assertFalse(saved["notify_discord"])

        # 4. should_run_scheduled_docker_update
        # Monday at 04:15 -> Matches! (2026-10-26 was a Monday)
        monday_dt = datetime(2026, 10, 26, 4, 15, 0)
        self.assertTrue(main.should_run_scheduled_docker_update(monday_dt))

        # Tuesday at 04:15 -> Day mismatch -> False
        tuesday_dt = datetime(2026, 10, 27, 4, 15, 0)
        self.assertFalse(main.should_run_scheduled_docker_update(tuesday_dt))

        # Monday at 04:16 -> Time mismatch -> False
        monday_wrong_time = datetime(2026, 10, 26, 4, 16, 0)
        self.assertFalse(main.should_run_scheduled_docker_update(monday_wrong_time))

        # Disabled schedule -> False
        main.set_docker_auto_update_schedule({"enabled": False, "day": "monday", "time": "04:15"})
        self.assertFalse(main.should_run_scheduled_docker_update(monday_dt))

        # 5. run_scheduled_docker_updates execution
        main.set_docker_auto_update_schedule({"enabled": True, "day": "daily", "time": "03:00", "notify_discord": True})
        target_dt = datetime(2026, 10, 28, 3, 0, 0)
        with (
            patch.object(main.docker_manager, "apply_updates", return_value={"success": True, "updated": [{"type": "container", "name": "c1", "success": True}]}),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            res = asyncio.run(main.run_scheduled_docker_updates(now_dt=target_dt))
            self.assertTrue(res["executed"])
            mock_send.assert_awaited_once()

            # Calling again in the same minute should not re-run (duplicate prevention)
            res_dup = asyncio.run(main.run_scheduled_docker_updates(now_dt=target_dt))
            self.assertFalse(res_dup["executed"])

    def test_timezone_settings_and_status(self):
        # 1. Default timezone in settings is UTC
        get_resp = asgi_get(api.app, "/api/settings", cookies=self.auth_cookies)
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json().get("timezone"), "UTC")

        # 2. Update timezone via POST /api/settings
        post_resp = asgi_post(
            api.app,
            "/api/settings",
            cookies=self.auth_cookies,
            json_body={"timezone": "America/New_York"},
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertEqual(post_resp.json().get("timezone"), "America/New_York")

        # 3. GET /api/status surfaces timezone globally
        status_resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json().get("timezone"), "America/New_York")
        self.assertEqual(status_resp.json().get("settings", {}).get("timezone"), "America/New_York")

    def test_docker_cache_and_invalidation(self):
        call_count = {"ps": 0}

        def fake_run_docker(args, timeout=30):
            if "ps" in args:
                call_count["ps"] += 1
                return (
                    '{"ID": "c1", "Names": "web", "Image": "nginx", "State": "running", "Status": "Up 1m", "Labels": ""}\n'
                )
            if "info" in args:
                return "24.0.0"
            if "version" in args:
                return "24.0.0"
            if "compose" in args:
                return "v2.20.0"
            return ""

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "_run_docker", side_effect=fake_run_docker),
        ):
            # 1. First call populates cache
            c1 = main.docker_manager.list_containers(all=True)
            self.assertEqual(len(c1), 1)
            self.assertEqual(call_count["ps"], 1)

            # 2. Second call within TTL uses cache (no subprocess call)
            c2 = main.docker_manager.list_containers(all=True)
            self.assertEqual(len(c2), 1)
            self.assertEqual(call_count["ps"], 1)

            # 3. bypass_cache=True forces re-fetch
            c3 = main.docker_manager.list_containers(all=True, bypass_cache=True)
            self.assertEqual(len(c3), 1)
            self.assertEqual(call_count["ps"], 2)

            # 4. Invalidation clears cache
            main.docker_manager.invalidate_cache()
            c4 = main.docker_manager.list_containers(all=True)
            self.assertEqual(len(c4), 1)
            self.assertEqual(call_count["ps"], 3)

            # 5. Mutation actions invalidate cache
            with patch.object(main.docker_manager, "_run_docker", return_value=""):
                main.docker_manager.execute_container_action("c1", "restart")
            # Next call should fetch again
            c5 = main.docker_manager.list_containers(all=True)
            self.assertEqual(len(c5), 1)
            self.assertEqual(call_count["ps"], 4)

    def test_docker_concurrent_stats_and_fallback(self):
        def fake_run_docker(args, timeout=30):
            if "images" in args:
                return "img1\nimg2\nimg3\n"
            if "volume" in args:
                return "vol1\nvol2\n"
            if "network" in args:
                raise RuntimeError("Network daemon unresponsive")
            return ""

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(
                main.docker_manager,
                "list_containers",
                return_value=[
                    {"id": "c1", "name": "c1", "state": "running", "status": "Up"},
                    {"id": "c2", "name": "c2", "state": "exited", "status": "Exited"},
                ],
            ),
            patch.object(main.docker_manager, "list_stacks", return_value=[{"name": "s1"}]),
            patch.object(main.docker_manager, "_run_docker", side_effect=fake_run_docker),
        ):
            stats = main.docker_manager.get_stats(bypass_cache=True)
            self.assertEqual(stats["containers"], 2)
            self.assertEqual(stats["running"], 1)
            self.assertEqual(stats["stopped"], 1)
            self.assertEqual(stats["images"], 3)
            self.assertEqual(stats["volumes"], 2)
            # The failing network inspection gracefully falls back to 0 without crashing
            self.assertEqual(stats["networks"], 0)
            self.assertEqual(stats["stacks"], 1)

    def test_docker_async_routes_and_websocket_update(self):
        with (
            patch.object(main.docker_manager, "is_installed", return_value=True),
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "get_version", return_value="29.0.0"),
            patch.object(main.docker_manager, "get_compose_version", return_value="v2.20.0"),
            patch.object(
                main.docker_manager,
                "get_stats",
                return_value={
                    "containers": 1,
                    "running": 1,
                    "stopped": 0,
                    "images": 1,
                    "volumes": 1,
                    "networks": 1,
                    "stacks": 0,
                },
            ),
        ):
            # Test direct async endpoint
            resp = asgi_get(api.app, "/api/docker/status", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["version"], "29.0.0")

            # Test async module update helper
            update = asyncio.run(api._build_module_update("docker"))
            self.assertEqual(update["type"], "module_update")
            self.assertEqual(update["module"], "docker")
            self.assertEqual(update["docker"]["version"], "29.0.0")

    def test_docker_cache_ttl_and_deep_copy_isolation(self):
        """Verify that cache respects TTL expiration and deep copies prevent in-place mutation."""
        mgr = main.DockerManager(stacks_dir=self.stacks_path, cache_ttl=0.2)
        base_time = 1000.0

        with (
            patch.object(mgr, "is_running", return_value=True),
            patch.object(
                mgr,
                "_run_docker",
                return_value='{"ID": "c1", "Names": "app", "State": "running"}\n',
            ),
        ):
            with patch("time.time", return_value=base_time):
                c1 = mgr.list_containers(all=True)
                self.assertEqual(len(c1), 1)
                self.assertEqual(c1[0]["name"], "app")

                # Test deep copy isolation: mutating c1 should not affect cached object
                c1[0]["name"] = "mutated_value"
                c2 = mgr.list_containers(all=True)
                self.assertEqual(c2[0]["name"], "app")

            # Fast forward time beyond TTL (0.2s)
            with (
                patch("time.time", return_value=base_time + 0.3),
                patch.object(
                    mgr,
                    "_run_docker",
                    return_value='{"ID": "c2", "Names": "new_app", "State": "running"}\n',
                ) as mock_run,
            ):
                c3 = mgr.list_containers(all=True)
                self.assertEqual(len(c3), 1)
                self.assertEqual(c3[0]["name"], "new_app")
                mock_run.assert_called_once()

    def test_docker_mutations_invalidate_cache(self):
        """Verify all mutation operations properly invalidate DockerManager cache."""
        mgr = main.DockerManager(stacks_dir=self.stacks_path, cache_ttl=10.0)
        mgr._set_cache("status", {"test": 123})
        mgr._set_cache("containers_True", [{"id": "c1"}])

        self.assertIsNotNone(mgr._get_cached("status"))

        # 1. pull_image
        with patch.object(mgr, "is_running", return_value=True), patch.object(
            mgr, "_run_docker", return_value="pulled"
        ):
            mgr.pull_image("redis:alpine")
            self.assertIsNone(mgr._get_cached("status"))

        # Re-prime cache and test remove_image
        mgr._set_cache("status", {"test": 123})
        with patch.object(mgr, "is_running", return_value=True), patch.object(
            mgr, "_run_docker", return_value="deleted"
        ):
            mgr.remove_image("img123")
            self.assertIsNone(mgr._get_cached("status"))

        # Re-prime cache and test prune_images
        mgr._set_cache("status", {"test": 123})
        with patch.object(mgr, "is_running", return_value=True), patch.object(
            mgr, "_run_docker", return_value="pruned"
        ):
            mgr.prune_images()
            self.assertIsNone(mgr._get_cached("status"))

        # Re-prime cache and test remove_volume
        mgr._set_cache("status", {"test": 123})
        with patch.object(mgr, "is_running", return_value=True), patch.object(
            mgr, "_run_docker", return_value="removed"
        ):
            mgr.remove_volume("vol123")
            self.assertIsNone(mgr._get_cached("status"))

        # Re-prime cache and test remove_network
        mgr._set_cache("status", {"test": 123})
        with patch.object(mgr, "is_running", return_value=True), patch.object(
            mgr, "_run_docker", return_value="removed"
        ):
            mgr.remove_network("net123")
            self.assertIsNone(mgr._get_cached("status"))

    def test_docker_not_running_stats_fast_path(self):
        """Verify get_stats immediately returns zeros when daemon is not running without running threadpool."""
        mgr = main.DockerManager(stacks_dir=self.stacks_path)
        with patch.object(mgr, "is_running", return_value=False):
            stats = mgr.get_stats()
            self.assertEqual(
                stats,
                {
                    "containers": 0,
                    "running": 0,
                    "stopped": 0,
                    "images": 0,
                    "volumes": 0,
                    "networks": 0,
                    "stacks": 0,
                },
            )

    def test_docker_templates_endpoint(self):
        """Verifies GET /api/docker/templates returns curated homelab templates with complete schemas."""
        unauth_resp = asgi_get(api.app, "/api/docker/templates")
        self.assertEqual(unauth_resp.status_code, 401)

        resp = asgi_get(api.app, "/api/docker/templates", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("templates", data)
        templates = data["templates"]
        self.assertGreaterEqual(len(templates), 10)

        required_keys = {
            "id",
            "name",
            "category",
            "description",
            "icon",
            "default_ports",
            "compose_yaml",
            "env_content",
        }
        found_ids = set()
        for tpl in templates:
            for k in required_keys:
                self.assertIn(k, tpl, f"Template {tpl.get('id')} missing key '{k}'")
            self.assertIsInstance(tpl["default_ports"], list)
            self.assertIsInstance(tpl["compose_yaml"], str)
            found_ids.add(tpl["id"])

            val = main.docker_manager.validate_compose_yaml(tpl["compose_yaml"])
            self.assertTrue(
                val["valid"],
                f"Template {tpl['id']} failed YAML validation: {val.get('errors')}",
            )
            self.assertGreater(len(val["services"]), 0)

        expected_apps = {
            "pihole",
            "uptime-kuma",
            "nginx-proxy-manager",
            "vaultwarden",
            "jellyfin",
            "home-assistant",
            "portainer-ce",
            "dozzle",
            "redis",
            "postgresql",
            "glances",
        }
        for app_id in expected_apps:
            self.assertIn(app_id, found_ids, f"Expected template '{app_id}' not found in catalog")

    def test_docker_stack_validate_endpoint(self):
        """Verifies POST /api/docker/stacks/validate validates compose YAML, syntax, services, and ports."""
        unauth_resp = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            json_body={"compose_yaml": "services:\n  app:\n    image: nginx\n"},
        )
        self.assertEqual(unauth_resp.status_code, 401)

        valid_yaml = (
            "services:\n"
            "  web:\n"
            "    image: nginx:1.25-alpine\n"
            "    ports:\n"
            "      - '8080:80'\n"
            "    volumes:\n"
            "      - webdata:/usr/share/nginx/html\n"
            "volumes:\n"
            "  webdata:\n"
        )
        resp = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": valid_yaml},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("valid"))
        self.assertIn("web", data.get("services", []))
        self.assertIn("8080:80", data.get("ports", []))
        self.assertIn("webdata", data.get("volumes", []))
        self.assertIn("nginx:1.25-alpine", data.get("image_tags", []))

        resp_empty = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": "   \n   "},
        )
        self.assertEqual(resp_empty.status_code, 200)
        empty_data = resp_empty.json()
        self.assertFalse(empty_data.get("valid"))
        self.assertGreater(len(empty_data.get("errors", [])), 0)

        bad_syntax_yaml = "services:\n  web:\n    image: [unbalanced list"
        resp_bad = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": bad_syntax_yaml},
        )
        self.assertEqual(resp_bad.status_code, 200)
        bad_data = resp_bad.json()
        self.assertFalse(bad_data.get("valid"))
        self.assertGreater(len(bad_data.get("errors", [])), 0)
        self.assertIsInstance(bad_data.get("error_line"), int)
        self.assertIsInstance(bad_data.get("error_col"), int)
        self.assertGreaterEqual(bad_data.get("error_line"), 1)
        self.assertGreaterEqual(bad_data.get("error_col"), 1)

    def test_docker_stack_format_endpoint_and_canonical_ordering(self):
        """Verifies POST /api/docker/stacks/format canonically orders YAML keys, normalizes tabs, and formats .env content."""
        unauth_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            json_body={"compose_yaml": "services:\n  app:\n    image: nginx\n"},
        )
        self.assertEqual(unauth_resp.status_code, 401)

        unformatted_yaml = (
            "volumes:\n"
            "\tdb_data:   \n"
            "x-custom:\n"
            "  foo: bar\n"
            "services:\n"
            "\tweb:  \n"
            "\t\tports:\n"
            "\t\t\t- '8080:80'\n"
            "\t\tcustom_opt: true\n"
            "\t\trestart: unless-stopped\n"
            "\t\tcontainer_name: my_web\n"
            "\t\timage: nginx:alpine   \n"
            "version: '3.8'\n"
        )
        raw_env = "  DB_USER = postgres  \r\n# Database secret  \r\n\r\n\r\n\r\n  DB_PASS=secret123   \r\n"

        resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": unformatted_yaml, "env_content": raw_env},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("valid"))
        formatted_yaml = data.get("formatted_yaml", "")
        self.assertNotIn("\t", formatted_yaml)
        # Top-level key order: version -> services -> volumes -> x-custom
        self.assertLess(formatted_yaml.index("version:"), formatted_yaml.index("services:"))
        self.assertLess(formatted_yaml.index("services:"), formatted_yaml.index("volumes:"))
        self.assertLess(formatted_yaml.index("volumes:"), formatted_yaml.index("x-custom:"))
        # Service key order: image -> container_name -> restart -> ports -> custom_opt
        self.assertLess(formatted_yaml.index("image:"), formatted_yaml.index("container_name:"))
        self.assertLess(formatted_yaml.index("container_name:"), formatted_yaml.index("restart:"))
        self.assertLess(formatted_yaml.index("restart:"), formatted_yaml.index("ports:"))
        self.assertLess(formatted_yaml.index("ports:"), formatted_yaml.index("custom_opt:"))

        formatted_env = data.get("formatted_env", "")
        self.assertEqual(formatted_env, "DB_USER=postgres\n# Database secret\n\nDB_PASS=secret123\n")
        self.assertIn("web", data.get("services", []))
        self.assertIn("8080:80", data.get("ports", []))
        self.assertIn("db_data", data.get("volumes", []))

        # Empty compose_yaml with env_content provided
        env_only_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": "   ", "env_content": "  FOO=bar  \r\n"},
        )
        self.assertEqual(env_only_resp.status_code, 200)
        env_only_data = env_only_resp.json()
        self.assertTrue(env_only_data.get("valid"))
        self.assertEqual(env_only_data.get("formatted_yaml"), "")
        self.assertEqual(env_only_data.get("formatted_env"), "FOO=bar\n")

        # Invalid YAML returns valid: False with error_line and error_col
        invalid_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": "services:\n  app:\n    ports: [unclosed"},
        )
        self.assertEqual(invalid_resp.status_code, 200)
        invalid_data = invalid_resp.json()
        self.assertFalse(invalid_data.get("valid"))
        self.assertIsInstance(invalid_data.get("error_line"), int)
        self.assertIsInstance(invalid_data.get("error_col"), int)

        missing_services_yaml = "version: '3.8'\nnetworks:\n  default:\n"
        resp_missing = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": missing_services_yaml},
        )
        self.assertEqual(resp_missing.status_code, 200)
        missing_data = resp_missing.json()
        self.assertFalse(missing_data.get("valid"))
        self.assertTrue(any("services" in err.lower() for err in missing_data.get("errors", [])))

        list_services_yaml = "services:\n  - web\n  - db\n"
        resp_list = asgi_post(
            api.app,
            "/api/docker/stacks/validate",
            cookies=self.auth_cookies,
            json_body={"compose_yaml": list_services_yaml},
        )
        self.assertEqual(resp_list.status_code, 200)
        list_data = resp_list.json()
        self.assertFalse(list_data.get("valid"))
        self.assertGreater(len(list_data.get("errors", [])), 0)

    def test_docker_stack_env_persistence(self):
        """Verifies stack .env creation, SQLite storage, migration, and retrieval via save_stack, get_stack, and API."""
        compose_yaml = "services:\n  app:\n    image: postgres:16\n"
        env_content = "POSTGRES_USER=admin\nPOSTGRES_PASSWORD=supersecret\n"

        with (
            patch.object(main.docker_manager, "is_running", return_value=True),
            patch.object(main.docker_manager, "_run_compose", return_value=""),
        ):
            saved = main.docker_manager.save_stack(
                "pg_stack",
                compose_yaml,
                env_content=env_content,
                deploy=False,
            )
            self.assertEqual(saved["name"], "pg_stack")
            self.assertEqual(saved["env_content"], env_content)

            env_file = self.stacks_path / "pg_stack" / ".env"
            self.assertTrue(env_file.is_file())
            self.assertEqual(env_file.read_text(encoding="utf-8"), env_content)

            retrieved = main.docker_manager.get_stack("pg_stack")
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved["env_content"], env_content)

            stacks = main.docker_manager.list_stacks()
            pg_meta = next((s for s in stacks if s["name"] == "pg_stack"), None)
            self.assertIsNotNone(pg_meta)
            self.assertEqual(pg_meta["env_content"], env_content)

            api_compose = "services:\n  web:\n    image: nginx\n"
            api_env = "VIRTUAL_HOST=example.com\n"
            post_resp = asgi_post(
                api.app,
                "/api/docker/stacks",
                cookies=self.auth_cookies,
                json_body={
                    "name": "api_stack",
                    "compose_yaml": api_compose,
                    "env_content": api_env,
                    "deploy": False,
                },
            )
            self.assertEqual(post_resp.status_code, 200)
            self.assertEqual(post_resp.json()["stack"]["env_content"], api_env)

            get_resp = asgi_get(
                api.app,
                "/api/docker/stacks/api_stack",
                cookies=self.auth_cookies,
            )
            self.assertEqual(get_resp.status_code, 200)
            self.assertEqual(get_resp.json()["stack"]["env_content"], api_env)

            updated_env = "VIRTUAL_HOST=updated.org\nDEBUG=1\n"
            put_resp = asgi_put(
                api.app,
                "/api/docker/stacks/api_stack",
                cookies=self.auth_cookies,
                json_body={
                    "name": "api_stack",
                    "compose_yaml": api_compose,
                    "env_content": updated_env,
                    "deploy": False,
                },
            )
            self.assertEqual(put_resp.status_code, 200)
            self.assertEqual(put_resp.json()["stack"]["env_content"], updated_env)

            api_env_file = self.stacks_path / "api_stack" / ".env"
            self.assertEqual(api_env_file.read_text(encoding="utf-8"), updated_env)

            legacy_db = Path(self.temp_dir.name) / "legacy.sqlite3"
            conn = sqlite3.connect(legacy_db)
            conn.execute(
                "CREATE TABLE docker_stacks ("
                "name TEXT PRIMARY KEY, "
                "compose_yaml TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO docker_stacks VALUES (?, ?, ?, ?, ?)",
                ("old_stack", "services:\n  a:\n    image: b\n", "saved", 100.0, 100.0),
            )
            conn.commit()
            conn.close()

            with patch.object(main, "RAID_STATE_DB", legacy_db):
                m_conn = main._connect_state_db(legacy_db)
                cols = {r[1] for r in m_conn.execute("PRAGMA table_info(docker_stacks)")}
                self.assertIn("env_content", cols)
                row = m_conn.execute("SELECT name, env_content FROM docker_stacks WHERE name = 'old_stack'").fetchone()
                self.assertEqual(row[0], "old_stack")
                self.assertEqual(row[1], "")
                m_conn.close()


class DirectSystemStreamsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_streams.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        # Seed test user and session
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_statvfs_disk_usage_calculation_and_formatting(self):
        stat_mock = MagicMock()
        stat_mock.f_frsize = 4096
        stat_mock.f_blocks = 268435456  # 1 TiB
        stat_mock.f_bfree = 134217728   # 512 GiB free
        stat_mock.f_bavail = 134217728  # 512 GiB avail

        with patch("os.statvfs", return_value=stat_mock):
            res = main.get_disk_usage_statvfs("/mnt/test")
            self.assertEqual(res["total"], 268435456 * 4096)
            self.assertEqual(res["free"], 134217728 * 4096)
            self.assertEqual(res["used"], (268435456 - 134217728) * 4096)
            self.assertEqual(res["percent"], 50.0)
            self.assertEqual(res["total_human"], "1.00T")
            self.assertEqual(res["used_human"], "512.00G")
            self.assertEqual(res["free_human"], "512.00G")
            self.assertIn("512.00G/1.00T (50.0%)", res["storage_info"])

        # Error branch
        with patch("os.statvfs", side_effect=OSError("Drive offline")):
            err_res = main.get_disk_usage_statvfs("/mnt/missing")
            self.assertEqual(err_res["total"], 0)
            self.assertEqual(err_res["percent"], 0.0)
            self.assertEqual(err_res["storage_info"], "N/A")

    def test_extract_raid_summary_data_with_statvfs_fallback(self):
        with (
            patch.object(main, "get_app_settings", return_value={"mount_path": "/mnt/test"}),
            patch("os.path.exists", return_value=True),
            patch.object(
                main,
                "get_disk_usage_statvfs",
                return_value={"storage_info": "1.00T/2.00T (50.0%) | 1.00T free"},
            ),
        ):
            data = main.extract_raid_summary_data("clean", SAMPLE_DETAILS, duf_output="")
            self.assertEqual(data["storage_info"], "1.00T/2.00T (50.0%) | 1.00T free")

    def test_read_sysfs_raid_state_branches(self):
        sysfs_base = Path(self.temp_dir.name) / "sys_block"
        md0_dir = sysfs_base / "md0" / "md"
        md0_dir.mkdir(parents=True, exist_ok=True)

        # 1. Clean array
        (md0_dir / "array_state").write_text("clean\n", encoding="utf-8")
        (md0_dir / "degraded").write_text("0\n", encoding="utf-8")
        (md0_dir / "level").write_text("raid1\n", encoding="utf-8")
        (md0_dir / "raid_disks").write_text("2\n", encoding="utf-8")

        sda1_dir = md0_dir / "dev-sda1"
        sda1_dir.mkdir(parents=True, exist_ok=True)
        (sda1_dir / "state").write_text("in_sync\n", encoding="utf-8")
        (sda1_dir / "slot").write_text("0\n", encoding="utf-8")

        with patch("main.Path", side_effect=lambda p: Path(str(p).replace("/sys/block", str(sysfs_base)))):
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "clean")
            self.assertIn("State : clean", details)
            self.assertIn("Raid Level : raid1", details)
            self.assertIn("/dev/sda1", details)

            # 2. Degraded state
            (md0_dir / "degraded").write_text("1\n", encoding="utf-8")
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "degraded")

            # 3. Resyncing state
            (md0_dir / "degraded").write_text("0\n", encoding="utf-8")
            (md0_dir / "sync_action").write_text("resync\n", encoding="utf-8")
            (md0_dir / "sync_completed").write_text("12345 / 50000\n", encoding="utf-8")
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "resyncing")
            self.assertIn("Check Status : 12345 / 50000", details)

            # 4. Recovering state
            (md0_dir / "sync_action").write_text("recover\n", encoding="utf-8")
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "recovering")

        # Non-existent device returns None
        self.assertIsNone(main.read_sysfs_raid_state("/dev/md_nonexistent"))

    def test_read_sysfs_raid_state_uuid_layout_chunk_consistency(self):
        sysfs_base = Path(self.temp_dir.name) / "sys_block_rich"
        md0_dir = sysfs_base / "md0" / "md"
        md0_dir.mkdir(parents=True, exist_ok=True)

        (md0_dir / "array_state").write_text("clean\n", encoding="utf-8")
        (md0_dir / "degraded").write_text("0\n", encoding="utf-8")
        (md0_dir / "level").write_text("raid1\n", encoding="utf-8")
        (md0_dir / "raid_disks").write_text("2\n", encoding="utf-8")
        (md0_dir / "uuid").write_text("e70a9de9:6c4e4b9a:28d5d415:7eb75962\n", encoding="utf-8")
        (md0_dir / "consistency_policy").write_text("resync\n", encoding="utf-8")

        sda1_dir = md0_dir / "dev-sda1"
        sda1_dir.mkdir(parents=True, exist_ok=True)
        (sda1_dir / "state").write_text("in_sync\n", encoding="utf-8")
        (sda1_dir / "slot").write_text("0\n", encoding="utf-8")

        with patch("main.Path", side_effect=lambda p: Path(str(p).replace("/sys/block", str(sysfs_base)))):
            # RAID 1 checks
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "clean")
            self.assertIn("UUID : e70a9de9:6c4e4b9a:28d5d415:7eb75962", details)
            self.assertIn("Layout : N/A (Mirroring)", details)
            self.assertIn("Chunk Size : N/A", details)
            self.assertIn("Consistency Policy : resync", details)
            self.assertIn("Update Time : N/A (Live Sysfs Stream)", details)

            data = main._extract_raid_summary_data(status, details)
            self.assertEqual(data["uuid"], "e70a9de9:6c4e4b9a:28d5d415:7eb75962")
            self.assertEqual(data["layout"], "N/A (Mirroring)")
            self.assertEqual(data["chunk_size"], "N/A")
            self.assertEqual(data["consistency_policy"], "resync")
            self.assertEqual(data["update_time"], "N/A (Live Sysfs Stream)")

            # RAID 5 checks with chunk size and layout mapping
            (md0_dir / "level").write_text("raid5\n", encoding="utf-8")
            (md0_dir / "layout").write_text("2\n", encoding="utf-8")
            (md0_dir / "chunk_size").write_text("524288\n", encoding="utf-8")
            (md0_dir / "consistency_policy").write_text("ppl\n", encoding="utf-8")

            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertIn("Layout : left-symmetric", details)
            self.assertIn("Chunk Size : 512K", details)
            self.assertIn("Consistency Policy : ppl", details)

            data_r5 = main._extract_raid_summary_data(status, details)
            self.assertEqual(data_r5["layout"], "left-symmetric")
            self.assertEqual(data_r5["chunk_size"], "512K")
            self.assertEqual(data_r5["consistency_policy"], "ppl")

    def test_extract_raid_summary_data_clean_fallbacks(self):
        # When status is "none" or details are empty
        data_none = main._extract_raid_summary_data("none", "")
        self.assertEqual(data_none["uuid"], "N/A")
        self.assertEqual(data_none["layout"], "N/A")
        self.assertEqual(data_none["chunk_size"], "N/A")
        self.assertEqual(data_none["consistency_policy"], "N/A")
        self.assertEqual(data_none["update_time"], "N/A")

        # When fields explicitly contain "unknown"
        details_with_unknowns = """/dev/md0:
        Raid Level : raid1
        Layout : unknown
        Chunk Size : unknown
        Consistency Policy : unknown
        UUID : unknown
        Update Time : unknown
"""
        data_clean = main._extract_raid_summary_data("active", details_with_unknowns)
        self.assertEqual(data_clean["uuid"], "N/A")
        self.assertEqual(data_clean["layout"], "N/A")
        self.assertEqual(data_clean["chunk_size"], "N/A")
        self.assertEqual(data_clean["consistency_policy"], "N/A")
        self.assertEqual(data_clean["update_time"], "N/A")

    def test_read_sysfs_raid_state_uuid_fallbacks(self):
        sysfs_base = Path(self.temp_dir.name) / "sys_block_fallback"
        md0_dir = sysfs_base / "md0" / "md"
        md0_dir.mkdir(parents=True, exist_ok=True)
        (md0_dir / "array_state").write_text("clean\n", encoding="utf-8")
        (md0_dir / "degraded").write_text("0\n", encoding="utf-8")
        (md0_dir / "level").write_text("raid1\n", encoding="utf-8")

        # Fallback 1: via /dev/disk/by-id/md-uuid-*
        mock_by_id_link = MagicMock()
        mock_by_id_link.name = "md-uuid-11112222-3333-4444-5555-666677778888"
        mock_by_id_link.resolve.return_value.name = "md0"

        mock_by_id_dir = MagicMock()
        mock_by_id_dir.exists.return_value = True
        mock_by_id_dir.glob.return_value = [mock_by_id_link]

        def path_side_effect(p):
            sp = str(p)
            if sp == "/dev/disk/by-id":
                return mock_by_id_dir
            return Path(sp.replace("/sys/block", str(sysfs_base)))

        with patch("main.Path", side_effect=path_side_effect):
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertIn("UUID : 11112222-3333-4444-5555-666677778888", details)

        # Fallback 2: via blkid
        mock_by_id_dir.glob.return_value = []
        with (
            patch("main.Path", side_effect=path_side_effect),
            patch("os.path.exists", return_value=True),
            patch("shutil.which", return_value="/usr/sbin/blkid"),
            patch("subprocess.check_output", return_value=b"99998888-7777-6666-5555-444433332222\n"),
        ):
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertIn("UUID : 99998888-7777-6666-5555-444433332222", details)

    def test_check_raid_status_prioritizes_sysfs(self):
        with (
            patch.object(main, "resolve_raid_device", return_value="/dev/md0"),
            patch.object(main, "read_sysfs_raid_state", return_value=("clean", "sysfs-details")),
            patch("main.check_output") as mock_co,
        ):
            status, details = main.check_raid_status()
            self.assertEqual(status, "clean")
            self.assertEqual(details, "sysfs-details")
            mock_co.assert_not_called()

    def test_docker_socket_requests_and_fallback(self):
        mgr = main.DockerManager(stacks_dir=Path(self.temp_dir.name) / "stacks", socket_path="/var/run/test_docker.sock")

        # Mock socket request helper
        mock_containers_json = json.dumps([
            {
                "Id": "c123456789012345",
                "Names": ["/my_web"],
                "Image": "nginx:alpine",
                "State": "running",
                "Status": "Up 5 minutes",
                "Ports": [{"IP": "0.0.0.0", "PublicPort": 8080, "PrivatePort": 80, "Type": "tcp"}],
                "Created": 1700000000,
                "Labels": {"com.docker.compose.project": "web_stack"},
            }
        ]).encode("utf-8")

        mock_images_json = json.dumps([
            {
                "Id": "sha256:abc123def456",
                "RepoTags": ["redis:alpine"],
                "Size": 35000000,
                "Created": 1700000000,
            }
        ]).encode("utf-8")

        mock_volumes_json = json.dumps({
            "Volumes": [
                {
                    "Name": "test_vol",
                    "Driver": "local",
                    "Scope": "local",
                    "Mountpoint": "/var/lib/docker/volumes/test_vol/_data",
                    "CreatedAt": "2026-10-01",
                }
            ]
        }).encode("utf-8")

        mock_networks_json = json.dumps([
            {
                "Id": "net123456789",
                "Name": "app_net",
                "Driver": "bridge",
                "Scope": "local",
                "Internal": False,
                "Created": "2026-10-01",
            }
        ]).encode("utf-8")

        mock_stats_json = json.dumps({
            "read": "2026-10-30T10:00:00Z",
            "cpu_stats": {"cpu_usage": {"total_usage": 100000}},
            "memory_stats": {"usage": 5000000},
        }).encode("utf-8")

        def fake_socket_request(method, path, body=None, timeout=5):
            if path == "/_ping":
                return 200, b"OK"
            if "/containers/json" in path:
                return 200, mock_containers_json
            if path == "/images/json":
                return 200, mock_images_json
            if path == "/volumes":
                return 200, mock_volumes_json
            if path == "/networks":
                return 200, mock_networks_json
            if "/containers/c123/stats" in path:
                return 200, mock_stats_json
            raise FileNotFoundError(f"Unknown path {path}")

        with patch.object(mgr, "_socket_request", side_effect=fake_socket_request):
            # 1. is_daemon_running via socket
            self.assertTrue(mgr.is_daemon_running())

            # 2. get_containers
            containers = mgr.get_containers(all=True)
            self.assertEqual(len(containers), 1)
            self.assertEqual(containers[0]["name"], "my_web")
            self.assertEqual(containers[0]["stack"], "web_stack")
            self.assertEqual(containers[0]["ports"], "0.0.0.0:8080->80/tcp")

            # 3. get_images
            images = mgr.get_images()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]["repository"], "redis")
            self.assertEqual(images[0]["tag"], "alpine")

            # 4. get_volumes
            volumes = mgr.get_volumes()
            self.assertEqual(len(volumes), 1)
            self.assertEqual(volumes[0]["name"], "test_vol")

            # 5. get_networks
            networks = mgr.get_networks()
            self.assertEqual(len(networks), 1)
            self.assertEqual(networks[0]["name"], "app_net")

            # 6. get_container_stats
            stats = mgr.get_container_stats("c123")
            self.assertIn("cpu_stats", stats)

        # Fallback when socket throws FileNotFoundError
        with (
            patch.object(mgr, "_socket_request", side_effect=FileNotFoundError("socket down")),
            patch.object(mgr, "_run_docker", return_value='{"ID": "cli_c", "Names": "cli_app", "State": "running"}\n'),
        ):
            c_fallback = mgr.get_containers(all=True)
            self.assertEqual(len(c_fallback), 1)
            self.assertEqual(c_fallback[0]["name"], "cli_app")

    def test_api_container_stats_endpoint(self):
        # 1. Unauthenticated request rejected
        resp_unauth = asgi_get(api.app, "/api/docker/containers/c123/stats")
        self.assertEqual(resp_unauth.status_code, 401)

        # 2. Authenticated request
        with patch.object(
            main.docker_manager,
            "get_container_stats",
            return_value={"cpu_percent": 12.5, "memory_usage": 1048576},
        ):
            resp = asgi_get(
                api.app,
                "/api/docker/containers/c123/stats",
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["stats"]["cpu_percent"], 12.5)

    def test_docker_events_listener_lifecycle_broadcast(self):
        event_line = json.dumps({
            "Type": "container",
            "Action": "start",
            "Actor": {
                "ID": "c1234567890123",
                "Attributes": {"name": "monitored_app"},
            },
            "time": 1700000000,
        }).encode("utf-8") + b"\n"

        async def run_listener_test():
            # Mock content stream
            class FakeContent:
                def __aiter__(self):
                    self.yielded = False
                    return self

                async def __anext__(self):
                    if not self.yielded:
                        self.yielded = True
                        return event_line
                    # Cancel listener after delivering event
                    raise asyncio.CancelledError()

            fake_resp = MagicMock()
            fake_resp.status = 200
            fake_resp.content = FakeContent()

            class FakeSessionContext:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc, tb):
                    pass
                def get(self, url):
                    class FakeGetContext:
                        async def __aenter__(self):
                            return fake_resp
                        async def __aexit__(self, exc_type, exc, tb):
                            pass
                    return FakeGetContext()

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("aiohttp.UnixConnector", return_value=MagicMock()),
                patch("aiohttp.ClientSession", return_value=FakeSessionContext()),
                patch.object(main.docker_manager, "invalidate_cache") as mock_inval,
                patch.object(api.ws_manager, "broadcast", new_callable=AsyncMock) as mock_broadcast,
            ):
                await api.docker_events_listener(socket_path="/var/run/fake.sock")
                mock_inval.assert_called_once()
                mock_broadcast.assert_called_once()
                payload = mock_broadcast.call_args[0][0]
                self.assertEqual(payload["type"], "docker_event")
                self.assertEqual(payload["event"]["action"], "start")
                self.assertEqual(payload["event"]["name"], "monitored_app")

        asyncio.run(run_listener_test())


class DockerUpdatesDecouplingAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        main.docker_manager.invalidate_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_lifecycle.sqlite3"
        self.stacks_path = Path(self.temp_dir.name) / "stacks"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.stacks_patcher = patch.object(main, "STACKS_DIR", self.stacks_path)
        self.dm_stacks_patcher = patch.object(
            main.docker_manager, "stacks_dir", self.stacks_path
        )
        self.db_patcher.start()
        self.stacks_patcher.start()
        self.dm_stacks_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        main.docker_manager.invalidate_cache()
        self.dm_stacks_patcher.stop()
        self.stacks_patcher.stop()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_updates_subtab_in_place_keyed_rendering_and_dom_retention(self):
        """Verify that renderDockerUpdates performs in-place keyed rendering using data-container-id
        and data-row-key without destroying or re-creating untouched DOM table rows."""
        import subprocess

        app_js = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("function renderDockerUpdates(", app_js)
        self.assertIn("data-container-id", app_js)
        self.assertIn("data-row-key", app_js)
        self.assertIn("lastDockerUpdatesRenderKey", app_js)

        # Node.js simulation validating DOM node retention and rowKey updating
        node_script = """
        const fs = require('fs');
        const vm = require('vm');
        const code = fs.readFileSync('static/app.js', 'utf8');

        class MockElement {
          constructor(tagName = 'div') {
            this.tagName = tagName.toUpperCase();
            this.children = [];
            this.dataset = {};
            this.attributes = {};
            this.classList = new Set();
            this._innerHTML = '';
          }
          setAttribute(name, val) {
            this.attributes[name] = val;
            if (name.startsWith('data-')) {
              const camel = name.slice(5).replace(/-([a-z])/g, (_, l) => l.toUpperCase());
              this.dataset[camel] = val;
            }
          }
          get innerHTML() { return this._innerHTML; }
          set innerHTML(val) {
            this._innerHTML = val;
            this.children = [];
          }
          appendChild(child) {
            this.children.push(child);
            child.parentNode = this;
          }
          insertBefore(newNode, refNode) {
            if (!refNode) {
              this.children.push(newNode);
            } else {
              const idx = this.children.indexOf(refNode);
              if (idx !== -1) {
                this.children.splice(idx, 0, newNode);
              } else {
                this.children.push(newNode);
              }
            }
            newNode.parentNode = this;
          }
          remove() {
            if (this.parentNode) {
              const idx = this.parentNode.children.indexOf(this);
              if (idx !== -1) this.parentNode.children.splice(idx, 1);
            }
          }
          closest() { return this; }
          querySelectorAll(sel) {
            if (sel.includes('tr[data-container-id]')) {
              return this.children.filter(c => c.tagName === 'TR' && c.dataset.containerId !== undefined);
            }
            if (sel.includes('.empty-state, .loading-state')) {
              return this.children.filter(c => c.classList.has('empty-state') || c.classList.has('loading-state'));
            }
            return [];
          }
        }

        const tbody = new MockElement('tbody');
        tbody.id = 'docker-updates-tbody';

        const context = vm.createContext({
          document: {
            getElementById: (id) => (id === 'docker-updates-tbody' ? tbody : null),
            createElement: (tag) => new MockElement(tag)
          },
          window: {},
          console: console,
          escapeHtml: (s) => String(s || ''),
          lastDockerUpdatesRenderKey: ''
        });

        const extractFn = (name) => {
          const start = code.indexOf('function ' + name);
          let depth = 0;
          let end = start;
          let foundBrace = false;
          for (let i = start; i < code.length; i++) {
            if (code[i] === '{') {
              depth++;
              foundBrace = true;
            } else if (code[i] === '}') {
              depth--;
              if (foundBrace && depth === 0) {
                end = i + 1;
                break;
              }
            }
          }
          return code.slice(start, end);
        };

        vm.runInContext(extractFn('generateUpdateRowHtml'), context);
        vm.runInContext(extractFn('renderDockerUpdates'), context);

        // Initial render: 2 containers
        const initialUpdates = [
          { container_id: 'c1', container_name: 'app1', image: 'nginx:latest', update_available: false },
          { container_id: 'c2', container_name: 'app2', image: 'redis:latest', update_available: true }
        ];
        context.renderDockerUpdates(initialUpdates);
        if (tbody.children.length !== 2) throw new Error('Expected 2 rows');
        const row1 = tbody.children[0];
        const row2 = tbody.children[1];

        // Second render: identical data short-circuit
        context.renderDockerUpdates(initialUpdates);
        if (tbody.children[0] !== row1 || tbody.children[1] !== row2) {
          throw new Error('Identical payload should short-circuit and preserve row objects');
        }

        // Third render: update c2 status only
        const updatedUpdates = [
          { container_id: 'c1', container_name: 'app1', image: 'nginx:latest', update_available: false },
          { container_id: 'c2', container_name: 'app2', image: 'redis:latest', update_available: false }
        ];
        context.renderDockerUpdates(updatedUpdates);
        if (tbody.children[0] !== row1) {
          throw new Error('Untouched row c1 was recreated instead of retained');
        }
        if (tbody.children[1] !== row2) {
          throw new Error('Updated row c2 was recreated instead of in-place updated');
        }
        if (!row2.dataset.rowKey.includes('false')) {
          throw new Error('Row 2 rowKey not updated');
        }

        console.log('SUCCESS');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node test failed: {proc.stderr}")
        self.assertIn("SUCCESS", proc.stdout)

    def test_docker_subtab_throttling_excludes_updates_from_live_ticks(self):
        """Verify that throttledRefreshDockerSubtab decouples 'updates' and static subtabs
        from high-frequency 5s interval ticks while preserving live ticks for 'containers'."""
        import subprocess

        app_js = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn('if (subtabName !== "containers") return;', app_js)
        self.assertIn('if (isDockerSubtabInFlight("containers")) return;', app_js)

        node_script = """
        const fs = require('fs');
        const vm = require('vm');
        const code = fs.readFileSync('static/app.js', 'utf8');

        let refreshed = [];
        const context = vm.createContext({
          currentDockerSubtab: 'updates',
          currentViewTab: 'containers',
          isDockerSubtabInFlight: () => false,
          refreshDockerSubtab: (s) => refreshed.push(s),
          lastDockerSubtabFetchTime: 0,
          DOCKER_SUBTAB_THROTTLE_MS: 800,
          dockerSubtabThrottleTimer: null,
          clearTimeout: () => {},
          setTimeout: () => {}
        });

        const extractFn = (name) => {
          const start = code.indexOf('function ' + name);
          let depth = 0;
          let end = start;
          let foundBrace = false;
          for (let i = start; i < code.length; i++) {
            if (code[i] === '{') {
              depth++;
              foundBrace = true;
            } else if (code[i] === '}') {
              depth--;
              if (foundBrace && depth === 0) {
                end = i + 1;
                break;
              }
            }
          }
          return code.slice(start, end);
        };

        vm.runInContext(extractFn('throttledRefreshDockerSubtab'), context);

        // 1. Calling for updates subtab must be ignored
        context.throttledRefreshDockerSubtab('updates');
        if (refreshed.length !== 0) throw new Error('Updates subtab must not be refreshed on tick');

        // 2. Default when current subtab is updates must be ignored
        context.throttledRefreshDockerSubtab();
        if (refreshed.length !== 0) throw new Error('Default subtab must not refresh updates');

        // 3. Stacks, images, volumes subtabs must also be ignored
        context.throttledRefreshDockerSubtab('stacks');
        context.throttledRefreshDockerSubtab('images');
        if (refreshed.length !== 0) throw new Error('Static subtabs must not refresh on tick');

        // 4. Calling for containers must trigger refresh
        context.throttledRefreshDockerSubtab('containers');
        if (refreshed.length !== 1 || refreshed[0] !== 'containers') {
          throw new Error('Containers subtab should refresh on live tick');
        }

        console.log('SUCCESS');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node test failed: {proc.stderr}")
        self.assertIn("SUCCESS", proc.stdout)

    def test_docker_updates_button_loading_state_and_styles(self):
        """Verify button loading indicators and CSS spin animation rules during scans."""
        css_text = Path("static/styles.css").read_text(encoding="utf-8")
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        app_js = Path("static/app.js").read_text(encoding="utf-8")

        # 1. HTML button existence
        self.assertIn('id="btn-check-docker-updates"', index_html)

        # 2. CSS spinner animation contract
        self.assertIn("#btn-check-docker-updates.loading .refresh-icon", css_text)
        self.assertIn("animation: spin", css_text)
        self.assertIn("#btn-check-docker-updates.loading", css_text)
        self.assertIn("cursor: wait", css_text)

        # 3. app.js button state transitions
        self.assertIn('btnCheckUpdates.classList.add("loading")', app_js)
        self.assertIn("btnCheckUpdates.disabled = true", app_js)
        self.assertIn("<span>Scanning...</span>", app_js)
        self.assertIn('btnCheckUpdates.classList.remove("loading")', app_js)
        self.assertIn("btnCheckUpdates.disabled = false", app_js)

        # 4. Delegated event listener for row updates
        self.assertIn('document.getElementById("docker-updates-tbody")', app_js)
        self.assertIn('e.target.closest("button[data-update-cid]")', app_js)

    def test_live_system_metrics_stream_independently_of_docker_updates(self):
        """Verify that system (CPU, RAM, statvfs) and RAID (sysfs) metrics stream continuously
        over WebSocket without invoking Docker updates scans or coupling to the Updates tab."""
        # Mock docker_manager.check_updates to ensure it is NOT called during system/RAID live refreshes
        with (
            patch.object(main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)),
            patch.object(main, "get_duf_output", return_value=SAMPLE_DUF_TABLE),
            patch.object(main.docker_manager, "check_updates") as mock_check_updates,
        ):
            ws_res = asgi_ws(
                api.app,
                "/api/ws",
                cookies=self.auth_cookies,
                client_messages=[
                    {"action": "refresh_module", "module": "system"},
                    {"action": "refresh_module", "module": "raid"},
                ],
            )
            self.assertTrue(ws_res.accepted)
            self.assertEqual(len(ws_res.messages), 3)  # 1 snapshot + 2 module_updates

            # Snapshot verification
            snapshot = ws_res.messages[0]
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertIn("system", snapshot)
            self.assertIn("cpu_percent", snapshot["system"])
            self.assertIn("memory", snapshot["system"])
            self.assertIn("percent", snapshot["system"]["memory"])
            self.assertIn("raid", snapshot)
            self.assertEqual(snapshot["raid"]["status"], "clean")

            # System module update
            system_msg = ws_res.messages[1]
            self.assertEqual(system_msg["type"], "module_update")
            self.assertEqual(system_msg["module"], "system")
            self.assertIn("cpu_percent", system_msg["system"])
            self.assertIn("memory", system_msg["system"])
            self.assertIn("total", system_msg["system"]["memory"])
            self.assertIn("disk", system_msg["system"])

            # RAID module update
            raid_msg = ws_res.messages[2]
            self.assertEqual(raid_msg["type"], "module_update")
            self.assertEqual(raid_msg["module"], "raid")
            self.assertEqual(raid_msg["raid"]["status"], "clean")

            # Crucial assertion: check_updates was NOT invoked
            mock_check_updates.assert_not_called()

    def test_external_stack_updates_ui_and_topbar_simplification(self):
        """Verify external stack container update button suppression, badge display,
        topbar pill removal, and sidebar version footer placement."""
        import subprocess

        index_html = Path("static/index.html").read_text(encoding="utf-8")
        css_text = Path("static/styles.css").read_text(encoding="utf-8")
        app_js = Path("static/app.js").read_text(encoding="utf-8")

        # 1. HTML Topbar verification: No header raid pill or live stream indicators
        topbar_start = index_html.find('<header class="main-topbar">')
        self.assertGreater(topbar_start, -1)
        topbar_end = index_html.find('</header>', topbar_start)
        self.assertGreater(topbar_end, topbar_start)
        topbar_html = index_html[topbar_start:topbar_end]

        self.assertNotIn('id="header-raid-pill"', topbar_html)
        self.assertNotIn('id="raid-status-badge"', topbar_html)
        self.assertNotIn('id="live-stream-indicator"', topbar_html)
        self.assertNotIn('id="auto-refresh-indicator"', topbar_html)

        # 2. Overview System Metrics: No system-interval-select
        overview_start = index_html.find('id="view-overview"')
        self.assertGreater(overview_start, -1)
        overview_end = index_html.find('id="view-raid"')
        self.assertGreater(overview_end, overview_start)
        overview_html = index_html[overview_start:overview_end]
        self.assertNotIn('id="system-interval-select"', overview_html)

        # 3. Sidebar Footer: Version display placed below user session box
        sidebar_footer_idx = index_html.find('class="sidebar-footer"')
        user_session_idx = index_html.find('id="user-session-box"')
        version_display_idx = index_html.find('id="sidebar-version-display"')
        version_badge_idx = index_html.find('id="version-badge"')

        self.assertGreater(sidebar_footer_idx, -1)
        self.assertGreater(user_session_idx, sidebar_footer_idx)
        self.assertGreater(version_display_idx, user_session_idx)
        self.assertGreater(version_badge_idx, version_display_idx)

        # 4. CSS verification for sidebar version and external stack badge
        self.assertIn("#sidebar-version-display", css_text)
        self.assertIn("var(--font-mono)", css_text)
        self.assertIn(".external-stack-badge", css_text)

        # 5. Node.js VM test for generateUpdateRowHtml and button gating
        node_script = """
        const fs = require('fs');
        const vm = require('vm');
        const code = fs.readFileSync('static/app.js', 'utf8');

        const context = vm.createContext({
          escapeHtml: (s) => String(s || '')
        });

        const extractFn = (name) => {
          const start = code.indexOf('function ' + name);
          let depth = 0;
          let end = start;
          let foundBrace = false;
          for (let i = start; i < code.length; i++) {
            if (code[i] === '{') {
              depth++;
              foundBrace = true;
            } else if (code[i] === '}') {
              depth--;
              if (foundBrace && depth === 0) {
                end = i + 1;
                break;
              }
            }
          }
          return code.slice(start, end);
        };

        vm.runInContext(extractFn('generateUpdateRowHtml'), context);

        // A. Managed container with update available -> Update button rendered
        const managedRow = context.generateUpdateRowHtml({
          container_id: 'cid-managed',
          container_name: 'managed-app',
          image: 'app:latest',
          stack: 'my-managed-stack',
          update_available: true,
          managed_stack: true,
          can_update: true
        });
        if (!managedRow.includes('btn-action-success') || !managedRow.includes('Update</button>')) {
          throw new Error('Managed container with update should render Update button');
        }

        // B. External stack container with update available -> NO Update button, renders External Stack badge
        const externalRow = context.generateUpdateRowHtml({
          container_id: 'cid-ext',
          container_name: 'portainer-app',
          image: 'portainer/agent:latest',
          stack: 'external-swarm',
          update_available: true,
          managed_stack: false,
          can_update: false
        });
        if (externalRow.includes('Update</button>')) {
          throw new Error('External stack container must NOT render Update button');
        }
        if (!externalRow.includes('External Stack')) {
          throw new Error('External stack container must render External Stack badge');
        }

        // C. Standalone container with update available -> Update button rendered
        const standaloneRow = context.generateUpdateRowHtml({
          container_id: 'cid-standalone',
          container_name: 'standalone-app',
          image: 'redis:alpine',
          stack: '',
          update_available: true,
          managed_stack: true,
          can_update: true
        });
        if (!standaloneRow.includes('Update</button>')) {
          throw new Error('Standalone container with update should render Update button');
        }

        console.log('SUCCESS');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node test failed: {proc.stderr}")
        self.assertIn("SUCCESS", proc.stdout)


class NotificationCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_nc_monitor.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_provider_classes_and_notification_manager(self):
        # 1. Base provider
        base = main.NotificationProvider()
        with self.assertRaises(NotImplementedError):
            asyncio.run(base.send_notification("Title", "Msg"))

        # 2. DiscordProvider validation
        disc_disabled = main.DiscordProvider(enabled=False)
        ok, msg = asyncio.run(disc_disabled.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        disc_no_token = main.DiscordProvider(enabled=True, bot_token="", channel_id="123")
        with patch.object(main, "get_app_settings", return_value={"discord_token": ""}):
            ok, msg = asyncio.run(disc_no_token.send_notification("T", "M"))
            self.assertFalse(ok)
            self.assertIn("token", msg.lower())

        disc_no_channel = main.DiscordProvider(enabled=True, bot_token="tok", channel_id="")
        with (
            patch.object(main, "CHANNEL_ID", ""),
            patch.object(main, "get_app_settings", return_value={"channel_id": ""}),
        ):
            ok, msg = asyncio.run(disc_no_channel.send_notification("T", "M"))
            self.assertFalse(ok)
            self.assertIn("channel", msg.lower())

        # DiscordProvider webhook branch
        disc_webhook = main.DiscordProvider(
            webhook_url="https://discord.com/api/webhooks/123/abc", enabled=True
        )
        mock_resp_204 = MagicMock(status=204)
        mock_session_disc = MagicMock()
        mock_session_disc.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp_204),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_disc),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(
                disc_webhook.send_notification("Webhook Title", "Webhook Message")
            )
            self.assertTrue(ok)
            self.assertIn("webhook", msg.lower())

        # 3. TelegramProvider validation
        tg_disabled = main.TelegramProvider(enabled=False)
        ok, msg = asyncio.run(tg_disabled.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        tg_no_tok = main.TelegramProvider(enabled=True, bot_token="", chat_id="123")
        ok, msg = asyncio.run(tg_no_tok.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("token", msg.lower())

        tg_no_chat = main.TelegramProvider(enabled=True, bot_token="tok", chat_id="")
        ok, msg = asyncio.run(tg_no_chat.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("chat id", msg.lower())

        # TelegramProvider success and fallback
        tg_success = main.TelegramProvider(
            enabled=True, bot_token="123:ABC", chat_id="45678"
        )
        mock_resp_tg = MagicMock(status=200)
        mock_resp_tg.text = AsyncMock(return_value='{"ok": true}')
        mock_session_tg = MagicMock()
        mock_session_tg.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp_tg),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_tg),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(
                tg_success.send_notification(
                    "TG Title",
                    "TG Message",
                    embed_data={"fields": [{"name": "Key", "value": "Val"}]},
                )
            )
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())

        # 4. WebhookProvider validation
        wh_disabled = main.WebhookProvider(enabled=False)
        ok, msg = asyncio.run(wh_disabled.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        wh_no_url = main.WebhookProvider(enabled=True, url="")
        ok, msg = asyncio.run(wh_no_url.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("url", msg.lower())

        wh_bad_url = main.WebhookProvider(enabled=True, url="ftp://example.com")
        ok, msg = asyncio.run(wh_bad_url.send_notification("T", "M"))
        self.assertFalse(ok)
        self.assertIn("http", msg.lower())

        wh_valid = main.WebhookProvider(
            enabled=True, url="https://example.com/alerts", headers={"X-Custom": "1"}
        )
        mock_resp_wh = MagicMock(status=200)
        mock_resp_wh.text = AsyncMock(return_value='{"received": true}')
        mock_session_wh = MagicMock()
        mock_session_wh.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp_wh),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_wh),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(wh_valid.send_notification("WH Title", "WH Message"))
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())

        # 5. NotificationManager send_channel & send_all
        mgr = main.NotificationManager({
            "channels": {
                "discord": {"enabled": True, "bot_token": "tok", "channel_id": "123"},
                "telegram": {"enabled": True, "bot_token": "tg_tok", "chat_id": "tg_chat"},
                "webhook": {"enabled": False, "url": ""},
            }
        })
        with (
            patch.object(mgr.providers["discord"], "send_notification", new=AsyncMock(return_value=(True, "Discord OK"))),
            patch.object(mgr.providers["telegram"], "send_notification", new=AsyncMock(return_value=(True, "TG OK"))),
        ):
            res = asyncio.run(mgr.send_all("Manager Title", "Manager Message"))
            self.assertIn("discord", res)
            self.assertIn("telegram", res)
            self.assertNotIn("webhook", res)

            # Test send_channel("all")
            all_ok, all_msg = asyncio.run(mgr.send_channel("all", "T", "M"))
            self.assertTrue(all_ok)
            self.assertIn("discord", all_msg.lower())

            # Test unknown channel
            bad_ch_ok, bad_ch_msg = asyncio.run(mgr.send_channel("pagerduty", "T", "M"))
            self.assertFalse(bad_ch_ok)
            self.assertIn("unknown", bad_ch_msg.lower())

    def test_schedule_cron_calculation(self):
        # 1. Hourly: exactly +3600s
        t0 = 1700000000.0
        next_hourly = main.compute_next_scheduled_run({"frequency": "hourly"}, from_time=t0)
        self.assertEqual(next_hourly, t0 + 3600.0)

        # 2. Custom interval: e.g. 12 hours
        next_interval = main.compute_next_scheduled_run(
            {"frequency": "interval", "interval_hours": 12}, from_time=t0
        )
        self.assertEqual(next_interval, t0 + 12 * 3600.0)

        # 3. Daily: fixed time
        # Given 2023-11-14 12:00:00 UTC (1700001600.0)
        # If schedule is 15:00 UTC -> same day at 15:00 UTC
        dt = datetime(2023, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
        ts_now = dt.timestamp()
        next_daily_same_day = main.compute_next_scheduled_run(
            {"frequency": "daily", "time": "15:00"}, from_time=ts_now, timezone_str="UTC"
        )
        dt_res = datetime.fromtimestamp(next_daily_same_day, tz=timezone.utc)
        self.assertEqual(dt_res.hour, 15)
        self.assertEqual(dt_res.day, 14)

        # If schedule is 09:00 UTC -> next day at 09:00 UTC
        next_daily_next_day = main.compute_next_scheduled_run(
            {"frequency": "daily", "time": "09:00"}, from_time=ts_now, timezone_str="UTC"
        )
        dt_next = datetime.fromtimestamp(next_daily_next_day, tz=timezone.utc)
        self.assertEqual(dt_next.hour, 9)
        self.assertEqual(dt_next.day, 15)

        # 4. Weekly: specific weekday and time
        # 2023-11-14 was a Tuesday (weekday 1). Target Wednesday (weekday 2) at 10:00 UTC
        next_weekly = main.compute_next_scheduled_run(
            {"frequency": "weekly", "day_of_week": "wednesday", "time": "10:00"},
            from_time=ts_now,
            timezone_str="UTC",
        )
        dt_weekly = datetime.fromtimestamp(next_weekly, tz=timezone.utc)
        self.assertEqual(dt_weekly.weekday(), 2)
        self.assertEqual(dt_weekly.hour, 10)
        self.assertEqual(dt_weekly.day, 15)

    def test_topics_and_periodic_report_generation(self):
        # All topics enabled
        with (
            patch.object(main, "check_raid_status", return_value=("clean", "All 2 drives functional")),
            patch.object(main, "get_disk_usage_statvfs", return_value={"storage_info": "10GB/100GB (10%) | 90GB free"}),
        ):
            title, desc, embed = main.build_periodic_report_data({
                "system_status": True,
                "disk_health": True,
                "raid_status": True,
            })
            self.assertIn("Periodic Report", title)
            field_names = [f["name"] for f in embed["fields"]]
            self.assertTrue(any("System Status" in n for n in field_names))
            self.assertTrue(any("Disk Health" in n for n in field_names))
            self.assertTrue(any("RAID Status" in n for n in field_names))

        # Only system_status enabled
        title, desc, embed = main.build_periodic_report_data({
            "system_status": True,
            "disk_health": False,
            "raid_status": False,
        })
        field_names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("System Status" in n for n in field_names))
        self.assertFalse(any("Disk Health" in n for n in field_names))
        self.assertFalse(any("RAID Status" in n for n in field_names))

    def test_threshold_evaluations_and_cooldown_throttling(self):
        t0 = 1700000000.0

        # Configure thresholds
        main.update_notification_center_config({
            "channels": {
                "discord": {"enabled": True, "bot_token": "dummy_token", "channel_id": "12345"},
            },
            "thresholds": {
                "enabled": True,
                "ram_percent": 80.0,
                "cpu_percent": 80.0,
                "disk_percent": 80.0,
                "raid_degraded": True,
                "cooldown_minutes": 60,
            },
        })

        # Mock metrics exceeding RAM threshold (85%), CPU low (20%), Disk low (40%), RAID clean
        mock_vm = MagicMock(percent=85.0, used=8500, total=10000)
        with (
            patch("psutil.virtual_memory", return_value=mock_vm),
            patch("psutil.cpu_percent", return_value=20.0),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 40.0, "storage_info": "40/100"}),
            patch.object(main, "check_raid_status", return_value=("clean", "optimal")),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            # 1. First evaluation: RAM alert fires
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t0))
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["metric"], "ram")
            self.assertEqual(mock_send.await_count, 1)

            # 2. Immediate second evaluation at t0 + 60s: in cooldown, should NOT fire again
            alerts_cd = asyncio.run(main.evaluate_threshold_alerts(now=t0 + 60.0))
            self.assertEqual(len(alerts_cd), 0)
            self.assertEqual(mock_send.await_count, 1)

            # 3. Third evaluation after cooldown expired (t0 + 3601s): fires again!
            alerts_exp = asyncio.run(main.evaluate_threshold_alerts(now=t0 + 3601.0))
            self.assertEqual(len(alerts_exp), 1)
            self.assertEqual(alerts_exp[0]["metric"], "ram")
            self.assertEqual(mock_send.await_count, 2)

        # Test RAID degraded alert trigger
        t1 = t0 + 4000.0
        with (
            patch("psutil.virtual_memory", return_value=MagicMock(percent=20.0)),
            patch("psutil.cpu_percent", return_value=10.0),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 30.0, "storage_info": "30/100"}),
            patch.object(main, "check_raid_status", return_value=("degraded", "/dev/sdb1 failed")),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send_raid,
        ):
            alerts_raid = asyncio.run(main.evaluate_threshold_alerts(now=t1))
            self.assertEqual(len(alerts_raid), 1)
            self.assertEqual(alerts_raid[0]["metric"], "raid_degraded")
            self.assertEqual(mock_send_raid.await_count, 1)

    def test_notification_center_sqlite_persistence_and_backward_compatibility(self):
        # 1. Initial default configuration
        cfg = main.get_notification_center_config()
        self.assertIn("channels", cfg)
        self.assertIn("schedule", cfg)
        self.assertIn("topics", cfg)
        self.assertIn("thresholds", cfg)

        # 2. Update via notification center
        updated = main.update_notification_center_config({
            "channels": {
                "discord": {
                    "enabled": True,
                    "bot_token": "my_new_bot_token",
                    "channel_id": "987654321",
                },
                "telegram": {
                    "enabled": True,
                    "bot_token": "tg_secret_123",
                    "chat_id": "777888",
                },
            },
            "schedule": {
                "frequency": "daily",
                "time": "14:30",
            },
        })
        self.assertTrue(updated["channels"]["discord"]["enabled"])
        self.assertEqual(updated["channels"]["discord"]["bot_token"], "my_new_bot_token")
        self.assertEqual(updated["channels"]["telegram"]["bot_token"], "tg_secret_123")

        # Verify backward compatibility with app_settings
        app_settings = main.get_app_settings()
        self.assertEqual(app_settings["discord_enabled"], "true")
        self.assertEqual(app_settings["discord_token"], "my_new_bot_token")
        self.assertEqual(app_settings["channel_id"], "987654321")

        # 3. Masked retrieval
        masked = main.get_masked_notification_center_config()
        self.assertEqual(masked["channels"]["discord"]["bot_token"], "********")
        self.assertTrue(masked["channels"]["discord"]["bot_token_configured"])
        self.assertEqual(masked["channels"]["telegram"]["bot_token"], "********")
        self.assertTrue(masked["channels"]["telegram"]["bot_token_configured"])

        # 4. Update with masked token (should retain real secret)
        re_saved = main.update_notification_center_config({
            "channels": {
                "discord": {
                    "bot_token": "********",
                    "channel_id": "111222333",
                },
            },
        })
        self.assertEqual(re_saved["channels"]["discord"]["bot_token"], "my_new_bot_token")
        self.assertEqual(re_saved["channels"]["discord"]["channel_id"], "111222333")

        # 5. Reverse compatibility: update_app_settings syncs notification_center_config
        main.update_app_settings({
            "discord_token": "synced_from_app_settings",
            "channel_id": "444555",
            "discord_enabled": "true",
        })
        synced_cfg = main.get_notification_center_config()
        self.assertEqual(synced_cfg["channels"]["discord"]["bot_token"], "synced_from_app_settings")
        self.assertEqual(synced_cfg["channels"]["discord"]["channel_id"], "444555")

    def test_notification_center_api_endpoints_and_scheduled_evaluation(self):
        # 1. GET /api/notifications/center (enforces auth)
        unauth_resp = asgi_get(api.app, "/api/notifications/center")
        self.assertEqual(unauth_resp.status_code, 401)

        # Authenticated GET
        resp = asgi_get(api.app, "/api/notifications/center", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("channels", data)
        self.assertIn("schedule", data)
        self.assertIn("topics", data)
        self.assertIn("thresholds", data)

        # 2. POST /api/notifications/center
        update_payload = {
            "channels": {
                "discord": {"enabled": True, "bot_token": "api_token_123", "channel_id": "555666"},
                "telegram": {"enabled": True, "bot_token": "tg_token_999", "chat_id": "333444"},
            },
            "topics": {
                "system_status": True,
                "disk_health": True,
                "raid_status": False,
            },
        }
        post_resp = asgi_post(
            api.app,
            "/api/notifications/center",
            json_body=update_payload,
            cookies=self.auth_cookies,
        )
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.json()
        self.assertTrue(post_data["channels"]["discord"]["enabled"])
        self.assertEqual(post_data["channels"]["discord"]["bot_token"], "********")
        self.assertFalse(post_data["topics"]["raid_status"])

        # 3. POST /api/notifications/test with explicit channel
        with patch.object(
            main, "send_test_notification", new=AsyncMock(return_value=(True, "Test notification sent successfully."))
        ) as mock_test_notif:
            test_resp = asgi_post(
                api.app,
                "/api/notifications/test",
                json_body={"channel": "telegram"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(test_resp.status_code, 200)
            self.assertEqual(test_resp.json(), {"success": True, "message": "Test notification sent successfully."})
            mock_test_notif.assert_awaited_once_with(channel="telegram")

        # 4. evaluate_scheduled_reports
        t_due = 1700000000.0
        main.set_notification_state_entry("raid_summary", sent_at=t_due - 100, due_at=t_due - 10, status="clean")
        with patch.object(main, "send_message", new=AsyncMock(return_value=True)):
            ran = asyncio.run(main.evaluate_scheduled_reports(now=t_due))
            self.assertTrue(ran)
            state = main.get_weekly_notification_state()
            self.assertIsNotNone(state)
            sent_at, due_at, status = state
            self.assertEqual(sent_at, t_due)
            self.assertGreater(due_at, t_due)
            self.assertEqual(status, "scheduled_report")


class MultiChannelAlertHubTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_hub.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_pushover_provider_dispatch_and_validation(self):
        # 1. Disabled validation
        disabled = main.PushoverProvider(enabled=False)
        ok, msg = asyncio.run(disabled.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        # 2. Missing user key
        no_user = main.PushoverProvider(enabled=True, user_key="", api_token="tok123")
        ok, msg = asyncio.run(no_user.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("user key", msg.lower())

        # 3. Missing API token
        no_tok = main.PushoverProvider(enabled=True, user_key="usr123", api_token="")
        ok, msg = asyncio.run(no_tok.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("api token", msg.lower())

        # 4. Successful dispatch asserting URL, headers, and payload
        provider = main.PushoverProvider(
            enabled=True,
            user_key="u_test_user",
            api_token="a_test_token",
            priority=1,
            sound="cosmic",
            device="iphone",
        )
        mock_resp = MagicMock(status=200)
        mock_resp.text = AsyncMock(return_value='{"status": 1, "request": "req-123"}')
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Pushover Title", "Pushover Message"))
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            call_json = mock_session.post.call_args.kwargs.get("json")
            self.assertEqual(call_url, "https://api.pushover.net/1/messages.json")
            self.assertEqual(call_json["user"], "u_test_user")
            self.assertEqual(call_json["token"], "a_test_token")
            self.assertEqual(call_json["title"], "Pushover Title")
            self.assertEqual(call_json["message"], "Pushover Message")
            self.assertEqual(call_json["priority"], 1)
            self.assertEqual(call_json["sound"], "cosmic")
            self.assertEqual(call_json["device"], "iphone")

        # 5. Error status response
        mock_err_resp = MagicMock(status=400)
        mock_err_resp.text = AsyncMock(return_value='{"errors": ["user key not found"]}')
        mock_session_err = MagicMock()
        mock_session_err.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_err_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_err),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("400", msg)
            self.assertIn("user key not found", msg)

        # 6. Request exception
        with patch("aiohttp.ClientSession", side_effect=Exception("Pushover network timeout")):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("network timeout", msg)

    def test_ntfy_provider_dispatch_and_validation(self):
        # 1. Disabled validation
        disabled = main.NtfyProvider(enabled=False)
        ok, msg = asyncio.run(disabled.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        # 2. Missing topic
        no_topic = main.NtfyProvider(enabled=True, topic="")
        ok, msg = asyncio.run(no_topic.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("topic", msg.lower())

        # 3. Successful dispatch asserting URL, headers, and body
        provider = main.NtfyProvider(
            enabled=True,
            server_url="https://ntfy.example.com/",
            topic="homelab-alerts",
            access_token="tk_ntfy_secret",
            priority=4,
            tags=["warning", "disk"],
        )
        mock_resp = MagicMock(status=200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Ntfy Alert", "Disk degraded"))
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            call_headers = mock_session.post.call_args.kwargs.get("headers")
            call_data = mock_session.post.call_args.kwargs.get("data")
            self.assertEqual(call_url, "https://ntfy.example.com/homelab-alerts")
            self.assertEqual(call_headers["Title"], "Ntfy Alert")
            self.assertEqual(call_headers["Priority"], "4")
            self.assertEqual(call_headers["Authorization"], "Bearer tk_ntfy_secret")
            self.assertEqual(call_headers["Tags"], "warning,disk")
            self.assertEqual(call_data, "Disk degraded")

        # 4. Error response
        mock_err_resp = MagicMock(status=403)
        mock_err_resp.text = AsyncMock(return_value="Forbidden topic write")
        mock_session_err = MagicMock()
        mock_session_err.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_err_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_err),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("403", msg)
            self.assertIn("Forbidden topic write", msg)

        # 5. Request exception
        with patch("aiohttp.ClientSession", side_effect=Exception("Connection refused")):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("Connection refused", msg)

    def test_gotify_provider_dispatch_and_validation(self):
        # 1. Disabled validation
        disabled = main.GotifyProvider(enabled=False)
        ok, msg = asyncio.run(disabled.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())

        # 2. Missing server_url
        no_url = main.GotifyProvider(enabled=True, server_url="", app_token="gotify123")
        ok, msg = asyncio.run(no_url.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("server url", msg.lower())

        # 3. Missing app_token
        no_tok = main.GotifyProvider(enabled=True, server_url="https://gotify.net", app_token="")
        ok, msg = asyncio.run(no_tok.send_notification("Title", "Message"))
        self.assertFalse(ok)
        self.assertIn("app token", msg.lower())

        # 4. Successful dispatch asserting URL, headers, and payload
        provider = main.GotifyProvider(
            enabled=True,
            server_url="https://gotify.internal:8443/",
            app_token="A1B2C3D4E5",
            priority=8,
        )
        mock_resp = MagicMock(status=200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Gotify Notice", "Array recovered"))
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            call_headers = mock_session.post.call_args.kwargs.get("headers")
            call_json = mock_session.post.call_args.kwargs.get("json")
            self.assertEqual(call_url, "https://gotify.internal:8443/message")
            self.assertEqual(call_headers["X-Gotify-Key"], "A1B2C3D4E5")
            self.assertEqual(call_json["title"], "Gotify Notice")
            self.assertEqual(call_json["message"], "Array recovered")
            self.assertEqual(call_json["priority"], 8)

        # 5. Error response
        mock_err_resp = MagicMock(status=500)
        mock_err_resp.text = AsyncMock(return_value="Internal server error")
        mock_session_err = MagicMock()
        mock_session_err.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_err_resp),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_err),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("500", msg)
            self.assertIn("Internal server error", msg)

        # 6. Request exception
        with patch("aiohttp.ClientSession", side_effect=Exception("Gotify timeout")):
            ok, msg = asyncio.run(provider.send_notification("Title", "Message"))
            self.assertFalse(ok)
            self.assertIn("Gotify timeout", msg)

    def test_notification_event_routing_and_dispatch_event(self):
        cfg = {
            "channels": {
                "discord": {
                    "enabled": True,
                    "bot_token": "d_tok",
                    "channel_id": "123",
                    "routing": {"raid_alerts": True, "raid_weekly": True, "cron_failures": False, "cron_successes": False},
                },
                "pushover": {
                    "enabled": True,
                    "user_key": "u_key",
                    "api_token": "a_tok",
                    "routing": {"raid_alerts": True, "raid_weekly": False, "cron_failures": True, "cron_successes": False},
                },
                "ntfy": {
                    "enabled": True,
                    "server_url": "https://ntfy.sh",
                    "topic": "test-topic",
                    "routing": {"raid_alerts": False, "raid_weekly": False, "cron_failures": True, "cron_successes": True},
                },
                "gotify": {
                    "enabled": False,
                    "server_url": "https://gotify.lan",
                    "app_token": "g_tok",
                },
            }
        }
        mgr = main.NotificationManager(cfg)

        with (
            patch.object(mgr.providers["discord"], "send_notification", new=AsyncMock(return_value=(True, "Discord OK"))) as mock_disc,
            patch.object(mgr.providers["pushover"], "send_notification", new=AsyncMock(return_value=(True, "Pushover OK"))) as mock_push,
            patch.object(mgr.providers["ntfy"], "send_notification", new=AsyncMock(return_value=(True, "Ntfy OK"))) as mock_ntfy,
        ):
            # 1. Event: raid_alerts -> discord and pushover are subscribed, ntfy is unsubscribed
            res_raid = asyncio.run(mgr.dispatch_event("raid_alerts", "RAID Degraded", "Drive sdb failed"))
            self.assertIn("discord", res_raid)
            self.assertIn("pushover", res_raid)
            self.assertNotIn("ntfy", res_raid)
            self.assertNotIn("gotify", res_raid)
            mock_disc.assert_awaited_once()
            mock_push.assert_awaited_once()
            mock_ntfy.assert_not_awaited()

            mock_disc.reset_mock()
            mock_push.reset_mock()
            mock_ntfy.reset_mock()

            # 2. Event: cron_failures -> pushover and ntfy subscribed, discord unsubscribed
            res_cron = asyncio.run(mgr.dispatch_event("cron_failures", "Cron Failed", "Backup job exited with code 1"))
            self.assertNotIn("discord", res_cron)
            self.assertIn("pushover", res_cron)
            self.assertIn("ntfy", res_cron)
            mock_disc.assert_not_awaited()
            mock_push.assert_awaited_once()
            mock_ntfy.assert_awaited_once()

            mock_disc.reset_mock()
            mock_push.reset_mock()
            mock_ntfy.reset_mock()

            # 3. Event: cron_successes -> only ntfy subscribed
            res_succ = asyncio.run(mgr.dispatch_event("cron_successes", "Cron Succeeded", "Pruned 5 images"))
            self.assertNotIn("discord", res_succ)
            self.assertNotIn("pushover", res_succ)
            self.assertIn("ntfy", res_succ)
            mock_ntfy.assert_awaited_once()

            # 4. Dispatch with no channels enabled
            empty_mgr = main.NotificationManager({"channels": {}})
            empty_res = asyncio.run(empty_mgr.dispatch_event("raid_alerts", "Title", "Msg"))
            self.assertIn("none", empty_res)
            self.assertFalse(empty_res["none"][0])

    def test_notification_center_api_channels_crud_and_masking(self):
        # 1. GET /api/notifications/center - verify default keys for all 6 channels
        resp = asgi_get(api.app, "/api/notifications/center", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        channels = data["channels"]
        for ch in ("discord", "telegram", "webhook", "pushover", "ntfy", "gotify"):
            self.assertIn(ch, channels)
            self.assertIn("routing", channels[ch])
            self.assertIn("raid_alerts", channels[ch]["routing"])
            self.assertIn("cron_failures", channels[ch]["routing"])

        # Pushover, ntfy, gotify should have configured and masked properties
        self.assertIn("user_key_configured", channels["pushover"])
        self.assertIn("api_token_configured", channels["pushover"])
        self.assertIn("token_masked", channels["pushover"])

        self.assertIn("access_token_configured", channels["ntfy"])
        self.assertIn("token_masked", channels["ntfy"])

        self.assertIn("app_token_configured", channels["gotify"])
        self.assertIn("token_masked", channels["gotify"])

        # 2. POST /api/notifications/center - update pushover, ntfy, gotify with real values & custom routing
        update_payload = {
            "channels": {
                "pushover": {
                    "enabled": True,
                    "user_key": "u_secret_key_1234",
                    "api_token": "a_secret_token_5678",
                    "priority": 1,
                    "sound": "echo",
                    "device": "tablet",
                    "routing": {"raid_alerts": True, "cron_failures": False},
                },
                "ntfy": {
                    "enabled": True,
                    "server_url": "https://alerts.myhomelab.org",
                    "topic": "servers",
                    "access_token": "tk_super_secret_9999",
                    "priority": 4,
                    "tags": ["cloud", "server"],
                    "routing": {"cron_successes": True},
                },
                "gotify": {
                    "enabled": True,
                    "server_url": "https://gotify.myhomelab.org",
                    "app_token": "g_secret_key_7777",
                    "priority": 7,
                    "routing": {"raid_weekly": False},
                },
            }
        }
        post_resp = asgi_post(
            api.app,
            "/api/notifications/center",
            json_body=update_payload,
            cookies=self.auth_cookies,
        )
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.json()
        post_ch = post_data["channels"]

        # Check pushover masking and routing
        self.assertTrue(post_ch["pushover"]["enabled"])
        self.assertTrue(post_ch["pushover"]["user_key_configured"])
        self.assertTrue(post_ch["pushover"]["api_token_configured"])
        self.assertEqual(post_ch["pushover"]["user_key"], "********")
        self.assertEqual(post_ch["pushover"]["api_token"], "********")
        self.assertEqual(post_ch["pushover"]["user_key_masked"], "••••1234")
        self.assertEqual(post_ch["pushover"]["token_masked"], "••••5678")
        self.assertTrue(post_ch["pushover"]["routing"]["raid_alerts"])
        self.assertFalse(post_ch["pushover"]["routing"]["cron_failures"])

        # Check ntfy masking and routing
        self.assertTrue(post_ch["ntfy"]["enabled"])
        self.assertTrue(post_ch["ntfy"]["access_token_configured"])
        self.assertEqual(post_ch["ntfy"]["access_token"], "********")
        self.assertEqual(post_ch["ntfy"]["token_masked"], "••••9999")
        self.assertTrue(post_ch["ntfy"]["routing"]["cron_successes"])

        # Check gotify masking and routing
        self.assertTrue(post_ch["gotify"]["enabled"])
        self.assertTrue(post_ch["gotify"]["app_token_configured"])
        self.assertEqual(post_ch["gotify"]["app_token"], "********")
        self.assertEqual(post_ch["gotify"]["token_masked"], "••••7777")
        self.assertFalse(post_ch["gotify"]["routing"]["raid_weekly"])

        # 3. Test preserving existing tokens when masked value sent back
        preserve_payload = {
            "channels": {
                "pushover": {
                    "user_key": "********",
                    "api_token": "••••5678",
                    "priority": 2,
                },
                "ntfy": {
                    "access_token": "********",
                },
                "gotify": {
                    "app_token": "••••7777",
                },
            }
        }
        pres_resp = asgi_post(
            api.app,
            "/api/notifications/center",
            json_body=preserve_payload,
            cookies=self.auth_cookies,
        )
        self.assertEqual(pres_resp.status_code, 200)
        # Verify underlying unmasked values in DB were preserved!
        unmasked_cfg = main.get_notification_center_config()
        self.assertEqual(unmasked_cfg["channels"]["pushover"]["user_key"], "u_secret_key_1234")
        self.assertEqual(unmasked_cfg["channels"]["pushover"]["api_token"], "a_secret_token_5678")
        self.assertEqual(unmasked_cfg["channels"]["pushover"]["priority"], 2)
        self.assertEqual(unmasked_cfg["channels"]["ntfy"]["access_token"], "tk_super_secret_9999")
        self.assertEqual(unmasked_cfg["channels"]["gotify"]["app_token"], "g_secret_key_7777")

    def test_notifications_test_api_channel_targets(self):
        # Test targeting pushover, ntfy, gotify via POST /api/notifications/test
        for channel in ("pushover", "ntfy", "gotify"):
            with patch.object(
                main,
                "send_test_notification",
                new=AsyncMock(return_value=(True, f"Test notification sent successfully to {channel}.")),
            ) as mock_send_test:
                resp = asgi_post(
                    api.app,
                    "/api/notifications/test",
                    json_body={"channel": channel},
                    cookies=self.auth_cookies,
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["success"], True)
                self.assertIn(channel, resp.json()["message"])
                mock_send_test.assert_awaited_once_with(channel=channel)

        # Test live send_test_notification dispatching to Pushover
        main.update_notification_center_config({
            "channels": {
                "pushover": {
                    "enabled": True,
                    "user_key": "live_user",
                    "api_token": "live_token",
                }
            }
        })
        mock_resp_push = MagicMock(status=200)
        mock_resp_push.text = AsyncMock(return_value='{"status": 1}')
        mock_session_push = MagicMock()
        mock_session_push.post = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp_push),
                __aexit__=AsyncMock(),
            )
        )
        with patch(
            "aiohttp.ClientSession",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session_push),
                __aexit__=AsyncMock(),
            ),
        ):
            ok, msg = asyncio.run(main.send_test_notification(channel="pushover"))
            self.assertTrue(ok)
            self.assertIn("success", msg.lower())


class DiscordNotificationFormattingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_formatting.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_format_progress_bar_and_bytes_human(self):
        # Progress bar checks
        self.assertEqual(main.format_progress_bar(0), "▱▱▱▱▱▱▱▱▱▱")
        self.assertEqual(main.format_progress_bar(34.2), "▰▰▰▱▱▱▱▱▱▱")
        self.assertEqual(main.format_progress_bar(51.0), "▰▰▰▰▰▱▱▱▱▱")
        self.assertEqual(main.format_progress_bar(100.0), "▰▰▰▰▰▰▰▰▰▰")
        self.assertEqual(main.format_progress_bar(-5), "▱▱▱▱▱▱▱▱▱▱")
        self.assertEqual(main.format_progress_bar(120), "▰▰▰▰▰▰▰▰▰▰")
        self.assertEqual(main.format_progress_bar("invalid"), "▱▱▱▱▱▱▱▱▱▱")

        # Custom length
        self.assertEqual(main.format_progress_bar(50.0, length=6), "▰▰▰▱▱▱")
        self.assertEqual(main.format_progress_bar(20.0, length=5), "▰▱▱▱▱")

        # Bytes human formatting
        self.assertEqual(main.format_bytes_human(500), "500 B")
        self.assertEqual(main.format_bytes_human(1024), "1 KB")
        self.assertEqual(main.format_bytes_human(8.1 * (1024**3)), "8.1 GB")
        self.assertEqual(main.format_bytes_human(15.9 * (1024**3)), "15.9 GB")
        self.assertEqual(main.format_bytes_human(4.5 * (1024**4)), "4.5 TB")
        self.assertEqual(main.format_bytes_human("bad"), "0 B")

    def test_dict_to_discord_embed_and_provider_delivery(self):
        # Verify alias
        self.assertIs(main.DiscordChannel, main.DiscordProvider)

        # Build embed from dictionary with author, footer, timestamp, and fields
        embed_dict = {
            "title": "Embed Title",
            "description": "Embed Description",
            "color": 0x2ECC71,
            "author": {
                "name": "Vigil System Monitor [myhost]",
                "icon_url": "https://example.com/icon.png",
                "url": "https://example.com",
            },
            "footer": {
                "text": "Vigil v0.9.6 • Linux • myhost",
                "icon_url": "https://example.com/footer.png",
            },
            "timestamp": "2026-10-31T03:00:00+00:00",
            "fields": [
                {"name": "CPU", "value": "34.2%", "inline": True},
                {"name": "Memory", "value": "51.0%", "inline": True},
            ],
        }
        embed = main.dict_to_discord_embed(embed_dict)
        self.assertEqual(embed.title, "Embed Title")
        self.assertEqual(embed.description, "Embed Description")
        self.assertEqual(embed.color.value, 0x2ECC71)
        self.assertEqual(embed.author.name, "Vigil System Monitor [myhost]")
        self.assertEqual(embed.author.icon_url, "https://example.com/icon.png")
        self.assertEqual(embed.author.url, "https://example.com")
        self.assertEqual(embed.footer.text, "Vigil v0.9.6 • Linux • myhost")
        self.assertEqual(embed.footer.icon_url, "https://example.com/footer.png")
        self.assertIsNotNone(embed.timestamp)
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(embed.fields[0].name, "CPU")
        self.assertTrue(embed.fields[0].inline)
        self.assertTrue(embed.fields[1].inline)

        # Test DiscordChannel.send_notification invokes send_message with full embed
        provider = main.DiscordChannel(bot_token="test_tok", channel_id="7890", enabled=True)
        with patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send:
            ok, msg = asyncio.run(provider.send_notification("Title", "Message", embed_data=embed_dict))
            self.assertTrue(ok)
            mock_send.assert_awaited_once()
            sent_embed = mock_send.await_args[0][0]
            self.assertIsInstance(sent_embed, discord.Embed)
            self.assertEqual(sent_embed.author.name, "Vigil System Monitor [myhost]")
            self.assertEqual(sent_embed.footer.text, "Vigil v0.9.6 • Linux • myhost")
            self.assertEqual(len(sent_embed.fields), 2)

    def test_modern_embed_layout_periodic_report_and_test_notification(self):
        # Mock metrics
        mock_vm = MagicMock(percent=51.0, used=int(8.1 * 1024**3), total=int(15.9 * 1024**3))
        with (
            patch("psutil.cpu_percent", return_value=34.2),
            patch("psutil.cpu_count", return_value=4),
            patch("psutil.virtual_memory", return_value=mock_vm),
            patch("psutil.boot_time", return_value=time.time() - 3600),
            patch("os.getloadavg", return_value=(0.45, 0.38, 0.32)),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 42.0, "used": 42000, "total": 100000, "free": 58000}),
            patch.object(main, "check_raid_status", return_value=("clean", SAMPLE_DETAILS)),
            patch("platform.node", return_value="vps-node-1"),
        ):
            title, desc, embed_data = main.build_periodic_report_data({
                "system_status": True,
                "disk_health": True,
                "raid_status": True,
            })
            self.assertIn("Periodic Report", title)
            self.assertIn("🟢 All Systems Operational", desc)
            self.assertEqual(embed_data["color"], 0x2ECC71)
            self.assertIn("vps-node-1", embed_data["author"]["name"])
            self.assertIn("vps-node-1", embed_data["footer"]["text"])
            self.assertIn("timestamp", embed_data)

            # Check CPU field
            field_map = {f["name"]: f for f in embed_data["fields"]}
            self.assertIn("System Status • CPU", field_map)
            cpu_field = field_map["System Status • CPU"]
            self.assertIn("▰▰▰▱▱▱▱▱▱▱", cpu_field["value"])
            self.assertIn("34.2%", cpu_field["value"])
            self.assertIn("4 cores", cpu_field["value"])
            self.assertTrue(cpu_field["inline"])

            # Check Memory field
            self.assertIn("System Status • Memory", field_map)
            mem_field = field_map["System Status • Memory"]
            self.assertIn("▰▰▰▰▰▱▱▱▱▱", mem_field["value"])
            self.assertIn("51.0%", mem_field["value"])
            self.assertTrue(mem_field["inline"])

            # Check Uptime & Load field
            self.assertIn("System Status • Uptime & Load", field_map)
            uptime_field = field_map["System Status • Uptime & Load"]
            self.assertIn("Load: `0.45, 0.38, 0.32`", uptime_field["value"])
            self.assertTrue(uptime_field["inline"])

            # Check Disk Health
            self.assertIn("Disk Health", field_map)
            disk_field = field_map["Disk Health"]
            self.assertIn("Root (/)", disk_field["value"])
            self.assertIn("42.0%", disk_field["value"])

            # Check RAID Status
            self.assertIn("RAID Status", field_map)
            raid_field = field_map["RAID Status"]
            self.assertIn("🟢 CLEAN", raid_field["value"])
            self.assertIn("RAID 1", raid_field["value"])
            self.assertIn("`2` active", raid_field["value"])

        # Degraded status banner test
        with (
            patch.object(main, "check_raid_status", return_value=("degraded", "md0 degraded")),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 50.0}),
        ):
            title_deg, desc_deg, embed_deg = main.build_periodic_report_data({
                "system_status": False,
                "disk_health": True,
                "raid_status": True,
            })
            self.assertIn("🔴 Attention: RAID Array DEGRADED", desc_deg)
            self.assertEqual(embed_deg["color"], 0xEF4444)

    def test_modern_threshold_alert_embeds(self):
        t0 = 1700000000.0
        main.update_notification_center_config({
            "channels": {"discord": {"enabled": True, "bot_token": "tok", "channel_id": "123"}},
            "thresholds": {
                "enabled": True,
                "ram_percent": 80.0,
                "cpu_percent": 85.0,
                "disk_percent": 85.0,
                "raid_degraded": True,
                "cooldown_minutes": 60,
            },
        })

        # Exceeding RAM (85% warning -> Amber 0xF59E0B)
        mock_vm = MagicMock(percent=85.0, used=int(8.5 * 1024**3), total=int(10 * 1024**3))
        with (
            patch("psutil.virtual_memory", return_value=mock_vm),
            patch("psutil.cpu_percent", return_value=10.0),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 40.0}),
            patch.object(main, "check_raid_status", return_value=("clean", "optimal")),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)),
            patch("platform.node", return_value="vps-node-1"),
        ):
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t0))
            self.assertEqual(len(alerts), 1)
            embed = alerts[0]["embed"]
            self.assertEqual(embed["color"], 0xF59E0B)
            self.assertIn("▰▰▰▰▰▰▰▰▱▱", embed["fields"][0]["value"])
            self.assertIn("vps-node-1", embed["author"]["name"])
            self.assertIn("vps-node-1", embed["footer"]["text"])
            self.assertIn("timestamp", embed)

        # Exceeding CPU (95% critical -> Red 0xEF4444)
        with (
            patch("psutil.virtual_memory", return_value=MagicMock(percent=20.0)),
            patch("psutil.cpu_percent", return_value=95.0),
            patch.object(main, "get_disk_usage_statvfs", return_value={"percent": 40.0}),
            patch.object(main, "check_raid_status", return_value=("clean", "optimal")),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)),
            patch("platform.node", return_value="vps-node-1"),
        ):
            alerts_cpu = asyncio.run(main.evaluate_threshold_alerts(now=t0 + 7200))
            self.assertEqual(len(alerts_cpu), 1)
            self.assertEqual(alerts_cpu[0]["metric"], "cpu")
            self.assertEqual(alerts_cpu[0]["embed"]["color"], 0xEF4444)
            self.assertIn("▰▰▰▰▰▰▰▰▰▰", alerts_cpu[0]["embed"]["fields"][0]["value"])

    def test_modern_raid_embed_structure(self):
        with patch("platform.node", return_value="vps-node-1"):
            embed = main.build_raid_embed("clean", SAMPLE_DETAILS, SAMPLE_DUF)
            self.assertEqual(embed.title, "RAID 1 | CLEAN")
            self.assertEqual(embed.color.value, 0x2ECC71)
            self.assertIn("Status", embed.description)
            self.assertIn("🟢", embed.description)
            self.assertIn("vps-node-1", embed.author.name)
            self.assertIn("vps-node-1", embed.footer.text)
            self.assertIsNotNone(embed.timestamp)

            field_names = [f.name for f in embed.fields]
            self.assertIn("Array Health", field_names)
            self.assertIn("Device Breakdown", field_names)


class TestSystemServicesEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_services_require_auth(self):
        resp = asgi_get(api.app, "/api/services")
        self.assertEqual(resp.status_code, 401)

    def test_get_services_error_handling(self):
        with patch.object(main, "get_system_services", side_effect=RuntimeError("systemctl failed")):
            resp = asgi_get(api.app, "/api/services", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching system services", resp.json()["detail"])

    def test_get_services_systemctl_unavailable(self):
        with patch("shutil.which", return_value=None):
            resp = asgi_get(api.app, "/api/services", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["systemctl_available"])
            self.assertEqual(data["services"], [])
            self.assertEqual(data["summary"]["total"], 0)
            self.assertEqual(data["failed_services"], [])

    def test_get_services_parsing_and_summary(self):
        mock_output = (
            "  cron.service                 loaded active running Regular background program processing daemon\n"
            "  nginx.service                loaded failed failed  A high performance web server\n"
            "  systemd-udevd.service        loaded active running Rule-based Manager for Device Events and Files\n"
            "  cups.service                 loaded inactive dead  CUPS Scheduler\n"
            "LOAD   = Reflects whether the unit definition was properly loaded.\n"
            "4 loaded units listed.\n"
        )
        with (
            patch("shutil.which", return_value="/bin/systemctl"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=mock_output, stderr=""),
            ),
        ):
            resp = asgi_get(api.app, "/api/services", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["systemctl_available"])
            self.assertEqual(len(data["services"]), 4)
            self.assertEqual(data["summary"]["total"], 4)
            self.assertEqual(data["summary"]["running"], 2)
            self.assertEqual(data["summary"]["failed"], 1)
            self.assertEqual(data["summary"]["inactive"], 1)
            self.assertEqual(data["failed_services"], ["nginx.service"])

            cron = next(s for s in data["services"] if s["unit"] == "cron.service")
            self.assertEqual(cron["name"], "cron")
            self.assertEqual(cron["load_state"], "loaded")
            self.assertEqual(cron["active_state"], "active")
            self.assertEqual(cron["sub_state"], "running")
            self.assertIn("Regular background", cron["description"])

    def test_service_action_validation_and_execution(self):
        # Invalid action
        resp = asgi_post(
            api.app,
            "/api/services/cron.service/action",
            json_body={"action": "reboot"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid action", resp.json()["detail"])

        # Invalid service name
        resp = asgi_post(
            api.app,
            "/api/services/bad;service/action",
            json_body={"action": "restart"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid service name", resp.json()["detail"])

        # Protected service stop rejection
        resp = asgi_post(
            api.app,
            "/api/services/dbus.service/action",
            json_body={"action": "stop"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("not permitted on protected service", resp.json()["detail"])

        # Successful restart execution
        with (
            patch("shutil.which", return_value="/bin/systemctl"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="Restarted cron", stderr=""),
            ) as mock_sub,
        ):
            resp = asgi_post(
                api.app,
                "/api/services/cron/action",
                json_body={"action": "restart"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["service"], "cron.service")
            self.assertEqual(data["action"], "restart")
            mock_sub.assert_called_once()
            self.assertEqual(mock_sub.call_args[0][0], ["systemctl", "restart", "cron.service"])

    def test_service_logs(self):
        with (
            patch("shutil.which", return_value="/bin/journalctl"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="Oct 30 12:00:00 cron[123]: started\nOct 30 12:01:00 cron[123]: ran job\n", stderr=""),
            ),
        ):
            resp = asgi_get(
                api.app,
                "/api/services/cron.service/logs?lines=50",
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["service"], "cron.service")
            self.assertEqual(len(data["logs"]), 2)
            self.assertEqual(data["lines"], 2)

    def test_vigil_logs_endpoints_and_fallback(self):
        # 1. Auth required
        unauth_resp = asgi_get(api.app, "/api/vigil/logs")
        self.assertEqual(unauth_resp.status_code, 401)
        unauth_sys = asgi_get(api.app, "/api/system/logs")
        self.assertEqual(unauth_sys.status_code, 401)

        # 2. Journalctl returns vigil log entries
        journal_output = "Oct 31 12:00:00 server vigil[100]: Vigil Host Manager v1.0.2 started\nOct 31 12:00:01 server vigil[100]: Listening on 0.0.0.0:8000\n"
        with (
            patch("shutil.which", return_value="/bin/journalctl"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=journal_output, stderr=""),
            ),
        ):
            resp = asgi_get(api.app, "/api/vigil/logs?lines=50", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["service"], "vigil.service")
            self.assertEqual(len(data["logs"]), 2)
            self.assertIn("Vigil Host Manager", data["logs"][0])

            # Alias /api/system/logs returns same structure
            resp_sys = asgi_get(api.app, "/api/system/logs?lines=50", cookies=self.auth_cookies)
            self.assertEqual(resp_sys.status_code, 200)
            self.assertEqual(resp_sys.json()["service"], "vigil.service")

        # 3. Journalctl unavailable -> falls back to file logs if present
        temp_log = Path(self.temp_dir.name) / "vigil.log"
        temp_log.write_text("2026-10-31 12:00:00 [INFO] File log line 1\n2026-10-31 12:00:01 [INFO] File log line 2\n", encoding="utf-8")
        with (
            patch("shutil.which", return_value=None),
            patch.object(main, "Path", side_effect=lambda p: temp_log if "vigil.log" in str(p) else Path(p)),
        ):
            logs = main.get_vigil_logs(lines=10)
            self.assertEqual(len(logs), 2)
            self.assertIn("File log line 1", logs[0])

        # 4. Neither journalctl nor file log present -> returns friendly fallback indicator
        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.is_file", return_value=False),
        ):
            logs_none = main.get_vigil_logs(lines=10)
            self.assertEqual(len(logs_none), 1)
            self.assertIn("-- No journald or file log entries found for vigil.service --", logs_none[0])

        # 5. Error handling returns 500
        with patch.object(main, "get_vigil_logs", side_effect=RuntimeError("unexpected log failure")):
            err_resp = asgi_get(api.app, "/api/vigil/logs", cookies=self.auth_cookies)
            self.assertEqual(err_resp.status_code, 500)
            self.assertIn("Error fetching Vigil logs", err_resp.json()["detail"])


class TestProcessExplorerEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_get_processes_live_and_summary(self):
        resp = asgi_get(api.app, "/api/processes", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("processes", data)
        self.assertIn("summary", data)
        summary = data["summary"]
        for key in ["total_processes", "running_count", "sleeping_count", "top_cpu_process", "top_memory_process"]:
            self.assertIn(key, summary)
        self.assertGreater(summary["total_processes"], 0)

    def test_get_processes_error_handling(self):
        with patch.object(main, "get_process_list", side_effect=RuntimeError("psutil error")):
            resp = asgi_get(api.app, "/api/processes", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching processes", resp.json()["detail"])

    def test_get_processes_sorting_and_filtering(self):
        class DummyProc:
            def __init__(self, info):
                self.info = info

        mock_procs = [
            DummyProc({"pid": 10, "name": "alpha", "username": "root", "cpu_percent": 10.0, "memory_percent": 50.0, "status": "running", "num_threads": 2, "create_time": 100.0}),
            DummyProc({"pid": 20, "name": "beta", "username": "user1", "cpu_percent": 80.0, "memory_percent": 10.0, "status": "sleeping", "num_threads": 1, "create_time": 200.0}),
            DummyProc({"pid": 30, "name": "gamma", "username": "user2", "cpu_percent": 40.0, "memory_percent": 30.0, "status": "running", "num_threads": 4, "create_time": 300.0}),
        ]

        with patch("psutil.process_iter", return_value=mock_procs):
            # Sort CPU desc (beta 80%, gamma 40%, alpha 10%)
            resp_cpu = asgi_get(api.app, "/api/processes?sort=cpu", cookies=self.auth_cookies)
            self.assertEqual(resp_cpu.status_code, 200)
            names_cpu = [p["name"] for p in resp_cpu.json()["processes"]]
            self.assertEqual(names_cpu, ["beta", "gamma", "alpha"])

            # Sort Memory desc (alpha 50%, gamma 30%, beta 10%)
            resp_mem = asgi_get(api.app, "/api/processes?sort=memory", cookies=self.auth_cookies)
            self.assertEqual(resp_mem.status_code, 200)
            names_mem = [p["name"] for p in resp_mem.json()["processes"]]
            self.assertEqual(names_mem, ["alpha", "gamma", "beta"])

            # Sort PID asc (10, 20, 30)
            resp_pid = asgi_get(api.app, "/api/processes?sort=pid", cookies=self.auth_cookies)
            self.assertEqual(resp_pid.status_code, 200)
            pids = [p["pid"] for p in resp_pid.json()["processes"]]
            self.assertEqual(pids, [10, 20, 30])

            # Filter search q=user1
            resp_q = asgi_get(api.app, "/api/processes?q=user1", cookies=self.auth_cookies)
            self.assertEqual(resp_q.status_code, 200)
            self.assertEqual(len(resp_q.json()["processes"]), 1)
            self.assertEqual(resp_q.json()["processes"][0]["name"], "beta")

            # Invalid sort rejected
            resp_bad = asgi_get(api.app, "/api/processes?sort=invalid", cookies=self.auth_cookies)
            self.assertEqual(resp_bad.status_code, 400)

    def test_kill_process_safety_and_signals(self):
        # PID 1 forbidden
        resp1 = asgi_post(api.app, "/api/processes/1/kill", json_body={"signal": "SIGTERM"}, cookies=self.auth_cookies)
        self.assertEqual(resp1.status_code, 403)
        self.assertIn("protected", resp1.json()["detail"])

        # PID 2 forbidden (kthreadd)
        resp2 = asgi_post(api.app, "/api/processes/2/kill", json_body={"signal": "SIGTERM"}, cookies=self.auth_cookies)
        self.assertEqual(resp2.status_code, 403)

        # Vigil own PID forbidden
        resp_own = asgi_post(api.app, f"/api/processes/{os.getpid()}/kill", json_body={"signal": "SIGTERM"}, cookies=self.auth_cookies)
        self.assertEqual(resp_own.status_code, 403)
        self.assertIn("Vigil", resp_own.json()["detail"])

        # Invalid signal
        resp_sig = asgi_post(api.app, "/api/processes/999/kill", json_body={"signal": "SIGINVALID"}, cookies=self.auth_cookies)
        self.assertEqual(resp_sig.status_code, 400)

        # Valid process kill
        mock_proc = MagicMock()
        with patch("psutil.Process", return_value=mock_proc):
            resp_term = asgi_post(api.app, "/api/processes/1234/kill", json_body={"signal": "SIGTERM"}, cookies=self.auth_cookies)
            self.assertEqual(resp_term.status_code, 200)
            self.assertEqual(resp_term.json()["status"], "ok")
            mock_proc.terminate.assert_called_once()

            resp_kill = asgi_post(api.app, "/api/processes/1234/kill", json_body={"signal": "SIGKILL"}, cookies=self.auth_cookies)
            self.assertEqual(resp_kill.status_code, 200)
            mock_proc.kill.assert_called_once()


class TestStorageAndFilesystemMetricsEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_system_metrics_endpoint_structure(self):
        resp = asgi_get(api.app, "/api/system/metrics", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("filesystems", data)
        self.assertIn("mounts", data)
        self.assertIn("disks", data)
        self.assertIn("services_summary", data)
        self.assertIn("memory", data)
        self.assertIn("cpu_percent", data)

        svc = data["services_summary"]
        for key in ["running", "failed", "total"]:
            self.assertIn(key, svc)

        if data["filesystems"]:
            disk = data["filesystems"][0]
            for field in ["mount_point", "mountpoint", "device", "fstype", "total", "used", "free", "percent"]:
                self.assertIn(field, disk)

    def test_system_storage_endpoint_includes_filesystems(self):
        resp = asgi_get(api.app, "/api/system/storage", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("disks", data)
        self.assertIn("filesystems", data)
        self.assertIn("mounts", data)

    def test_status_endpoint_includes_services_summary(self):
        resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("services_summary", data)
        self.assertIn("filesystems", data["system"])
        self.assertIn("mounts", data["system"])

    def test_storage_and_metrics_error_handling(self):
        with patch.object(api, "get_storage_metrics", side_effect=RuntimeError("storage error")):
            resp = asgi_get(api.app, "/api/system/storage", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching storage metrics", resp.json()["detail"])

        with patch.object(api, "get_system_metrics", side_effect=RuntimeError("metrics error")):
            resp = asgi_get(api.app, "/api/system/metrics", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching system metrics", resp.json()["detail"])


class TestNetworkTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_get_system_metrics_data_includes_network(self):
        metrics = main.get_system_metrics_data()
        self.assertIn("network", metrics)
        net = metrics["network"]
        self.assertIn("total_bytes_sent", net)
        self.assertIn("total_bytes_recv", net)
        self.assertIn("interfaces", net)
        self.assertIsInstance(net["total_bytes_sent"], int)
        self.assertIsInstance(net["total_bytes_recv"], int)
        self.assertIsInstance(net["interfaces"], list)

        for iface in net["interfaces"]:
            self.assertIn("interface", iface)
            self.assertNotEqual(iface["interface"], "lo")
            self.assertIn("ip", iface)
            self.assertIn("is_up", iface)
            self.assertIsInstance(iface["is_up"], bool)
            self.assertIn("bytes_sent", iface)
            self.assertIn("bytes_recv", iface)
            self.assertIn("packets_sent", iface)
            self.assertIn("packets_recv", iface)

    def test_api_system_metrics_and_status_network(self):
        resp = asgi_get(api.app, "/api/system/metrics", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("network", data)
        self.assertIn("total_bytes_sent", data["network"])
        self.assertIn("total_bytes_recv", data["network"])
        self.assertIn("interfaces", data["network"])

        resp_status = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
        self.assertEqual(resp_status.status_code, 200)
        status_data = resp_status.json()
        self.assertIn("network", status_data["system"])
        self.assertIn("network", status_data)

    def test_network_telemetry_mocked_interfaces_and_loopback_skipped(self):
        from collections import namedtuple
        SnicAddr = namedtuple("snicaddr", ["family", "address", "netmask", "broadcast", "ptp"])
        Snetio = namedtuple("snetio", ["bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errin", "errout", "dropin", "dropout"])
        SnicStats = namedtuple("snicstats", ["isup", "duplex", "speed", "mtu", "flags"])

        fake_addrs = {
            "lo": [SnicAddr(socket.AF_INET, "127.0.0.1", "255.0.0.0", None, None)],
            "eth0": [
                SnicAddr(socket.AF_INET, "192.168.1.100", "255.255.255.0", "192.168.1.255", None),
                SnicAddr(socket.AF_INET6, "fe80::1", "ffff:ffff:ffff:ffff::", None, None),
            ],
            "enp3s0": [
                SnicAddr(socket.AF_INET, "10.0.0.5", "255.255.255.0", None, None),
            ],
        }
        fake_io = {
            "lo": Snetio(999999, 999999, 500, 500, 0, 0, 0, 0),
            "eth0": Snetio(1000, 2000, 10, 20, 0, 0, 0, 0),
            "enp3s0": Snetio(3000, 4000, 30, 40, 0, 0, 0, 0),
        }
        fake_stats = {
            "lo": SnicStats(True, 0, 0, 65536, ""),
            "eth0": SnicStats(True, 2, 1000, 1500, ""),
            "enp3s0": SnicStats(False, 0, 0, 1500, ""),
        }

        with patch("psutil.net_if_addrs", return_value=fake_addrs), \
             patch("psutil.net_io_counters", return_value=fake_io), \
             patch("psutil.net_if_stats", return_value=fake_stats):

            metrics = main.get_system_metrics_data()
            net = metrics["network"]
            self.assertEqual(net["total_bytes_sent"], 4000)
            self.assertEqual(net["total_bytes_recv"], 6000)
            self.assertEqual(len(net["interfaces"]), 2)

            eth0 = next((i for i in net["interfaces"] if i["interface"] == "eth0"), None)
            self.assertIsNotNone(eth0)
            self.assertEqual(eth0["ip"], "192.168.1.100")
            self.assertTrue(eth0["is_up"])
            self.assertEqual(eth0["bytes_sent"], 1000)
            self.assertEqual(eth0["bytes_recv"], 2000)
            self.assertEqual(eth0["packets_sent"], 10)
            self.assertEqual(eth0["packets_recv"], 20)

            enp3s0 = next((i for i in net["interfaces"] if i["interface"] == "enp3s0"), None)
            self.assertIsNotNone(enp3s0)
            self.assertEqual(enp3s0["ip"], "10.0.0.5")
            self.assertFalse(enp3s0["is_up"])
            self.assertEqual(enp3s0["bytes_sent"], 3000)
            self.assertEqual(enp3s0["bytes_recv"], 4000)
            self.assertEqual(enp3s0["packets_sent"], 30)
            self.assertEqual(enp3s0["packets_recv"], 40)


class TestSystemUserGroupEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_get_users_and_groups(self):
        resp_u = asgi_get(api.app, "/api/system/users", cookies=self.auth_cookies)
        self.assertEqual(resp_u.status_code, 200)
        data_u = resp_u.json()
        self.assertIn("users", data_u)
        self.assertIn("summary", data_u)
        self.assertIn("total_users", data_u["summary"])
        self.assertIn("regular_users_count", data_u["summary"])
        self.assertIn("system_users_count", data_u["summary"])

        if data_u["users"]:
            user0 = data_u["users"][0]
            for key in ["username", "uid", "gid", "home", "shell", "is_system", "groups"]:
                self.assertIn(key, user0)

        resp_g = asgi_get(api.app, "/api/system/groups", cookies=self.auth_cookies)
        self.assertEqual(resp_g.status_code, 200)
        data_g = resp_g.json()
        self.assertIn("groups", data_g)
        self.assertIn("summary", data_g)
        self.assertIn("total_groups", data_g["summary"])

    def test_get_users_and_groups_error_handling(self):
        with patch.object(main, "get_system_users", side_effect=RuntimeError("user error")):
            resp = asgi_get(api.app, "/api/system/users", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching system users", resp.json()["detail"])

        with patch.object(main, "get_system_groups", side_effect=RuntimeError("group error")):
            resp = asgi_get(api.app, "/api/system/groups", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching system groups", resp.json()["detail"])

    def test_user_create_validation_and_execution(self):
        # Invalid username
        resp_inv = asgi_post(
            api.app,
            "/api/system/users",
            json_body={"username": "123_invalid!"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_inv.status_code, 400)

        with (
            patch("shutil.which", return_value="/usr/sbin/useradd"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ) as mock_sub,
        ):
            resp = asgi_post(
                api.app,
                "/api/system/users",
                json_body={"username": "testuser", "shell": "/bin/bash", "groups": ["sudo"], "create_home": True},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            mock_sub.assert_called_once()
            self.assertEqual(mock_sub.call_args[0][0], ["useradd", "-m", "-s", "/bin/bash", "-G", "sudo", "testuser"])

    def test_user_delete_safety(self):
        # Root forbidden
        resp_root = asgi_request(
            api.app,
            "DELETE",
            "/api/system/users/root",
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_root.status_code, 403)

        # Regular user delete allowed
        with (
            patch("shutil.which", return_value="/usr/sbin/userdel"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ):
            resp = asgi_request(
                api.app,
                "DELETE",
                "/api/system/users/olduser?remove_home=true",
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["removed_home"])

    def test_group_create_and_delete_protection(self):
        # Delete protected group root
        resp_del_root = asgi_request(
            api.app,
            "DELETE",
            "/api/system/groups/root",
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_del_root.status_code, 403)

        # Create group
        with (
            patch("shutil.which", return_value="/usr/sbin/groupadd"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ):
            resp_cg = asgi_post(
                api.app,
                "/api/system/groups",
                json_body={"name": "testgroup"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp_cg.status_code, 200)

        # Membership action
        with (
            patch("shutil.which", side_effect=lambda cmd: "/usr/bin/gpasswd" if cmd == "gpasswd" else None),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ):
            resp_mem = asgi_post(
                api.app,
                "/api/system/groups/testgroup/members",
                json_body={"action": "add", "username": "testuser"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp_mem.status_code, 200)
            self.assertEqual(resp_mem.json()["action"], "add")


class TestAuditLoggingEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_record_and_get_audit_logs(self):
        main.record_audit_event("auth.test", "Test description 1", actor="admin", severity="info")
        main.record_audit_event("service.action", "Test description 2", actor="admin", severity="warning")
        main.record_audit_event("system.user.create", "User bob created", actor="alice", severity="info")

        resp = asgi_get(api.app, "/api/audit/logs", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["logs"]), 3)

        # Filter by severity
        resp_sev = asgi_get(api.app, "/api/audit/logs?severity=warning", cookies=self.auth_cookies)
        self.assertEqual(resp_sev.status_code, 200)
        self.assertEqual(resp_sev.json()["total"], 1)
        self.assertEqual(resp_sev.json()["logs"][0]["event_type"], "service.action")

        # Search query
        resp_q = asgi_get(api.app, "/api/audit/logs?q=bob", cookies=self.auth_cookies)
        self.assertEqual(resp_q.status_code, 200)
        self.assertEqual(resp_q.json()["total"], 1)
        self.assertEqual(resp_q.json()["logs"][0]["event_type"], "system.user.create")

    def test_auth_failure_and_success_logs_audit_event(self):
        # Failed login
        resp_fail = asgi_post(api.app, "/api/auth/login", json_body={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(resp_fail.status_code, 401)

        logs, total = main.get_audit_logs(event_type="auth.login.failure")
        self.assertGreaterEqual(total, 1)
        self.assertEqual(logs[0]["actor"], "admin")
        self.assertEqual(logs[0]["severity"], "warning")

        # Successful login
        resp_ok = asgi_post(api.app, "/api/auth/login", json_body={"username": "admin", "password": "adminpass"})
        self.assertEqual(resp_ok.status_code, 200)

        logs_ok, total_ok = main.get_audit_logs(event_type="auth.login.success")
        self.assertGreaterEqual(total_ok, 1)
        self.assertEqual(logs_ok[0]["actor"], "admin")
        self.assertEqual(logs_ok[0]["severity"], "info")

    def test_settings_update_logs_audit_event(self):
        resp = asgi_post(
            api.app,
            "/api/settings",
            json_body={"channel_id": "999888777"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp.status_code, 200)

        logs, total = main.get_audit_logs(event_type="settings.update")
        self.assertGreaterEqual(total, 1)
        self.assertIn("channel_id", logs[0]["description"])
        self.assertEqual(logs[0]["actor"], "admin")

    def test_get_audit_logs_error_handling(self):
        with patch.object(main, "get_audit_logs", side_effect=RuntimeError("audit db error")):
            resp = asgi_get(api.app, "/api/audit/logs", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Error fetching audit logs", resp.json()["detail"])


class TestPackageManagerBackendAndEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_package_manager_support_detection(self):
        pm = main.PackageManager()
        with patch("shutil.which", return_value=None):
            self.assertFalse(pm.is_supported())
        with patch("shutil.which", return_value="/usr/bin/dpkg"):
            self.assertTrue(pm.is_supported())

    def test_reboot_status_detection(self):
        pm = main.PackageManager()
        with patch("os.path.exists", return_value=False):
            status = pm.get_reboot_status()
            self.assertFalse(status["reboot_required"])
            self.assertEqual(status["packages"], [])

        def fake_exists(path):
            return str(path) in ("/run/reboot-required", "/run/reboot-required.pkgs")

        mock_content = "linux-image-6.8.0-generic\nlibc6\n"
        with patch("os.path.exists", side_effect=fake_exists), \
             patch("builtins.open", mock_open(read_data=mock_content)):
            status = pm.get_reboot_status()
            self.assertTrue(status["reboot_required"])
            self.assertIn("linux-image-6.8.0-generic", status["packages"])
            self.assertIn("libc6", status["packages"])

    def test_load_installed_packages(self):
        pm = main.PackageManager()
        dpkg_output = (
            "curl\t7.81.0-1ubuntu1.15\tamd64\tinstall ok installed\tcommand line tool for transferring data\n"
            "vim\t2:8.2.3995-1ubuntu2\tamd64\tinstall ok installed\tVi IMproved - enhanced vi editor\n"
            "old-pkg\t1.0.0\tamd64\tdeinstall ok config-files\tremoved package\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=dpkg_output)
            packages = pm._load_installed_packages()
            self.assertEqual(len(packages), 2)
            self.assertEqual(packages[0]["name"], "curl")
            self.assertEqual(packages[0]["version"], "7.81.0-1ubuntu1.15")
            self.assertEqual(packages[0]["architecture"], "amd64")
            self.assertEqual(packages[1]["name"], "vim")

    def test_get_installed_packages_filtering_and_pagination(self):
        pm = main.PackageManager()
        sample_packages = [
            {"name": f"pkg-{i:03d}", "version": f"1.{i}.0", "architecture": "amd64", "status": "installed", "summary": f"Sample summary {i}"}
            for i in range(120)
        ]
        with patch.object(pm, "_load_installed_packages", return_value=sample_packages):
            # Page 1 (limit 50)
            res1 = pm.get_installed_packages(limit=50, page=1)
            self.assertEqual(res1["total"], 120)
            self.assertEqual(len(res1["packages"]), 50)
            self.assertEqual(res1["page"], 1)
            self.assertEqual(res1["packages"][0]["name"], "pkg-000")

            # Page 3 (items 100-119)
            res3 = pm.get_installed_packages(limit=50, page=3)
            self.assertEqual(res3["total"], 120)
            self.assertEqual(len(res3["packages"]), 20)
            self.assertEqual(res3["page"], 3)
            self.assertEqual(res3["packages"][0]["name"], "pkg-100")

            # Search filter
            res_filtered = pm.get_installed_packages(query="042", limit=50, page=1)
            self.assertEqual(res_filtered["total"], 1)
            self.assertEqual(res_filtered["packages"][0]["name"], "pkg-042")

    def test_get_available_updates_parsing(self):
        pm = main.PackageManager()
        apt_dist_output = (
            "Reading package lists... Done\n"
            "Building dependency tree... Done\n"
            "Inst curl [7.81.0-1ubuntu1.15] (7.81.0-1ubuntu1.16 Ubuntu:22.04/jammy-updates [amd64])\n"
            "Inst libssl3 [3.0.2-0ubuntu1.14] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])\n"
            "Conf curl (7.81.0-1ubuntu1.16 Ubuntu:22.04/jammy-updates [amd64])\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=apt_dist_output)
            res = pm.get_available_updates(force=True)
            self.assertEqual(res["total_updates"], 2)
            self.assertEqual(res["security_updates"], 1)
            self.assertEqual(res["standard_updates"], 1)
            self.assertEqual(len(res["updates"]), 2)

            curl_up = next(u for u in res["updates"] if u["package"] == "curl")
            self.assertFalse(curl_up["is_security"])
            self.assertEqual(curl_up["current_version"], "7.81.0-1ubuntu1.15")
            self.assertEqual(curl_up["candidate_version"], "7.81.0-1ubuntu1.16")

            ssl_up = next(u for u in res["updates"] if u["package"] == "libssl3")
            self.assertTrue(ssl_up["is_security"])

    def test_get_available_updates_caching(self):
        pm = main.PackageManager()
        apt_dist_output = (
            "Reading package lists... Done\n"
            "Inst curl [7.81.0-1ubuntu1.15] (7.81.0-1ubuntu1.16 Ubuntu:22.04/jammy-updates [amd64])\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=apt_dist_output)
            # First call populates cache
            res1 = pm.get_available_updates(force=False)
            self.assertEqual(res1["total_updates"], 1)
            self.assertEqual(mock_run.call_count, 1)

            # Second call should use cache, no subprocess.run call
            res2 = pm.get_available_updates(force=False)
            self.assertEqual(res2["total_updates"], 1)
            self.assertEqual(mock_run.call_count, 1)

            # Force=True should bypass cache and invoke subprocess.run
            res3 = pm.get_available_updates(force=True)
            self.assertEqual(res3["total_updates"], 1)
            self.assertEqual(mock_run.call_count, 2)

            # Invalidate cache
            pm.invalidate_cache()
            res4 = pm.get_available_updates(force=False)
            self.assertEqual(mock_run.call_count, 3)

    def test_get_updates_summary_caching_and_inspection(self):
        pm = main.PackageManager()

        # 1. Unsupported system
        with patch.object(pm, "is_supported", return_value=False):
            summary = pm.get_updates_summary()
            self.assertFalse(summary["supported"])
            self.assertEqual(summary["total"], 0)
            self.assertEqual(summary["security_count"], 0)
            self.assertFalse(summary["reboot_required"])

        # 2. Uses cache if available
        with patch.object(pm, "is_supported", return_value=True), \
             patch.object(pm, "get_reboot_status", return_value={"reboot_required": False, "packages": []}):
            with pm._cache_lock:
                pm._updates_cache = (
                    time.time(),
                    {"total_updates": 7, "security_updates": 3, "updates": []},
                )
            cached_sum = pm.get_updates_summary()
            self.assertTrue(cached_sum["supported"])
            self.assertEqual(cached_sum["total"], 7)
            self.assertEqual(cached_sum["security_count"], 3)
            self.assertFalse(cached_sum["reboot_required"])

        # 3. Fast inspection from notifier file if cache is None
        pm.invalidate_cache()
        notifier_data = "12 updates can be applied immediately.\n4 of these updates are standard security updates.\n"
        with patch.object(pm, "is_supported", return_value=True), \
             patch.object(pm, "get_reboot_status", return_value={"reboot_required": True, "packages": ["linux-image"]}), \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.read_text", return_value=notifier_data):
            file_sum = pm.get_updates_summary()
            self.assertTrue(file_sum["supported"])
            self.assertEqual(file_sum["total"], 12)
            self.assertEqual(file_sum["security_count"], 4)
            self.assertTrue(file_sum["reboot_required"])

        # 4. Fallback when no cache and no notifier file
        pm.invalidate_cache()
        with patch.object(pm, "is_supported", return_value=True), \
             patch.object(pm, "get_reboot_status", return_value={"reboot_required": False, "packages": []}), \
             patch("pathlib.Path.is_file", return_value=False):
            empty_sum = pm.get_updates_summary()
            self.assertTrue(empty_sum["supported"])
            self.assertEqual(empty_sum["total"], 0)
            self.assertEqual(empty_sum["security_count"], 0)
            self.assertFalse(empty_sum["reboot_required"])

    def test_run_upgrade_dry_run_and_sanitization(self):
        pm = main.PackageManager()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Simulated upgrade OK\n", stderr="")
            res = pm.run_upgrade(packages=["curl"], dry_run=True)
            self.assertTrue(res["success"])
            self.assertTrue(res["dry_run"])
            self.assertIn("Simulated upgrade OK", res["output"])

        # Test injection / invalid package name sanitization
        with self.assertRaises(ValueError):
            pm.run_upgrade(packages=["curl; rm -rf /"], dry_run=True)

        # Test API endpoint returns 400 for invalid package name
        api_bad = asgi_post(api.app, "/api/packages/upgrade", json_body={"packages": ["curl; rm -rf /"]}, cookies=self.auth_cookies)
        self.assertEqual(api_bad.status_code, 400)
        self.assertIn("Invalid package name", api_bad.json()["detail"])

    def test_run_upgrade_real_installation_and_audit(self):
        pm = main.PackageManager()
        with pm._cache_lock:
            pm._updates_cache = (time.time(), {"total_updates": 2, "updates": []})
            pm._installed_cache = (time.time(), [{"name": "curl"}])

        # 1. Real upgrade with dry_run=False passes Dpkg::Options and DEBIAN_FRONTEND=noninteractive
        with (
            patch.object(pm, "is_supported", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Unpacking curl...\nSetting up curl...\n", stderr=""
            )
            res = pm.run_upgrade(packages=["curl"], dry_run=False)
            self.assertTrue(res["success"])
            self.assertFalse(res["dry_run"])
            self.assertIn("Setting up curl", res["output"])
            self.assertIsNone(pm._updates_cache)
            self.assertIsNone(pm._installed_cache)

            mock_run.assert_called_once()
            cmd_arg = mock_run.call_args[0][0]
            self.assertEqual(
                cmd_arg,
                [
                    "apt-get",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confdef",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    "install",
                    "--only-upgrade",
                    "curl",
                ],
            )
            kwargs = mock_run.call_args[1]
            self.assertEqual(kwargs.get("timeout"), 300)
            self.assertEqual(kwargs.get("env", {}).get("DEBIAN_FRONTEND"), "noninteractive")

        # 2. Retry with sudo -n when permission denied
        with (
            patch.object(pm, "is_supported", return_value=True),
            patch("shutil.which", return_value="/usr/bin/sudo"),
            patch("subprocess.run") as mock_run_sudo,
        ):
            mock_run_sudo.side_effect = [
                MagicMock(
                    returncode=100,
                    stdout="",
                    stderr="E: Could not open lock file /var/lib/dpkg/lock-frontend - open (13: Permission denied)",
                ),
                MagicMock(returncode=0, stdout="Upgraded via sudo\n", stderr=""),
            ]
            res_sudo = pm.run_upgrade(packages=None, dry_run=False)
            self.assertTrue(res_sudo["success"])
            self.assertEqual(mock_run_sudo.call_count, 2)
            second_cmd = mock_run_sudo.call_args_list[1][0][0]
            self.assertEqual(
                second_cmd[:4],
                ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive"],
            )
            self.assertIn("dist-upgrade", second_cmd)

        # 3. POST /api/packages/upgrade with dry_run=False logs audit event
        with patch.object(
            main.package_manager,
            "run_upgrade",
            return_value={
                "success": True,
                "dry_run": False,
                "command": "apt-get -y install --only-upgrade curl",
                "output": "Upgraded curl",
                "error": "",
                "returncode": 0,
            },
        ) as mock_upg:
            resp = asgi_post(
                api.app,
                "/api/packages/upgrade",
                json_body={"packages": ["curl"], "dry_run": False},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])
            mock_upg.assert_called_once_with(packages=["curl"], dry_run=False)

            logs, total = main.get_audit_logs(event_type="packages.upgrade")
            self.assertGreaterEqual(total, 1)
            self.assertEqual(logs[0]["actor"], "admin")
            self.assertEqual(logs[0]["severity"], "info")
            self.assertIn("curl", logs[0]["description"])

    def test_api_package_endpoints_and_status(self):
        # 1. Status includes reboot_status and updates_summary
        status_resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
        self.assertEqual(status_resp.status_code, 200)
        status_data = status_resp.json()
        self.assertIn("reboot_status", status_data)
        self.assertIn("updates_summary", status_data)
        self.assertIn("supported", status_data["updates_summary"])
        self.assertIn("total", status_data["updates_summary"])
        self.assertIn("security_count", status_data["updates_summary"])
        self.assertIn("reboot_required", status_data["updates_summary"])

        # 2. GET /api/packages/summary
        with patch.object(main.package_manager, "get_updates_summary", return_value={
            "supported": True,
            "total": 3,
            "security_count": 1,
            "reboot_required": False,
        }):
            summary_resp = asgi_get(api.app, "/api/packages/summary", cookies=self.auth_cookies)
            self.assertEqual(summary_resp.status_code, 200)
            sum_data = summary_resp.json()
            self.assertTrue(sum_data["supported"])
            self.assertEqual(sum_data["total"], 3)
            self.assertEqual(sum_data["security_count"], 1)
            self.assertFalse(sum_data["reboot_required"])

        # 2b. GET /api/packages/summary requires auth
        unauth_resp = asgi_get(api.app, "/api/packages/summary")
        self.assertEqual(unauth_resp.status_code, 401)

        # 3. GET /api/packages/reboot
        reboot_resp = asgi_get(api.app, "/api/packages/reboot", cookies=self.auth_cookies)
        self.assertEqual(reboot_resp.status_code, 200)
        self.assertIn("reboot_required", reboot_resp.json())

        # 4. GET /api/packages/updates
        with patch.object(main.package_manager, "get_available_updates", return_value={
            "supported": True,
            "total_updates": 1,
            "security_updates": 0,
            "standard_updates": 1,
            "updates": [{"package": "curl", "current_version": "1.0", "candidate_version": "1.1", "is_security": False, "origin": "apt"}],
            "reboot_status": {"reboot_required": False, "packages": []},
            "checked_at": "2026-10-31T12:00:00Z"
        }):
            updates_resp = asgi_get(api.app, "/api/packages/updates", cookies=self.auth_cookies)
            self.assertEqual(updates_resp.status_code, 200)
            self.assertEqual(updates_resp.json()["total_updates"], 1)

        # 4. GET /api/packages/installed
        with patch.object(main.package_manager, "get_installed_packages", return_value={
            "supported": True,
            "total": 42,
            "page": 1,
            "limit": 50,
            "packages": [{"name": "vim", "version": "9.0", "architecture": "amd64", "status": "installed", "summary": "editor"}]
        }):
            inst_resp = asgi_get(api.app, "/api/packages/installed?q=vim", cookies=self.auth_cookies)
            self.assertEqual(inst_resp.status_code, 200)
            self.assertEqual(inst_resp.json()["total"], 42)

        # 5. POST /api/packages/refresh
        with patch.object(main.package_manager, "refresh_repositories", return_value={"success": True, "output": "Fetched 10MB"}):
            ref_resp = asgi_post(api.app, "/api/packages/refresh", cookies=self.auth_cookies)
            self.assertEqual(ref_resp.status_code, 200)
            self.assertTrue(ref_resp.json()["success"])

        # 6. POST /api/packages/upgrade
        with patch.object(main.package_manager, "run_upgrade", return_value={"success": True, "dry_run": True, "output": "Done", "returncode": 0}):
            upg_resp = asgi_post(api.app, "/api/packages/upgrade", json_body={"packages": ["curl"], "dry_run": True}, cookies=self.auth_cookies)
            self.assertEqual(upg_resp.status_code, 200)
            self.assertTrue(upg_resp.json()["success"])

    def test_dom_elements_present(self):
        index_html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="nav-tab-packages"', index_html)
        self.assertIn('id="view-packages"', index_html)
        self.assertIn('id="pkg-reboot-banner"', index_html)
        self.assertIn('id="subtab-pkg-updates-btn"', index_html)
        self.assertIn('id="subtab-pkg-installed-btn"', index_html)
        self.assertIn('id="pkg-updates-table"', index_html)
        self.assertIn('id="pkg-installed-table"', index_html)
        self.assertIn('id="pkg-upgrade-modal"', index_html)
        self.assertIn('id="overview-packages-badge"', index_html)

    def test_spa_packages_routes(self):
        resp_pkg = asgi_get(api.app, "/packages")
        self.assertEqual(resp_pkg.status_code, 200)
        self.assertIn("text/html", resp_pkg.headers.get("content-type", ""))

        resp_upd = asgi_get(api.app, "/updates")
        self.assertEqual(resp_upd.status_code, 200)
        self.assertIn("text/html", resp_upd.headers.get("content-type", ""))

    def test_frontend_functions_defined(self):
        app_js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function formatRelativeTime(", app_js)
        self.assertIn("function fetchPackageUpdates(", app_js)
        self.assertIn("function fetchInstalledPackages(", app_js)
        # Preloading checks
        self.assertIn("fetchDockerUpdates({ background: true });", app_js)
        self.assertIn("fetchInstalledPackages(\"\", 1);", app_js)


class TestModernizedUIContractsAndTelemetry(unittest.TestCase):
    """Test invariants for modern UI contracts, unified cards, and network telemetry."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

        self.index_html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.app_js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_notifications_tab_modernization_contracts(self):
        # 1. Modals exist
        self.assertIn('id="modal-notif-channels"', self.index_html)
        self.assertIn('id="modal-notif-schedule"', self.index_html)
        self.assertIn('id="modal-notif-thresholds"', self.index_html)

        # 2. Control ribbon exists in notifications view
        self.assertIn('class="notif-control-ribbon"', self.index_html)
        self.assertIn('id="btn-open-notif-channels"', self.index_html)
        self.assertIn('id="btn-open-notif-schedule"', self.index_html)
        self.assertIn('id="btn-open-notif-thresholds"', self.index_html)

        # 3. Notification audit history table prominently displayed
        self.assertIn('id="notification-history-table"', self.index_html)
        self.assertIn('id="notification-history-tbody"', self.index_html)
        self.assertIn('id="history-count-badge"', self.index_html)

        # 4. Form inputs preserved for settings compatibility
        preserved_inputs = [
            'id="notif-channel-discord-enable"',
            'id="discord-enabled-toggle"',
            'id="notif-discord-token-input"',
            'id="discord-token-input"',
            'id="notif-discord-channel-input"',
            'id="discord-channel-input"',
            'id="notif-channel-telegram-enable"',
            'id="notif-telegram-token-input"',
            'id="notif-telegram-chat-id-input"',
            'id="notif-channel-webhook-enable"',
            'id="notif-webhook-url-input"',
            'id="notif-schedule-enable"',
            'id="notif-schedule-freq"',
            'id="notif-schedule-time"',
            'id="notif-topic-system"',
            'id="notif-topic-disks"',
            'id="notif-topic-raid"',
            'id="notif-threshold-ram-slider"',
            'id="notif-threshold-ram-value"',
            'id="notif-threshold-cpu-slider"',
            'id="notif-threshold-cpu-value"',
            'id="notif-threshold-disk-slider"',
            'id="notif-threshold-disk-value"',
            'id="notif-threshold-raid-enable"',
        ]
        for input_id in preserved_inputs:
            self.assertIn(input_id, self.index_html)

    def test_packages_refresh_button_icon_contract(self):
        # Verify #btn-refresh-packages has class="btn-module-refresh" and refresh-icon SVG
        self.assertIn('id="btn-refresh-packages" class="btn-module-refresh"', self.index_html)
        self.assertIn('svg class="refresh-icon"', self.index_html)

    def test_storage_tab_unified_array_and_disks(self):
        # Unified card RAID Array & Member Disks
        self.assertIn('class="card raid-details-card"', self.index_html)
        self.assertIn('RAID Array &amp; Member Disks', self.index_html)
        self.assertIn('id="active-disks-table"', self.index_html)
        self.assertIn('id="active-disks-tbody"', self.index_html)
        self.assertIn('id="raid-card-badge"', self.index_html)
        self.assertIn('id="raid-active-devices"', self.index_html)
        self.assertIn('id="raid-working-devices"', self.index_html)
        self.assertIn('id="raid-failed-devices"', self.index_html)
        self.assertIn('id="raid-spare-devices"', self.index_html)

        # Storage view toolbar: live streaming pulse dot replaces redundant boost badge & interval select
        raid_view_start = self.index_html.find('id="view-raid"')
        raid_view_end = self.index_html.find('id="view-containers"')
        self.assertGreater(raid_view_start, -1)
        self.assertGreater(raid_view_end, raid_view_start)
        storage_toolbar = self.index_html[raid_view_start:raid_view_end]
        self.assertIn('<span class="module-status-dot" title="Live stream active"></span>', storage_toolbar)
        self.assertNotIn('id="raid-adaptive-badge"', self.index_html)
        self.assertNotIn('id="raid-interval-select"', self.index_html)

        # RAID details list: UUID, Layout / Chunk, Consistency Policy present; Update Time removed
        self.assertIn('id="raid-uuid"', storage_toolbar)
        self.assertIn('id="raid-layout-chunk"', storage_toolbar)
        self.assertIn('id="raid-consistency-policy"', storage_toolbar)
        self.assertNotIn('id="raid-update-time"', self.index_html)
        self.assertNotIn('<dt>Update Time</dt>', storage_toolbar)

        # Frontend JS contract: app.js formatting and backwards compatibility
        self.assertIn('raid.uuid && raid.uuid !== "unknown" ? raid.uuid : "N/A"', self.app_js)
        self.assertIn('layoutChunkVal = "N/A (Mirrored)";', self.app_js)
        self.assertIn('raid.layout} / ${raid.chunk_size}', self.app_js)
        self.assertIn('raid.consistency_policy && raid.consistency_policy !== "unknown"', self.app_js)
        self.assertIn('setField("raid-consistency-policy", consistencyPolicyVal);', self.app_js)
        self.assertIn('setField("raid-consistency-events", consistencyPolicyVal);', self.app_js)


    def test_overview_tab_overhaul_invariants(self):
        # Jump buttons removed from visible ribbon (hidden)
        jump_btns = [
            'id="btn-goto-raid" type="button" class="btn-ribbon-nav hidden"',
            'id="btn-goto-containers" type="button" class="btn-ribbon-nav hidden"',
            'id="btn-goto-notifications" type="button" class="btn-ribbon-nav hidden"',
            'id="btn-goto-services" type="button" class="btn-ribbon-nav hidden"',
            'id="btn-goto-packages" type="button" class="btn-ribbon-nav hidden"',
        ]
        for btn in jump_btns:
            self.assertIn(btn, self.index_html)

        # CPU Telemetry: collapsible button hidden, drilldown always visible
        self.assertIn('id="btn-toggle-cpu-cores" class="btn-icon-subtle hidden"', self.index_html)
        self.assertIn('id="cpu-cores-drilldown" class="cpu-cores-drilldown"', self.index_html)
        self.assertIn('id="cpu-cores-grid"', self.index_html)

        # Redundant storage metric card is hidden
        self.assertIn('id="storage-metric-card" style="display: none;"', self.index_html)

        # Network Telemetry in Host Platform card
        net_elements = [
            'id="host-network-interface"',
            'id="host-network-ip"',
            'id="host-network-rx"',
            'id="host-network-tx"',
            'id="host-network-status"',
        ]
        for elem in net_elements:
            self.assertIn(elem, self.index_html)

    def test_overview_page_redesign_layout_and_chromeless_invariants(self):
        """Assert full cross-tier contracts and CSS invariants for Overview redesign.
        
        Requirements:
        1. No container/card chrome on overview telemetry objects (CPU, Memory, Host Platform, Ribbon).
        2. Cohesive flow layout without bucketed section chrome.
        3. No internal scrollbar in .cpu-cores-grid (all cores displayed without overflow scrolling).
        4. No text truncation in overview status ribbon descriptions (fully readable).
        5. Balanced 2-column desktop grid collapsing cleanly on responsive viewports.
        6. Full preservation of all 58+ DOM IDs and hidden storage card contract.
        """
        css_text = (Path(__file__).parent / "static" / "styles.css").read_text(encoding="utf-8")

        # 1. Flow container & header structure in HTML
        self.assertIn('class="overview-flow"', self.index_html)
        self.assertIn('class="overview-inline-toolbar"', self.index_html)
        self.assertIn('id="system-metrics-heading" class="sr-only"', self.index_html)
        self.assertIn('class="metrics-grid overview-flow-grid"', self.index_html)

        # 2. Absence of card chrome on overview metric cards in HTML (no .card class)
        self.assertIn('<article class="metric-card" id="cpu-metric-card">', self.index_html)
        self.assertNotIn('<article class="card metric-card" id="cpu-metric-card">', self.index_html)
        self.assertIn('<article class="metric-card" id="memory-metric-card">', self.index_html)
        self.assertNotIn('<article class="card metric-card" id="memory-metric-card">', self.index_html)
        self.assertIn('<article class="metric-card" id="host-platform-card">', self.index_html)
        self.assertNotIn('<article class="card metric-card" id="host-platform-card">', self.index_html)

        # 3. Flat Status Ribbon (no card chrome)
        self.assertIn('class="overview-status-ribbon overview-cockpit-grid overview-ribbon-flat"', self.index_html)

        # 4. Storage card preserved with hidden class and style for contract compatibility
        self.assertIn('id="storage-metric-card"', self.index_html)
        self.assertIn('class="card metric-card hidden" id="storage-metric-card" style="display: none;" aria-hidden="true"', self.index_html)

        # 5. CSS Invariants: Chrome-less styling for overview telemetry objects
        self.assertIn(".overview-flow-grid > .metric-card", css_text)
        metric_card_rule = re.search(r'\.overview-flow-grid > \.metric-card\s*\{([^}]+)\}', css_text)
        self.assertIsNotNone(metric_card_rule, ".overview-flow-grid > .metric-card rule must be defined in CSS")
        rule_content = metric_card_rule.group(1)
        self.assertIn("background: transparent;", rule_content)
        self.assertIn("border: none;", rule_content)
        self.assertIn("box-shadow: none;", rule_content)

        ribbon_rule = re.search(r'\.overview-status-ribbon\.overview-ribbon-flat\s*\{([^}]+)\}', css_text)
        self.assertIsNotNone(ribbon_rule, ".overview-status-ribbon.overview-ribbon-flat rule must be defined in CSS")
        ribbon_content = ribbon_rule.group(1)
        self.assertIn("background: transparent;", ribbon_content)
        self.assertIn("box-shadow: none;", ribbon_content)

        # 6. CSS Invariants: Status ribbon description readability (no ellipsis / no overflow hidden)
        ribbon_desc_rule = re.search(r'\.overview-ribbon-flat \.status-ribbon-desc\s*\{([^}]+)\}', css_text)
        self.assertIsNotNone(ribbon_desc_rule, ".overview-ribbon-flat .status-ribbon-desc rule must be defined")
        desc_content = ribbon_desc_rule.group(1)
        self.assertIn("white-space: normal;", desc_content)
        self.assertIn("overflow: visible;", desc_content)
        self.assertIn("text-overflow: unset;", desc_content)

        # 7. CSS Invariants: No internal scrollbar in .cpu-cores-grid
        cores_grid_rule = re.search(r'(?<![#.-])\.cpu-cores-grid\s*\{([^}]+)\}', css_text)
        self.assertIsNotNone(cores_grid_rule, ".cpu-cores-grid rule must be defined")
        grid_content = cores_grid_rule.group(1)
        self.assertNotIn("overflow-y", grid_content, ".cpu-cores-grid must not have internal scrollbar overflow-y")
        self.assertNotIn("max-height", grid_content, ".cpu-cores-grid must not constrain height with max-height")

        # 8. CSS Invariants: Full-width stacked linear flow layout
        self.assertIn(".metrics-grid.overview-flow-grid", css_text)
        self.assertIn(".overview-flow-grid > #cpu-metric-card", css_text)
        self.assertIn(".overview-flow-grid > #memory-metric-card", css_text)
        self.assertIn(".overview-flow-grid > #host-platform-card", css_text)
        self.assertIn(".overview-flow-grid > #storage-metric-card", css_text)
        self.assertIn("display: none !important;", css_text)

        # 9. All 59 Overview DOM IDs preserved in index.html
        overview_dom_ids = [
            "system-metrics-heading", "system-last-updated", "refresh-system-btn",
            "overview-raid-status-badge", "overview-raid-summary", "btn-goto-raid",
            "overview-docker-badge", "overview-docker-summary", "btn-goto-containers",
            "overview-discord-badge", "overview-discord-summary", "btn-goto-notifications",
            "overview-services-badge", "overview-services-summary", "btn-goto-services",
            "overview-packages-badge", "overview-packages-summary", "btn-goto-packages",
            "cpu-metric-card", "cpu-cores-badge", "cpu-percent-value", "cpu-progress-bar",
            "cpu-load-detail", "cpu-subtitle", "btn-toggle-cpu-cores", "cpu-cores-drilldown",
            "cpu-cores-unified-canvas", "cpu-cores-grid",
            "memory-metric-card", "memory-percent-value", "memory-progress-bar",
            "memory-chart-container", "memory-history-chart", "memChartGrad",
            "memory-chart-area", "memory-chart-line", "memory-detail",
            "memory-breakdown-chips", "memory-chip-used", "memory-chip-cached", "memory-chip-free",
            "storage-metric-card", "disk-percent-value", "disk-detail", "disk-progress-bar",
            "btn-goto-storage", "overview-mounts-container", "overview-mounts-list",
            "host-platform-card", "uptime-value", "uptime-load-badge", "host-os-detail",
            "host-kernel-detail", "boot-time-detail", "host-network-status",
            "host-network-interface", "host-network-ip", "host-network-rx", "host-network-tx",
        ]
        self.assertEqual(len(overview_dom_ids), 59)
        for dom_id in overview_dom_ids:
            self.assertIn(f'id="{dom_id}"', self.index_html, f"Expected DOM element id '{dom_id}' to be present in index.html")

    def test_windows_task_manager_style_cpu_per_core_charts(self):
        """Verify Windows Task Manager style rolling time-series graph for CPU cores."""
        css_text = (Path(__file__).parent / "static" / "styles.css").read_text(encoding="utf-8")
        
        # 1. HTML DOM Structure: #cpu-cores-unified-canvas inside #cpu-cores-drilldown
        self.assertIn('id="cpu-cores-drilldown"', self.index_html)
        self.assertIn('id="cpu-cores-unified-canvas"', self.index_html)
        drilldown_pos = self.index_html.find('id="cpu-cores-drilldown"')
        canvas_pos = self.index_html.find('id="cpu-cores-unified-canvas"')
        cores_grid_pos = self.index_html.find('id="cpu-cores-grid"')
        self.assertGreater(drilldown_pos, -1)
        self.assertGreater(canvas_pos, drilldown_pos)
        self.assertGreater(cores_grid_pos, canvas_pos)

        # 2. CPU Core Grid elements and indicators
        self.assertIn("cpu-core-item", self.app_js)
        self.assertIn("cpu-core-label", self.app_js)
        self.assertIn("cpu-core-percent", self.app_js)
        self.assertIn("cpu-core-track", self.app_js)
        self.assertIn("cpu-core-color-dot", self.app_js)

        # 3. JS Invariants: buffer, limit, drawing function, multi-core buffers, colors, devicePixelRatio, hover highlighting
        self.assertIn("CPU_CORE_HISTORY_LIMIT", self.app_js)
        self.assertIn("CPU_CORE_COLORS", self.app_js)
        self.assertIn("cpuCoresHistoryBuffers", self.app_js)
        self.assertIn("function drawCpuCoreGraph(", self.app_js)
        self.assertIn("devicePixelRatio", self.app_js)
        self.assertIn("activeHoverIndex", self.app_js)
        self.assertIn("isEmphasized", self.app_js)
        self.assertIn("isDimmed", self.app_js)

        # 4. CSS Invariants
        self.assertIn(".cpu-unified-chart-container", css_text)
        self.assertIn(".cpu-unified-chart-canvas", css_text)
        self.assertIn(".cpu-core-canvas", css_text)
        self.assertIn(".cpu-core-item", css_text)
        self.assertIn(".cpu-core-header", css_text)
        self.assertIn(".cpu-core-color-dot", css_text)
        self.assertIn(".cpu-core-track", css_text)

    def test_slide_over_notification_drawer_contracts(self):
        """Verify Slide-Over Notification Drawer markup, topbar trigger, and handlers."""
        # Topbar trigger & badge
        self.assertIn('id="btn-topbar-notifications"', self.index_html)
        self.assertIn('id="notif-drawer-badge"', self.index_html)

        # Drawer container & backdrop
        self.assertIn('id="notification-drawer"', self.index_html)
        self.assertIn('id="notification-drawer-backdrop"', self.index_html)

        # Drawer header & controls
        self.assertNotIn('id="drawer-notif-count"', self.index_html)
        self.assertIn('id="btn-drawer-refresh-history"', self.index_html)
        self.assertIn('id="btn-close-notification-drawer"', self.index_html)
        self.assertIn('id="btn-drawer-dismiss-all"', self.index_html)

        # Navigation links to Notification Center (replaces redundant modals)
        self.assertIn('id="btn-drawer-goto-notifications"', self.index_html)
        self.assertIn('id="btn-drawer-footer-settings"', self.index_html)

        # Redundant modal buttons and status strip are removed from drawer
        self.assertNotIn('id="btn-drawer-channels"', self.index_html)
        self.assertNotIn('id="btn-drawer-schedule"', self.index_html)
        self.assertNotIn('id="btn-drawer-thresholds"', self.index_html)
        self.assertNotIn('id="btn-drawer-send-test"', self.index_html)
        self.assertNotIn('id="drawer-discord-status"', self.index_html)
        self.assertNotIn('id="drawer-threshold-summary"', self.index_html)
        self.assertNotIn('id="drawer-schedule-summary"', self.index_html)

        # Live scrollable stream list and category filter tabs
        self.assertIn('id="drawer-notification-list"', self.index_html)
        self.assertIn('id="drawer-tab-all"', self.index_html)
        self.assertIn('id="drawer-tab-alerts"', self.index_html)
        self.assertIn('id="drawer-tab-app"', self.index_html)

        # JS handlers & functions
        self.assertIn("openNotificationDrawer", self.app_js)
        self.assertIn("closeNotificationDrawer", self.app_js)
        self.assertIn("renderDrawerNotificationList", self.app_js)
        self.assertIn("fetchNotificationHistoryDrawer", self.app_js)
        self.assertIn("btn-drawer-goto-notifications", self.app_js)
        self.assertIn("btn-drawer-footer-settings", self.app_js)

        # Topbar opens drawer; Sidebar nav-tab only navigates to view without opening drawer
        self.assertIn('"btn-topbar-notifications"', self.app_js)
        self.assertIn('"nav-tab-notifications"', self.app_js)
        nav_tab_idx = self.app_js.rfind('"nav-tab-notifications"')
        self.assertGreater(nav_tab_idx, -1)
        nav_tab_snippet = self.app_js[nav_tab_idx:nav_tab_idx + 150]
        self.assertIn('switchViewTab("notifications", true)', nav_tab_snippet)
        self.assertNotIn("openNotificationDrawer", nav_tab_snippet)

    def test_system_update_notice_bar_and_polling_contracts(self):
        """Verify dismissible system update notice bar, package nav badges, and background polling."""
        # HTML Elements
        self.assertIn('id="system-update-notice-bar"', self.index_html)
        self.assertIn('id="update-indicator-badge"', self.index_html)
        self.assertIn('id="update-notice-text"', self.index_html)
        self.assertIn('id="system-update-notice-message"', self.index_html)
        self.assertIn('id="btn-notice-view-updates"', self.index_html)
        self.assertIn('id="btn-dismiss-update-notice"', self.index_html)
        self.assertIn('id="nav-badge-packages"', self.index_html)

        # JS Polling & Logic
        self.assertIn("/api/packages/summary", self.app_js)
        self.assertIn("pollSystemUpdates", self.app_js)
        self.assertIn("SYSTEM_UPDATE_POLL_INTERVAL_MS", self.app_js)
        self.assertIn("renderSystemUpdateNotice", self.app_js)

        # Backend endpoints contract: /api/packages/summary and /api/status updates_summary
        status_resp = asgi_get(api.app, "/api/status", cookies=self.auth_cookies)
        self.assertEqual(status_resp.status_code, 200)
        status_data = status_resp.json()
        self.assertIn("updates_summary", status_data)
        self.assertIn("total", status_data["updates_summary"])
        self.assertIn("security_count", status_data["updates_summary"])
        self.assertIn("reboot_required", status_data["updates_summary"])

        summary_resp = asgi_get(api.app, "/api/packages/summary", cookies=self.auth_cookies)
        self.assertEqual(summary_resp.status_code, 200)
        summary_data = summary_resp.json()
        self.assertIn("total", summary_data)
        self.assertIn("security_count", summary_data)
        self.assertIn("reboot_required", summary_data)

    def test_raid_resyncing_status_not_degraded_and_sync_tracking(self):
        """Verify RAID resyncing is tracked as resyncing (not degraded), progress is extracted, and threshold alerts do not flag it as degraded."""
        # 1. /proc/mdstat parsing
        mdstat_content = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      1048576 blocks [2/2] [UU]
      [====>...............]  resync = 24.5% (256896/1048576) finish=5.2min speed=30234K/sec

unused devices: <none>
"""
        with patch("main.Path.exists", return_value=True), patch("main.Path.read_text", return_value=mdstat_content):
            info = main.get_mdstat_sync_progress("md0")
            self.assertEqual(info["action"], "resync")
            self.assertAlmostEqual(info["percent"], 24.5)
            self.assertEqual(info["completed"], "256896")
            self.assertEqual(info["total"], "1048576")
            self.assertEqual(info["eta"], "5.2min")

        # 2. mdadm detail output with State: clean, resyncing should be resyncing (not clean, not degraded)
        mdadm_resync_output = b"""/dev/md0:
        Version : 1.2
        Raid Level : raid1
        Array Size : 1048576 (1024.00 MiB 1073.74 MB)
        Raid Devices : 2
        Total Devices : 2
        State : clean, resyncing
        Active Devices : 2
        Working Devices : 2
        Failed Devices : 0
        Spare Devices : 0
        Consistency Policy : resync
        Resync Status : 35.8% complete
"""
        with (
            patch.object(main, "resolve_raid_device", return_value="/dev/md0"),
            patch.object(main, "read_sysfs_raid_state", return_value=None),
            patch("main.check_output", return_value=mdadm_resync_output),
        ):
            status, details = main.check_raid_status()
            self.assertEqual(status, "resyncing")
            self.assertIn("Resync Status : 35.8% complete", details)

            data = main.extract_raid_summary_data(status, details, "")
            self.assertEqual(data["status"], "resyncing")
            self.assertEqual(data["sync_action"], "resync")
            self.assertAlmostEqual(data["sync_percent"], 35.8)

        # 3. Sysfs with sync_action=resync even with degraded file present should be resyncing, not degraded
        sysfs_base = Path(self.temp_dir.name) / "sysfs_resync_test"
        md_dir = sysfs_base / "md0" / "md"
        md_dir.mkdir(parents=True, exist_ok=True)
        (md_dir / "array_state").write_text("clean\n", encoding="utf-8")
        (md_dir / "degraded").write_text("0\n", encoding="utf-8")
        (md_dir / "sync_action").write_text("resync\n", encoding="utf-8")
        (md_dir / "sync_completed").write_text("50000 / 100000\n", encoding="utf-8")
        (md_dir / "sync_speed").write_text("45000\n", encoding="utf-8")

        with patch("main.Path", side_effect=lambda p: Path(str(p).replace("/sys/block", str(sysfs_base)))):
            status, details = main.read_sysfs_raid_state("/dev/md0")
            self.assertEqual(status, "resyncing")
            self.assertIn("Resync Status : 50.0% complete", details)

            data = main.extract_raid_summary_data(status, details, "")
            self.assertEqual(data["status"], "resyncing")
            self.assertAlmostEqual(data["sync_percent"], 50.0)

        # 4. Threshold alerts must NOT trigger 'raid_degraded' alert when status is 'resyncing'
        t_now = 1700000000.0
        main.update_notification_center_config({
            "thresholds": {
                "enabled": True,
                "ram_percent": 99.0,
                "cpu_percent": 99.0,
                "disk_percent": 99.0,
                "raid_degraded": True,
                "cooldown_minutes": 60,
            },
        })
        with (
            patch.object(main, "check_raid_status", return_value=("resyncing", mdadm_resync_output.decode())),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t_now))
            raid_alerts = [a for a in alerts if a.get("metric") == "raid_degraded"]
            self.assertEqual(len(raid_alerts), 0, "Resyncing status must not trigger a degraded RAID alert")
            self.assertEqual(mock_send.await_count, 0)

        # 5. Threshold alerts MUST trigger 'raid_degraded' when status is truly 'degraded'
        with (
            patch.object(main, "check_raid_status", return_value=("degraded", "/dev/sda failed")),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
        ):
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t_now + 10))
            raid_alerts = [a for a in alerts if a.get("metric") == "raid_degraded"]
            self.assertEqual(len(raid_alerts), 1, "Truly degraded status must trigger raid_degraded alert")

    def test_audit_subtabs_and_memory_telemetry_contracts(self):
        """Verify DOM elements and contracts for audit subtabs, memory telemetry, and notification drawer."""
        # Memory task manager badge & available chip
        self.assertIn('id="memory-total-badge"', self.index_html)
        self.assertIn('id="memory-chip-available"', self.index_html)
        self.assertIn("memory-taskmgr-grid", self.index_html)

        # Audit & Logs subtabs
        self.assertIn('id="subtab-audit-security-btn"', self.index_html)
        self.assertIn('id="subtab-audit-alerts-btn"', self.index_html)
        self.assertIn('id="audit-security-subtab-panel"', self.index_html)
        self.assertIn('id="audit-alerts-subtab-panel"', self.index_html)
        self.assertIn('id="btn-goto-alert-logs"', self.index_html)

        # Notification drawer filter pills and dismiss all
        self.assertIn('id="btn-drawer-dismiss-all"', self.index_html)
        self.assertIn('id="drawer-tab-all"', self.index_html)
        self.assertIn('id="drawer-tab-alerts"', self.index_html)
        self.assertIn('id="drawer-tab-app"', self.index_html)

        # JavaScript wiring
        self.assertIn("switchAuditSubtab", self.app_js)
        self.assertIn("btn-drawer-dismiss-all", self.app_js)
        self.assertIn("memory-chip-available", self.app_js)

    def test_streamlined_layout_network_telemetry_and_groups_preloading(self):
        """Verify full-width CPU chart, memory chunks, physical network adapter priority, and groups preloading."""
        # Full width CPU chart at top of CPU card
        self.assertIn("cpu-unified-chart-container", self.index_html)
        self.assertIn("cpu-summary-block", self.index_html)
        cpu_chart_pos = self.index_html.find('class="cpu-unified-chart-container"')
        cpu_summary_pos = self.index_html.find('class="cpu-summary-block"')
        cores_grid_pos = self.index_html.find('id="cpu-cores-grid"')
        self.assertGreater(cpu_chart_pos, -1)
        self.assertGreater(cpu_summary_pos, -1)
        self.assertGreater(cores_grid_pos, -1)
        self.assertLess(cpu_chart_pos, cpu_summary_pos, "Core chart must span top of CPU card before telemetry summary")
        self.assertLess(cpu_summary_pos, cores_grid_pos, "CPU telemetry summary must come before core grid")

        # Memory chunks grid
        self.assertIn("memory-chunks-grid", self.index_html)
        self.assertIn("memory-chunk-used", self.index_html)
        self.assertIn("memory-chunk-available", self.index_html)
        self.assertIn("memory-chunk-cached", self.index_html)
        self.assertIn("memory-chunk-free", self.index_html)

        # Network physical / virtual adapter classification
        net = main.get_network_metrics()
        self.assertIn("primary_interface", net)
        self.assertIn("primary_ip", net)
        for iface in net["interfaces"]:
            self.assertIn("is_physical", iface)
            self.assertIn("is_virtual", iface)
            self.assertIn("adapter_type", iface)

        # Users & Groups preloading wiring in JS
        self.assertIn("fetchSystemGroups", self.app_js)
        users_switch_idx = self.app_js.find("else if (isUsers)")
        self.assertGreater(users_switch_idx, -1)
        users_snippet = self.app_js[users_switch_idx:users_switch_idx + 600]
        self.assertIn("fetchSystemGroups()", users_snippet)


class TestVigilSentinelProbes(unittest.TestCase):
    """Comprehensive test suite for Vigil Sentinel: Service Uptime & Health Probes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sentinel_test.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

        main._sentinel_alert_queue.clear()
        main._probe_last_run.clear()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_sentinel_db_schema_and_crud(self):
        """Test probe table creation, insert, read, update, and delete."""
        probe = main.create_sentinel_probe(
            name="Primary API",
            type="http",
            target="https://api.example.com/health",
            interval_seconds=30,
            timeout_seconds=5,
            expected_status=200,
            keyword="ok",
            ssl_check=True,
            enabled=True,
        )
        self.assertIsNotNone(probe)
        probe_id = probe["id"]
        self.assertEqual(probe["name"], "Primary API")
        self.assertEqual(probe["type"], "http")
        self.assertEqual(probe["target"], "https://api.example.com/health")
        self.assertEqual(probe["interval_seconds"], 30)
        self.assertTrue(probe["ssl_check"])
        self.assertTrue(probe["enabled"])

        # Fetch single
        fetched = main.get_sentinel_probe(probe_id)
        self.assertEqual(fetched, probe)

        # Update
        updated = main.update_sentinel_probe(
            probe_id, {"name": "Primary API v2", "interval_seconds": 60, "enabled": False}
        )
        self.assertEqual(updated["name"], "Primary API v2")
        self.assertEqual(updated["interval_seconds"], 60)
        self.assertFalse(updated["enabled"])

        # Fetch all
        all_probes = main.get_all_sentinel_probes(enabled_only=False)
        self.assertEqual(len(all_probes), 1)
        enabled_probes = main.get_all_sentinel_probes(enabled_only=True)
        self.assertEqual(len(enabled_probes), 0)

        # Delete
        deleted = main.delete_sentinel_probe(probe_id)
        self.assertTrue(deleted)
        self.assertIsNone(main.get_sentinel_probe(probe_id))

    def test_execute_probe_http(self):
        """Test HTTP probe execution including status matching, keyword check, and error paths."""
        probe = {
            "name": "Test Web",
            "type": "http",
            "target": "http://example.com/health",
            "timeout_seconds": 5,
            "expected_status": 200,
            "keyword": "healthy",
            "ssl_check": False,
        }

        # 1. Success case
        class MockContent:
            async def read(self, n):
                return b"System is healthy and active"

        class MockResponse:
            status = 200
            content = MockContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def get(self, url, **kwargs):
                return MockResponse()

        with patch("aiohttp.ClientSession", side_effect=MockSession):
            result = asyncio.run(main.execute_probe(probe))
            self.assertEqual(result["status"], "up")
            self.assertEqual(result["http_status"], 200)
            self.assertEqual(result["error_message"], "")

        # 2. Keyword missing -> down
        probe_with_bad_kw = dict(probe, keyword="nonexistent_string")
        with patch("aiohttp.ClientSession", side_effect=MockSession):
            result = asyncio.run(main.execute_probe(probe_with_bad_kw))
            self.assertEqual(result["status"], "down")
            self.assertIn("Keyword 'nonexistent_string' not found", result["error_message"])

        # 3. Status mismatch -> down
        class Mock500Response(MockResponse):
            status = 500

        class Mock500Session(MockSession):
            def get(self, url, **kwargs):
                return Mock500Response()

        with patch("aiohttp.ClientSession", side_effect=Mock500Session):
            result = asyncio.run(main.execute_probe(probe))
            self.assertEqual(result["status"], "down")
            self.assertEqual(result["http_status"], 500)
            self.assertIn("Expected HTTP 200, received 500", result["error_message"])

        # 4. Timeout -> down
        class MockTimeoutSession(MockSession):
            def get(self, url, **kwargs):
                raise asyncio.TimeoutError()

        with patch("aiohttp.ClientSession", side_effect=MockTimeoutSession):
            result = asyncio.run(main.execute_probe(probe))
            self.assertEqual(result["status"], "down")
            self.assertIn("timed out", result["error_message"])

    def test_execute_probe_ssl_check(self):
        """Test SSL certificate parsing and degraded threshold for expiring certs."""
        # Test SSL helper date calculation with mock socket and wrap_socket
        mock_cert = {
            "notAfter": "Nov 10 00:00:00 2026 GMT"
        }
        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = mock_cert
        mock_sock = MagicMock()

        with patch("socket.create_connection", return_value=mock_sock), \
             patch("ssl.create_default_context") as mock_ctx:
            mock_ctx.return_value.wrap_socket.return_value.__enter__.return_value = mock_ssock
            days = main.get_ssl_days_remaining("example.com", 443)
            self.assertIsNotNone(days)
            self.assertIsInstance(days, int)

        # Test degraded state when cert has <= 14 days remaining
        probe = {
            "name": "HTTPS Test",
            "type": "http",
            "target": "https://secure.example.com",
            "timeout_seconds": 5,
            "expected_status": 200,
            "ssl_check": True,
        }

        class MockContent:
            async def read(self, n):
                return b"OK"

        class MockResponse:
            status = 200
            content = MockContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def get(self, url, **kwargs):
                return MockResponse()

        with patch("aiohttp.ClientSession", side_effect=MockSession), \
             patch.object(main, "get_ssl_days_remaining", return_value=7):
            result = asyncio.run(main.execute_probe(probe))
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["ssl_days_remaining"], 7)
            self.assertIn("SSL certificate expires in 7 day(s)", result["error_message"])

    def test_execute_probe_tcp(self):
        """Test TCP port probe execution with success and connection failures."""
        # 1. Success
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(mock_reader, mock_writer))):
            result = asyncio.run(main.execute_probe({
                "type": "tcp",
                "target": "127.0.0.1:5432",
                "timeout_seconds": 5,
            }))
            self.assertEqual(result["status"], "up")
            self.assertGreaterEqual(result["response_time_ms"], 0.0)

        # 2. Connection refused / failure
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError("Connection refused")):
            result = asyncio.run(main.execute_probe({
                "type": "tcp",
                "target": "127.0.0.1:5432",
                "timeout_seconds": 5,
            }))
            self.assertEqual(result["status"], "down")
            self.assertIn("Connection refused", result["error_message"])

        # 3. Invalid target without port
        result = asyncio.run(main.execute_probe({
            "type": "tcp",
            "target": "127.0.0.1",
            "timeout_seconds": 5,
        }))
        self.assertEqual(result["status"], "down")
        self.assertIn("host:port format required", result["error_message"])

    def test_execute_probe_ping(self):
        """Test ping probe execution with subprocess mock and fallback check."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            b"64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=14.2 ms\n",
            b"",
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = asyncio.run(main.execute_probe({
                "type": "ping",
                "target": "1.1.1.1",
                "timeout_seconds": 3,
            }))
            self.assertEqual(result["status"], "up")
            self.assertEqual(result["response_time_ms"], 14.2)

        # Ping failure returncode 1
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"Destination Host Unreachable\n")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = asyncio.run(main.execute_probe({
                "type": "ping",
                "target": "1.1.1.1",
                "timeout_seconds": 3,
            }))
            self.assertEqual(result["status"], "down")
            self.assertIn("Destination Host Unreachable", result["error_message"])

    def test_record_probe_check_history_and_pruning(self):
        """Verify history entries are stored, limited, and pruned to 200 items."""
        probe = main.create_sentinel_probe("History Probe", "http", "http://test.local")
        probe_id = probe["id"]

        for i in range(210):
            main.record_probe_check(probe_id, {
                "status": "up",
                "response_time_ms": 10.0 + i,
                "http_status": 200,
                "error_message": "",
                "ssl_days_remaining": None,
            })

        history = main.get_probe_history(probe_id, limit=300)
        self.assertEqual(len(history), 200, "History must be pruned to max 200 records")
        # Ensure latest records are returned first (timestamp DESC)
        self.assertGreaterEqual(history[0]["timestamp"], history[-1]["timestamp"])

    def test_state_transitions_audit_and_alert_queue(self):
        """Verify UP -> DOWN and DOWN -> UP trigger audit logs and queue alerts."""
        probe = main.create_sentinel_probe("Alert Probe", "http", "http://test.local")
        probe_id = probe["id"]

        # Initial check (UP) -> no alert queue (since no prior state)
        main.record_probe_check(probe_id, {"status": "up", "response_time_ms": 15.0})
        self.assertEqual(len(main._sentinel_alert_queue), 0)

        # UP -> DOWN transition
        main.record_probe_check(
            probe_id,
            {"status": "down", "response_time_ms": 0.0, "error_message": "Connection timed out"},
        )
        self.assertEqual(len(main._sentinel_alert_queue), 1)
        alert = main._sentinel_alert_queue[0]
        self.assertEqual(alert["old_status"], "up")
        self.assertEqual(alert["new_status"], "down")
        self.assertIn("Connection timed out", alert["result"]["error_message"])

        # Check audit log
        logs, total = main.get_audit_logs(event_type="sentinel.probe.down")
        self.assertEqual(total, 1)
        self.assertIn("transitioned from UP to DOWN", logs[0]["description"])

        # DOWN -> DOWN (no new transition alert)
        main.record_probe_check(
            probe_id,
            {"status": "down", "response_time_ms": 0.0, "error_message": "Still down"},
        )
        self.assertEqual(len(main._sentinel_alert_queue), 1)

        # DOWN -> UP transition
        main.record_probe_check(probe_id, {"status": "up", "response_time_ms": 12.0})
        self.assertEqual(len(main._sentinel_alert_queue), 2)
        recovered_alert = main._sentinel_alert_queue[1]
        self.assertEqual(recovered_alert["old_status"], "down")
        self.assertEqual(recovered_alert["new_status"], "up")

        up_logs, total_up = main.get_audit_logs(event_type="sentinel.probe.up")
        self.assertEqual(total_up, 1)
        self.assertIn("recovered", up_logs[0]["description"])

    def test_get_probes_summary(self):
        """Verify get_probes_summary calculates uptime_24h_percent, recent_checks, and avg latency."""
        probe = main.create_sentinel_probe("Summary Probe", "tcp", "127.0.0.1:8080")
        probe_id = probe["id"]

        # 1. No checks yet -> pending
        summary = main.get_probes_summary()
        self.assertEqual(len(summary), 1)
        p = summary[0]
        self.assertEqual(p["last_status"], "pending")
        self.assertEqual(p["uptime_24h_percent"], 100.0)
        self.assertEqual(p["recent_checks"], [])

        # 2. Add 3 UP and 1 DOWN check
        main.record_probe_check(probe_id, {"status": "up", "response_time_ms": 20.0})
        main.record_probe_check(probe_id, {"status": "up", "response_time_ms": 30.0})
        main.record_probe_check(probe_id, {"status": "down", "response_time_ms": 0.0})
        main.record_probe_check(probe_id, {"status": "up", "response_time_ms": 10.0})

        summary2 = main.get_probes_summary()
        p2 = summary2[0]
        self.assertEqual(p2["last_status"], "up")
        self.assertEqual(p2["last_response_time_ms"], 10.0)
        self.assertEqual(p2["uptime_24h_percent"], 75.0)  # 3 up out of 4 total
        self.assertEqual(p2["recent_checks"], ["up", "up", "down", "up"])
        self.assertEqual(p2["avg_response_time_ms"], 15.0)  # (20+30+0+10)/4 = 15.0

    def test_sentinel_probes_worker(self):
        """Verify sentinel_probes_worker executes enabled probes according to intervals."""
        probe = main.create_sentinel_probe("Worker Probe", "tcp", "127.0.0.1:9000", interval_seconds=5)

        async def run_worker_briefly():
            worker_task = asyncio.create_task(main.sentinel_probes_worker(sleep_interval=0.01))
            await asyncio.sleep(0.05)
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        with patch.object(
            main, "execute_probe", new=AsyncMock(return_value={"status": "up", "response_time_ms": 5.0})
        ) as mock_exec:
            asyncio.run(run_worker_briefly())
            self.assertGreaterEqual(mock_exec.await_count, 1)

        history = main.get_probe_history(probe["id"])
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "up")

    def test_api_probes_crud_endpoints(self):
        """Test /api/probes REST API: GET, POST, GET/{id}, PUT/{id}, DELETE/{id}."""
        # 1. Unauthenticated request -> 401
        res = asgi_get(api.app, "/api/probes")
        self.assertEqual(res.status_code, 401)

        # 2. Authenticated GET (empty list)
        res = asgi_get(api.app, "/api/probes", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("probes", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], 0)

        # 3. Create probe via POST
        create_payload = {
            "name": "API Service",
            "type": "http",
            "target": "http://127.0.0.1:8080/health",
            "interval_seconds": 30,
            "timeout_seconds": 5,
            "expected_status": 200,
            "keyword": "OK",
            "ssl_check": False,
            "enabled": True,
        }
        with patch.object(
            main, "execute_probe", new=AsyncMock(return_value={"status": "up", "response_time_ms": 12.5, "http_status": 200})
        ):
            res = asgi_post(api.app, "/api/probes", json_body=create_payload, cookies=self.auth_cookies)
            self.assertEqual(res.status_code, 200)
            created = res.json()
            self.assertEqual(created["name"], "API Service")
            probe_id = created["id"]

        # 4. GET by ID
        res = asgi_get(api.app, f"/api/probes/{probe_id}", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["id"], probe_id)

        # 5. GET invalid ID -> 404
        res = asgi_get(api.app, "/api/probes/99999", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

        # 6. PUT update probe
        update_payload = {"name": "API Service Renamed", "interval_seconds": 120}
        res = asgi_request(api.app, "PUT", f"/api/probes/{probe_id}", json_body=update_payload, cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "API Service Renamed")
        self.assertEqual(res.json()["interval_seconds"], 120)

        # 7. DELETE probe
        res = asgi_request(api.app, "DELETE", f"/api/probes/{probe_id}", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

        # Verify 404 after deletion
        res = asgi_get(api.app, f"/api/probes/{probe_id}", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

    def test_api_probes_test_and_history_endpoints(self):
        """Test /api/probes/{id}/test and /api/probes/{id}/history endpoints."""
        probe = main.create_sentinel_probe("Test Endpoint Probe", "tcp", "127.0.0.1:8080")
        probe_id = probe["id"]

        # POST /api/probes/{id}/test
        with patch.object(
            main, "execute_probe", new=AsyncMock(return_value={"status": "up", "response_time_ms": 8.0})
        ):
            res = asgi_post(api.app, f"/api/probes/{probe_id}/test", cookies=self.auth_cookies)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["result"]["status"], "up")

        # GET /api/probes/{id}/history
        res = asgi_get(api.app, f"/api/probes/{probe_id}/history", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 200)
        history_data = res.json()
        self.assertIn("history", history_data)
        self.assertEqual(history_data["probe_id"], probe_id)
        self.assertEqual(len(history_data["history"]), 1)
        self.assertEqual(history_data["history"][0]["status"], "up")

    def test_vigil_sentinel_frontend_dom_and_js_contracts(self):
        """Assert complete frontend DOM element contracts, JS function signatures, and SPA routes."""
        index_html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")

        # 1. Sidebar Navigation
        self.assertIn('id="nav-tab-probes"', index_html)
        self.assertIn('id="nav-tab-probes-badge"', index_html)
        self.assertIn('Uptime &amp; Probes', index_html)

        # 2. Probes View Panel & Toolbar
        self.assertIn('id="view-probes"', index_html)
        self.assertIn('id="probes-last-updated"', index_html)
        self.assertIn('id="btn-add-probe"', index_html)
        self.assertIn('id="refresh-probes-btn"', index_html)

        # 3. Status Ribbon Counters
        self.assertIn('id="probes-total-count"', index_html)
        self.assertIn('id="probes-up-count"', index_html)
        self.assertIn('id="probes-down-count"', index_html)
        self.assertIn('id="probes-avg-latency"', index_html)

        # 4. Probes Grid & Empty State
        self.assertIn('id="probes-cards-grid"', index_html)
        self.assertIn('id="probes-empty-state"', index_html)
        self.assertIn('id="btn-empty-add-probe"', index_html)

        # 5. Modal Dialog & Form Fields
        self.assertIn('id="probe-modal"', index_html)
        self.assertIn('id="probe-modal-title"', index_html)
        self.assertIn('id="probe-form"', index_html)
        self.assertIn('id="probe-form-name"', index_html)
        self.assertIn('id="probe-form-type"', index_html)
        self.assertIn('id="probe-form-target"', index_html)
        self.assertIn('id="probe-form-interval"', index_html)
        self.assertIn('id="probe-form-timeout"', index_html)
        self.assertIn('id="probe-form-status"', index_html)
        self.assertIn('id="probe-form-keyword"', index_html)
        self.assertIn('id="probe-form-ssl"', index_html)
        self.assertIn('id="probe-form-enabled"', index_html)
        self.assertIn('id="btn-probe-modal-cancel"', index_html)
        self.assertIn('id="btn-probe-modal-save"', index_html)

        # 6. JavaScript Client Functions in static/app.js
        self.assertIn("function fetchProbes(", app_js)
        self.assertIn("function renderProbes(", app_js)
        self.assertIn("function openAddProbeModal(", app_js)
        self.assertIn("function openEditProbeModal(", app_js)
        self.assertIn("function saveProbe(", app_js)
        self.assertIn("function deleteProbe(", app_js)
        self.assertIn("function testProbe(", app_js)

        # 7. SPA Routes for Probes & Sentinel
        for path in ["/probes", "/sentinel"]:
            res = asgi_get(api.app, path)
            self.assertEqual(res.status_code, 200, f"SPA route {path} must return 200")
            self.assertIn("text/html", res.headers.get("content-type", ""))

    def test_api_probes_edge_cases_and_error_handling(self):
        """Test edge cases: missing probe on test/history, non-existent update/delete, malformed targets."""
        # Non-existent probe test -> 404
        res = asgi_post(api.app, "/api/probes/99999/test", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

        # Non-existent probe history -> 404
        res = asgi_get(api.app, "/api/probes/99999/history", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

        # Non-existent probe update -> 404
        res = asgi_request(api.app, "PUT", "/api/probes/99999", json_body={"name": "Ghost"}, cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

        # Non-existent probe delete -> 404
        res = asgi_request(api.app, "DELETE", "/api/probes/99999", cookies=self.auth_cookies)
        self.assertEqual(res.status_code, 404)

        # Malformed TCP target (no port or invalid port)
        tcp_probe_invalid = {
            "name": "Bad TCP",
            "type": "tcp",
            "target": "invalid_no_port",
            "timeout_seconds": 1,
        }
        res_tcp = asyncio.run(main.execute_probe(tcp_probe_invalid))
        self.assertEqual(res_tcp["status"], "down")
        self.assertIn("Invalid TCP target", res_tcp["error_message"])

        # Unreachable ping target
        with patch("asyncio.create_subprocess_exec", side_effect=Exception("Ping tool not found")):
            res_ping = asyncio.run(main.execute_probe({"name": "Ping Fail", "type": "ping", "target": "10.255.255.1"}))
            self.assertEqual(res_ping["status"], "down")


class TestHardwareTelemetryAndSmartCollector(unittest.TestCase):
    """Test suite for S.M.A.R.T. disk telemetry, hardware thermal sensors, and early-warning alerts."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_hardware.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("hwadmin", "hwpass", email="hwadmin@example.com")
        self.session_token = main.create_session("hwadmin")
        self.auth_cookies = {"session_token": self.session_token}
        main.clear_smart_cache()

    def tearDown(self):
        main.clear_smart_cache()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_parse_smartctl_json_sata_drive(self):
        raw = {
            "json_format_version": [1, 0],
            "device": {"name": "/dev/sda", "info_name": "/dev/sda", "type": "ata", "protocol": "ATA"},
            "model_name": "Crucial CT1000MX500SSD1",
            "serial_number": "2045E4D12345",
            "firmware_version": "M3CR033",
            "smart_status": {"passed": True},
            "power_on_time": {"hours": 14220},
            "temperature": {"current": 33},
            "ata_smart_attributes": {
                "revision": 16,
                "table": [
                    {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0, "string": "0"}},
                    {"id": 9, "name": "Power_On_Hours", "raw": {"value": 14220, "string": "14220"}},
                    {"id": 194, "name": "Temperature_Celsius", "raw": {"value": 33, "string": "33"}},
                    {"id": 202, "name": "Percent_Lifetime_Remain", "raw": {"value": 96, "string": "96"}},
                ],
            },
        }
        res = main.parse_smartctl_json(raw, "/dev/sda")
        self.assertEqual(res["device"], "/dev/sda")
        self.assertEqual(res["model"], "Crucial CT1000MX500SSD1")
        self.assertEqual(res["serial_number"], "2045E4D12345")
        self.assertEqual(res["firmware_version"], "M3CR033")
        self.assertIs(res["passed"], True)
        self.assertEqual(res["temperature_c"], 33)
        self.assertEqual(res["power_on_hours"], 14220)
        self.assertEqual(res["reallocated_sectors"], 0)
        self.assertEqual(res["media_errors"], 0)
        self.assertEqual(res["wear_level_percent"], 4)  # 100 - 96% lifetime remain
        self.assertEqual(res["device_type"], "ssd")
        self.assertEqual(res["status"], "passed")
        self.assertIsInstance(res["raw_json"], dict)

    def test_parse_smartctl_json_nvme_drive(self):
        raw = {
            "json_format_version": [1, 0],
            "device": {"name": "/dev/nvme0n1", "info_name": "/dev/nvme0n1", "type": "nvme", "protocol": "NVMe"},
            "model_name": "Samsung SSD 970 EVO Plus 1TB",
            "serial_number": "S4EVNF0M123456",
            "firmware_version": "2B2QEXM7",
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {
                "critical_warning": 0,
                "temperature": 41,
                "percentage_used": 5,
                "power_on_hours": 9520,
                "media_errors": 0,
            },
            "temperature": {"current": 41},
            "power_on_time": {"hours": 9520},
        }
        res = main.parse_smartctl_json(raw, "/dev/nvme0n1")
        self.assertEqual(res["device"], "/dev/nvme0n1")
        self.assertEqual(res["model"], "Samsung SSD 970 EVO Plus 1TB")
        self.assertEqual(res["serial_number"], "S4EVNF0M123456")
        self.assertEqual(res["firmware_version"], "2B2QEXM7")
        self.assertIs(res["passed"], True)
        self.assertEqual(res["temperature_c"], 41)
        self.assertEqual(res["power_on_hours"], 9520)
        self.assertEqual(res["wear_level_percent"], 5)
        self.assertEqual(res["media_errors"], 0)
        self.assertEqual(res["reallocated_sectors"], 0)
        self.assertEqual(res["device_type"], "nvme")
        self.assertEqual(res["status"], "passed")

    def test_parse_smartctl_json_failing_hdd(self):
        raw = {
            "json_format_version": [1, 0],
            "device": {"name": "/dev/sdb", "info_name": "/dev/sdb", "type": "ata", "protocol": "ATA"},
            "model_name": "WDC WD40EFRX-68N32N0",
            "serial_number": "WD-WCC7K1234567",
            "firmware_version": "82.00A82",
            "smart_status": {"passed": False},
            "power_on_time": {"hours": 28500},
            "temperature": {"current": 58},
            "ata_smart_attributes": {
                "revision": 16,
                "table": [
                    {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 145, "string": "145"}},
                    {"id": 187, "name": "Reported_Uncorrect", "raw": {"value": 12, "string": "12"}},
                    {"id": 194, "name": "Temperature_Celsius", "raw": {"value": 58, "string": "58"}},
                ],
            },
        }
        res = main.parse_smartctl_json(raw, "/dev/sdb")
        self.assertIs(res["passed"], False)
        self.assertEqual(res["temperature_c"], 58)
        self.assertEqual(res["reallocated_sectors"], 145)
        self.assertEqual(res["media_errors"], 12)
        self.assertEqual(res["device_type"], "hdd")
        self.assertEqual(res["status"], "failed")

    def test_get_disk_smart_metrics_caching_and_force_refresh(self):
        mock_proc_1 = MagicMock(
            stdout=json.dumps({
                "device": {"name": "/dev/sda"},
                "model_name": "Drive One",
                "serial_number": "S1",
                "smart_status": {"passed": True},
                "temperature": {"current": 32},
            }),
            stderr="",
            returncode=0,
        )
        mock_proc_2 = MagicMock(
            stdout=json.dumps({
                "device": {"name": "/dev/sda"},
                "model_name": "Drive One",
                "serial_number": "S1",
                "smart_status": {"passed": True},
                "temperature": {"current": 37},
            }),
            stderr="",
            returncode=0,
        )

        with patch("subprocess.run", return_value=mock_proc_1) as mock_run:
            res1 = main.get_disk_smart_metrics("/dev/sda")
            self.assertEqual(res1["temperature_c"], 32)
            self.assertEqual(mock_run.call_count, 1)

            # Second call within 60s should return cached result without calling subprocess
            mock_run.return_value = mock_proc_2
            res2 = main.get_disk_smart_metrics("/dev/sda")
            self.assertEqual(res2["temperature_c"], 32)
            self.assertEqual(mock_run.call_count, 1)

            # Force refresh should bypass cache and call subprocess
            res3 = main.get_disk_smart_metrics("/dev/sda", force_refresh=True)
            self.assertEqual(res3["temperature_c"], 37)
            self.assertEqual(mock_run.call_count, 2)

    def test_get_disk_smart_metrics_fallback_on_missing_smartctl_or_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("No smartctl")):
            res = main.get_disk_smart_metrics("/dev/sdz")
            self.assertEqual(res["device"], "/dev/sdz")
            self.assertEqual(res["model"], "Unknown")
            self.assertIs(res["passed"], True)
            self.assertIsNone(res["temperature_c"])
            self.assertIsNone(res["raw_json"])
            self.assertIn(res["status"], ("unavailable", "error"))

    def test_discover_physical_disks_sysfs(self):
        fake_entries = [
            Path("/sys/block/sda"),
            Path("/sys/block/sdb"),
            Path("/sys/block/nvme0n1"),
            Path("/sys/block/loop0"),
            Path("/sys/block/dm-0"),
            Path("/sys/block/md0"),
        ]
        with (
            patch("main.Path.is_dir", return_value=True),
            patch("main.Path.iterdir", return_value=fake_entries),
            patch("main.Path.exists", return_value=False),  # no 'partition' file
        ):
            disks = main.discover_physical_disks(force_refresh=True)
            self.assertIn("/dev/sda", disks)
            self.assertIn("/dev/sdb", disks)
            self.assertIn("/dev/nvme0n1", disks)
            self.assertNotIn("/dev/loop0", disks)
            self.assertNotIn("/dev/dm-0", disks)
            self.assertNotIn("/dev/md0", disks)

    def test_get_hardware_thermal_metrics_extraction(self):
        # Create a mock hwmon directory structure
        hwmon_dir = Path(self.temp_dir.name) / "sys_hwmon"
        h0 = hwmon_dir / "hwmon0"
        h0.mkdir(parents=True, exist_ok=True)
        (h0 / "name").write_text("coretemp\n")
        (h0 / "temp1_input").write_text("45000\n")
        (h0 / "temp1_label").write_text("Package id 0\n")
        (h0 / "temp2_input").write_text("42000\n")
        (h0 / "temp2_label").write_text("Core 0\n")
        (h0 / "temp3_input").write_text("43000\n")
        (h0 / "temp3_label").write_text("Core 1\n")
        (h0 / "fan1_input").write_text("1350\n")
        (h0 / "fan1_label").write_text("CPU Fan\n")

        thermal_dir = Path(self.temp_dir.name) / "sys_thermal"
        tz0 = thermal_dir / "thermal_zone0"
        tz0.mkdir(parents=True, exist_ok=True)
        (tz0 / "type").write_text("acpitz\n")
        (tz0 / "temp").write_text("39000\n")

        orig_path = Path
        def mock_path(p, *args, **kwargs):
            p_str = str(p)
            if p_str == "/sys/class/hwmon":
                return hwmon_dir
            if p_str == "/sys/class/thermal":
                return thermal_dir
            return orig_path(p, *args, **kwargs)

        with patch("main.Path", side_effect=mock_path):
            thermals = main.get_hardware_thermal_metrics()
            self.assertEqual(thermals["cpu_package_c"], 45.0)
            self.assertEqual(len(thermals["cores"]), 2)
            self.assertEqual(thermals["cores"][0]["label"], "Core 0")
            self.assertEqual(thermals["cores"][0]["temp_c"], 42.0)
            self.assertEqual(thermals["cores"][1]["label"], "Core 1")
            self.assertEqual(thermals["cores"][1]["temp_c"], 43.0)
            self.assertEqual(len(thermals["fans"]), 1)
            self.assertEqual(thermals["fans"][0]["label"], "CPU Fan")
            self.assertEqual(thermals["fans"][0]["rpm"], 1350)
            self.assertTrue(any(s["name"] == "acpitz" for s in thermals["sensors"]))

    def test_storage_metrics_and_raid_summary_enrichment(self):
        mock_smart = [
            {
                "device": "/dev/sda",
                "model": "Crucial CT1000MX500SSD1",
                "passed": True,
                "temperature_c": 34,
                "wear_level_percent": 3,
                "reallocated_sectors": 0,
            },
            {
                "device": "/dev/sdb",
                "model": "Crucial CT1000MX500SSD1",
                "passed": True,
                "temperature_c": 36,
                "wear_level_percent": 4,
                "reallocated_sectors": 0,
            },
        ]
        with patch.object(main, "get_all_disks_smart_metrics", return_value=mock_smart):
            storage = main.get_storage_metrics()
            for disk in storage:
                self.assertIn("temperature_c", disk)
                self.assertIn("smart_passed", disk)
                self.assertIn("smart_status", disk)
                self.assertIn("model", disk)
                self.assertIn("wear_level_percent", disk)

            raid_sum = main._extract_raid_summary_data("clean", SAMPLE_DETAILS, SAMPLE_DUF)
            for dev in raid_sum.get("devices", []):
                self.assertIn("temperature_c", dev)
                self.assertIn("passed", dev)
                self.assertIn("smart_status", dev)
                self.assertEqual(dev["smart_status"], "PASSED")

            metrics = main.get_system_metrics_data()
            self.assertIn("physical_disks", metrics)
            self.assertIn("thermals", metrics)
            self.assertIn("thermal", metrics)

    def test_hardware_api_endpoints_disks_and_thermals(self):
        # 1. Unauthenticated requests must fail with 401
        res_unauth_disks = asgi_get(api.app, "/api/hardware/disks")
        self.assertEqual(res_unauth_disks.status_code, 401)

        res_unauth_thermals = asgi_get(api.app, "/api/hardware/thermals")
        self.assertEqual(res_unauth_thermals.status_code, 401)

        mock_disks = [
            {
                "device": "/dev/sda",
                "model": "Samsung SSD 860",
                "serial_number": "S123",
                "firmware_version": "1.0",
                "passed": True,
                "temperature_c": 33,
                "power_on_hours": 5000,
                "wear_level_percent": 2,
                "reallocated_sectors": 0,
                "media_errors": 0,
                "device_type": "ssd",
                "raw_json": {"dummy": "data"},
                "status": "passed",
            }
        ]
        mock_thermals = {
            "cpu_package_c": 44.5,
            "cores": [{"label": "Core 0", "temp_c": 43.0}],
            "sensors": [{"name": "CPU", "temp_c": 44.5}],
            "fans": [{"label": "CPU Fan", "rpm": 1400}],
        }

        # 2. Authenticated GET /api/hardware/disks
        with patch.object(main, "get_all_disks_smart_metrics", return_value=mock_disks):
            res_disks = asgi_get(api.app, "/api/hardware/disks", cookies=self.auth_cookies)
            self.assertEqual(res_disks.status_code, 200)
            data_disks = res_disks.json()
            self.assertIsInstance(data_disks, list)
            self.assertEqual(len(data_disks), 1)
            self.assertEqual(data_disks[0]["device"], "/dev/sda")
            self.assertEqual(data_disks[0]["temperature_c"], 33)

        # 3. Authenticated GET /api/hardware/disks/{disk_name}
        with patch.object(main, "get_disk_smart_metrics", return_value=mock_disks[0]):
            res_single = asgi_get(api.app, "/api/hardware/disks/sda", cookies=self.auth_cookies)
            self.assertEqual(res_single.status_code, 200)
            single_data = res_single.json()
            self.assertEqual(single_data["device"], "/dev/sda")
            self.assertEqual(single_data["raw_json"], {"dummy": "data"})

        # 4. Authenticated GET /api/hardware/disks/{nonexistent} -> 404
        with (
            patch.object(main, "discover_physical_disks", return_value=["/dev/sda"]),
            patch.object(main, "get_disk_smart_metrics", return_value={
                "device": "/dev/unknown_ghost_99",
                "model": "Unknown",
                "raw_json": None,
                "status": "not_found",
                "error": "No such device",
            }),
        ):
            res_404 = asgi_get(api.app, "/api/hardware/disks/unknown_ghost_99", cookies=self.auth_cookies)
            self.assertEqual(res_404.status_code, 404)

        # 5. Authenticated GET /api/hardware/thermals
        with patch.object(main, "get_hardware_thermal_metrics", return_value=mock_thermals):
            res_th = asgi_get(api.app, "/api/hardware/thermals", cookies=self.auth_cookies)
            self.assertEqual(res_th.status_code, 200)
            data_th = res_th.json()
            self.assertEqual(data_th["cpu_package_c"], 44.5)
            self.assertEqual(len(data_th["cores"]), 1)
            self.assertEqual(len(data_th["fans"]), 1)

        # 6. Authenticated GET /api/system/storage includes physical_disks
        with patch.object(main, "get_all_disks_smart_metrics", return_value=mock_disks):
            res_storage = asgi_get(api.app, "/api/system/storage", cookies=self.auth_cookies)
            self.assertEqual(res_storage.status_code, 200)
            data_st = res_storage.json()
            self.assertIn("physical_disks", data_st)
            self.assertEqual(len(data_st["physical_disks"]), 1)

    def test_threshold_alerts_for_smart_failure_and_high_temperatures(self):
        t0 = 1700000000.0
        main.update_notification_center_config({
            "channels": {
                "discord": {"enabled": True, "bot_token": "dummy_token", "channel_id": "12345"},
            },
            "thresholds": {
                "enabled": True,
                "ram_percent": 99.0,
                "cpu_percent": 99.0,
                "disk_percent": 99.0,
                "raid_degraded": False,
                "smart_failure": True,
                "disk_temp_hdd": 55.0,
                "disk_temp_nvme": 70.0,
                "cooldown_minutes": 60,
            },
        })

        mock_disks = [
            # 1. Normal drive (no alert)
            {
                "device": "/dev/sda",
                "model": "Normal HDD",
                "passed": True,
                "temperature_c": 38,
                "device_type": "hdd",
                "reallocated_sectors": 0,
            },
            # 2. Failing SMART drive (should alert)
            {
                "device": "/dev/sdb",
                "model": "Failing HDD",
                "passed": False,
                "temperature_c": 42,
                "device_type": "hdd",
                "reallocated_sectors": 128,
                "media_errors": 5,
            },
            # 3. Overheating HDD >= 55C (should alert)
            {
                "device": "/dev/sdc",
                "model": "Hot HDD",
                "passed": True,
                "temperature_c": 57,
                "device_type": "hdd",
                "reallocated_sectors": 0,
            },
            # 4. Overheating NVMe >= 70C (should alert)
            {
                "device": "/dev/nvme0n1",
                "model": "Hot NVMe",
                "passed": True,
                "temperature_c": 73,
                "device_type": "nvme",
            },
        ]

        with (
            patch.object(main, "get_all_disks_smart_metrics", return_value=mock_disks),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)) as mock_send,
            patch("platform.node", return_value="vps-node-1"),
        ):
            # First run: 3 alerts triggered (sdb smart failure, sdc high temp, nvme0n1 high temp)
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t0))
            metrics_triggered = {a["metric"] for a in alerts}
            self.assertEqual(len(alerts), 3)
            self.assertIn("smart_fail_sdb", metrics_triggered)
            self.assertIn("disk_temp_sdc", metrics_triggered)
            self.assertIn("disk_temp_nvme0n1", metrics_triggered)
            self.assertEqual(mock_send.await_count, 3)

            # Check alert details
            sdb_alert = next(a for a in alerts if a["metric"] == "smart_fail_sdb")
            self.assertEqual(sdb_alert["embed"]["color"], 0xEF4444)
            self.assertIn("S.M.A.R.T. Failure", sdb_alert["title"])

            sdc_alert = next(a for a in alerts if a["metric"] == "disk_temp_sdc")
            self.assertIn("57°C", sdc_alert["title"])

            # Immediate second evaluation at t0 + 60s: all in cooldown, 0 alerts
            alerts_cd = asyncio.run(main.evaluate_threshold_alerts(now=t0 + 60.0))
            self.assertEqual(len(alerts_cd), 0)
            self.assertEqual(mock_send.await_count, 3)

    def test_hardware_health_cockpit_frontend_dom_and_ui_contracts(self):
        """Validate Phase 2 Hardware Health Cockpit DOM elements, JS contracts, and CSS rules.

        Verifies:
        1. Constituent Member Disks table (#disks-table, #active-disks-table, #active-disks-tbody) with 6 columns.
        2. Clickable S.M.A.R.T. badge and temperature pill generation in client JS.
        3. S.M.A.R.T. Details Modal (#smart-modal) with headers, badges, metrics cards, table, and footer.
        4. Overview View #cpu-temp-badge in CPU Telemetry header.
        5. Client JS functions: fetchSmartDisks, fetchHardwareThermals, openSmartModal, closeSmartModal, updateCpuTempBadge.
        6. CSS styling for smart badges, modals, and thermal indicators.
        """
        # 1. Inspect static/index.html DOM elements
        index_resp = asgi_get(api.app, "/")
        self.assertEqual(index_resp.status_code, 200)
        html = index_resp.text

        # 1a. Storage View (#view-raid) Member Disks Table with 6 columns
        raid_start = html.find('id="view-raid"')
        self.assertGreater(raid_start, -1, "Expected #view-raid container in index.html")
        raid_end = html.find('id="view-containers"')
        self.assertGreater(raid_end, raid_start)
        raid_html = html[raid_start:raid_end]

        self.assertIn('id="disks-table"', raid_html)
        self.assertIn('id="active-disks-table"', raid_html)
        self.assertIn('id="active-disks-tbody"', raid_html)
        self.assertIn('id="disks-count-badge"', raid_html)

        # Verify 6 required table columns in the header
        self.assertIn("<th>Device</th>", raid_html)
        self.assertIn("<th>Model</th>", raid_html)
        self.assertIn("<th>State</th>", raid_html)
        self.assertIn("<th>S.M.A.R.T. Health</th>", raid_html)
        self.assertIn("<th>Temp</th>", raid_html)
        self.assertIn("<th>Slot / Role</th>", raid_html)

        # 1b. Overview View CPU Telemetry Temperature Badge
        overview_start = html.find('id="view-overview"')
        self.assertGreater(overview_start, -1)
        overview_end = html.find('id="view-raid"')
        self.assertGreater(overview_end, overview_start)
        overview_html = html[overview_start:overview_end]

        self.assertIn('id="cpu-temp-badge"', overview_html)
        self.assertIn('id="cpu-cores-badge"', overview_html)
        self.assertIn("CPU Telemetry", overview_html)

        # 1c. S.M.A.R.T. Details Modal Structure (#smart-modal)
        smart_modal_start = html.find('id="smart-modal"')
        self.assertGreater(smart_modal_start, -1, "Expected #smart-modal container in index.html")
        smart_modal_end = html.find('id="toast-container"', smart_modal_start)
        self.assertGreater(smart_modal_end, smart_modal_start)
        modal_html = html[smart_modal_start:smart_modal_end]

        # Modal Header
        self.assertIn('id="smart-modal-title"', modal_html)
        self.assertIn('id="smart-modal-subtitle"', modal_html)
        self.assertIn('id="btn-smart-modal-close"', modal_html)

        # Modal Badges
        self.assertIn('id="smart-modal-status"', modal_html)
        self.assertIn('id="smart-modal-type"', modal_html)
        self.assertIn('id="smart-modal-model"', modal_html)
        self.assertIn('id="smart-modal-serial"', modal_html)
        self.assertIn('id="smart-modal-firmware"', modal_html)

        # Modal Key Metrics Cards
        self.assertIn('id="smart-modal-temp"', modal_html)
        self.assertIn('id="smart-modal-hours"', modal_html)
        self.assertIn('id="smart-modal-reallocated"', modal_html)
        self.assertIn('id="smart-modal-wear"', modal_html)
        self.assertIn('id="smart-modal-errors"', modal_html)

        # Modal Attributes Table
        self.assertIn('id="smart-modal-attributes-container"', modal_html)
        self.assertIn('id="smart-modal-attributes-table"', modal_html)
        self.assertIn('id="smart-modal-attributes-tbody"', modal_html)

        # Modal Footer Actions
        self.assertIn('id="btn-smart-modal-cancel"', modal_html)

        # 2. Inspect static/app.js client logic contracts
        app_js = Path("static/app.js").read_text(encoding="utf-8")

        # Function signatures
        self.assertIn("async function fetchSmartDisks(", app_js)
        self.assertIn("async function fetchHardwareThermals(", app_js)
        self.assertIn("function updateCpuTempBadge(", app_js)
        self.assertIn("async function openSmartModal(", app_js)
        self.assertIn("function closeSmartModal(", app_js)

        # Clickable S.M.A.R.T. badge and temp pill creation
        self.assertIn("smart-badge", app_js)
        self.assertIn('smartBtn.setAttribute("data-disk", cleanDev)', app_js)
        self.assertIn("temp-badge", app_js)
        self.assertIn("temp-ok", app_js)
        self.assertIn("temp-warn", app_js)
        self.assertIn("temp-crit", app_js)

        # Event delegation and modal event listeners
        self.assertIn('active-disks-tbody', app_js)
        self.assertIn('button.smart-badge[data-disk]', app_js)
        self.assertIn('btn-smart-modal-close', app_js)
        self.assertIn('btn-smart-modal-cancel', app_js)
        self.assertIn('smart-modal', app_js)

        # 3. Inspect static/styles.css CSS rules
        styles_css = Path("static/styles.css").read_text(encoding="utf-8")
        self.assertIn(".smart-modal-card", styles_css)
        self.assertIn(".smart-summary-banner", styles_css)
        self.assertIn(".smart-metrics-grid", styles_css)
        self.assertIn(".smart-metric-card", styles_css)
        self.assertIn(".smart-badge", styles_css)
        self.assertIn(".smart-passed", styles_css)
        self.assertIn(".smart-failed", styles_css)
        self.assertIn(".smart-unknown", styles_css)
        self.assertIn(".temp-badge", styles_css)
        self.assertIn(".temp-ok", styles_css)
        self.assertIn(".temp-warn", styles_css)
        self.assertIn(".temp-crit", styles_css)


class TestNetworkSecurityAndFirewallManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_is_ip_public_helper(self):
        self.assertTrue(main._is_ip_public("0.0.0.0"))
        self.assertTrue(main._is_ip_public("::"))
        self.assertTrue(main._is_ip_public(""))
        self.assertTrue(main._is_ip_public("*"))
        self.assertTrue(main._is_ip_public("192.168.1.100"))
        self.assertTrue(main._is_ip_public("10.0.0.5"))
        self.assertTrue(main._is_ip_public("1.1.1.1"))

        self.assertFalse(main._is_ip_public("127.0.0.1"))
        self.assertFalse(main._is_ip_public("127.0.0.53"))
        self.assertFalse(main._is_ip_public("::1"))

    @patch("main.list_docker_containers")
    @patch("main.psutil.Process")
    @patch("main.psutil.net_connections")
    def test_get_open_listening_ports_psutil_mock(self, mock_conns, mock_proc, mock_containers):
        mock_containers.return_value = [
            {
                "id": "c1234567890ab",
                "name": "nginx-proxy",
                "state": "running",
                "ports": "0.0.0.0:80->80/tcp, :::80->80/tcp",
            }
        ]

        from collections import namedtuple
        SConn = namedtuple("SConn", ["fd", "family", "type", "laddr", "raddr", "status", "pid"])
        LAddr = namedtuple("LAddr", ["ip", "port"])

        c_tcp_pub = SConn(fd=1, family=socket.AF_INET, type=socket.SOCK_STREAM, laddr=LAddr("0.0.0.0", 80), raddr=(), status="LISTEN", pid=101)
        c_tcp_priv = SConn(fd=2, family=socket.AF_INET, type=socket.SOCK_STREAM, laddr=LAddr("127.0.0.1", 3306), raddr=(), status="LISTEN", pid=102)
        c_udp_pub = SConn(fd=3, family=socket.AF_INET, type=socket.SOCK_DGRAM, laddr=LAddr("0.0.0.0", 53), raddr=(), status="NONE", pid=103)
        c_tcp_est = SConn(fd=4, family=socket.AF_INET, type=socket.SOCK_STREAM, laddr=LAddr("192.168.1.50", 45678), raddr=LAddr("93.184.216.34", 443), status="ESTABLISHED", pid=104)

        mock_conns.return_value = [c_tcp_priv, c_tcp_est, c_tcp_pub, c_udp_pub]

        def proc_side_effect(pid):
            p = MagicMock()
            if pid == 101:
                p.name.return_value = "docker-proxy"
                p.username.return_value = "root"
            elif pid == 102:
                p.name.return_value = "mysqld"
                p.username.return_value = "mysql"
            elif pid == 103:
                p.name.return_value = "named"
                p.username.return_value = "bind"
            else:
                p.name.return_value = "other"
                p.username.return_value = "nobody"
            return p

        mock_proc.side_effect = proc_side_effect

        ports = main.get_open_listening_ports()
        self.assertEqual(len(ports), 3)

        self.assertEqual(ports[0]["port"], 53)
        self.assertEqual(ports[0]["proto"], "udp")
        self.assertTrue(ports[0]["is_public"])
        self.assertEqual(ports[0]["process_name"], "named")
        self.assertEqual(ports[0]["user"], "bind")

        self.assertEqual(ports[1]["port"], 80)
        self.assertEqual(ports[1]["proto"], "tcp")
        self.assertTrue(ports[1]["is_public"])
        self.assertEqual(ports[1]["process_name"], "docker-proxy")
        self.assertEqual(ports[1]["container_name"], "nginx-proxy")

        self.assertEqual(ports[2]["port"], 3306)
        self.assertEqual(ports[2]["proto"], "tcp")
        self.assertFalse(ports[2]["is_public"])
        self.assertEqual(ports[2]["process_name"], "mysqld")
        self.assertEqual(ports[2]["container_name"], "")

    @patch("main._get_socket_inode_to_pid_map")
    @patch("main._parse_proc_net_file")
    @patch("main.psutil.net_connections")
    def test_get_open_listening_ports_fallback_proc_net(self, mock_conns, mock_parse, mock_inode_map):
        mock_conns.side_effect = psutil.AccessDenied()
        mock_inode_map.return_value = {"12345": 501}

        def parse_side_effect(filepath, proto):
            if filepath == "/proc/net/tcp":
                return [{"proto": "tcp", "ip": "0.0.0.0", "port": 8080, "inode": "12345"}]
            if filepath == "/proc/net/udp":
                return [{"proto": "udp", "ip": "127.0.0.1", "port": 5353, "inode": ""}]
            return []

        mock_parse.side_effect = parse_side_effect

        with patch("main.psutil.Process") as mock_proc:
            p = MagicMock()
            p.name.return_value = "proc-app"
            p.username.return_value = "appuser"
            mock_proc.return_value = p

            ports = main.get_open_listening_ports()
            self.assertEqual(len(ports), 2)
            self.assertEqual(ports[0]["port"], 8080)
            self.assertTrue(ports[0]["is_public"])
            self.assertEqual(ports[0]["pid"], 501)
            self.assertEqual(ports[0]["process_name"], "proc-app")

            self.assertEqual(ports[1]["port"], 5353)
            self.assertFalse(ports[1]["is_public"])
            self.assertIsNone(ports[1]["pid"])

    @patch("main.list_docker_containers")
    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.is_file")
    @patch("main.psutil.Process")
    @patch("main.psutil.net_connections")
    def test_docker_container_pid_cgroup_correlation(self, mock_conns, mock_proc, mock_isfile, mock_read, mock_containers):
        mock_containers.return_value = [
            {
                "id": "e3b0c44298fc1c149afbf4c8996fb924",
                "name": "redis-cache",
                "state": "running",
                "ports": "",
            }
        ]

        from collections import namedtuple
        SConn = namedtuple("SConn", ["fd", "family", "type", "laddr", "raddr", "status", "pid"])
        LAddr = namedtuple("LAddr", ["ip", "port"])

        mock_conns.return_value = [
            SConn(fd=1, family=socket.AF_INET, type=socket.SOCK_STREAM, laddr=LAddr("127.0.0.1", 6379), raddr=(), status="LISTEN", pid=202)
        ]

        p = MagicMock()
        p.name.return_value = "redis-server"
        p.username.return_value = "redis"
        mock_proc.return_value = p

        mock_isfile.return_value = True
        mock_read.return_value = "0::/system.slice/docker-e3b0c44298fc1c149afbf4c8996fb924.scope\n"

        ports = main.get_open_listening_ports()
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["port"], 6379)
        self.assertEqual(ports[0]["container_name"], "redis-cache")

    @patch("shutil.which")
    def test_ufw_firewall_status_not_installed(self, mock_which):
        mock_which.return_value = None
        with patch("os.path.isfile", return_value=False):
            status = main.get_ufw_firewall_status()
            self.assertFalse(status["available"])
            self.assertFalse(status["enabled"])
            self.assertEqual(status["default_incoming"], "deny")
            self.assertEqual(status["default_outgoing"], "allow")
            self.assertEqual(status["rules"], [])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_ufw_firewall_status_parsing_active_with_rules(self, mock_which, mock_run):
        mock_which.return_value = "/usr/sbin/ufw"
        raw_output = """
Status: active
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 80/tcp                     ALLOW IN    Anywhere
[ 3] 443                        ALLOW IN    192.168.1.0/24
[ 4] 22/tcp (v6)                ALLOW IN    Anywhere (v6)
[ 5] 53/udp                     DENY IN     Anywhere
[ 6] 8080                       REJECT IN   10.0.0.0/8
"""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = raw_output
        proc.stderr = ""
        mock_run.return_value = proc

        with patch("os.path.isfile", return_value=True):
            status = main.get_ufw_firewall_status()
            self.assertTrue(status["available"])
            self.assertTrue(status["enabled"])
            self.assertEqual(status["default_incoming"], "deny")
            self.assertEqual(status["default_outgoing"], "allow")
            self.assertEqual(len(status["rules"]), 6)

            r1 = status["rules"][0]
            self.assertEqual(r1["index"], 1)
            self.assertEqual(r1["to_port"], "22")
            self.assertEqual(r1["proto"], "tcp")
            self.assertEqual(r1["action"], "ALLOW")
            self.assertEqual(r1["from_ip"], "Anywhere")
            self.assertFalse(r1["v6"])

            r3 = status["rules"][2]
            self.assertEqual(r3["index"], 3)
            self.assertEqual(r3["to_port"], "443")
            self.assertEqual(r3["proto"], "any")
            self.assertEqual(r3["action"], "ALLOW")
            self.assertEqual(r3["from_ip"], "192.168.1.0/24")

            r4 = status["rules"][3]
            self.assertEqual(r4["index"], 4)
            self.assertEqual(r4["to_port"], "22")
            self.assertEqual(r4["proto"], "tcp")
            self.assertTrue(r4["v6"])

            r5 = status["rules"][4]
            self.assertEqual(r5["index"], 5)
            self.assertEqual(r5["to_port"], "53")
            self.assertEqual(r5["proto"], "udp")
            self.assertEqual(r5["action"], "DENY")

            r6 = status["rules"][5]
            self.assertEqual(r6["index"], 6)
            self.assertEqual(r6["to_port"], "8080")
            self.assertEqual(r6["action"], "REJECT")
            self.assertEqual(r6["from_ip"], "10.0.0.0/8")

    def test_manage_ufw_rule_input_validation(self):
        res = main.manage_ufw_rule("kill", "80")
        self.assertFalse(res["success"])
        self.assertIn("Invalid action", res["message"])

        res = main.manage_ufw_rule("allow", "80", proto="icmp")
        self.assertFalse(res["success"])
        self.assertIn("Invalid proto", res["message"])

        res = main.manage_ufw_rule("allow", "80; rm -rf /")
        self.assertFalse(res["success"])
        self.assertIn("Invalid port specification", res["message"])

        res = main.manage_ufw_rule("allow", "80 && reboot")
        self.assertFalse(res["success"])

        res = main.manage_ufw_rule("allow", "70000")
        self.assertFalse(res["success"])
        self.assertIn("out of range", res["message"])

        res = main.manage_ufw_rule("allow", "9000:8000")
        self.assertFalse(res["success"])
        self.assertIn("boundaries", res["message"])

        res = main.manage_ufw_rule("allow", "80", from_ip="300.0.0.1")
        self.assertFalse(res["success"])
        self.assertIn("Invalid source IP", res["message"])

        res = main.manage_ufw_rule("delete", "0")
        self.assertFalse(res["success"])
        self.assertIn("positive integer", res["message"])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_manage_ufw_rule_execution_and_audit_logging(self, mock_which, mock_run):
        mock_which.return_value = "/usr/sbin/ufw"
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Rule added\n"
        proc.stderr = ""
        mock_run.return_value = proc

        with patch("os.path.isfile", return_value=True), patch("os.access", return_value=True):
            res = main.manage_ufw_rule(
                "allow",
                "8443",
                proto="tcp",
                from_ip="192.168.1.0/24",
                actor="admin",
                ip_address="127.0.0.1",
            )
            self.assertTrue(res["success"])
            self.assertIn("Rule added", res["message"])

            mock_run.assert_called()
            call_args = mock_run.call_args[0][0]
            self.assertIn("allow", call_args)
            self.assertIn("from", call_args)
            self.assertIn("192.168.1.0/24", call_args)
            self.assertIn("proto", call_args)
            self.assertIn("tcp", call_args)
            self.assertIn("8443", call_args)

            logs, count = main.get_audit_logs(event_type="network.firewall.rule_added")
            self.assertGreaterEqual(count, 1)
            self.assertEqual(logs[0]["actor"], "admin")
            self.assertIn("8443", logs[0]["description"])
            self.assertEqual(logs[0]["metadata"]["action"], "allow")
            self.assertEqual(logs[0]["metadata"]["port"], "8443")
            self.assertEqual(logs[0]["metadata"]["proto"], "tcp")
            self.assertEqual(logs[0]["metadata"]["from_ip"], "192.168.1.0/24")

            proc.stdout = "Rule deleted\n"
            res_del = main.manage_ufw_rule("delete", "2", actor="admin")
            self.assertTrue(res_del["success"])

            del_logs, del_count = main.get_audit_logs(event_type="network.firewall.rule_deleted")
            self.assertGreaterEqual(del_count, 1)
            self.assertEqual(del_logs[0]["metadata"]["port"], "2")

    def test_api_network_endpoints_authentication(self):
        resp = asgi_get(api.app, "/api/network/ports")
        self.assertEqual(resp.status_code, 401)

        resp = asgi_get(api.app, "/api/network/firewall")
        self.assertEqual(resp.status_code, 401)

        resp = asgi_post(api.app, "/api/network/firewall/rules", json_body={"action": "allow", "port": "80"})
        self.assertEqual(resp.status_code, 401)

        resp = asgi_delete(api.app, "/api/network/firewall/rules/1")
        self.assertEqual(resp.status_code, 401)

    @patch("main.manage_ufw_rule")
    @patch("main.get_ufw_firewall_status")
    @patch("main.get_open_listening_ports")
    def test_api_network_ports_and_firewall_authenticated(self, mock_ports, mock_fw_status, mock_manage):
        mock_ports.return_value = [
            {"proto": "tcp", "ip": "0.0.0.0", "port": 80, "is_public": True, "pid": 10, "process_name": "nginx", "user": "www", "container_name": ""},
            {"proto": "tcp", "ip": "127.0.0.1", "port": 3306, "is_public": False, "pid": 20, "process_name": "mysql", "user": "db", "container_name": ""},
        ]
        mock_fw_status.return_value = {
            "available": True,
            "enabled": True,
            "default_incoming": "deny",
            "default_outgoing": "allow",
            "rules": [{"index": 1, "to_port": "80", "proto": "tcp", "action": "ALLOW", "from_ip": "Anywhere", "v6": False, "raw": "[ 1] 80/tcp ALLOW IN Anywhere"}],
        }
        mock_manage.return_value = {"success": True, "message": "Rule applied"}

        resp = asgi_get(api.app, "/api/network/ports", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("ports", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["public"], 1)
        self.assertEqual(data["summary"]["local"], 1)
        self.assertEqual(data["summary"]["total_listening"], 2)
        self.assertEqual(data["summary"]["public_exposed_count"], 1)
        self.assertEqual(data["summary"]["local_only_count"], 1)
        self.assertEqual(data["summary"]["tcp_count"], 2)
        self.assertEqual(data["summary"]["udp_count"], 0)

        resp_fw = asgi_get(api.app, "/api/network/firewall", cookies=self.auth_cookies)
        self.assertEqual(resp_fw.status_code, 200)
        fw_data = resp_fw.json()
        self.assertTrue(fw_data["enabled"])
        self.assertEqual(len(fw_data["rules"]), 1)

        resp_add = asgi_post(
            api.app,
            "/api/network/firewall/rules",
            json_body={"action": "allow", "port": "443", "proto": "tcp"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_add.status_code, 200)
        self.assertTrue(resp_add.json()["success"])

        mock_manage.return_value = {"success": False, "message": "Invalid action 'invalid'"}
        resp_inv = asgi_post(
            api.app,
            "/api/network/firewall/rules",
            json_body={"action": "invalid", "port": "443"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_inv.status_code, 400)

        mock_manage.return_value = {"success": True, "message": "Rule deleted"}
        resp_del = asgi_delete(api.app, "/api/network/firewall/rules/1", cookies=self.auth_cookies)
        self.assertEqual(resp_del.status_code, 200)
        self.assertTrue(resp_del.json()["success"])

        resp_del_inv = asgi_delete(api.app, "/api/network/firewall/rules/0", cookies=self.auth_cookies)
        self.assertEqual(resp_del_inv.status_code, 400)

        resp_spa_net = asgi_get(api.app, "/network", cookies=self.auth_cookies)
        self.assertEqual(resp_spa_net.status_code, 200)
        resp_spa_fw = asgi_get(api.app, "/firewall", cookies=self.auth_cookies)
        self.assertEqual(resp_spa_fw.status_code, 200)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_manage_ufw_rule_limit_action_and_rule_index_kwarg(self, mock_which, mock_run):
        """Verify manage_ufw_rule supports 'limit' action and optional rule_index parameter."""
        mock_which.return_value = "/usr/sbin/ufw"
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Rule updated\n"
        proc.stderr = ""
        mock_run.return_value = proc

        with patch("os.path.isfile", return_value=True), patch("os.access", return_value=True):
            res_limit = main.manage_ufw_rule("limit", "22", proto="tcp", actor="admin")
            self.assertTrue(res_limit["success"])
            call_args = mock_run.call_args[0][0]
            self.assertIn("limit", call_args)
            self.assertIn("22/tcp", call_args)

            res_idx = main.manage_ufw_rule("delete", rule_index=3, actor="admin")
            self.assertTrue(res_idx["success"])
            del_args = mock_run.call_args[0][0]
            self.assertIn("delete", del_args)
            self.assertIn("3", del_args)

    def test_phase3_frontend_dom_and_js_contracts_and_vm_execution(self):
        """Verify Phase 3 Security/Firewall DOM elements, CSS classes, JS functions, and VM rendering."""
        import subprocess
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        required_ids = [
            "nav-tab-security",
            "view-security",
            "refresh-security-btn",
            "sec-total-ports",
            "sec-public-ports",
            "sec-local-ports",
            "sec-firewall-status",
            "sec-ports-filter",
            "sec-ports-table",
            "sec-ports-tbody",
            "btn-add-firewall-rule",
            "sec-firewall-policies",
            "sec-firewall-table",
            "sec-firewall-tbody",
            "firewall-modal",
            "btn-firewall-modal-close",
            "firewall-form",
            "firewall-form-action",
            "firewall-form-proto",
            "firewall-form-port",
            "firewall-form-from",
            "btn-firewall-modal-cancel",
            "btn-firewall-modal-save",
        ]
        for dom_id in required_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        for fn_name in [
            "async function fetchNetworkSecurity(",
            "function renderNetworkSecurity(",
            "function filterPortsTable(",
            "function openAddFirewallRuleModal(",
            "function closeFirewallModal(",
            "async function handleFirewallFormSubmit(",
            "async function deleteFirewallRule(",
        ]:
            self.assertIn(fn_name, js, f"Missing {fn_name} in static/app.js")

        for css_sel in [".security-content-grid", ".exposure-badge", ".exp-public", ".exp-local", "#sec-ports-table", "#sec-firewall-table"]:
            self.assertIn(css_sel, css, f"Missing {css_sel} in static/styles.css")

        # Execute renderNetworkSecurity and filterPortsTable in Node.js VM
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) {
    idx = source.indexOf('function ' + name + '(');
  }
  if (idx === -1) throw new Error('Not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') { depth--; if (started && depth === 0) return source.slice(idx, i + 1); }
    i++;
  }
  throw new Error('Unbalanced: ' + name);
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      innerHTML: '',
      value: '',
      className: '',
      querySelectorAll: () => [],
      querySelector: () => null,
      appendChild: () => {},
      classList: {
        _set: new Set(),
        add(...cls) { cls.forEach(c => this._set.add(c)); },
        remove(...cls) { cls.forEach(c => this._set.delete(c)); },
        toggle(c, force) {
          if (force === undefined) {
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          } else if (force) this._set.add(c); else this._set.delete(c);
        },
        contains(c) { return this._set.has(c); }
      }
    };
  }
  return elements[id];
}

const sandbox = {
  document: { getElementById: getEl, createElement: () => getEl('tmp') },
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
};
vm.createContext(sandbox);
vm.runInContext(extractFunc(code, 'filterPortsTable'), sandbox);
vm.runInContext(extractFunc(code, 'renderNetworkSecurity'), sandbox);

const portsPayload = {
  ports: [
    { proto: 'tcp', ip: '0.0.0.0', port: 443, is_public: true, pid: 101, process_name: 'nginx', user: 'root', container_name: 'web-proxy' },
    { proto: 'tcp', ip: '127.0.0.1', port: 5432, is_public: false, pid: 202, process_name: 'postgres', user: 'postgres', container_name: '' }
  ],
  summary: { total: 2, public: 1, local: 1 }
};
const fwPayload = {
  available: true,
  enabled: true,
  default_incoming: 'deny',
  default_outgoing: 'allow',
  rules: [
    { index: 1, to_port: '443', proto: 'tcp', action: 'ALLOW', from_ip: 'Anywhere', v6: false }
  ]
};

sandbox.renderNetworkSecurity(portsPayload, fwPayload);
if (getEl('sec-total-ports').textContent !== '2') throw new Error('Bad total ports: ' + getEl('sec-total-ports').textContent);
if (getEl('sec-public-ports').textContent !== '1') throw new Error('Bad public ports');
if (getEl('sec-local-ports').textContent !== '1') throw new Error('Bad local ports');
if (getEl('sec-firewall-status').textContent !== 'ACTIVE') throw new Error('Bad firewall status: ' + getEl('sec-firewall-status').textContent);
if (!getEl('sec-ports-tbody').innerHTML.includes('PUBLIC')) throw new Error('Missing PUBLIC badge');
if (!getEl('sec-ports-tbody').innerHTML.includes('LOCAL')) throw new Error('Missing LOCAL badge');
if (!getEl('sec-ports-tbody').innerHTML.includes('web-proxy')) throw new Error('Missing container pill');
if (!getEl('sec-firewall-tbody').innerHTML.includes('ALLOW')) throw new Error('Missing ALLOW rule');

// Test filterPortsTable row filtering
const mockRows = [
  { _search: 'tcp 443 0.0.0.0 public nginx 101 root web-proxy', style: { display: '' }, classList: { toggle() {} }, getAttribute: () => 'tcp 443 0.0.0.0 public nginx 101 root web-proxy' },
  { _search: 'tcp 5432 127.0.0.1 local postgres 202 postgres', style: { display: '' }, classList: { toggle() {} }, getAttribute: () => 'tcp 5432 127.0.0.1 local postgres 202 postgres' }
];
getEl('sec-ports-tbody').querySelectorAll = (sel) => sel === 'tr[data-search]' ? mockRows : [];
getEl('sec-ports-filter').value = 'postgres';
sandbox.filterPortsTable();
if (mockRows[0].style.display !== 'none') throw new Error('Filter failed to hide nginx row');
if (mockRows[1].style.display !== '') throw new Error('Filter hid matching postgres row');
console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], cwd=str(root), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"Node VM test failed: {proc.stderr or proc.stdout}")
        self.assertIn("OK", proc.stdout)

    def test_refinement_1_packages_navbar_single_badge_no_duplicates(self):
        """Verify #nav-tab-packages has single #nav-badge-packages and no duplicate #nav-tab-packages-badge."""
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="nav-badge-packages"', html)
        self.assertNotIn("nav-tab-packages-badge", html)
        self.assertIn("nav-badge-packages", js)
        self.assertNotIn("nav-tab-packages-badge", js)

    def test_refinement_2_packages_real_installation_contracts(self):
        """Verify frontend and backend contracts for real package installation vs simulation."""
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="btn-install-all-packages"', html)
        self.assertIn('id="btn-pkg-modal-install"', html)
        self.assertIn("btn-install-single", js)
        self.assertIn("btn-upgrade-single", js)
        self.assertIn("async function installPackageUpgrade(", js)
        self.assertIn("dry_run: false", js)

    def test_refinement_3_cpu_core_graph_zero_percent_visibility_vm(self):
        """Verify .cpu-unified-chart-container does not clip the 150px canvas and drawCpuCoreGraph renders 0% Y-axis label and 0% traces."""
        import re
        import subprocess
        root = Path(__file__).parent
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        # 1. Assert .cpu-unified-chart-container CSS block has height: auto, display: flex, flex-direction: column, and NOT height: 150px
        container_match = re.search(r"\.cpu-unified-chart-container\s*\{([^}]+)\}", css)
        self.assertIsNotNone(container_match, "Missing .cpu-unified-chart-container rule in static/styles.css")
        container_body = container_match.group(1)
        self.assertIn("height: auto", container_body)
        self.assertIn("display: flex", container_body)
        self.assertIn("flex-direction: column", container_body)
        self.assertNotIn("height: 150px", container_body)

        # 2. Assert .cpu-unified-chart-canvas CSS block has height: 150px, border: none, border-radius: 0
        canvas_match = re.search(r"\.cpu-unified-chart-canvas\s*\{([^}]+)\}", css)
        self.assertIsNotNone(canvas_match, "Missing .cpu-unified-chart-canvas rule in static/styles.css")
        canvas_body = canvas_match.group(1)
        self.assertIn("height: 150px", canvas_body)
        self.assertIn("border: none", canvas_body)
        self.assertIn("border-radius: 0", canvas_body)

        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

const idx = code.indexOf('function drawCpuCoreGraph(');
if (idx === -1) throw new Error('drawCpuCoreGraph not found');
let depth = 0, started = false, i = idx;
while (i < code.length) {
  if (code[i] === '{') { depth++; started = true; }
  else if (code[i] === '}') { depth--; if (started && depth === 0) { i++; break; } }
  i++;
}
const fnSource = code.slice(idx, i);

const texts = [];
const linePoints = [];
const mockCtx = {
  save() {},
  restore() {},
  scale() {},
  clearRect() {},
  fillRect() {},
  beginPath() {},
  closePath() {},
  fill() {},
  arc() {},
  createLinearGradient: () => ({ addColorStop() {} }),
  moveTo(x, y) { linePoints.push({ type: 'move', x, y }); },
  lineTo(x, y) { linePoints.push({ type: 'line', x, y }); },
  stroke() {},
  fillText(text, x, y) { texts.push({ text: String(text), x, y }); },
  setLineDash() {},
  strokeStyle: '',
  fillStyle: '',
  lineWidth: 1,
  font: '',
  textAlign: '',
  textBaseline: '',
  lineJoin: '',
  lineCap: '',
};
const canvas = {
  width: 600,
  height: 150,
  getBoundingClientRect: () => ({ width: 600, height: 150 }),
  getContext: () => mockCtx,
};

const sandbox = {
  window: { devicePixelRatio: 1 },
  CPU_CORE_HISTORY_LIMIT: 40,
  CPU_CORE_COLORS: ['#38bdf8', '#34d399'],
};
vm.createContext(sandbox);
vm.runInContext(fnSource, sandbox);

sandbox.drawCpuCoreGraph(canvas, [[0, 0, 0, 0], [0, 0, 0, 0]]);
const expectedLabels = ['100%', '75%', '50%', '25%', '0%'];
for (const lbl of expectedLabels) {
  if (!texts.find(t => t.text === lbl)) {
    throw new Error('Missing Y-axis label: ' + lbl);
  }
}
const zeroLabel = texts.find(t => t.text === '0%');
if (zeroLabel.y !== 132.5) {
  throw new Error('Expected 0% label at y=132.5 in 150px canvas, got: ' + zeroLabel.y);
}
if (zeroLabel.y > canvas.height - 10 || zeroLabel.y < 10) {
  throw new Error('0% label Y coordinate is clipped: ' + zeroLabel.y);
}
// In 150px canvas: padTop=14, padBottom=18 => plotH=118. 0% trace is at y = padTop + plotH = 132.
const traceMoves = linePoints.filter(p => p.type === 'move' && p.y === 132);
if (traceMoves.length === 0) throw new Error('No 0% core trace drawn at y=132');
console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], cwd=str(root), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"drawCpuCoreGraph VM test failed: {proc.stderr or proc.stdout}")
        self.assertIn("OK", proc.stdout)

    def test_refinement_4_docker_volumes_remove_and_prune_contracts(self):
        """Verify frontend and backend contracts for Docker volume removal and pruning."""
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="btn-prune-volumes"', html)
        self.assertIn("Prune Unused Volumes", html)
        self.assertIn("async function handlePruneVolumes(", js)
        self.assertIn("async function handleRemoveVolume(", js)
        self.assertIn("/api/docker/volumes/prune", js)
        self.assertIn("data-vol-delete", js)

    @patch("main.get_ufw_firewall_status")
    @patch("main._run_fail2ban_command")
    @patch("subprocess.run")
    @patch("shutil.which")
    @patch("psutil.users")
    def test_get_ssh_security_telemetry_parsing_and_correlation(
        self,
        mock_users,
        mock_which,
        mock_run,
        mock_f2b_cmd,
        mock_ufw_status,
    ):
        """Verify get_ssh_security_telemetry parses active sessions, last logins, fail2ban, UFW bans, and auth.log failures."""
        from collections import namedtuple
        SUser = namedtuple("SUser", ["name", "terminal", "host", "started", "pid"])
        mock_users.return_value = [
            SUser(name="anthony", terminal="pts/0", host="198.51.100.25", started=1700000000.0, pid=1234),
            SUser(name="root", terminal="tty1", host="", started=1700000100.0, pid=5678),
        ]

        def fake_which(cmd):
            if cmd in ("last", "fail2ban-client"):
                return f"/usr/bin/{cmd}"
            return None

        mock_which.side_effect = fake_which

        last_proc = MagicMock()
        last_proc.returncode = 0
        last_proc.stdout = (
            "anthony  pts/0        Tue Nov 14 10:00   still logged in   198.51.100.25\n"
            "reboot   system boot  Tue Nov 14 09:00   still running     6.8.0-generic\n"
            "admin    pts/1        Mon Nov 13 18:20 - 19:00  (00:40)    203.0.113.10\n"
            "\n"
            "wtmp begins Mon Nov  1 00:00:00 2026\n"
        )
        mock_run.return_value = last_proc

        f2b_output = (
            "Status for the jail: sshd\n"
            "|- Filter\n"
            "|  |- Currently failed:\t3\n"
            "|  |- Total failed:\t42\n"
            "|  `- File list:\t/var/log/auth.log\n"
            "`- Actions\n"
            "   |- Currently banned:\t2\n"
            "   |- Total banned:\t7\n"
            "   `- Banned IP list:\t203.0.113.50 198.51.100.99\n"
        )
        mock_f2b_cmd.return_value = (True, f2b_output)

        mock_ufw_status.return_value = {
            "available": True,
            "enabled": True,
            "default_incoming": "deny",
            "default_outgoing": "allow",
            "rules": [
                {"index": 1, "to_port": "Anywhere", "proto": "any", "action": "DENY", "from_ip": "203.0.113.50", "v6": False},
                {"index": 2, "to_port": "22", "proto": "tcp", "action": "REJECT", "from_ip": "192.0.2.88", "v6": False},
                {"index": 3, "to_port": "80", "proto": "tcp", "action": "ALLOW", "from_ip": "Anywhere", "v6": False},
                {"index": 4, "to_port": "53", "proto": "udp", "action": "DENY", "from_ip": "Anywhere", "v6": False},
            ],
        }

        sample_auth_log = "\n".join([
            "2026-11-04T12:01:01Z server sshd[1001]: Failed password for invalid user admin from 203.0.113.50 port 54321 ssh2",
            "2026-11-04T12:01:05Z server sshd[1002]: Failed password for root from 203.0.113.50 port 54322 ssh2",
            "2026-11-04T12:02:10Z server sshd[1003]: Invalid user deploy from 198.51.100.77 port 41111",
            "2026-11-04T12:03:15Z server sshd[1004]: Connection closed by authenticating user ubuntu 198.51.100.77 port 41112 [preauth]",
            "2026-11-04T12:04:20Z server sshd[1005]: Disconnecting invalid user oracle 203.0.113.50 port 54329 [preauth]",
            "2026-11-04T12:05:00Z server sshd[1006]: PAM 2 more authentication failures; logname= uid=0 euid=0 tty=ssh rhost=198.51.100.200  user=root",
        ])

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write(sample_auth_log)
            temp_log_path = Path(tf.name)

        try:
            with patch.object(main, "SSH_AUTH_LOG_PATH", temp_log_path):
                telemetry = main.get_ssh_security_telemetry()
        finally:
            temp_log_path.unlink(missing_ok=True)

        # Active sessions
        self.assertEqual(len(telemetry["active_sessions"]), 2)
        self.assertEqual(telemetry["active_sessions"][0]["username"], "anthony")
        self.assertEqual(telemetry["active_sessions"][0]["host"], "198.51.100.25")
        self.assertEqual(telemetry["active_sessions"][0]["pid"], 1234)
        self.assertEqual(telemetry["active_sessions"][1]["username"], "root")
        self.assertEqual(telemetry["active_sessions"][1]["host"], "local")

        # Recent logins
        self.assertEqual(len(telemetry["recent_logins"]), 2)
        self.assertEqual(telemetry["recent_logins"][0]["username"], "anthony")
        self.assertEqual(telemetry["recent_logins"][0]["host"], "198.51.100.25")
        self.assertEqual(telemetry["recent_logins"][1]["username"], "admin")
        self.assertEqual(telemetry["recent_logins"][1]["host"], "203.0.113.10")

        # Fail2ban
        f2b = telemetry["fail2ban"]
        self.assertTrue(f2b["installed"])
        self.assertTrue(f2b["active"])
        self.assertEqual(f2b["jail"], "sshd")
        self.assertEqual(f2b["currently_failed"], 3)
        self.assertEqual(f2b["total_failed"], 42)
        self.assertEqual(f2b["currently_banned"], 2)
        self.assertEqual(f2b["total_banned"], 7)
        self.assertEqual(f2b["banned_ips"], ["203.0.113.50", "198.51.100.99"])

        # UFW & unified banned IPs
        self.assertEqual(telemetry["ufw_banned_ips"], ["203.0.113.50", "192.0.2.88"])
        banned_map = {item["ip"]: item["sources"] for item in telemetry["banned_ips"]}
        self.assertEqual(banned_map["203.0.113.50"], ["ufw", "fail2ban"])
        self.assertEqual(banned_map["192.0.2.88"], ["ufw"])
        self.assertEqual(banned_map["198.51.100.99"], ["fail2ban"])

        # Brute-force log parsing
        self.assertEqual(telemetry["failed_attempts_total"], 6)
        self.assertEqual(len(telemetry["recent_failures"]), 6)
        self.assertEqual(telemetry["recent_failures"][0]["ip"], "198.51.100.200")
        self.assertEqual(telemetry["recent_failures"][0]["username"], "root")

        # Top offenders (sorted by is_banned desc, count desc; includes zero-count banned IPs)
        offenders = telemetry["top_offenders"]
        offender_by_ip = {o["ip"]: o for o in offenders}
        self.assertIn("203.0.113.50", offender_by_ip)
        self.assertEqual(offender_by_ip["203.0.113.50"]["count"], 3)
        self.assertEqual(offender_by_ip["203.0.113.50"]["targeted_users"], ["admin", "oracle", "root"])
        self.assertTrue(offender_by_ip["203.0.113.50"]["is_banned"])
        self.assertEqual(offender_by_ip["203.0.113.50"]["ban_sources"], ["ufw", "fail2ban"])

        # Banned IPs with 0 log occurrences must also appear in top_offenders so user can unban them
        self.assertIn("192.0.2.88", offender_by_ip)
        self.assertEqual(offender_by_ip["192.0.2.88"]["count"], 0)
        self.assertTrue(offender_by_ip["192.0.2.88"]["is_banned"])

        self.assertIn("198.51.100.99", offender_by_ip)
        self.assertEqual(offender_by_ip["198.51.100.99"]["count"], 0)
        self.assertTrue(offender_by_ip["198.51.100.99"]["is_banned"])

        # Unbanned attacker with 2 failures
        self.assertIn("198.51.100.77", offender_by_ip)
        self.assertEqual(offender_by_ip["198.51.100.77"]["count"], 2)
        self.assertFalse(offender_by_ip["198.51.100.77"]["is_banned"])
        self.assertEqual(offender_by_ip["198.51.100.77"]["targeted_users"], ["deploy", "ubuntu"])

        # First entry in top_offenders should be banned with highest count (203.0.113.50)
        self.assertEqual(offenders[0]["ip"], "203.0.113.50")

    @patch("main.get_ssh_security_telemetry")
    @patch("main.get_ufw_firewall_status")
    @patch("main._run_fail2ban_command")
    @patch("main._run_ufw_command")
    @patch("shutil.which")
    def test_manage_ssh_ip_ban_and_validation(
        self,
        mock_which,
        mock_ufw_cmd,
        mock_f2b_cmd,
        mock_fw_status,
        mock_ssh_telemetry,
    ):
        """Verify manage_ssh_ip_ban ban/unban execution, audit events, and validation against loopback/self-lockout."""
        mock_which.return_value = "/usr/bin/fail2ban-client"
        mock_ufw_cmd.return_value = (True, "Rule added")
        mock_f2b_cmd.return_value = (True, "1")
        mock_fw_status.return_value = {
            "available": True,
            "enabled": True,
            "rules": [
                {"index": 2, "to_port": "Anywhere", "proto": "any", "action": "DENY", "from_ip": "203.0.113.50", "v6": False}
            ],
        }
        mock_ssh_telemetry.return_value = {"active_sessions": [], "banned_ips": []}

        # 1. Invalid action
        with self.assertRaises(ValueError):
            main.manage_ssh_ip_ban("drop", "203.0.113.50")

        # 2. Invalid IP
        with self.assertRaises(ValueError):
            main.manage_ssh_ip_ban("ban", "999.999.999.999")
        with self.assertRaises(ValueError):
            main.manage_ssh_ip_ban("ban", "")

        # 3. Loopback & unspecified protection
        for forbidden_ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "::", "127.0.0.0/8"):
            with self.assertRaises(ValueError):
                main.manage_ssh_ip_ban("ban", forbidden_ip)

        # 4. Self-lockout protection (single IP and CIDR containing client_ip)
        with self.assertRaises(ValueError) as ctx:
            main.manage_ssh_ip_ban("ban", "198.51.100.42", actor="admin", client_ip="198.51.100.42")
        self.assertIn("Self-lockout", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            main.manage_ssh_ip_ban("ban", "198.51.100.0/24", actor="admin", client_ip="198.51.100.42")
        self.assertIn("Self-lockout", str(ctx2.exception))

        # 5. Successful ban
        res_ban = main.manage_ssh_ip_ban("ban", "203.0.113.50", actor="admin", client_ip="198.51.100.42")
        self.assertEqual(res_ban["status"], "ok")
        self.assertEqual(res_ban["action"], "ban")
        self.assertEqual(res_ban["ip"], "203.0.113.50")
        mock_ufw_cmd.assert_any_call(["ufw", "insert", "1", "deny", "from", "203.0.113.50", "to", "any"])
        mock_f2b_cmd.assert_any_call(["fail2ban-client", "set", "sshd", "banip", "203.0.113.50"])

        ban_logs, ban_count = main.get_audit_logs(event_type="security.ip.banned")
        self.assertGreaterEqual(ban_count, 1)
        self.assertEqual(ban_logs[0]["actor"], "admin")
        self.assertEqual(ban_logs[0]["metadata"]["target"], "203.0.113.50")

        # 6. Fallback when ufw insert 1 fails (e.g. empty rule table)
        mock_ufw_cmd.reset_mock()
        mock_ufw_cmd.side_effect = [(False, "Invalid position '1'"), (True, "Rule added")]
        res_ban_cidr = main.manage_ssh_ip_ban("ban", "203.0.113.0/24", actor="admin", client_ip="198.51.100.42")
        self.assertEqual(res_ban_cidr["status"], "ok")
        self.assertEqual(res_ban_cidr["ip"], "203.0.113.0/24")
        self.assertEqual(mock_ufw_cmd.call_count, 2)
        mock_ufw_cmd.assert_any_call(["ufw", "deny", "from", "203.0.113.0/24", "to", "any"])

        # 7. Successful unban
        mock_ufw_cmd.reset_mock()
        mock_ufw_cmd.side_effect = None
        mock_ufw_cmd.return_value = (True, "Rule deleted")
        res_unban = main.manage_ssh_ip_ban("unban", "203.0.113.50", actor="admin", client_ip="198.51.100.42")
        self.assertEqual(res_unban["status"], "ok")
        self.assertEqual(res_unban["action"], "unban")
        self.assertEqual(res_unban["ip"], "203.0.113.50")
        mock_ufw_cmd.assert_any_call(["ufw", "--force", "delete", "deny", "from", "203.0.113.50", "to", "any"])
        mock_ufw_cmd.assert_any_call(["ufw", "--force", "delete", "2"])
        mock_f2b_cmd.assert_any_call(["fail2ban-client", "set", "sshd", "unbanip", "203.0.113.50"])

        unban_logs, unban_count = main.get_audit_logs(event_type="security.ip.unbanned")
        self.assertGreaterEqual(unban_count, 1)
        self.assertEqual(unban_logs[0]["metadata"]["target"], "203.0.113.50")

    @patch("main.manage_ssh_ip_ban")
    @patch("main.get_ssh_security_telemetry")
    def test_api_ssh_security_endpoints(self, mock_ssh_telemetry, mock_manage_ban):
        """Verify GET /api/network/ssh-security and POST /api/network/ssh-security/ban endpoints."""
        # Unauthenticated requests must return 401
        self.assertEqual(asgi_get(api.app, "/api/network/ssh-security").status_code, 401)
        self.assertEqual(
            asgi_post(api.app, "/api/network/ssh-security/ban", json_body={"action": "ban", "ip": "203.0.113.50"}).status_code,
            401,
        )

        mock_ssh_telemetry.return_value = {
            "active_sessions": [{"username": "admin", "terminal": "pts/0", "host": "local", "started": 1700000000.0, "started_iso": "2023-11-14 22:13:20", "pid": 111}],
            "recent_logins": [],
            "fail2ban": {"installed": True, "active": True, "jail": "sshd", "currently_failed": 0, "total_failed": 0, "currently_banned": 1, "total_banned": 1, "banned_ips": ["203.0.113.50"]},
            "ufw_banned_ips": ["203.0.113.50"],
            "banned_ips": [{"ip": "203.0.113.50", "sources": ["ufw", "fail2ban"]}],
            "failed_attempts_total": 5,
            "top_offenders": [{"ip": "203.0.113.50", "count": 5, "targeted_users": ["root"], "last_seen": "Nov  4 12:00:00", "is_banned": True, "ban_sources": ["ufw", "fail2ban"]}],
            "recent_failures": [],
        }

        resp_get = asgi_get(api.app, "/api/network/ssh-security", cookies=self.auth_cookies)
        self.assertEqual(resp_get.status_code, 200)
        data = resp_get.json()
        self.assertEqual(data["failed_attempts_total"], 5)
        self.assertEqual(len(data["active_sessions"]), 1)
        self.assertEqual(data["banned_ips"][0]["ip"], "203.0.113.50")

        mock_manage_ban.return_value = {
            "status": "ok",
            "action": "ban",
            "ip": "203.0.113.50",
            "message": "Banned IP 203.0.113.50",
            "ssh_security": mock_ssh_telemetry.return_value,
            "firewall": {"available": True, "enabled": True, "rules": []},
        }
        resp_post = asgi_post(
            api.app,
            "/api/network/ssh-security/ban",
            json_body={"action": "ban", "ip": "203.0.113.50"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_post.status_code, 200)
        self.assertEqual(resp_post.json()["status"], "ok")
        mock_manage_ban.assert_called_once()

        # ValueError -> 400
        mock_manage_ban.side_effect = ValueError("Cannot ban loopback address '127.0.0.1'.")
        resp_400 = asgi_post(
            api.app,
            "/api/network/ssh-security/ban",
            json_body={"action": "ban", "ip": "127.0.0.1"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_400.status_code, 400)
        self.assertIn("loopback", resp_400.json()["detail"])

        # RuntimeError -> 500
        mock_manage_ban.side_effect = RuntimeError("UFW execution failed")
        resp_500 = asgi_post(
            api.app,
            "/api/network/ssh-security/ban",
            json_body={"action": "ban", "ip": "203.0.113.99"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(resp_500.status_code, 500)
        self.assertIn("UFW execution failed", resp_500.json()["detail"])

        # GET /api/network/security unauthenticated -> 401
        self.assertEqual(asgi_get(api.app, "/api/network/security").status_code, 401)

        # GET /api/network/security authenticated -> 200 with ports, summary, firewall, ssh_security
        with (
            patch("main.get_open_listening_ports", return_value=[]),
            patch("main.get_ufw_firewall_status", return_value={"available": True, "enabled": True, "rules": []}),
        ):
            resp_net_sec = asgi_get(api.app, "/api/network/security", cookies=self.auth_cookies)
            self.assertEqual(resp_net_sec.status_code, 200)
            net_sec_data = resp_net_sec.json()
            self.assertIn("ports", net_sec_data)
            self.assertIn("summary", net_sec_data)
            self.assertIn("firewall", net_sec_data)
            self.assertIn("ssh_security", net_sec_data)
            self.assertEqual(net_sec_data["ssh_security"]["failed_attempts_total"], 5)

    def test_feature_b_ssh_security_frontend_dom_and_vm_contracts(self):
        """Verify Feature B SSH Intrusion Detection, Active Sessions & IP Ban Manager DOM, CSS, and renderSshSecurity VM execution."""
        import subprocess
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        required_ssh_ids = [
            "sec-active-sessions-count",
            "sec-failed-ssh-count",
            "sec-banned-ips-count",
            "sec-ssh-card",
            "sec-fail2ban-badge",
            "sec-ban-ip-input",
            "sec-ban-ip-btn",
            "sec-ssh-offenders-table",
            "sec-ssh-offenders-tbody",
            "sec-sessions-card",
            "sec-sessions-badge",
            "sec-sessions-table",
            "sec-sessions-tbody",
            "sec-recent-auth-table",
            "sec-recent-auth-tbody",
        ]
        for dom_id in required_ssh_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        for fn_name in [
            "function renderSshSecurity(",
            "async function submitSshIpBan(",
        ]:
            self.assertIn(fn_name, js, f"Missing {fn_name} in static/app.js")

        for css_sel in [
            ".sec-manual-ban-bar",
            ".sec-ban-input",
            ".btn-ban-ip",
            ".btn-unban-ip",
            "#sec-ssh-offenders-table",
            "#sec-sessions-table",
            "#sec-recent-auth-table",
        ]:
            self.assertIn(css_sel, css, f"Missing {css_sel} in static/styles.css")

        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) {
    idx = source.indexOf('function ' + name + '(');
  }
  if (idx === -1) throw new Error('Not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') { depth--; if (started && depth === 0) return source.slice(idx, i + 1); }
    i++;
  }
  throw new Error('Unbalanced: ' + name);
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      innerHTML: '',
      value: '',
      className: '',
    };
  }
  return elements[id];
}

const sandbox = {
  document: { getElementById: getEl },
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
};
vm.createContext(sandbox);
vm.runInContext(extractFunc(code, 'renderSshSecurity'), sandbox);

const sshPayload = {
  active_sessions: [
    { username: 'anthony', terminal: 'pts/0', host: '198.51.100.25', pid: 4321, started: 1700000000, started_iso: '2026-11-04 12:00:00' }
  ],
  recent_logins: [
    { username: 'anthony', terminal: 'pts/0', host: '198.51.100.25', time_range: 'Wed Nov 4 12:00 still logged in', raw: 'anthony pts/0 198.51.100.25' }
  ],
  fail2ban: {
    installed: true,
    active: true,
    jail: 'sshd',
    currently_failed: 2,
    total_failed: 12,
    currently_banned: 1,
    total_banned: 4,
    banned_ips: ['203.0.113.50']
  },
  ufw_banned_ips: ['203.0.113.50'],
  banned_ips: [
    { ip: '203.0.113.50', sources: ['ufw', 'fail2ban'] }
  ],
  failed_attempts_total: 8,
  top_offenders: [
    { ip: '203.0.113.50', count: 6, targeted_users: ['root', 'admin'], last_seen: '2026-11-04T12:01:05Z', is_banned: true, ban_sources: ['ufw', 'fail2ban'] },
    { ip: '198.51.100.77', count: 2, targeted_users: ['deploy'], last_seen: '2026-11-04T12:02:10Z', is_banned: false, ban_sources: [] }
  ],
  recent_failures: [
    { timestamp: '2026-11-04T12:02:10Z', ip: '198.51.100.77', username: 'deploy', reason: 'Invalid user' }
  ]
};

sandbox.renderSshSecurity(sshPayload);

if (getEl('sec-active-sessions-count').textContent !== '1') throw new Error('Bad active sessions count: ' + getEl('sec-active-sessions-count').textContent);
if (getEl('sec-failed-ssh-count').textContent !== '8') throw new Error('Bad failed ssh count: ' + getEl('sec-failed-ssh-count').textContent);
if (getEl('sec-banned-ips-count').textContent !== '1 Banned') throw new Error('Bad banned ips count: ' + getEl('sec-banned-ips-count').textContent);
if (!getEl('sec-fail2ban-badge').textContent.includes('FAIL2BAN: ACTIVE (1 Banned)')) throw new Error('Bad fail2ban badge: ' + getEl('sec-fail2ban-badge').textContent);
if (!getEl('sec-ssh-offenders-tbody').innerHTML.includes('data-unban-ip="203.0.113.50"')) throw new Error('Missing unban button for 203.0.113.50');
if (!getEl('sec-ssh-offenders-tbody').innerHTML.includes('BANNED (UFW + FAIL2BAN)')) throw new Error('Missing BANNED (UFW + FAIL2BAN) badge');
if (!getEl('sec-ssh-offenders-tbody').innerHTML.includes('data-ban-ip="198.51.100.77"')) throw new Error('Missing ban button for 198.51.100.77');
if (getEl('sec-sessions-badge').textContent !== '1 Active') throw new Error('Bad sessions badge: ' + getEl('sec-sessions-badge').textContent);
if (!getEl('sec-sessions-tbody').innerHTML.includes('anthony') || !getEl('sec-sessions-tbody').innerHTML.includes('pts/0')) throw new Error('Missing active session row');
if (!getEl('sec-recent-auth-tbody').innerHTML.includes('AUTH FAIL') || !getEl('sec-recent-auth-tbody').innerHTML.includes('LOGIN')) throw new Error('Missing recent auth fail or login rows');
console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], cwd=str(root), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"renderSshSecurity VM test failed: {proc.stderr or proc.stdout}")
        self.assertIn("OK", proc.stdout)


class TestBackupsAndCronScheduler(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_backups_cron.db"
        main.init_state_db(self.db_path)
        # Clear default seeded jobs so test starts with clean slate
        conn = main._connect_state_db(self.db_path)
        conn.execute("DELETE FROM cron_jobs")
        conn.commit()
        conn.close()
        self.db_patcher1 = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher2 = patch.object(main, "STATE_DB_PATH", self.db_path)
        self.db_patcher1.start()
        self.db_patcher2.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}
        self.auth_headers = {"Authorization": f"Bearer {self.session_token}"}

        self.sample_src = Path(self.temp_dir.name) / "sample_data"
        self.sample_src.mkdir(parents=True, exist_ok=True)
        (self.sample_src / "app.py").write_text("print('hello world')", encoding="utf-8")
        (self.sample_src / "config.json").write_text('{"env": "test"}', encoding="utf-8")
        (self.sample_src / "debug.log").write_text("dummy log content", encoding="utf-8")
        sub_dir = self.sample_src / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "data.txt").write_text("sub file content", encoding="utf-8")

    def tearDown(self):
        self.db_patcher2.stop()
        self.db_patcher1.stop()
        self.temp_dir.cleanup()

    def test_get_backups_dir_resolution(self):
        backups_dir = main.get_backups_dir(self.db_path)
        self.assertTrue(backups_dir.exists())
        self.assertTrue(backups_dir.is_dir())
        self.assertEqual(backups_dir, self.db_path.parent / "backups")

        # Test configurable backup_dir when using DEFAULT_STATE_DB_PATH
        custom_env_dir = Path(self.temp_dir.name) / "custom_env_backups"
        with patch.dict(os.environ, {"VIGIL_BACKUP_DIR": str(custom_env_dir)}):
            resolved = main.get_backups_dir(main.DEFAULT_STATE_DB_PATH)
            self.assertEqual(resolved, custom_env_dir.resolve())
            self.assertTrue(custom_env_dir.is_dir())

        # Test app_settings backup_dir override with DEFAULT_STATE_DB_PATH
        custom_settings_dir = Path(self.temp_dir.name) / "custom_settings_backups"
        with patch.dict(os.environ, {"VIGIL_BACKUP_DIR": ""}):
            with patch.object(
                main,
                "get_app_settings",
                return_value={"backup_dir": str(custom_settings_dir)},
            ):
                resolved_cfg = main.get_backups_dir(main.DEFAULT_STATE_DB_PATH)
                self.assertEqual(resolved_cfg, custom_settings_dir.resolve())
                self.assertTrue(custom_settings_dir.is_dir())

    def test_security_guards_path_validation(self):
        with self.assertRaises(ValueError):
            main._validate_safe_host_directory("/", purpose="test")
        with self.assertRaises(ValueError):
            main._validate_safe_host_directory("/proc", purpose="test")
        with self.assertRaises(ValueError):
            main._validate_safe_host_directory("/sys/kernel", purpose="test")
        with self.assertRaises(ValueError):
            main._validate_safe_host_directory("", purpose="test")

        with self.assertRaises(ValueError):
            main._validate_archive_filename("../etc/passwd")
        with self.assertRaises(ValueError):
            main._validate_archive_filename("subdir/test.tar.gz")
        with self.assertRaises(ValueError):
            main._validate_archive_filename("")

    def test_create_backup_archive_with_excludes_retention_and_sha256(self):
        archive1 = main.create_backup_archive(
            target_type="directory",
            source_path=str(self.sample_src),
            target_name="retention-demo",
            excludes=["*.log"],
            retain_count=2,
            actor="admin",
            db_path=self.db_path,
        )
        self.assertEqual(archive1["target_type"], "directory")
        self.assertEqual(archive1["target_name"], "retention-demo")
        self.assertEqual(archive1["file_count"], 3)  # app.py, config.json, sub/data.txt
        self.assertEqual(len(archive1["sha256"]), 64)
        self.assertGreater(archive1["size_bytes"], 0)
        self.assertTrue(archive1["exists_on_disk"])

        # Check archive exists in list
        archives = main.list_backup_archives(self.db_path)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["filename"], archive1["filename"])

        # Create 2 more archives to test retain_count=2 pruning
        time.sleep(1.05)  # Ensure unique second timestamp
        archive2 = main.create_backup_archive(
            target_type="directory",
            source_path=str(self.sample_src),
            target_name="retention-demo",
            retain_count=2,
            actor="admin",
            db_path=self.db_path,
        )
        time.sleep(1.05)
        archive3 = main.create_backup_archive(
            target_type="directory",
            source_path=str(self.sample_src),
            target_name="retention-demo",
            retain_count=2,
            actor="admin",
            db_path=self.db_path,
        )

        archives_after = main.list_backup_archives(self.db_path)
        self.assertEqual(len(archives_after), 2)
        archive_names = [a["filename"] for a in archives_after]
        self.assertIn(archive3["filename"], archive_names)
        self.assertIn(archive2["filename"], archive_names)
        self.assertNotIn(archive1["filename"], archive_names)

        # File for archive1 was pruned from disk
        backups_dir = main.get_backups_dir(self.db_path)
        self.assertFalse((backups_dir / archive1["filename"]).exists())

    def test_restore_and_delete_backup_archive(self):
        archive = main.create_backup_archive(
            target_type="directory",
            source_path=str(self.sample_src),
            target_name="restore-test",
            excludes=["*.log"],
            retain_count=5,
            actor="admin",
            db_path=self.db_path,
        )

        restore_dest = Path(self.temp_dir.name) / "restore_dest"
        restore_dest.mkdir(parents=True, exist_ok=True)
        res = main.restore_backup_archive(
            filename=archive["filename"],
            destination_path=str(restore_dest),
            actor="admin",
            db_path=self.db_path,
        )
        self.assertTrue(res["restored"])
        self.assertEqual(res["restored_files"], 3)
        self.assertTrue((restore_dest / "sample_data" / "app.py").is_file())
        self.assertEqual(
            (restore_dest / "sample_data" / "app.py").read_text(encoding="utf-8"),
            "print('hello world')",
        )
        self.assertFalse((restore_dest / "sample_data" / "debug.log").exists())

        # Unsafe destination rejected
        with self.assertRaises(ValueError):
            main.restore_backup_archive(
                filename=archive["filename"],
                destination_path="/",
                actor="admin",
                db_path=self.db_path,
            )

        # Nonexistent archive restore raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            main.restore_backup_archive(
                filename="nonexistent-archive.tar.gz",
                destination_path=str(restore_dest),
                actor="admin",
                db_path=self.db_path,
            )

        # Delete archive
        del_res = main.delete_backup_archive(
            archive["filename"], actor="admin", db_path=self.db_path
        )
        self.assertTrue(del_res["deleted"])
        self.assertEqual(len(main.list_backup_archives(self.db_path)), 0)

        # Deleting again raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            main.delete_backup_archive(
                archive["filename"], actor="admin", db_path=self.db_path
            )

    def test_save_list_and_delete_backup_target(self):
        target = main.save_backup_target(
            target_data={
                "name": "Project Data",
                "target_type": "directory",
                "source_path": str(self.sample_src),
                "excludes": ["*.log", ".git/*"],
                "retain_count": 5,
            },
            actor="admin",
            db_path=self.db_path,
        )
        self.assertGreater(target["id"], 0)
        self.assertEqual(target["name"], "Project Data")
        self.assertEqual(target["excludes"], ["*.log", ".git/*"])

        saved = main.list_backup_targets(self.db_path)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], target["id"])

        # Update target
        updated = main.save_backup_target(
            target_id=target["id"],
            name="Project Data Renamed",
            target_type="directory",
            source_path=str(self.sample_src),
            retain_count=10,
            actor="admin",
            db_path=self.db_path,
        )
        self.assertEqual(updated["name"], "Project Data Renamed")
        self.assertEqual(updated["retain_count"], 10)

        # Delete target
        del_res = main.delete_backup_target(target["id"], actor="admin", db_path=self.db_path)
        self.assertTrue(del_res["deleted"])
        self.assertEqual(len(main.list_backup_targets(self.db_path)), 0)

        # Deleting again raises ValueError
        with self.assertRaises(ValueError):
            main.delete_backup_target(target["id"], actor="admin", db_path=self.db_path)

    def test_get_backups_overview(self):
        overview = main.get_backups_overview(self.db_path)
        self.assertIn("backup_dir", overview)
        self.assertIn("total_archives", overview)
        self.assertIn("total_size_bytes", overview)
        self.assertIn("saved_targets", overview)
        self.assertIn("archives", overview)
        self.assertNotIn("stack_targets", overview)

    def test_cron_expression_validation_and_next_timestamp(self):
        self.assertTrue(main.validate_cron_expression("*/5 * * * *"))
        self.assertTrue(main.validate_cron_expression("0 0 * * *"))
        self.assertTrue(main.validate_cron_expression("30 4 1,15 * 1-5"))
        self.assertTrue(main.validate_cron_expression("0 12 1 jan-dec sun"))

        self.assertFalse(main.validate_cron_expression("not a cron"))
        self.assertFalse(main.validate_cron_expression("* * * *"))  # 4 fields
        self.assertFalse(main.validate_cron_expression("60 * * * *"))  # minute out of range
        self.assertFalse(main.validate_cron_expression("0 25 * * *"))  # hour out of range
        self.assertFalse(main.validate_cron_expression("0 0 32 * *"))  # day out of range
        self.assertFalse(main.validate_cron_expression("0 0 1 13 *"))  # month out of range
        self.assertFalse(main.validate_cron_expression("0 0 1 1 8"))  # dow out of range

        next_ts = main.get_next_cron_run_timestamp("*/15 * * * *", now_ts=1700000000.0)
        self.assertGreater(next_ts, 1700000000.0)

    def test_cron_job_crud_and_toggle(self):
        initial_jobs = main.list_cron_jobs(self.db_path)["jobs"]
        initial_count = len(initial_jobs)
        job = main.create_or_update_cron_job(
            {
                "name": "Hourly Directory Backup",
                "task_type": "backup.directory",
                "cron_expression": "0 * * * *",
                "task_config": {
                    "source_path": str(self.sample_src),
                    "target_name": "hourly-dir",
                    "retain_count": 5,
                },
                "enabled": True,
                "notify_on_failure": True,
                "notify_on_success": False,
            },
            actor="admin",
            db_path=self.db_path,
        )
        jid = job["id"]
        self.assertGreater(jid, 0)
        self.assertEqual(job["name"], "Hourly Directory Backup")
        self.assertTrue(job["enabled"])
        self.assertGreater(job["next_run_at"], 0)

        # Toggle job off and on
        toggled_off = main.toggle_cron_job(jid, enabled=False, actor="admin", db_path=self.db_path)
        self.assertFalse(toggled_off["enabled"])

        toggled_on = main.toggle_cron_job(jid, actor="admin", db_path=self.db_path)
        self.assertTrue(toggled_on["enabled"])

        # Update job
        updated = main.create_or_update_cron_job(
            {
                "id": jid,
                "name": "Bi-Hourly Directory Backup",
                "task_type": "backup.directory",
                "cron_expression": "0 */2 * * *",
                "task_config": {
                    "source_path": str(self.sample_src),
                    "target_name": "bihourly-dir",
                },
                "enabled": True,
                "notify_on_failure": False,
                "notify_on_success": True,
            },
            actor="admin",
            db_path=self.db_path,
        )
        self.assertEqual(updated["id"], jid)
        self.assertEqual(updated["name"], "Bi-Hourly Directory Backup")
        self.assertEqual(updated["cron_expression"], "0 */2 * * *")
        self.assertFalse(updated["notify_on_failure"])
        self.assertTrue(updated["notify_on_success"])

        # Directly verify all updated columns in cron_jobs table
        conn = main._connect_state_db(self.db_path)
        row = conn.execute(
            """
            SELECT name, task_type, cron_expression, task_config_json,
                   enabled, notify_on_failure, notify_on_success, next_run_at, updated_at
            FROM cron_jobs WHERE id = ?
            """,
            (jid,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Bi-Hourly Directory Backup")
        self.assertEqual(row[1], "backup.directory")
        self.assertEqual(row[2], "0 */2 * * *")
        self.assertIn("bihourly-dir", row[3])
        self.assertEqual(row[4], 1)
        self.assertEqual(row[5], 0)
        self.assertEqual(row[6], 1)
        self.assertGreater(row[7], 0)
        self.assertGreater(row[8], 0)

        # Updating non-existent job ID raises ValueError
        with self.assertRaises(ValueError):
            main.create_or_update_cron_job(
                {
                    "id": 99999,
                    "name": "Missing Job",
                    "task_type": "docker.prune",
                    "cron_expression": "0 0 * * *",
                },
                actor="admin",
                db_path=self.db_path,
            )

        # Status reporting tasks default notify_on_success to True
        smart_job = main.create_or_update_cron_job(
            {
                "name": "Default Notify Test",
                "task_type": "storage.smart_check",
                "cron_expression": "0 5 * * *",
            },
            actor="admin",
            db_path=self.db_path,
        )
        self.assertTrue(smart_job["notify_on_success"])
        main.delete_cron_job(smart_job["id"], actor="admin", db_path=self.db_path)

        # List cron jobs
        listing = main.list_cron_jobs(self.db_path)
        self.assertEqual(len(listing["jobs"]), initial_count + 1)
        job_ids = [j["id"] for j in listing["jobs"]]
        self.assertIn(jid, job_ids)
        self.assertGreater(len(listing["supported_task_types"]), 0)

        # Delete job
        del_res = main.delete_cron_job(jid, actor="admin", db_path=self.db_path)
        self.assertTrue(del_res["deleted"])
        self.assertEqual(len(main.list_cron_jobs(self.db_path)["jobs"]), initial_count)

        # Deleting non-existent job raises ValueError
        with self.assertRaises(ValueError):
            main.delete_cron_job(jid, actor="admin", db_path=self.db_path)

    def test_run_cron_job_now_and_evaluation(self):
        job = main.create_or_update_cron_job(
            {
                "name": "Immediate Backup Job",
                "task_type": "backup.directory",
                "cron_expression": "0 0 * * *",
                "task_config": {
                    "source_path": str(self.sample_src),
                    "target_name": "immediate-backup",
                },
                "enabled": True,
                "notify_on_failure": False,
                "notify_on_success": False,
            },
            actor="admin",
            db_path=self.db_path,
        )
        jid = job["id"]

        # Run immediately
        run_res = main.run_cron_job_now(jid, trigger_source="manual", actor="admin", db_path=self.db_path)
        self.assertEqual(run_res["status"], "success")
        self.assertEqual(run_res["job_id"], jid)
        self.assertIn("Created directory backup", run_res["output"])

        # Check job run history
        listing = main.list_cron_jobs(self.db_path)
        matching_job = [j for j in listing["jobs"] if j["id"] == jid][0]
        self.assertEqual(matching_job["last_status"], "success")
        self.assertGreater(len(listing["recent_runs"]), 0)
        self.assertEqual(listing["recent_runs"][0]["trigger_source"], "manual")

        # Evaluate due cron jobs: set next_run_at to the past
        conn = main._connect_state_db(self.db_path)
        conn.execute("UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (time.time() - 60, jid))
        conn.commit()
        conn.close()

        due_runs = main.evaluate_due_cron_jobs(now_ts=time.time(), db_path=self.db_path)
        self.assertEqual(len(due_runs), 1)
        self.assertEqual(due_runs[0]["job_id"], jid)
        self.assertEqual(due_runs[0]["status"], "success")
        self.assertEqual(due_runs[0]["trigger_source"], "schedule")

        # Run non-existent job raises ValueError
        with self.assertRaises(ValueError):
            main.run_cron_job_now(99999, actor="admin", db_path=self.db_path)

    def test_cron_other_supported_task_types(self):
        # 1. docker.prune
        with patch.object(main.DOCKER_MANAGER, "prune_images", return_value={"output": "images pruned"}) as mock_pi, \
             patch.object(main.DOCKER_MANAGER, "prune_volumes", return_value={"output": "volumes pruned"}) as mock_pv:
            job_docker = main.create_or_update_cron_job(
                {
                    "name": "Docker Cleanup",
                    "task_type": "docker.prune",
                    "cron_expression": "0 2 * * *",
                    "task_config": {"prune_images": True, "prune_volumes": True},
                },
                actor="admin",
                db_path=self.db_path,
            )
            run_docker = main.run_cron_job_now(job_docker["id"], actor="admin", db_path=self.db_path)
            self.assertEqual(run_docker["status"], "success")
            self.assertIn("Images pruned", run_docker["output"])
            self.assertIn("Volumes pruned", run_docker["output"])

        # 2. notifications.summary
        with patch.object(main.NotificationManager, "send_all", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"discord": (True, 200)}
            job_notif = main.create_or_update_cron_job(
                {
                    "name": "Daily Digest",
                    "task_type": "notifications.summary",
                    "cron_expression": "0 8 * * *",
                    "task_config": {},
                },
                actor="admin",
                db_path=self.db_path,
            )
            run_notif = main.run_cron_job_now(job_notif["id"], actor="admin", db_path=self.db_path)
            self.assertEqual(run_notif["status"], "success")

        # 3. storage.smart_check
        with patch("main.get_all_disks_smart_metrics", return_value=[{"device": "sda", "passed": True}]), \
             patch("main.check_raid_status", return_value=("clean", "md0 : active raid1 sda1[0] sdb1[1]")), \
             patch("main.get_duf_output", return_value="duf stats"), \
             patch.object(main.NotificationManager, "send_all", new_callable=AsyncMock) as mock_send_smart:
            mock_send_smart.return_value = {"discord": (True, 200)}
            job_smart = main.create_or_update_cron_job(
                {
                    "name": "SMART Check",
                    "task_type": "storage.smart_check",
                    "cron_expression": "0 1 * * *",
                    "task_config": {},
                },
                actor="admin",
                db_path=self.db_path,
            )
            self.assertTrue(job_smart["notify_on_success"])
            run_smart = main.run_cron_job_now(job_smart["id"], actor="admin", db_path=self.db_path)
            self.assertEqual(run_smart["status"], "success")
            self.assertIn("Checked 1 disk(s)", run_smart["output"])
            self.assertIn("posted status embed to Discord", run_smart["output"])
            mock_send_smart.assert_called_once()
            call_args = mock_send_smart.call_args[0]
            self.assertIn("Vigil RAID Status: CLEAN", call_args[0])
            self.assertIn("Checked 1 disk(s)", call_args[1])
            self.assertIsInstance(call_args[2], dict)
            self.assertEqual(call_args[2].get("color"), 0x2ECC71)

        # storage.smart_check with degraded raid and failed smart disk
        with patch("main.get_all_disks_smart_metrics", return_value=[{"device": "sdb", "passed": False, "status": "failed"}]), \
             patch("main.check_raid_status", return_value=("degraded", "md0 : degraded raid1 sda1[0]")), \
             patch("main.get_duf_output", return_value="duf stats degraded"), \
             patch.object(main.NotificationManager, "send_all", new_callable=AsyncMock) as mock_send_degraded:
            mock_send_degraded.return_value = {"discord": (True, 200)}
            run_degraded = main.run_cron_job_now(job_smart["id"], actor="admin", db_path=self.db_path)
            self.assertEqual(run_degraded["status"], "success")
            self.assertIn("failed_smart=1", run_degraded["output"])
            mock_send_degraded.assert_called_once()
            deg_args = mock_send_degraded.call_args[0]
            self.assertEqual(deg_args[0], "Vigil Storage Health Warning")
            self.assertEqual(deg_args[2].get("color"), 0xF39C12)
            self.assertTrue(any("S.M.A.R.T. Disks Failing" in f.get("name", "") for f in deg_args[2].get("fields", [])))

        # Invalid task_type raises ValueError
        with self.assertRaises(ValueError):
            main.create_or_update_cron_job(
                {
                    "name": "Invalid Task",
                    "task_type": "invalid.task.type",
                    "cron_expression": "* * * * *",
                },
                actor="admin",
                db_path=self.db_path,
            )

    def test_cron_initial_seed_defaults(self):
        seed_db = Path(self.temp_dir.name) / "test_initial_seed.db"
        main.init_state_db(seed_db)
        listing = main.list_cron_jobs(seed_db)["jobs"]
        notif_job = next((j for j in listing if j["task_type"] == "notifications.summary"), None)
        smart_job = next((j for j in listing if j["task_type"] == "storage.smart_check"), None)
        self.assertIsNotNone(notif_job)
        self.assertIsNotNone(smart_job)
        self.assertTrue(notif_job["notify_on_success"])
        self.assertTrue(smart_job["notify_on_success"])

    def test_fastapi_backups_endpoints_auth_and_lifecycle(self):
        # 1. Unauthenticated requests rejected
        unauth_get = asgi_get(api.app, "/api/backups")
        self.assertEqual(unauth_get.status_code, 401)

        unauth_post = asgi_post(
            api.app,
            "/api/backups/run",
            json_body={"target_type": "directory", "source_path": str(self.sample_src)},
        )
        self.assertEqual(unauth_post.status_code, 401)

        # 2. GET /api/backups
        resp = asgi_get(api.app, "/api/backups", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("backup_dir", data)
        self.assertIn("total_archives", data)
        self.assertNotIn("stack_targets", data)

        # 2b. POST /api/backups/config
        cfg_custom = Path(self.temp_dir.name) / "configured_archives"
        conf_resp = asgi_post(
            api.app,
            "/api/backups/config",
            json_body={"backup_dir": str(cfg_custom)},
            cookies=self.auth_cookies,
        )
        self.assertEqual(conf_resp.status_code, 200)
        self.assertEqual(conf_resp.json()["status"], "ok")
        self.assertEqual(conf_resp.json()["backup_dir"], str(cfg_custom.resolve()))
        self.assertTrue(cfg_custom.is_dir())

        # Restricted or root backup_dir rejected with 400
        conf_bad = asgi_post(
            api.app,
            "/api/backups/config",
            json_body={"backup_dir": "/proc"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(conf_bad.status_code, 400)

        conf_root = asgi_post(
            api.app,
            "/api/backups/config",
            json_body={"backup_dir": "/"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(conf_root.status_code, 400)

        # 3. POST /api/backups/run - invalid source directory returns 400
        bad_run = asgi_post(
            api.app,
            "/api/backups/run",
            json_body={"target_type": "directory", "source_path": "/nonexistent/path/xyz"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_run.status_code, 400)

        # Valid backup run
        good_run = asgi_post(
            api.app,
            "/api/backups/run",
            json_body={
                "target_type": "directory",
                "source_path": str(self.sample_src),
                "target_name": "api-backup",
                "excludes": ["*.log"],
                "retain_count": 3,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(good_run.status_code, 200)
        archive_info = good_run.json()["archive"]
        filename = archive_info["filename"]

        # 4. GET /api/backups/archives/{filename}/download
        down_resp = asgi_get(
            api.app,
            f"/api/backups/archives/{filename}/download",
            cookies=self.auth_cookies,
        )
        self.assertEqual(down_resp.status_code, 200)
        self.assertEqual(down_resp.headers.get("content-type"), "application/gzip")
        self.assertGreater(len(down_resp.content), 0)

        # Download traversal returns 400
        down_bad = asgi_get(
            api.app,
            "/api/backups/archives/..%2F..%2Fetc%2Fpasswd/download",
            cookies=self.auth_cookies,
        )
        self.assertEqual(down_bad.status_code, 400)

        # Download missing archive returns 404
        down_missing = asgi_get(
            api.app,
            "/api/backups/archives/missing_file.tar.gz/download",
            cookies=self.auth_cookies,
        )
        self.assertEqual(down_missing.status_code, 404)

        # 5. POST /api/backups/archives/{filename}/restore
        restore_dest = Path(self.temp_dir.name) / "api_restored"
        restore_dest.mkdir(parents=True, exist_ok=True)
        rest_resp = asgi_post(
            api.app,
            f"/api/backups/archives/{filename}/restore",
            json_body={"destination_path": str(restore_dest)},
            cookies=self.auth_cookies,
        )
        self.assertEqual(rest_resp.status_code, 200)
        self.assertTrue(rest_resp.json()["success"])

        # Restore missing archive returns 404
        rest_missing = asgi_post(
            api.app,
            "/api/backups/archives/not_there.tar.gz/restore",
            json_body={"destination_path": str(restore_dest)},
            cookies=self.auth_cookies,
        )
        self.assertEqual(rest_missing.status_code, 404)

        # Restore to restricted path '/' returns 400
        rest_root = asgi_post(
            api.app,
            f"/api/backups/archives/{filename}/restore",
            json_body={"destination_path": "/"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(rest_root.status_code, 400)

        # 6. DELETE /api/backups/archives/{filename}
        del_arch = asgi_delete(
            api.app,
            f"/api/backups/archives/{filename}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_arch.status_code, 200)

        # Deleting again returns 404
        del_again = asgi_delete(
            api.app,
            f"/api/backups/archives/{filename}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_again.status_code, 404)

        # 7. POST /api/backups/targets & DELETE /api/backups/targets/{id}
        bad_target = asgi_post(
            api.app,
            "/api/backups/targets",
            json_body={"name": "Bad", "source_path": "/proc"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_target.status_code, 400)

        good_target = asgi_post(
            api.app,
            "/api/backups/targets",
            json_body={
                "name": "API Saved Target",
                "target_type": "directory",
                "source_path": str(self.sample_src),
                "excludes": ["*.log"],
                "retain_count": 5,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(good_target.status_code, 200)
        tid = good_target.json()["target"]["id"]

        del_target = asgi_delete(
            api.app,
            f"/api/backups/targets/{tid}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_target.status_code, 200)

        # Deleting non-existent target returns 404
        del_missing = asgi_delete(
            api.app,
            "/api/backups/targets/99999",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_missing.status_code, 404)

    def test_fastapi_cron_endpoints_auth_and_lifecycle(self):
        # 1. Unauthenticated requests rejected
        unauth_get = asgi_get(api.app, "/api/cron/jobs")
        self.assertEqual(unauth_get.status_code, 401)

        unauth_post = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={"name": "Test", "task_type": "docker.prune", "cron_expression": "* * * * *"},
        )
        self.assertEqual(unauth_post.status_code, 401)

        # 2. GET /api/cron/jobs
        resp = asgi_get(api.app, "/api/cron/jobs", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("jobs", resp.json())
        self.assertIn("supported_task_types", resp.json())
        task_types = [
            t["type"] if isinstance(t, dict) else t
            for t in resp.json()["supported_task_types"]
        ]
        self.assertNotIn("backup.stack", task_types)
        self.assertIn("backup.directory", task_types)
        self.assertIn("notifications.summary", task_types)

        # 3. POST /api/cron/jobs - validation errors
        bad_name = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={"name": "", "task_type": "docker.prune", "cron_expression": "0 0 * * *"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_name.status_code, 400)

        bad_cron = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={"name": "Bad Cron", "task_type": "docker.prune", "cron_expression": "bad-cron"},
            cookies=self.auth_cookies,
        )
        self.assertEqual(bad_cron.status_code, 400)

        # Valid cron job creation
        good_job = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={
                "name": "Nightly Prune",
                "task_type": "docker.prune",
                "cron_expression": "0 3 * * *",
                "task_config": {"prune_images": True, "prune_volumes": False},
                "enabled": True,
                "notify_on_failure": True,
                "notify_on_success": False,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(good_job.status_code, 200)
        job_data = good_job.json()["job"]
        jid = job_data["id"]

        # Update existing job via POST /api/cron/jobs with id
        update_post = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={
                "id": jid,
                "name": "Nightly Prune Updated",
                "task_type": "docker.prune",
                "cron_expression": "0 4 * * *",
                "task_config": {"prune_images": True, "prune_volumes": True},
                "enabled": True,
                "notify_on_failure": False,
                "notify_on_success": True,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(update_post.status_code, 200)
        self.assertEqual(update_post.json()["job"]["id"], jid)
        self.assertEqual(update_post.json()["job"]["name"], "Nightly Prune Updated")
        self.assertEqual(update_post.json()["job"]["cron_expression"], "0 4 * * *")
        self.assertTrue(update_post.json()["job"]["notify_on_success"])
        self.assertFalse(update_post.json()["job"]["notify_on_failure"])

        # Update existing job via PUT /api/cron/jobs/{job_id}
        update_put = asgi_put(
            api.app,
            f"/api/cron/jobs/{jid}",
            json_body={
                "name": "Nightly Prune via PUT",
                "task_type": "docker.prune",
                "cron_expression": "0 5 * * *",
                "task_config": {"prune_images": False, "prune_volumes": True},
                "enabled": True,
                "notify_on_failure": True,
                "notify_on_success": True,
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(update_put.status_code, 200)
        self.assertEqual(update_put.json()["job"]["name"], "Nightly Prune via PUT")
        self.assertEqual(update_put.json()["job"]["cron_expression"], "0 5 * * *")

        # Update non-existent job via POST /api/cron/jobs with unknown id returns 404
        post_missing = asgi_post(
            api.app,
            "/api/cron/jobs",
            json_body={
                "id": 99999,
                "name": "Missing Job",
                "task_type": "docker.prune",
                "cron_expression": "0 0 * * *",
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(post_missing.status_code, 404)

        # Update non-existent job via PUT /api/cron/jobs/{id} returns 404
        put_missing = asgi_put(
            api.app,
            "/api/cron/jobs/99999",
            json_body={
                "name": "Missing Job",
                "task_type": "docker.prune",
                "cron_expression": "0 0 * * *",
            },
            cookies=self.auth_cookies,
        )
        self.assertEqual(put_missing.status_code, 404)

        # 4. POST /api/cron/jobs/{id}/toggle
        togg = asgi_post(
            api.app,
            f"/api/cron/jobs/{jid}/toggle",
            json_body={"enabled": False},
            cookies=self.auth_cookies,
        )
        self.assertEqual(togg.status_code, 200)
        self.assertFalse(togg.json()["job"]["enabled"])

        # Toggle non-existent returns 404
        togg_missing = asgi_post(
            api.app,
            "/api/cron/jobs/99999/toggle",
            json_body={"enabled": True},
            cookies=self.auth_cookies,
        )
        self.assertEqual(togg_missing.status_code, 404)

        # 5. POST /api/cron/jobs/{id}/run
        with patch.object(main.DOCKER_MANAGER, "prune_images", return_value={"output": "done"}):
            run_resp = asgi_post(
                api.app,
                f"/api/cron/jobs/{jid}/run",
                cookies=self.auth_cookies,
            )
            self.assertEqual(run_resp.status_code, 200)
            self.assertEqual(run_resp.json()["run"]["status"], "success")

        # Run non-existent returns 404
        run_missing = asgi_post(
            api.app,
            "/api/cron/jobs/99999/run",
            cookies=self.auth_cookies,
        )
        self.assertEqual(run_missing.status_code, 404)

        # 6. DELETE /api/cron/jobs/{id}
        del_resp = asgi_delete(
            api.app,
            f"/api/cron/jobs/{jid}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_resp.status_code, 200)

        # Deleting non-existent returns 404
        del_missing = asgi_delete(
            api.app,
            f"/api/cron/jobs/{jid}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(del_missing.status_code, 404)

    def test_backups_and_cron_frontend_dom_and_vm_contracts(self):
        """Verify Backups and Cron Scheduler DOM element IDs, JS functions, CSS classes, and VM execution."""
        import subprocess
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        required_backup_cron_ids = [
            "nav-tab-backups",
            "view-backups",
            "backups-refresh-btn",
            "backups-total-archives",
            "backups-total-size",
            "backups-stacks-count",
            "cron-active-jobs-count",
            "backups-manager-card",
            "backup-create-form",
            "backup-source-path",
            "backup-target-name",
            "backup-excludes",
            "backup-retain-count",
            "backup-save-target-btn",
            "backup-run-now-btn",
            "backup-targets-table",
            "backup-targets-tbody",
            "backup-archives-card",
            "backup-dir-badge",
            "backup-dir-input",
            "backup-dir-save-btn",
            "backup-archives-table",
            "backup-archives-tbody",
            "cron-scheduler-card",
            "cron-job-form",
            "cron-job-id",
            "cron-job-edit-indicator",
            "cron-editing-job-name",
            "cron-cancel-edit-btn",
            "cron-job-name",
            "cron-task-type",
            "cron-preset-select",
            "cron-expression-input",
            "cron-config-path-group",
            "cron-config-source-path",
            "cron-config-retain-group",
            "cron-config-retain-count",
            "cron-config-prune-group",
            "cron-config-prune-volumes",
            "cron-notify-failure",
            "cron-notify-success",
            "cron-save-job-btn",
            "cron-jobs-table",
            "cron-jobs-tbody",
            "cron-runs-table",
            "cron-runs-tbody",
        ]
        for dom_id in required_backup_cron_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        self.assertIn("<th>Notifications</th>", html, "Missing Notifications header in static/index.html")

        required_fns = [
            "async function fetchBackups(",
            "function renderBackups(",
            "async function fetchCronJobs(",
            "function renderCronJobs(",
            "function editCronJob(",
            "function cancelEditCronJob(",
            "function handleCronTaskTypeChange(",
            "async function handleSaveCronJob(",
            "async function toggleCronJob(",
            "async function runCronJobNow(",
            "async function deleteCronJob(",
            "async function executeBackupRun(",
            "async function restoreBackupArchive(",
            "async function deleteBackupArchive(",
            "async function saveBackupTarget(",
            "async function deleteBackupTarget(",
            "function updateCronFormTaskConfigVisibility(",
            "function syncBackupFormWithSelectedStack(",
        ]
        for fn_name in required_fns:
            self.assertIn(fn_name, js, f"Missing {fn_name} in static/app.js")

        required_css = [
            ".backups-content-grid",
            ".backups-card",
            ".backups-form-panel",
            ".backups-form-grid",
            ".btn-backup-action",
            ".btn-backup-action.btn-edit",
            ".cron-edit-banner",
            ".cron-notif-badge",
            ".target-kind-badge",
            "#backup-targets-table",
            "#backup-archives-table",
            "#cron-jobs-table",
            "#cron-runs-table",
        ]
        for css_sel in required_css:
            self.assertIn(css_sel, css, f"Missing {css_sel} in static/styles.css")

        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) {
    idx = source.indexOf('function ' + name + '(');
  }
  if (idx === -1) throw new Error('Not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') { depth--; if (started && depth === 0) return source.slice(idx, i + 1); }
    i++;
  }
  throw new Error('Unbalanced: ' + name);
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      innerHTML: '',
      value: '',
      className: '',
      checked: false,
      style: {},
      selectedIndex: 0,
      options: [
        { value: '0 2 * * *', text: 'Daily at 02:00' },
        { value: 'custom', text: 'Custom Expression' }
      ],
      children: [],
      scrollIntoView: () => {},
      focus: () => {},
      getAttribute: () => '',
      classList: {
        _set: new Set(),
        add(...cls) { cls.forEach(c => this._set.add(c)); },
        remove(...cls) { cls.forEach(c => this._set.delete(c)); },
        toggle(c, force) {
          if (force === undefined) {
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          } else if (force) this._set.add(c); else this._set.delete(c);
        },
        contains(c) { return this._set.has(c); }
      }
    };
  }
  return elements[id];
}

const sandbox = {
  document: { getElementById: getEl, createElement: () => getEl('tmp') },
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  formatBytes: (bytes) => (bytes / (1024 * 1024)).toFixed(1) + ' MB',
  formatDateTime: (ts) => '2026-11-04 12:00:00',
  showToast: () => {},
  cachedCronData: null,
};
vm.createContext(sandbox);
vm.runInContext(extractFunc(code, 'syncBackupFormWithSelectedStack'), sandbox);
vm.runInContext(extractFunc(code, 'renderBackups'), sandbox);
vm.runInContext(extractFunc(code, 'updateCronFormTaskConfigVisibility'), sandbox);
vm.runInContext(extractFunc(code, 'renderCronJobs'), sandbox);
vm.runInContext(extractFunc(code, 'editCronJob'), sandbox);
vm.runInContext(extractFunc(code, 'cancelEditCronJob'), sandbox);
vm.runInContext(extractFunc(code, 'handleCronTaskTypeChange'), sandbox);

const backupsPayload = {
  backup_dir: '/home/anthony/.vigil/backups',
  total_archives: 2,
  total_size_bytes: 14889779,
  stack_targets: [
    { stack_name: 'plex', path: '/home/anthony/docker/plex', compose_file: '/home/anthony/docker/plex/docker-compose.yml', exists: true, container_count: 2 }
  ],
  saved_targets: [
    { id: 1, name: 'AppData Directory', target_type: 'directory', source_path: '/home/anthony/appdata', retain_count: 7, excludes: ['*.log'] }
  ],
  archives: [
    { filename: 'plex_20261104_120000.tar.gz', target_type: 'stack', target_name: 'plex', source_path: '/home/anthony/docker/plex', size_bytes: 1048576, file_count: 15, created_at: 1700000000, sha256: 'a1b2c3d4e5f678901234567890abcdef1234567890abcdef1234567890abcdef' }
  ]
};

sandbox.renderBackups(backupsPayload);
if (getEl('backups-total-archives').textContent !== '2') throw new Error('Bad total archives: ' + getEl('backups-total-archives').textContent);
if (getEl('backups-total-size').textContent !== '14.2 MB') throw new Error('Bad total size: ' + getEl('backups-total-size').textContent);
if (getEl('backups-stacks-count').textContent !== '1') throw new Error('Bad stacks count: ' + getEl('backups-stacks-count').textContent);
if (!getEl('backup-targets-tbody').innerHTML.includes('AppData Directory')) throw new Error('Missing AppData Directory in targets tbody');
if (!getEl('backup-archives-tbody').innerHTML.includes('plex_20261104_120000.tar.gz')) throw new Error('Missing archive filename in archives tbody');
if (!getEl('backup-archives-tbody').innerHTML.includes('data-restore-archive')) throw new Error('Missing restore button in archives tbody');

const cronPayload = {
  jobs: [
    { id: 10, name: 'Nightly Backup', task_type: 'backup.directory', cron_expression: '0 2 * * *', enabled: true, notify_on_failure: true, notify_on_success: true, task_config: { source_path: '/home/anthony/appdata', retain_count: 5 }, last_status: 'success', next_run_at: 1700000000 },
    { id: 11, name: 'Failure Only Job', task_type: 'docker.prune', cron_expression: '0 3 * * *', enabled: true, notify_on_failure: true, notify_on_success: false, task_config: { prune_volumes: true }, last_status: 'never', next_run_at: 1700003600 },
    { id: 12, name: 'Success Only Job', task_type: 'storage.smart_check', cron_expression: '0 6 * * *', enabled: true, notify_on_failure: false, notify_on_success: true, task_config: {}, last_status: 'success', next_run_at: 1700010000 },
    { id: 13, name: 'Silent Job', task_type: 'notifications.summary', cron_expression: '0 9 * * *', enabled: false, notify_on_failure: false, notify_on_success: false, task_config: {}, last_status: 'never', next_run_at: 1700020000 }
  ],
  recent_runs: [
    { id: 101, job_id: 10, job_name: 'Nightly Backup', task_type: 'backup.directory', trigger_source: 'schedule', status: 'success', started_at: 1700000000, duration_ms: 1200, output: 'Created backup archive appdata.tar.gz' }
  ],
  supported_task_types: [
    { type: 'backup.directory', label: 'Custom Directory Backup' },
    { type: 'docker.prune', label: 'Docker System & Volume Prune' },
    { type: 'storage.smart_check', label: 'S.M.A.R.T. & RAID Health Check' },
    { type: 'notifications.summary', label: 'Periodic System Notification Summary' }
  ]
};

sandbox.cachedCronData = cronPayload;
sandbox.renderCronJobs(cronPayload);

if (!getEl('cron-active-jobs-count').textContent.includes('3 Active')) throw new Error('Bad active cron jobs count: ' + getEl('cron-active-jobs-count').textContent);
const jobsTbodyHtml = getEl('cron-jobs-tbody').innerHTML;
if (!jobsTbodyHtml.includes('Nightly Backup')) throw new Error('Missing Nightly Backup in jobs tbody');
if (!jobsTbodyHtml.includes('0 2 * * *')) throw new Error('Missing cron expression in jobs tbody');
if (!jobsTbodyHtml.includes('data-edit-cron-id="10"')) throw new Error('Missing data-edit-cron-id="10" button in jobs tbody');
if (!jobsTbodyHtml.includes('data-edit-cron-id="11"')) throw new Error('Missing data-edit-cron-id="11" button in jobs tbody');

// Verify notification badge representations
if (!jobsTbodyHtml.includes('🔔 Success + Failure')) throw new Error('Missing Success + Failure badge');
if (!jobsTbodyHtml.includes('🔔 Failure Only')) throw new Error('Missing Failure Only badge');
if (!jobsTbodyHtml.includes('🔔 Success Only')) throw new Error('Missing Success Only badge');
if (!jobsTbodyHtml.includes('🔕 None')) throw new Error('Missing None badge');

if (!getEl('cron-runs-tbody').innerHTML.includes('Created backup archive appdata.tar.gz')) throw new Error('Missing run output in runs tbody');

// Test editCronJob(10)
sandbox.editCronJob(10);
if (getEl('cron-job-id').value !== '10') throw new Error('editCronJob failed to set cron-job-id: ' + getEl('cron-job-id').value);
if (getEl('cron-job-name').value !== 'Nightly Backup') throw new Error('editCronJob failed to set name');
if (getEl('cron-editing-job-name').textContent !== 'Nightly Backup') throw new Error('editCronJob failed to set banner job name');
if (getEl('cron-save-job-btn').textContent !== 'Update Scheduled Job') throw new Error('editCronJob failed to toggle button text: ' + getEl('cron-save-job-btn').textContent);
if (getEl('cron-job-edit-indicator').classList.contains('hidden')) throw new Error('edit banner should not be hidden in edit mode');

// Test cancelEditCronJob()
sandbox.cancelEditCronJob();
if (getEl('cron-job-id').value !== '') throw new Error('cancelEditCronJob failed to clear id');
if (getEl('cron-job-name').value !== '') throw new Error('cancelEditCronJob failed to clear name');
if (getEl('cron-save-job-btn').textContent !== 'Save Scheduled Job') throw new Error('cancelEditCronJob failed to reset button text: ' + getEl('cron-save-job-btn').textContent);
if (!getEl('cron-job-edit-indicator').classList.contains('hidden')) throw new Error('edit banner should be hidden after cancel');

// Test handleCronTaskTypeChange()
getEl('cron-task-type').value = 'storage.smart_check';
getEl('cron-notify-success').checked = false;
sandbox.handleCronTaskTypeChange();
if (!getEl('cron-notify-success').checked) throw new Error('handleCronTaskTypeChange should auto-check notify_on_success for storage.smart_check');

console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], cwd=str(root), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"Backups/Cron Node VM test failed: {proc.stderr or proc.stdout}")
        self.assertIn("OK", proc.stdout)


class TestHistoricalTelemetryTimeMachine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "raid_state.db"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()

        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_telemetry_table_schema_and_init(self):
        main.init_db(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        try:
            columns = {
                row[1]: row[2]
                for row in conn.execute("PRAGMA table_info(telemetry_samples)").fetchall()
            }
            expected = {
                "id": "INTEGER",
                "timestamp": "INTEGER",
                "cpu_percent": "REAL",
                "memory_percent": "REAL",
                "memory_used_bytes": "INTEGER",
                "memory_total_bytes": "INTEGER",
                "net_rx_rate_bps": "REAL",
                "net_tx_rate_bps": "REAL",
                "cpu_temp": "REAL",
            }
            for col, dtype in expected.items():
                self.assertIn(col, columns)
                self.assertEqual(columns[col].upper(), dtype)

            indexes = [
                row[1]
                for row in conn.execute("PRAGMA index_list(telemetry_samples)").fetchall()
            ]
            self.assertIn("idx_telemetry_samples_timestamp", indexes)
        finally:
            conn.close()

    def test_record_telemetry_sample_with_mocked_system_data(self):
        mock_vm = MagicMock()
        mock_vm.percent = 55.5
        mock_vm.used = 8 * 1024 * 1024 * 1024
        mock_vm.total = 16 * 1024 * 1024 * 1024

        mock_io1 = MagicMock()
        mock_io1.bytes_recv = 1000000
        mock_io1.bytes_sent = 500000

        with patch("psutil.cpu_percent", return_value=35.2), \
             patch("psutil.virtual_memory", return_value=mock_vm), \
             patch("psutil.net_io_counters", return_value=mock_io1), \
             patch.object(main, "get_hardware_thermal_metrics", return_value={"cpu_package_c": 52.0}):
            main._last_telemetry_net_sample = None
            sample1 = main.record_telemetry_sample(db_path=self.db_path, timestamp=1700000000)

        self.assertEqual(sample1["timestamp"], 1700000000)
        self.assertEqual(sample1["cpu_percent"], 35.2)
        self.assertEqual(sample1["memory_percent"], 55.5)
        self.assertEqual(sample1["memory_used_bytes"], 8 * 1024 * 1024 * 1024)
        self.assertEqual(sample1["memory_total_bytes"], 16 * 1024 * 1024 * 1024)
        self.assertEqual(sample1["cpu_temp"], 52.0)
        self.assertEqual(sample1["net_rx_rate_bps"], 0.0)

        # Second sample after time delta
        mock_io2 = MagicMock()
        mock_io2.bytes_recv = 1010000  # +10000 bytes in 1 sec
        mock_io2.bytes_sent = 505000   # +5000 bytes in 1 sec

        with patch("psutil.cpu_percent", return_value=40.0), \
             patch("psutil.virtual_memory", return_value=mock_vm), \
             patch("psutil.net_io_counters", return_value=mock_io2), \
             patch.object(main, "get_hardware_thermal_metrics", return_value={"cpu_package_c": 54.0}), \
             patch("time.time", return_value=1700000001.0):
            main._last_telemetry_net_sample = (1700000000.0, 1000000, 500000)
            sample2 = main.record_telemetry_sample(db_path=self.db_path, timestamp=1700000001)

        self.assertEqual(sample2["timestamp"], 1700000001)
        self.assertEqual(sample2["cpu_percent"], 40.0)
        self.assertEqual(sample2["net_rx_rate_bps"], 10000.0)
        self.assertEqual(sample2["net_tx_rate_bps"], 5000.0)
        self.assertEqual(sample2["cpu_temp"], 54.0)

    def test_prune_telemetry_samples(self):
        now = 1700000000
        # Sample 10 days old
        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=now - (10 * 86400),
            cpu_percent=10.0,
            memory_percent=20.0,
        )
        # Sample 3 days old
        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=now - (3 * 86400),
            cpu_percent=15.0,
            memory_percent=25.0,
        )
        # Current sample
        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=now,
            cpu_percent=20.0,
            memory_percent=30.0,
        )

        with patch("time.time", return_value=float(now)):
            deleted = main.prune_telemetry_samples(max_age_days=7, db_path=self.db_path)

        self.assertEqual(deleted, 1)

        conn = sqlite3.connect(str(self.db_path))
        try:
            remaining = conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
            self.assertEqual(remaining, 2)
        finally:
            conn.close()

    def test_get_telemetry_history_aggregation_and_summary(self):
        end_time = 1700000000
        # Insert samples over past hour
        t1 = end_time - 3000
        t2 = end_time - 2000
        t3 = end_time - 1000

        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=t1,
            cpu_percent=10.0,
            memory_percent=20.0,
            memory_used_bytes=2000,
            memory_total_bytes=10000,
            net_rx_rate_bps=100.0,
            net_tx_rate_bps=50.0,
            cpu_temp=40.0,
        )
        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=t2,
            cpu_percent=20.0,
            memory_percent=40.0,
            memory_used_bytes=4000,
            memory_total_bytes=10000,
            net_rx_rate_bps=200.0,
            net_tx_rate_bps=100.0,
            cpu_temp=50.0,
        )
        main.record_telemetry_sample(
            db_path=self.db_path,
            timestamp=t3,
            cpu_percent=30.0,
            memory_percent=60.0,
            memory_used_bytes=6000,
            memory_total_bytes=10000,
            net_rx_rate_bps=300.0,
            net_tx_rate_bps=150.0,
            cpu_temp=60.0,
        )

        # Query 1h history
        data = main.get_telemetry_history(
            range_key="1h",
            points_limit=60,
            db_path=self.db_path,
            end_time=end_time,
        )

        self.assertEqual(data["range"], "1h")
        self.assertEqual(data["start_time"], end_time - 3600)
        self.assertEqual(data["end_time"], end_time)
        self.assertEqual(data["total_samples"], 3)
        self.assertEqual(data["summary"]["cpu"]["min"], 10.0)
        self.assertEqual(data["summary"]["cpu"]["max"], 30.0)
        self.assertEqual(data["summary"]["cpu"]["avg"], 20.0)
        self.assertEqual(data["summary"]["memory"]["min"], 20.0)
        self.assertEqual(data["summary"]["memory"]["max"], 60.0)
        self.assertEqual(data["summary"]["memory"]["avg"], 40.0)
        self.assertEqual(data["summary"]["net_rx"]["min"], 100.0)
        self.assertEqual(data["summary"]["net_rx"]["max"], 300.0)
        self.assertEqual(data["summary"]["net_rx"]["avg"], 200.0)
        self.assertEqual(data["summary"]["net_tx"]["min"], 50.0)
        self.assertEqual(data["summary"]["net_tx"]["max"], 150.0)
        self.assertEqual(data["summary"]["net_tx"]["avg"], 100.0)
        self.assertEqual(data["summary"]["cpu_temp"]["min"], 40.0)
        self.assertEqual(data["summary"]["cpu_temp"]["max"], 60.0)
        self.assertEqual(data["summary"]["cpu_temp"]["avg"], 50.0)

        self.assertTrue(len(data["points"]) >= 1)
        for pt in data["points"]:
            self.assertIn("timestamp", pt)
            self.assertIn("iso_time", pt)
            self.assertIn("cpu_percent", pt)
            self.assertIn("memory_percent", pt)
            self.assertIn("net_rx_rate_bps", pt)
            self.assertIn("net_tx_rate_bps", pt)
            self.assertIn("cpu_temp", pt)

        # Check supported ranges 6h, 24h, 7d
        for r_key in ["6h", "24h", "7d"]:
            res = main.get_telemetry_history(range_key=r_key, db_path=self.db_path, end_time=end_time)
            self.assertEqual(res["range"], r_key)

    def test_api_telemetry_history_and_sample_endpoints(self):
        # 1. Unauthenticated request should fail
        unauth_resp = asgi_get(api.app, "/api/telemetry/history")
        self.assertEqual(unauth_resp.status_code, 401)

        unauth_post = asgi_post(api.app, "/api/telemetry/sample")
        self.assertEqual(unauth_post.status_code, 401)

        # 2. Record a sample via POST
        with patch("psutil.cpu_percent", return_value=25.0), \
             patch("psutil.virtual_memory", return_value=MagicMock(percent=45.0, used=4000, total=8000)), \
             patch.object(main, "get_hardware_thermal_metrics", return_value={"cpu_package_c": 48.5}):
            post_resp = asgi_post(api.app, "/api/telemetry/sample", cookies=self.auth_cookies)

        self.assertEqual(post_resp.status_code, 200)
        sample_data = post_resp.json()
        self.assertEqual(sample_data["cpu_percent"], 25.0)
        self.assertEqual(sample_data["memory_percent"], 45.0)
        self.assertEqual(sample_data["cpu_temp"], 48.5)

        # 3. Query history via GET
        get_resp = asgi_get(api.app, "/api/telemetry/history?range=1h&points=30", cookies=self.auth_cookies)
        self.assertEqual(get_resp.status_code, 200)
        hist_data = get_resp.json()
        self.assertEqual(hist_data["range"], "1h")
        self.assertEqual(hist_data["total_samples"], 1)
        self.assertEqual(len(hist_data["points"]), 1)
        self.assertEqual(hist_data["points"][0]["cpu_percent"], 25.0)

        # 4. Invalid range validation returns 400
        invalid_resp = asgi_get(api.app, "/api/telemetry/history?range=invalid", cookies=self.auth_cookies)
        self.assertEqual(invalid_resp.status_code, 400)
        self.assertIn("Invalid range", invalid_resp.json()["detail"])

    def test_empty_telemetry_history_and_clamped_points(self):
        # Empty DB returns graceful 0-sample summary
        empty_hist = main.get_telemetry_history(range_key="1h", points_limit=5, db_path=self.db_path)
        self.assertEqual(empty_hist["total_samples"], 0)
        self.assertEqual(empty_hist["points"], [])
        self.assertEqual(empty_hist["summary"]["cpu"]["avg"], 0.0)
        # points_limit clamped to 10 minimum: 3600 // 10 = 360 bucket_seconds
        self.assertEqual(empty_hist["bucket_seconds"], 360)

        # points_limit clamped to 300 maximum: 3600 // 300 = 12 bucket_seconds
        clamped_high = main.get_telemetry_history(range_key="1h", points_limit=999, db_path=self.db_path)
        self.assertEqual(clamped_high["bucket_seconds"], 12)

    def test_evaluator_loop_telemetry_integration(self):
        record_called = False
        prune_called = False

        def fake_record():
            nonlocal record_called
            record_called = True
            return {}

        def fake_prune(days=7):
            nonlocal prune_called
            prune_called = True
            return 0

        async def run_one_iteration():
            with patch.object(main, "record_telemetry_sample", side_effect=fake_record), \
                 patch.object(main, "prune_telemetry_samples", side_effect=fake_prune), \
                 patch.object(main, "evaluate_threshold_alerts", return_value=None), \
                 patch.object(main, "evaluate_scheduled_reports", return_value=None), \
                 patch.object(main, "evaluate_due_cron_jobs", return_value=None), \
                 patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                api._eval_loop_count = 119  # next iteration triggers prune (120)
                try:
                    await api.notifications_evaluator_loop()
                except asyncio.CancelledError:
                    pass

        asyncio.run(run_one_iteration())
        self.assertTrue(record_called)
        self.assertTrue(prune_called)

    def test_historical_telemetry_frontend_dom_and_vm_contracts(self):
        import subprocess

        root = Path(__file__).resolve().parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")

        # 1. HTML DOM Element contracts
        required_ids = [
            "telemetry-timemachine-bar",
            "btn-telemetry-range-live",
            "btn-telemetry-range-1h",
            "btn-telemetry-range-6h",
            "btn-telemetry-range-24h",
            "btn-telemetry-range-7d",
            "telemetry-history-summary",
            "telemetry-history-title",
            "telemetry-history-samples-count",
            "telemetry-stat-cpu",
            "telemetry-stat-mem",
            "telemetry-stat-net",
            "telemetry-stat-temp",
            "telemetry-historical-chart-container",
            "telemetry-historical-canvas",
            "telemetry-historical-tooltip",
            "telemetry-history-empty",
        ]
        for el_id in required_ids:
            self.assertIn(f'id="{el_id}"', html, f"Missing required telemetry DOM element: id='{el_id}'")

        # 2. Key app.js functions present
        for fn_name in [
            "getRangeDescription",
            "setTelemetryRange",
            "fetchTelemetryHistory",
            "renderTelemetryHistorySummary",
            "renderTelemetryHistoricalChart",
            "positionTelemetryTooltip",
            "initTelemetryTimeMachine",
        ]:
            self.assertIn(f"function {fn_name}", app_js, f"Missing telemetry JS function: {fn_name}")

        # 3. Node.js VM Contract Execution
        node_script = """
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) {
    idx = source.indexOf('function ' + name + '(');
  }
  if (idx === -1) throw new Error('Not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') { depth--; if (started && depth === 0) return source.slice(idx, i + 1); }
    i++;
  }
  throw new Error('Unbalanced: ' + name);
}

const elements = {};
function getEl(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      textContent: '',
      innerHTML: '',
      value: '',
      style: {},
      attributes: {},
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] || ''; },
      parentElement: { clientWidth: 900, clientHeight: 260 },
      classList: {
        _set: new Set(),
        add(...cls) { cls.forEach(c => this._set.add(c)); },
        remove(...cls) { cls.forEach(c => this._set.delete(c)); },
        toggle(c, force) {
          if (force === undefined) {
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          } else if (force) this._set.add(c); else this._set.delete(c);
        },
        contains(c) { return this._set.has(c); }
      }
    };
  }
  return elements[id];
}

let fetchTelemetryHistoryCalledWith = null;
const sandbox = {
  document: { getElementById: getEl, createElement: () => getEl('tmp') },
  currentTelemetryRange: 'live',
  cachedTelemetryHistory: null,
  isFetchingTelemetryHistory: false,
  activeTelemetryHoverIndex: null,
  escapeHtml: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  formatNetworkRate: (bps) => `${Number(bps || 0).toFixed(0)} B/s`,
  formatDateTime: (ts) => '2026-11-05 12:00:00',
  fetchTelemetryHistory: async (r) => { fetchTelemetryHistoryCalledWith = r; },
};

vm.createContext(sandbox);
vm.runInContext(extractFunc(code, 'getRangeDescription'), sandbox);
vm.runInContext(extractFunc(code, 'renderTelemetryHistorySummary'), sandbox);
vm.runInContext(extractFunc(code, 'positionTelemetryTooltip'), sandbox);
vm.runInContext(extractFunc(code, 'setTelemetryRange'), sandbox);

// Test getRangeDescription
if (sandbox.getRangeDescription('1h') !== 'Historical Telemetry (Last 1 Hour)') throw new Error('Bad 1h description');
if (sandbox.getRangeDescription('7d') !== 'Historical Telemetry (Last 7 Days)') throw new Error('Bad 7d description');

// Test setTelemetryRange to 6h
sandbox.setTelemetryRange('6h');
if (sandbox.currentTelemetryRange !== '6h') throw new Error('currentTelemetryRange not updated to 6h');
if (!getEl('btn-telemetry-range-6h').classList.contains('active')) throw new Error('6h button not marked active');
if (getEl('btn-telemetry-range-live').classList.contains('active')) throw new Error('live button still marked active');
if (getEl('telemetry-history-summary').classList.contains('hidden')) throw new Error('history summary not visible');
if (getEl('telemetry-historical-chart-container').classList.contains('hidden')) throw new Error('chart container not visible');
if (fetchTelemetryHistoryCalledWith !== '6h') throw new Error('fetchTelemetryHistory was not called with 6h');

// Test setTelemetryRange back to live
sandbox.setTelemetryRange('live');
if (sandbox.currentTelemetryRange !== 'live') throw new Error('currentTelemetryRange not updated to live');
if (!getEl('btn-telemetry-range-live').classList.contains('active')) throw new Error('live button not marked active');
if (!getEl('telemetry-history-summary').classList.contains('hidden')) throw new Error('history summary should be hidden in live');
if (!getEl('telemetry-historical-chart-container').classList.contains('hidden')) throw new Error('chart container should be hidden in live');

// Test renderTelemetryHistorySummary
const sampleData = {
  total_samples: 42,
  summary: {
    cpu: { min: 12.5, max: 78.2, avg: 34.1 },
    memory: { min: 40.0, max: 55.0, avg: 48.0 },
    net_rx_rate_bps: { min: 100, max: 2000, avg: 500 },
    net_tx_rate_bps: { min: 50, max: 1000, avg: 250 },
    cpu_temp: { min: 45.0, max: 68.0, avg: 53.5 }
  }
};
sandbox.renderTelemetryHistorySummary(sampleData);
if (getEl('telemetry-history-samples-count').textContent !== '42 samples') throw new Error('Bad sample count: ' + getEl('telemetry-history-samples-count').textContent);
if (!getEl('telemetry-stat-cpu').textContent.includes('Avg: 34.1%')) throw new Error('Bad cpu avg: ' + getEl('telemetry-stat-cpu').textContent);
if (!getEl('telemetry-stat-mem').textContent.includes('Avg: 48.0%')) throw new Error('Bad mem avg: ' + getEl('telemetry-stat-mem').textContent);
if (!getEl('telemetry-stat-net').textContent.includes('RX: 500 B/s')) throw new Error('Bad net rx: ' + getEl('telemetry-stat-net').textContent);
if (!getEl('telemetry-stat-temp').textContent.includes('Avg: 53.5°C')) throw new Error('Bad temp avg: ' + getEl('telemetry-stat-temp').textContent);

// Test positionTelemetryTooltip
const mockCanvas = { parentElement: { clientWidth: 900, clientHeight: 260 } };
sandbox.positionTelemetryTooltip(mockCanvas, 150, 80, {
  timestamp: 1700000000,
  cpu_percent: 42.0,
  memory_percent: 60.5,
  net_rx_rate_bps: 1250,
  net_tx_rate_bps: 750,
  cpu_temp: 51.2
});
const tooltipEl = getEl('telemetry-historical-tooltip');
if (tooltipEl.classList.contains('hidden')) throw new Error('Tooltip should not be hidden');
if (!tooltipEl.innerHTML.includes('42.0%')) throw new Error('Tooltip missing CPU 42.0%');
if (!tooltipEl.innerHTML.includes('60.5%')) throw new Error('Tooltip missing Mem 60.5%');
if (!tooltipEl.innerHTML.includes('51.2°C')) throw new Error('Tooltip missing Temp 51.2°C');

console.log('OK');
"""
        proc = subprocess.run(["node", "-e", node_script], cwd=str(root), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, f"Telemetry Frontend VM test failed: {proc.stderr or proc.stdout}")
        self.assertIn("OK", proc.stdout)


class ApiEndpointAuthenticationSecurityTests(unittest.TestCase):
    """Exhaustive automated security audit verifying that 100% of non-whitelisted API endpoints require auth."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.db_patcher = patch.object(main, "STATE_DB_PATH", self.db_path)
        self.db_patcher.start()
        main.init_state_db()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_all_api_endpoints_require_auth(self):
        public_whitelist = {
            ("GET", "/api/auth/status"),
            ("POST", "/api/auth/login"),
            ("POST", "/api/auth/setup"),
            ("POST", "/api/auth/logout"),
            ("POST", "/api/auth/google"),
        }

        mock_params = {
            "disk_name": "sda",
            "name": "test-item",
            "id": "item123",
            "service_name": "test-service",
            "pid": "1234",
            "username": "testuser",
            "groupname": "testgroup",
            "probe_id": "1",
            "rule_index": "1",
            "target_id": "1",
            "filename": "test-archive.tar.gz",
            "job_id": "1",
            "container_id": "test_container",
        }

        def fill_route_path(tmpl: str) -> str:
            return re.sub(
                r"\{([a-zA-Z0-9_]+)(?::[a-zA-Z0-9_]+)?\}",
                lambda m: mock_params.get(m.group(1), "test"),
                tmpl,
            )

        api_routes_found = 0
        tested_endpoints = []

        for route in api.app.routes:
            path = getattr(route, "path", None)
            if not path or not path.startswith("/api/"):
                continue

            # Check for WebSocket endpoint
            if (
                path == "/api/ws"
                or getattr(route, "protocol", None) == "websocket"
                or "websocket" in type(route).__name__.lower()
            ):
                resolved_path = fill_route_path(path)
                unauth_ws = asgi_ws(api.app, resolved_path)
                self.assertEqual(
                    unauth_ws.close_code,
                    1008,
                    f"Expected WebSocket unauth connection to close with 1008 on {resolved_path}, got {unauth_ws.close_code}",
                )
                api_routes_found += 1
                tested_endpoints.append(("WS", path))
                continue

            # Check HTTP routes
            methods = getattr(route, "methods", None) or set()
            for method in sorted(methods):
                if method in ("HEAD", "OPTIONS"):
                    continue

                if (method, path) in public_whitelist:
                    continue

                api_routes_found += 1
                tested_endpoints.append((method, path))

                # 1. Verify require_auth is declared in dependencies or parameters
                dependant = getattr(route, "dependant", None)
                self.assertIsNotNone(
                    dependant,
                    f"Route {method} {path} has no dependant object",
                )
                dep_calls = [
                    d.call
                    for d in (getattr(dependant, "dependencies", []) or [])
                    if hasattr(d, "call")
                ]
                endpoint = getattr(route, "endpoint", None)
                has_dep = api.require_auth in dep_calls
                if not has_dep and endpoint:
                    import inspect
                    sig = inspect.signature(endpoint)
                    for param in sig.parameters.values():
                        if param.default is not inspect.Parameter.empty and hasattr(param.default, "dependency"):
                            if param.default.dependency == api.require_auth:
                                has_dep = True
                                break

                self.assertTrue(
                    has_dep,
                    f"Endpoint {method} {path} does NOT declare require_auth dependency!",
                )

                # 2. Execute an unauthenticated request and assert 401 Unauthorized
                resolved_path = fill_route_path(path)
                resp = asgi_request(api.app, method, resolved_path)
                self.assertEqual(
                    resp.status_code,
                    401,
                    f"Expected 401 for unauthenticated {method} {resolved_path} (from {path}), got {resp.status_code}",
                )

        self.assertGreater(
            len(tested_endpoints),
            40,
            f"Expected > 40 tested API endpoints, only found {len(tested_endpoints)}",
        )
        print(f"\n[AUTH SECURITY AUDIT] Verified {len(tested_endpoints)} non-whitelisted endpoints strictly require authentication.")

    def test_frontend_reload_lifecycle_backups_and_cron(self):
        """Validate that static/app.js triggers refreshBackupsAndCron and updateBackupsNavBadge on auth success."""
        app_js_path = Path(__file__).resolve().parent / "static" / "app.js"
        content = app_js_path.read_text(encoding="utf-8")

        auth_success_match = re.search(
            r"function\s+onAuthenticatedSuccess\s*\([^)]*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        self.assertIsNotNone(
            auth_success_match,
            "onAuthenticatedSuccess function not found in app.js",
        )
        auth_body = auth_success_match.group(1)

        self.assertIn("refreshBackupsAndCron()", auth_body)
        self.assertIn("updateBackupsNavBadge()", auth_body)
        self.assertIn('currentViewTab === "backups"', auth_body)
        self.assertIn("isBackupsAndCronRefreshing", content)


class DockerContainerTerminalBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.db_patcher = patch.object(main, "STATE_DB_PATH", self.db_path)
        self.db_patcher.start()
        main.init_state_db()
        self.username = "admin"
        main.create_user(self.username, "Password123!")
        self.token = main.create_session(self.username)

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_create_container_exec_success_default_and_custom_options(self):
        mgr = main.DockerManager()
        with patch.object(mgr, "is_running", return_value=True):
            # 1. Default options
            with patch.object(mgr, "_socket_request", return_value=(201, b'{"Id": "exec_12345"}')) as mock_req:
                res = mgr.create_container_exec("c_test")
                self.assertEqual(res, {"id": "exec_12345"})
                mock_req.assert_called_once()
                args, kwargs = mock_req.call_args
                self.assertEqual(args[0], "POST")
                self.assertEqual(args[1], "/containers/c_test/exec")
                payload = json.loads(kwargs["body"].decode("utf-8"))
                self.assertEqual(payload["Cmd"], ["/bin/sh"])
                self.assertEqual(payload["User"], "")
                self.assertTrue(payload["Tty"])
                self.assertTrue(payload["AttachStdin"])
                self.assertTrue(payload["AttachStdout"])
                self.assertTrue(payload["AttachStderr"])
                self.assertEqual(
                    payload["Env"],
                    ["TERM=xterm-256color", "COLORTERM=truecolor", "LANG=C.UTF-8"],
                )

            # 2. Custom options with list env
            with patch.object(mgr, "_socket_request", return_value=(200, b'{"Id": "exec_custom"}')) as mock_req:
                res = mgr.create_container_exec(
                    "c_custom",
                    cmd=["/bin/bash"],
                    user="node",
                    tty=False,
                    env=["TERM=xterm", "CUSTOM_VAR=test"],
                )
                self.assertEqual(res, {"id": "exec_custom"})
                payload = json.loads(mock_req.call_args[1]["body"].decode("utf-8"))
                self.assertEqual(payload["Cmd"], ["/bin/bash"])
                self.assertEqual(payload["User"], "node")
                self.assertFalse(payload["Tty"])
                self.assertEqual(
                    payload["Env"],
                    ["TERM=xterm", "COLORTERM=truecolor", "LANG=C.UTF-8", "CUSTOM_VAR=test"],
                )

            # 3. Custom options with dict env
            with patch.object(mgr, "_socket_request", return_value=(200, b'{"Id": "exec_dict"}')) as mock_req:
                res = mgr.create_container_exec(
                    "c_dict",
                    env={"COLORTERM": "24bit", "FOO": "bar"},
                )
                self.assertEqual(res, {"id": "exec_dict"})
                payload = json.loads(mock_req.call_args[1]["body"].decode("utf-8"))
                self.assertEqual(
                    payload["Env"],
                    ["TERM=xterm-256color", "COLORTERM=24bit", "LANG=C.UTF-8", "FOO=bar"],
                )

    def test_create_container_exec_error_handling(self):
        mgr = main.DockerManager()
        # Empty container ID
        with self.assertRaises(ValueError):
            mgr.create_container_exec("  ")

        # Daemon offline
        with patch.object(mgr, "is_running", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                mgr.create_container_exec("c1")
            self.assertIn("daemon is not running", str(ctx.exception).lower())

        # Container not found (404)
        with patch.object(mgr, "is_running", return_value=True):
            with patch.object(mgr, "_socket_request", return_value=(404, b'{"message": "No such container: c1"}')):
                with self.assertRaises(FileNotFoundError) as ctx:
                    mgr.create_container_exec("c1")
                self.assertIn("not found", str(ctx.exception))

            # Server error (500)
            with patch.object(mgr, "_socket_request", return_value=(500, b'{"message": "daemon crashed"}')):
                with self.assertRaises(RuntimeError) as ctx:
                    mgr.create_container_exec("c1")
                self.assertIn("Failed to create exec instance", str(ctx.exception))

    def test_resize_container_exec(self):
        mgr = main.DockerManager()
        with patch.object(mgr, "is_running", return_value=True):
            # Success 201
            with patch.object(mgr, "_socket_request", return_value=(201, b"")) as mock_req:
                ok = mgr.resize_container_exec("exec_abc", height=35, width=110)
                self.assertTrue(ok)
                mock_req.assert_called_once_with("POST", "/exec/exec_abc/resize?h=35&w=110", timeout=5.0)

            # Success 200
            with patch.object(mgr, "_socket_request", return_value=(200, b"")):
                self.assertTrue(mgr.resize_container_exec("exec_abc", height=24, width=80))

            # Failure HTTP code
            with patch.object(mgr, "_socket_request", return_value=(500, b"error")):
                ok = mgr.resize_container_exec("exec_abc", height=24, width=80)
                self.assertFalse(ok)

        # Empty exec_id or daemon offline
        self.assertFalse(mgr.resize_container_exec("", 24, 80))
        with patch.object(mgr, "is_running", return_value=False):
            self.assertFalse(mgr.resize_container_exec("exec_abc", 24, 80))

    def test_start_container_exec_socket_handshake_and_errors(self):
        # Empty exec_id
        mgr = main.DockerManager(socket_path="/tmp/nonexistent_docker.sock")
        with self.assertRaises(ValueError):
            mgr.start_container_exec_socket("")

        # Socket file does not exist
        with self.assertRaises(FileNotFoundError):
            mgr.start_container_exec_socket("exec_123")

        # Test handshake using a Unix domain socket server
        sock_dir = tempfile.mkdtemp()
        real_sock_path = os.path.join(sock_dir, "docker.sock")
        try:
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(real_sock_path)
            server_sock.listen(1)

            received_req = bytearray()

            def handle_client():
                conn, _ = server_sock.accept()
                try:
                    while b"\r\n\r\n" not in received_req:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        received_req.extend(chunk)
                    conn.sendall(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
                    time.sleep(0.05)
                finally:
                    pass

            t = threading.Thread(target=handle_client)
            t.start()

            test_mgr = main.DockerManager(socket_path=real_sock_path)
            client_sock = test_mgr.start_container_exec_socket("exec_test_456")
            t.join()

            self.assertIn(b"POST /exec/exec_test_456/start HTTP/1.1", received_req)
            self.assertIn(b"Upgrade: tcp", received_req)
            self.assertIn(b"Connection: Upgrade", received_req)
            self.assertIn(b'{"Detach": false, "Tty": true}', received_req)

            client_sock.close()
            server_sock.close()
        finally:
            shutil.rmtree(sock_dir, ignore_errors=True)

    def test_start_container_exec_socket_handshake_failure(self):
        sock_dir = tempfile.mkdtemp()
        real_sock_path = os.path.join(sock_dir, "docker.sock")
        try:
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(real_sock_path)
            server_sock.listen(1)

            def handle_bad_client():
                conn, _ = server_sock.accept()
                try:
                    conn.recv(1024)
                    conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                finally:
                    conn.close()

            t = threading.Thread(target=handle_bad_client)
            t.start()

            test_mgr = main.DockerManager(socket_path=real_sock_path)
            with self.assertRaises(RuntimeError) as ctx:
                test_mgr.start_container_exec_socket("exec_bad")
            self.assertIn("500", str(ctx.exception))
            t.join()
            server_sock.close()
        finally:
            shutil.rmtree(sock_dir, ignore_errors=True)

    def test_terminal_websocket_unauthenticated_rejected_with_1008(self):
        # 1. No token at all
        res = asgi_ws(api.app, "/api/docker/containers/c123/terminal")
        self.assertEqual(res.close_code, 1008)

        # 2. Invalid token
        res_invalid = asgi_ws(api.app, "/api/docker/containers/c123/terminal?token=invalid_token")
        self.assertEqual(res_invalid.close_code, 1008)

    def test_terminal_websocket_exec_creation_error_closes_with_1011(self):
        with patch.object(main.docker_manager, "create_container_exec", side_effect=RuntimeError("Container not running")):
            res = asgi_ws(
                api.app,
                f"/api/docker/containers/c123/terminal?token={self.token}",
            )
            self.assertTrue(res.accepted)
            self.assertEqual(res.close_code, 1011)

    def test_terminal_websocket_socket_connect_error_closes_with_1011(self):
        with (
            patch.object(main.docker_manager, "create_container_exec", return_value={"id": "exec_abc"}),
            patch.object(main.docker_manager, "start_container_exec_socket", side_effect=ConnectionError("Docker socket closed")),
        ):
            res = asgi_ws(
                api.app,
                f"/api/docker/containers/c123/terminal?token={self.token}",
            )
            self.assertTrue(res.accepted)
            self.assertEqual(res.close_code, 1011)

    def test_terminal_websocket_authenticated_relay(self):
        sock_a, sock_b = socket.socketpair()
        try:
            with (
                patch.object(main.docker_manager, "create_container_exec", return_value={"id": "exec_live_999"}) as mock_create,
                patch.object(main.docker_manager, "start_container_exec_socket", return_value=sock_a) as mock_start,
                patch.object(main.docker_manager, "resize_container_exec", return_value=True) as mock_resize,
                patch.object(main, "record_audit_event") as mock_audit,
            ):
                # Send stdout data from Docker socket end before disconnect
                sock_b.sendall(b"root@box:/# ")

                client_msgs = [
                    {"type": "websocket.receive", "text": '{"type": "resize", "cols": 110, "rows": 32}'},
                    {"type": "websocket.receive", "text": "uname -a\n"},
                ]
                res = asgi_ws(
                    api.app,
                    f"/api/docker/containers/c123/terminal?token={self.token}&shell=/bin/bash&user=alice",
                    client_messages=client_msgs,
                )

                self.assertTrue(res.accepted)
                mock_create.assert_called_once_with(
                    "c123",
                    cmd=["/bin/bash"],
                    user="alice",
                    tty=True,
                )
                mock_start.assert_called_once_with("exec_live_999")
                mock_resize.assert_called_with("exec_live_999", height=32, width=110)
                mock_audit.assert_called_once()
                self.assertEqual(mock_audit.call_args[0][0], "docker.container.terminal_opened")

                # Verify stdin was written to Docker socket
                received_stdin = sock_b.recv(1024)
                self.assertEqual(received_stdin, b"uname -a\n")

                # Verify stdout was delivered to client WebSocket as bytes
                byte_messages = [m.get("bytes") for m in res.messages if isinstance(m, dict) and "bytes" in m]
                self.assertIn(b"root@box:/# ", byte_messages)
        finally:
            try:
                sock_b.close()
            except Exception:
                pass


class DockerContainerTerminalFrontendTests(unittest.TestCase):
    """Frontend DOM contracts, ANSI terminal rendering, and keyboard handler tests."""

    def test_docker_container_terminal_dom_and_ui_contracts(self):
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        # 1. HTML DOM ID contracts
        required_dom_ids = [
            "container-terminal-modal",
            "container-terminal-title",
            "container-terminal-subtitle",
            "container-terminal-shell-select",
            "container-terminal-user-input",
            "btn-reconnect-terminal",
            "btn-clear-terminal",
            "btn-fullscreen-terminal",
            "btn-close-terminal",
            "container-terminal-body",
            "container-terminal-screen",
            "container-terminal-status",
        ]
        for dom_id in required_dom_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        # Shell select options
        self.assertIn('<option value="/bin/sh" selected>/bin/sh</option>', html)
        self.assertIn('<option value="/bin/bash">/bin/bash</option>', html)

        # Screen accessibility & focus attributes
        self.assertIn('tabindex="0"', html)
        self.assertIn('aria-label="Container Interactive Terminal"', html)

        # Status badge & hint text
        self.assertIn("Disconnected", html)
        self.assertIn("Ctrl+C, Ctrl+D, Tab, Arrows, and Copy/Paste supported", html)

        # 2. CSS contracts
        required_css_selectors = [
            ".btn-terminal",
            ".terminal-modal-card",
            ".modal-overlay.terminal-fullscreen",
            ".terminal-screen",
            ".terminal-cursor",
            ".terminal-row",
            ".status-dot.pulse",
            ".ansi-red",
            ".ansi-green",
            ".ansi-yellow",
            ".ansi-blue",
            ".ansi-magenta",
            ".ansi-cyan",
            ".ansi-white",
            ".ansi-bright-red",
            ".ansi-bg-red",
            ".ansi-bold",
            ".ansi-dim",
            ".ansi-underline",
        ]
        for sel in required_css_selectors:
            self.assertIn(sel, css, f"Missing CSS selector {sel} in static/styles.css")

        # 3. JS function contracts
        required_js_funcs = [
            "function openContainerTerminal(",
            "function closeContainerTerminal(",
            "connectContainerTerminal(",
            "function clearTerminalScreen(",
            "function toggleTerminalFullscreen(",
            "function feedTerminalText(",
            "function handleTerminalKeyDown(",
            "function sendTerminalResize(",
            "data-terminal-container-id=",
        ]
        for fn in required_js_funcs:
            self.assertIn(fn, js, f"Missing JS element or function {fn} in static/app.js")

    def test_docker_container_terminal_ansi_engine_and_keyboard_in_node_vm(self):
        import json
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');

// Mock browser globals for DOM & WebSocket testing
let sentWsData = [];
let mockWsInstance = null;

class MockWebSocket {{
  constructor(url) {{
    this.url = url;
    this.readyState = 1; // OPEN
    mockWsInstance = this;
    sentWsData = [];
  }}
  send(data) {{
    sentWsData.push(data);
  }}
  close() {{
    this.readyState = 3;
    if (this.onclose) this.onclose({{ code: 1000 }});
  }}
}}
MockWebSocket.OPEN = 1;

global.WebSocket = MockWebSocket;
global.window = {{
  location: {{ protocol: 'http:', host: 'localhost:8000' }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  addEventListener: () => {{}},
  getSelection: () => ({{ toString: () => '' }}),
}};
global.requestAnimationFrame = (fn) => fn();

const mockElements = {{}};
function getMockElement(id) {{
  if (!mockElements[id]) {{
    mockElements[id] = {{
      id: id,
      className: '',
      classList: {{
        add: function(c) {{ if (!this.classes.includes(c)) this.classes.push(c); }},
        remove: function(c) {{ this.classes = this.classes.filter(x => x !== c); }},
        toggle: function(c) {{
          if (this.classes.includes(c)) {{ this.remove(c); return false; }}
          else {{ this.add(c); return true; }}
        }},
        contains: function(c) {{ return this.classes.includes(c); }},
        classes: []
      }},
      style: {{}},
      innerHTML: '',
      textContent: '',
      value: '',
      scrollTop: 0,
      scrollHeight: 100,
      clientWidth: 800,
      clientHeight: 400,
      focus: () => {{}},
      appendChild: () => {{}},
      removeChild: () => {{}},
      getBoundingClientRect: () => ({{ width: 85, height: 19 }})
    }};
  }}
  return mockElements[id];
}}

global.document = {{
  getElementById: (id) => getMockElement(id),
  createElement: (tag) => ({{
    style: {{}},
    getBoundingClientRect: () => ({{ width: 85, height: 19 }}),
    textContent: ''
  }}),
  activeElement: null,
  addEventListener: () => {{}}
}};

// Read and extract terminal functions from app.js
const appJsCode = fs.readFileSync('{js_path}', 'utf-8');

const vm = require('vm');

// Evaluate state and terminal methods in current scope
vm.runInThisContext(`
var activeTerminalContainerId = null;
var activeTerminalContainerName = "";
var terminalWs = null;
var terminalUtf8Decoder = new TextDecoder("utf-8", {{ stream: true }});
var terminalLines = [[]];
var terminalCursorRow = 0;
var terminalCursorCol = 0;
var terminalCursorVisible = true;
var terminalSavedLines = null;
var terminalSavedCursorRow = 0;
var terminalSavedCursorCol = 0;
var terminalCurrentStyle = {{
  fg: null,
  bg: null,
  bold: false,
  dim: false,
  italic: false,
  underline: false,
  inverse: false,
}};
var terminalParserState = "NORMAL";
var terminalCsiParams = "";
var terminalOscParams = "";
var terminalPendingBuffer = "";
var terminalRenderScheduled = false;

function escapeHtml(str) {{ return String(str || ''); }}
function getStoredToken() {{ return 'test-session-token-123'; }}
` + appJsCode.slice(appJsCode.indexOf('function openContainerTerminal('), appJsCode.indexOf('async function fetchDockerImages(')));

// Test 1: Open Terminal & check WebSocket connection
openContainerTerminal('c_test_99', 'my_service', '/bin/bash', 'developer');
if (!terminalWs) throw new Error("terminalWs should be initialized");
if (!terminalWs.url.includes('/api/docker/containers/c_test_99/terminal')) throw new Error("Incorrect WS URL: " + terminalWs.url);
if (!terminalWs.url.includes('shell=%2Fbin%2Fbash')) throw new Error("Missing shell in WS URL");
if (!terminalWs.url.includes('user=developer')) throw new Error("Missing user in WS URL");
if (!terminalWs.url.includes('token=test-session-token-123')) throw new Error("Missing token in WS URL");

// Test 2: Trigger WS onopen & verify resize frame sent
terminalWs.onopen();
const resizeMsg = sentWsData.find(d => typeof d === 'string' && d.includes('"resize"'));
if (!resizeMsg) throw new Error("Expected initial resize message sent on connect, got: " + JSON.stringify(sentWsData));
const parsedResize = JSON.parse(resizeMsg);
if (parsedResize.type !== 'resize' || typeof parsedResize.cols !== 'number' || typeof parsedResize.rows !== 'number') {{
  throw new Error("Invalid resize payload: " + resizeMsg);
}}

// Test 3: Feed ANSI text stream and verify parser state & DOM output
sentWsData = [];
feedTerminalText("\\x1b[32mPASS\\x1b[0m: container healthy\\r\\nSecond line");
const screenEl = document.getElementById("container-terminal-screen");
if (!screenEl.innerHTML.includes('ansi-green')) throw new Error("Missing ansi-green span in screen innerHTML: " + screenEl.innerHTML);
if (!screenEl.innerHTML.includes('PASS')) throw new Error("Missing PASS text in screen innerHTML");
if (!screenEl.innerHTML.includes('Second line')) throw new Error("Missing Second line in screen innerHTML");

// Test 4: Backspace and Carriage Return handling
feedTerminalText("\\r\\x1b[KNew Prompt > ");
if (!screenEl.innerHTML.includes('New Prompt &gt; ')) throw new Error("Carriage return and erase in line failed: " + screenEl.innerHTML);

// Test 5: Keyboard handlers
// Enter
sentWsData = [];
handleTerminalKeyDown({{ key: 'Enter', preventDefault: () => {{}} }});
if (sentWsData[0] !== '\\r') throw new Error("Enter should send \\\\r, sent: " + JSON.stringify(sentWsData));

// Backspace
sentWsData = [];
handleTerminalKeyDown({{ key: 'Backspace', preventDefault: () => {{}} }});
if (sentWsData[0] !== '\\x7f') throw new Error("Backspace should send \\\\x7f, sent: " + JSON.stringify(sentWsData));

// Tab
sentWsData = [];
let tabPrevented = false;
handleTerminalKeyDown({{ key: 'Tab', preventDefault: () => {{ tabPrevented = true; }} }});
if (sentWsData[0] !== '\\t' || !tabPrevented) throw new Error("Tab should send \\\\t and prevent default");

// ArrowUp
sentWsData = [];
handleTerminalKeyDown({{ key: 'ArrowUp', preventDefault: () => {{}} }});
if (sentWsData[0] !== '\\x1b[A') throw new Error("ArrowUp should send \\\\x1b[A");

// Ctrl+C
sentWsData = [];
handleTerminalKeyDown({{ key: 'c', ctrlKey: true, preventDefault: () => {{}} }});
if (sentWsData[0] !== '\\x03') throw new Error("Ctrl+C should send \\\\x03");

// Regular character 'x'
sentWsData = [];
handleTerminalKeyDown({{ key: 'x', preventDefault: () => {{}} }});
if (sentWsData[0] !== 'x') throw new Error("Char 'x' should send 'x'");

// Test 6: DEC Private Mode handling (?25h / ?25l cursor visibility)
resetTerminalBuffer();
if (!terminalCursorVisible) throw new Error("Cursor should be visible by default");
feedTerminalText("\\x1b[?25l");
if (terminalCursorVisible) throw new Error("Cursor should be hidden after ?25l");
renderTerminalToDOM();
if (screenEl.innerHTML.includes("terminal-cursor")) throw new Error("DOM should not contain terminal-cursor when hidden");
feedTerminalText("\\x1b[?25h");
if (!terminalCursorVisible) throw new Error("Cursor should be visible after ?25h");
renderTerminalToDOM();
if (!screenEl.innerHTML.includes("terminal-cursor")) throw new Error("DOM should contain terminal-cursor when visible");

// Test 7: DEC Private Mode handling (?1049h / ?1049l alternate screen buffer)
resetTerminalBuffer();
feedTerminalText("Primary Screen Content\\r\\nLine 2");
if (terminalLines.length < 2) throw new Error("Primary screen should have 2 lines");
feedTerminalText("\\x1b[?1049h");
if (terminalLines.length !== 1 || terminalLines[0].length !== 0) throw new Error("Alt buffer should be empty on ?1049h");
feedTerminalText("Alt Buffer Content");
if (terminalLines[0].map(c => c.char).join("") !== "Alt Buffer Content") throw new Error("Alt buffer text mismatch");
feedTerminalText("\\x1b[?1049l");
const restoredText = terminalLines[0].map(c => c.char).join("");
if (restoredText !== "Primary Screen Content") throw new Error("Primary buffer not restored after ?1049l: " + restoredText);

// Test 8: DEC Private Mode: Bracketed paste (?2004) and cursor keys (?1) ignored cleanly
resetTerminalBuffer();
feedTerminalText("\\x1b[?2004h\\x1b[?1hClean Content\\x1b[?2004l");
const cleanText = terminalLines[0].map(c => c.char).join("");
if (cleanText !== "Clean Content") throw new Error("Clean Content corrupted by DEC modes: " + cleanText);

// Test 9: Control Character Filtering (DEL 0x7f, C1 codes 0x80-0x9f, U+FFFD, control chars < 32)
resetTerminalBuffer();
feedTerminalText("Valid\\x7f\\x80\\x9f\\ufffdText\\x01\\x02\\x05End");
const filteredText = terminalLines[0].map(c => c.char).join("");
if (filteredText !== "ValidTextEnd") throw new Error("Control characters not properly filtered: " + filteredText);

// Test 10: Split ANSI Sequences across chunks and UTF-8 stream decoding
resetTerminalBuffer();
feedTerminalText("\\x1b[3");
feedTerminalText("1mRed\\x1b[0m");
renderTerminalToDOM();
if (!screenEl.innerHTML.includes("ansi-red") || !screenEl.innerHTML.includes("Red")) {{
  throw new Error("Split ANSI sequence failed to render red text: " + screenEl.innerHTML);
}}
const utf8Decoder = new TextDecoder("utf-8", {{ stream: true }});
const chunk1 = new Uint8Array([0xF0, 0x9F]);
const chunk2 = new Uint8Array([0x9A, 0x80]);
const decodedChar = utf8Decoder.decode(chunk1, {{ stream: true }}) + utf8Decoder.decode(chunk2, {{ stream: true }});
if (decodedChar !== "🚀") throw new Error("Stream UTF-8 decode failed: " + decodedChar);

// Close terminal
closeContainerTerminal();
if (terminalWs !== null) throw new Error("terminalWs should be null after close");
console.log("ALL_NODE_TERMINAL_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_TERMINAL_VM_CHECKS_PASSED", res.stdout)


class DockerActionsDropdownFrontendTests(unittest.TestCase):
    """Validation tests for the streamlined Actions column with Primary Quick Actions + '⋯' Dropdown."""

    def test_actions_dropdown_css_and_js_contracts(self):
        root = Path(__file__).parent
        css_path = root / "static" / "styles.css"
        js_path = root / "static" / "app.js"

        self.assertTrue(css_path.exists(), "static/styles.css does not exist")
        self.assertTrue(js_path.exists(), "static/app.js does not exist")

        css = css_path.read_text(encoding="utf-8")
        js = js_path.read_text(encoding="utf-8")

        # Required CSS selectors
        required_css = [
            ".actions-dropdown",
            ".btn-action-more",
            ".actions-dropdown-menu",
            ".actions-dropdown-menu.show",
            ".actions-dropdown-item",
            ".actions-dropdown-item:hover",
            ".actions-dropdown-item.danger",
            ".actions-dropdown-item.danger:hover",
            ".actions-dropdown-divider",
        ]
        for sel in required_css:
            self.assertIn(sel, css, f"Missing CSS selector '{sel}' in static/styles.css")

        # Required JS contracts
        required_js = [
            "function closeAllActionsDropdowns(",
            "function initActionsDropdowns(",
            "function renderDockerStacks(",
            "function renderDockerContainers(",
            ".actions-dropdown-menu.show",
            "btn-action-more",
            "actions-dropdown-item",
            "actions-dropdown-divider",
        ]
        for item in required_js:
            self.assertIn(item, js, f"Missing JS element or function '{item}' in static/app.js")

    def test_actions_dropdown_rendering_and_behavior_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');

// Minimal DOM simulation for Node VM test
const listeners = {{}};
global.document = {{
  listeners: {{}},
  addEventListener(event, fn) {{
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(fn);
  }},
  querySelectorAll(sel) {{
    if (sel === '.actions-dropdown-menu.show') {{
      return global.openMenus || [];
    }}
    return [];
  }},
  getElementById(id) {{
    if (!global.elements) global.elements = {{}};
    if (!global.elements[id]) {{
      global.elements[id] = {{
        id: id,
        innerHTML: '',
        children: [],
        value: '',
        querySelectorAll(s) {{ return []; }}
      }};
    }}
    return global.elements[id];
  }}
}};
global.window = {{
  addEventListener() {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');

// Load script in a function wrapper with mocked environment
const vm = require('vm');
const ctx = vm.createContext({{
  document: global.document,
  window: global.window,
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

// Test 1: Stacks rendering for running vs stopped
const mockStacks = [
  {{ name: "web-stack", status: "running", running_count: 2, containers_count: 2, managed: true }},
  {{ name: "db-stack", status: "stopped", running_count: 0, containers_count: 1, managed: true }},
  {{ name: "ext-stack", status: "running", running_count: 1, containers_count: 1, managed: false }}
];

ctx.cachedDockerStacks = mockStacks;
ctx.renderDockerStacks(mockStacks);
const stacksHtml = ctx.document.getElementById("docker-stacks-tbody").innerHTML;

// Verify Running stack buttons
if (!stacksHtml.includes('data-stack-action="restart" data-stack="web-stack">Restart</button>')) {{
  throw new Error("Missing visible Restart button for running stack");
}}
if (!stacksHtml.includes('data-stack-action="edit" data-stack="web-stack">Edit</button>')) {{
  throw new Error("Missing visible Edit button for running stack");
}}
if (!stacksHtml.includes('data-stack-action="stop" data-stack="web-stack">Stop</button>')) {{
  throw new Error("Missing dropdown Stop button for running stack");
}}
if (!stacksHtml.includes('data-stack-action="pull" data-stack="web-stack">Pull</button>')) {{
  throw new Error("Missing dropdown Pull button for running stack");
}}
if (!stacksHtml.includes('data-stack-action="down" data-stack="web-stack">Down</button>')) {{
  throw new Error("Missing dropdown Down button for running stack");
}}
if (!stacksHtml.includes('data-stack-action="delete" data-stack="web-stack">Delete</button>')) {{
  throw new Error("Missing dropdown Delete button for running stack");
}}

// Verify Stopped stack buttons
if (!stacksHtml.includes('data-stack-action="start" data-stack="db-stack">Start</button>')) {{
  throw new Error("Missing visible Start button for stopped stack");
}}
if (!stacksHtml.includes('data-stack-action="edit" data-stack="db-stack">Edit</button>')) {{
  throw new Error("Missing visible Edit button for stopped stack");
}}
if (!stacksHtml.includes('data-stack-action="pull" data-stack="db-stack">Pull</button>')) {{
  throw new Error("Missing dropdown Pull button for stopped stack");
}}
if (!stacksHtml.includes('data-stack-action="down" data-stack="db-stack">Down</button>')) {{
  throw new Error("Missing dropdown Down button for stopped stack");
}}
if (!stacksHtml.includes('data-stack-action="delete" data-stack="db-stack">Delete</button>')) {{
  throw new Error("Missing dropdown Delete button for stopped stack");
}}

// Verify External stack read-only protection
if (!stacksHtml.includes("Read-Only (External)")) {{
  throw new Error("Missing external stack read-only label");
}}

// Test 2: Containers rendering for running, stopped, paused
const mockContainers = [
  {{ id: "c_run1", name: "app-server", state: "running", status: "Up 2 hours", ports: "80:80", stack: "web-stack" }},
  {{ id: "c_stop1", name: "batch-worker", state: "exited", status: "Exited (0)", ports: "", stack: "" }},
  {{ id: "c_pause1", name: "cache-redis", state: "paused", status: "Up (Paused)", ports: "6379:6379", stack: "" }}
];

ctx.renderDockerContainers(null, mockContainers);
const containersHtml = ctx.document.getElementById("docker-containers-tbody").innerHTML;

// Verify Running container buttons
if (!containersHtml.includes('data-c-action="restart" data-cid="c_run1">Restart</button>')) {{
  throw new Error("Missing visible Restart button for running container");
}}
if (!containersHtml.includes('data-c-action="logs" data-cid="c_run1"')) {{
  throw new Error("Missing visible Logs button for running container");
}}
if (!containersHtml.includes('data-terminal-container-id="c_run1"')) {{
  throw new Error("Missing visible Terminal button for running container");
}}
if (!containersHtml.includes('data-c-action="stop" data-cid="c_run1">Stop</button>')) {{
  throw new Error("Missing dropdown Stop button for running container");
}}
if (!containersHtml.includes('data-c-action="pause" data-cid="c_run1">Pause</button>')) {{
  throw new Error("Missing dropdown Pause button for running container");
}}
if (!containersHtml.includes('data-c-action="remove" data-cid="c_run1"')) {{
  throw new Error("Missing dropdown Remove button for running container");
}}

// Verify Stopped container buttons
if (!containersHtml.includes('data-c-action="start" data-cid="c_stop1">Start</button>')) {{
  throw new Error("Missing visible Start button for stopped container");
}}
if (!containersHtml.includes('data-c-action="logs" data-cid="c_stop1"')) {{
  throw new Error("Missing visible Logs button for stopped container");
}}
// Terminal is in dropdown for stopped container
if (!containersHtml.includes('actions-dropdown-item btn-terminal" data-c-action="terminal" data-terminal-container-id="c_stop1"')) {{
  throw new Error("Missing dropdown Terminal button for stopped container");
}}

// Verify Paused container buttons
if (!containersHtml.includes('data-c-action="unpause" data-cid="c_pause1">Unpause</button>')) {{
  throw new Error("Missing visible Unpause button for paused container");
}}
if (!containersHtml.includes('data-c-action="stop" data-cid="c_pause1">Stop</button>')) {{
  throw new Error("Missing dropdown Stop button for paused container");
}}

// Test 3: Dropdown close helpers
let closed = false;
global.openMenus = [{{
  classList: {{
    remove(cls) {{
      if (cls === 'show') closed = true;
    }}
  }}
}}];

ctx.closeAllActionsDropdowns();
if (!closed) {{
  throw new Error("closeAllActionsDropdowns failed to remove 'show' class");
}}

console.log("ALL_NODE_ACTIONS_DROPDOWN_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_ACTIONS_DROPDOWN_VM_CHECKS_PASSED", res.stdout)


class DockerAppCatalogAndComposeEditorFrontendTests(unittest.TestCase):
    """Validation tests for Homelab 1-Click App Catalog and Visual Compose Editor."""

    def test_docker_compose_and_app_catalog_dom_contracts(self):
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        # 1. App Catalog Modal DOM contracts
        catalog_dom_ids = [
            "app-catalog-modal",
            "app-catalog-title",
            "app-catalog-subtitle",
            "close-app-catalog-btn",
            "catalog-search-input",
            "catalog-category-filters",
            "app-catalog-grid",
            "btn-app-catalog",
        ]
        for dom_id in catalog_dom_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        # Category filter buttons
        categories = ["All", "Monitoring", "Networking", "Security", "Media", "Database", "Automation", "Management"]
        for cat in categories:
            self.assertIn(f'data-category="{cat}"', html, f"Missing category filter '{cat}' in index.html")

        # 2. Stack Editor Modal DOM contracts
        stack_editor_dom_ids = [
            "stack-editor-modal",
            "stack-editor-title",
            "stack-editor-subtitle",
            "close-stack-editor-btn",
            "stack-editor-feedback",
            "stack-editor-form",
            "stack-name-input",
            "stack-editor-tabs-nav",
            "tab-btn-compose",
            "tab-btn-env",
            "editor-tab-panel-compose",
            "editor-tab-panel-env",
            "stack-yaml-input",
            "stack-env-input",
            "stack-validate-btn",
            "stack-template-btn",
            "stack-validation-status",
            "stack-deploy-toggle",
            "save-stack-btn",
            "save-stack-text",
            "btn-new-stack",
        ]
        for dom_id in stack_editor_dom_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing #{dom_id} in static/index.html")

        # 3. CSS Contracts
        css_selectors = [
            ".app-catalog-modal-card",
            ".catalog-toolbar",
            ".catalog-category-filters",
            ".category-filter-btn",
            ".app-catalog-grid",
            ".catalog-card",
            ".btn-catalog-deploy",
            ".btn-catalog-customize",
            ".editor-tabs-nav",
            ".editor-tab-btn",
            ".editor-tab-btn.active",
            ".validation-status-box",
            ".validation-badge-valid",
            ".validation-badge-invalid",
        ]
        for sel in css_selectors:
            self.assertIn(sel, css, f"Missing CSS selector '{sel}' in static/styles.css")

        # 4. JS Function & Event Contract Parity
        required_js_symbols = [
            "function openAppCatalog(",
            "function closeAppCatalog(",
            "function renderAppCatalog(",
            "function handle1ClickLaunch(",
            "function handleCustomizeTemplate(",
            "function openStackEditor(",
            "function closeStackEditor(",
            "function switchStackEditorTab(",
            "function validateStackYaml(",
            "function handleSaveStack(",
            "function fetchAppTemplates(",
            "btn-app-catalog",
            "close-app-catalog-btn",
            "btn-new-stack",
            "close-stack-editor-btn",
            "stack-validate-btn",
            "stack-template-btn",
            "tab-btn-compose",
            "tab-btn-env",
        ]
        for symbol in required_js_symbols:
            self.assertIn(symbol, js, f"Missing JS function or element symbol '{symbol}' in static/app.js")

    def test_app_catalog_filtering_search_and_rendering_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const listeners = {{}};
  return {{
    id: id,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) classes.delete(cls); else classes.add(cls);
        }} else if (force) {{
          classes.add(cls);
        }} else {{
          classes.delete(cls);
        }}
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    listeners: listeners,
    addEventListener(evt, fn) {{
      if (!listeners[evt]) listeners[evt] = [];
      listeners[evt].push(fn);
    }},
    value: '',
    checked: false,
    disabled: false,
    textContent: '',
    innerHTML: '',
    querySelectorAll(sel) {{
      if (sel === 'button[data-template-action]') {{
        const regex = /<button[^>]*data-template-action="([^"]+)"[^>]*data-template-id="([^"]+)"/g;
        const matches = [];
        let m;
        while ((m = regex.exec(this.innerHTML)) !== null) {{
          const action = m[1];
          const tid = m[2];
          const btnElem = createMockElement("btn-" + tid + "-" + action);
          btnElem.getAttribute = (attr) => attr === "data-template-action" ? action : (attr === "data-template-id" ? tid : null);
          matches.push(btnElem);
        }}
        return matches;
      }}
      return [];
    }},
    focus() {{}},
    scrollIntoView() {{}}
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{ addEventListener: () => {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  confirm: () => true,
  alert: () => {{}}
}});

vm.runInContext(appJs, ctx);

const mockTemplates = [
  {{ id: 'pihole', name: 'Pi-hole', category: 'Security', description: 'Network-wide ad blocking', default_ports: ['53', '80'], compose_yaml: 'services:\\n  pihole:\\n    image: pihole/pihole', env_content: 'TZ=America/New_York' }},
  {{ id: 'uptime-kuma', name: 'Uptime Kuma', category: 'Monitoring', description: 'Self-hosted monitoring tool', default_ports: ['3001'], compose_yaml: 'services:\\n  uptime-kuma:\\n    image: louislam/uptime-kuma:1', env_content: '' }},
  {{ id: 'nginx-proxy-manager', name: 'Nginx Proxy Manager', category: 'Networking', description: 'Docker container management web UI', default_ports: ['80', '81', '443'], compose_yaml: 'services:\\n  npm:\\n    image: jc21/nginx-proxy-manager', env_content: '' }},
  {{ id: 'jellyfin', name: 'Jellyfin', category: 'Media', description: 'The Free Software Media System', default_ports: ['8096'], compose_yaml: 'services:\\n  jellyfin:\\n    image: jellyfin/jellyfin', env_content: '' }},
  {{ id: 'vaultwarden', name: 'Vaultwarden', category: 'Security', description: 'Lightweight Bitwarden server', default_ports: ['8080'], compose_yaml: 'services:\\n  vaultwarden:\\n    image: vaultwarden/server', env_content: '' }}
];

ctx.mockTemplates = mockTemplates;
vm.runInContext("cachedAppTemplates = mockTemplates;", ctx);

// 1. Test modal opening
const catalogModal = getEl('app-catalog-modal');
catalogModal.classList.add('hidden');
ctx.openAppCatalog();
if (catalogModal.classList.contains('hidden')) {{
  throw new Error("openAppCatalog failed to remove hidden class");
}}

// 2. Test Category filtering
// Category: Monitoring
vm.runInContext("currentAppCatalogCategory = 'Monitoring';", ctx);
getEl('catalog-search-input').value = '';
ctx.renderAppCatalog();
let gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Uptime Kuma')) throw new Error("Monitoring category missing Uptime Kuma");
if (gridHtml.includes('Pi-hole')) throw new Error("Monitoring category unexpectedly contains Pi-hole");
if (gridHtml.includes('Jellyfin')) throw new Error("Monitoring category unexpectedly contains Jellyfin");

// Category: Security
vm.runInContext("currentAppCatalogCategory = 'Security';", ctx);
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Pi-hole')) throw new Error("Security category missing Pi-hole");
if (!gridHtml.includes('Vaultwarden')) throw new Error("Security category missing Vaultwarden");
if (gridHtml.includes('Uptime Kuma')) throw new Error("Security category unexpectedly contains Uptime Kuma");

// Category: All
vm.runInContext("currentAppCatalogCategory = 'All';", ctx);
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Pi-hole') || !gridHtml.includes('Uptime Kuma') || !gridHtml.includes('Jellyfin')) {{
  throw new Error("All category does not contain all templates");
}}

// 3. Test Search filtering
// Search by port
getEl('catalog-search-input').value = '3001';
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Uptime Kuma') || gridHtml.includes('Pi-hole')) {{
  throw new Error("Search by port 3001 failed");
}}

// Search by description keyword
getEl('catalog-search-input').value = 'bitwarden';
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Vaultwarden') || gridHtml.includes('Jellyfin')) {{
  throw new Error("Search by description keyword failed");
}}

// Search by name
getEl('catalog-search-input').value = 'jellyfin';
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('Jellyfin') || gridHtml.includes('Vaultwarden')) {{
  throw new Error("Search by name failed");
}}

// Empty search results
getEl('catalog-search-input').value = 'nonexistent-app-xyz';
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('No applications found matching filters.')) {{
  throw new Error("Empty search query state missing");
}}

// 4. Test Card Buttons & Tags
getEl('catalog-search-input').value = '';
ctx.renderAppCatalog();
gridHtml = getEl('app-catalog-grid').innerHTML;
if (!gridHtml.includes('data-template-action="deploy"')) throw new Error("Missing 1-Click Launch action button");
if (!gridHtml.includes('data-template-action="customize"')) throw new Error("Missing Customize action button");
if (!gridHtml.includes('catalog-port-tag">53</span>')) throw new Error("Missing port tag rendering");

// Test modal close
ctx.closeAppCatalog();
if (!catalogModal.classList.contains('hidden')) {{
  throw new Error("closeAppCatalog failed to add hidden class");
}}

console.log("ALL_NODE_APP_CATALOG_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_APP_CATALOG_VM_CHECKS_PASSED", res.stdout)

    def test_stack_editor_tab_switching_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  return {{
    id: id,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
    }},
    value: '',
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{ addEventListener: () => {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

const tabCompose = getEl('tab-btn-compose');
const tabEnv = getEl('tab-btn-env');
const panelCompose = getEl('editor-tab-panel-compose');
const panelEnv = getEl('editor-tab-panel-env');

// Switch to env tab
ctx.switchStackEditorTab('env');
if (!tabEnv.classList.contains('active')) throw new Error("tabEnv missing active class");
if (tabCompose.classList.contains('active')) throw new Error("tabCompose unexpectedly has active class");
if (!panelCompose.classList.contains('hidden')) throw new Error("panelCompose missing hidden class");
if (panelEnv.classList.contains('hidden')) throw new Error("panelEnv unexpectedly has hidden class");
if (vm.runInContext("activeStackEditorTab", ctx) !== 'env') throw new Error("activeStackEditorTab not 'env'");

// Switch back to compose tab
ctx.switchStackEditorTab('compose');
if (!tabCompose.classList.contains('active')) throw new Error("tabCompose missing active class");
if (tabEnv.classList.contains('active')) throw new Error("tabEnv unexpectedly has active class");
if (panelCompose.classList.contains('hidden')) throw new Error("panelCompose unexpectedly has hidden class");
if (!panelEnv.classList.contains('hidden')) throw new Error("panelEnv missing hidden class");
if (vm.runInContext("activeStackEditorTab", ctx) !== 'compose') throw new Error("activeStackEditorTab not 'compose'");

console.log("ALL_NODE_STACK_EDITOR_TAB_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_STACK_EDITOR_TAB_VM_CHECKS_PASSED", res.stdout)

    def test_stack_yaml_validation_triggering_and_feedback_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  return {{
    id: id,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    value: '',
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{ addEventListener: () => {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

(async () => {{
  const statusBox = getEl('stack-validation-status');

  // Test 1: Empty YAML triggering
  await ctx.validateStackYaml('');
  if (statusBox.classList.contains('hidden')) throw new Error("Empty validation statusBox has hidden class");
  if (!statusBox.innerHTML.includes('Empty YAML') || !statusBox.innerHTML.includes('Compose YAML content cannot be empty.')) {{
    throw new Error("Empty YAML validation text missing: " + statusBox.innerHTML);
  }}

  // Test 2: Valid YAML feedback rendering
  ctx.apiFetch = async function(url, opts) {{
    return {{
      ok: true,
      json: async () => ({{
        valid: true,
        services: ['web-server', 'redis-db'],
        ports: ['8080:80', '6379:6379'],
        warnings: ['Warning: No restart policy specified']
      }})
    }};
  }};

  await ctx.validateStackYaml('services:\\n  web-server:\\n    image: nginx\\n  redis-db:\\n    image: redis');
  if (statusBox.classList.contains('hidden')) throw new Error("Valid statusBox has hidden class");
  if (!statusBox.innerHTML.includes('Valid Compose YAML')) throw new Error("Valid YAML badge missing");
  if (!statusBox.innerHTML.includes('2 services detected')) throw new Error("Service count missing");
  if (!statusBox.innerHTML.includes('web-server') || !statusBox.innerHTML.includes('redis-db')) throw new Error("Service names missing in pills");
  if (!statusBox.innerHTML.includes('8080:80') || !statusBox.innerHTML.includes('6379:6379')) throw new Error("Port pills missing");
  if (!statusBox.innerHTML.includes('Warning: No restart policy specified')) throw new Error("Warning message missing");

  // Test 3: Invalid YAML feedback rendering
  ctx.apiFetch = async function(url, opts) {{
    return {{
      ok: false,
      json: async () => ({{
        valid: false,
        errors: ['YAML parsing error at line 4: mapping values are not allowed here']
      }})
    }};
  }};

  await ctx.validateStackYaml('services:\\n  bad: [unclosed');
  if (statusBox.classList.contains('hidden')) throw new Error("Invalid statusBox has hidden class");
  if (!statusBox.innerHTML.includes('Invalid Compose YAML')) throw new Error("Invalid YAML badge missing");
  if (!statusBox.innerHTML.includes('YAML parsing error at line 4')) throw new Error("Error details missing");

  // Test 4: Network failure error handling
  ctx.apiFetch = async function(url, opts) {{
    throw new Error("Connection failed: backend unreachable");
  }};

  await ctx.validateStackYaml('services:\\n  valid: test');
  if (!statusBox.innerHTML.includes('Validation Error') || !statusBox.innerHTML.includes('Connection failed: backend unreachable')) {{
    throw new Error("Network error feedback missing: " + statusBox.innerHTML);
  }}

  console.log("ALL_NODE_STACK_YAML_VALIDATION_VM_CHECKS_PASSED");
}})();
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_STACK_YAML_VALIDATION_VM_CHECKS_PASSED", res.stdout)

    def test_1click_deploy_and_stack_save_payload_construction_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const listeners = {{}};
  return {{
    id: id,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    listeners: listeners,
    addEventListener(evt, fn) {{
      if (!listeners[evt]) listeners[evt] = [];
      listeners[evt].push(fn);
    }},
    value: '',
    checked: false,
    disabled: false,
    textContent: '',
    innerHTML: '',
    focus() {{}},
    scrollIntoView() {{}}
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const capturedCalls = [];

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{ addEventListener: () => {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  confirm: (prompt) => true,
  alert: (msg) => {{}}
}});

vm.runInContext(appJs, ctx);

ctx.apiFetch = async function(url, options = {{}}) {{
  capturedCalls.push({{ url, options }});
  return {{
    ok: true,
    json: async () => ({{ success: true }})
  }};
}};

(async () => {{
  // Test 1: 1-Click Launch with .env and collision naming resolution
  capturedCalls.length = 0;
  vm.runInContext("cachedDockerStacks = [{{ name: 'pihole' }}];", ctx);

  const template = {{
    id: 'pihole',
    name: 'Pi-hole',
    compose_yaml: 'services:\\n  pihole:\\n    image: pihole/pihole:latest',
    env_content: 'TZ=America/New_York\\nWEBPASSWORD=admin_pass'
  }};

  await ctx.handle1ClickLaunch(template);

  const launchCall = capturedCalls.find(c => c.url === '/api/docker/stacks' && c.options.method === 'POST');
  if (!launchCall) throw new Error("handle1ClickLaunch POST /api/docker/stacks call not found among calls: " + JSON.stringify(capturedCalls));
  const launchPayload = JSON.parse(launchCall.options.body);
  if (launchPayload.name !== 'pihole-2') throw new Error("Collision deduplication failed, got: " + launchPayload.name);
  if (launchPayload.compose_yaml !== template.compose_yaml) throw new Error("Compose YAML mismatch");
  if (launchPayload.env_content !== 'TZ=America/New_York\\nWEBPASSWORD=admin_pass') throw new Error(".env content mismatch: " + launchPayload.env_content);
  if (launchPayload.deploy !== true) throw new Error("deploy flag must be true");

  // Test 2: Stack Editor Save - New Stack with .env (POST /api/docker/stacks)
  capturedCalls.length = 0;
  getEl('stack-name-input').value = 'homelab-stack';
  getEl('stack-name-input').disabled = false;
  getEl('stack-yaml-input').value = 'services:\\n  app:\\n    image: node:alpine';
  getEl('stack-env-input').value = 'NODE_ENV=production\\nPORT=3000';
  getEl('stack-deploy-toggle').checked = true;

  await ctx.handleSaveStack();

  const createCall = capturedCalls.find(c => c.url === '/api/docker/stacks' && c.options.method === 'POST');
  if (!createCall) throw new Error("Create stack POST /api/docker/stacks call not found");
  const createPayload = JSON.parse(createCall.options.body);
  if (createPayload.name !== 'homelab-stack') throw new Error("Create name mismatch: " + createPayload.name);
  if (createPayload.compose_yaml !== 'services:\\n  app:\\n    image: node:alpine') throw new Error("Create compose_yaml mismatch");
  if (createPayload.env_content !== 'NODE_ENV=production\\nPORT=3000') throw new Error("Create env_content mismatch");
  if (createPayload.deploy !== true) throw new Error("Create deploy flag mismatch");

  // Test 3: Stack Editor Save - Edit Existing Stack with PUT /api/docker/stacks/:name
  capturedCalls.length = 0;
  getEl('stack-name-input').value = 'homelab-stack';
  getEl('stack-name-input').disabled = true; // Signals edit mode
  getEl('stack-yaml-input').value = 'services:\\n  app:\\n    image: node:20-alpine';
  getEl('stack-env-input').value = 'NODE_ENV=production\\nPORT=8080';
  getEl('stack-deploy-toggle').checked = false;

  await ctx.handleSaveStack();

  const editCall = capturedCalls.find(c => c.url === '/api/docker/stacks/homelab-stack' && c.options.method === 'PUT');
  if (!editCall) throw new Error("Edit stack PUT /api/docker/stacks/homelab-stack call not found");
  const editPayload = JSON.parse(editCall.options.body);
  if (editPayload.name !== 'homelab-stack') throw new Error("Edit name mismatch: " + editPayload.name);
  if (editPayload.compose_yaml !== 'services:\\n  app:\\n    image: node:20-alpine') throw new Error("Edit compose_yaml mismatch");
  if (editPayload.env_content !== 'NODE_ENV=production\\nPORT=8080') throw new Error("Edit env_content mismatch");
  if (editPayload.deploy !== false) throw new Error("Edit deploy flag mismatch, expected false");

  // Test 4: Frontend Validation Protection (Invalid Name)
  capturedCalls.length = 0;
  getEl('stack-name-input').disabled = false;
  getEl('stack-name-input').value = 'invalid name with spaces';
  const feedback = getEl('stack-editor-feedback');
  feedback.classList.add('hidden');

  await ctx.handleSaveStack();
  if (capturedCalls.length !== 0) throw new Error("API call should not be dispatched for invalid stack name");
  if (feedback.classList.contains('hidden')) throw new Error("Feedback was not displayed for invalid stack name");
  if (!feedback.textContent.includes("letters, numbers, dashes, and underscores")) {{
    throw new Error("Feedback message mismatch: " + feedback.textContent);
  }}

  // Test 5: Frontend Validation Protection (Empty YAML)
  capturedCalls.length = 0;
  getEl('stack-name-input').value = 'valid-name';
  getEl('stack-yaml-input').value = '';
  feedback.classList.add('hidden');

  await ctx.handleSaveStack();
  if (capturedCalls.length !== 0) throw new Error("API call should not be dispatched for empty YAML");
  if (feedback.classList.contains('hidden')) throw new Error("Feedback was not displayed for empty YAML");
  if (!feedback.textContent.includes("cannot be empty")) {{
    throw new Error("Feedback message mismatch for empty YAML: " + feedback.textContent);
  }}

  console.log("ALL_NODE_1CLICK_DEPLOY_AND_SAVE_VM_CHECKS_PASSED");
}})();
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_1CLICK_DEPLOY_AND_SAVE_VM_CHECKS_PASSED", res.stdout)


class MultiChannelAlertHubFrontendTests(unittest.TestCase):
    """Validation tests for Multi-Channel Alert Hub DOM elements, UI contracts, and Node VM execution."""

    def test_multichannel_alert_hub_dom_and_ui_contracts(self):
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")

        # 1. Status badges for all 6 channels in the delivery channels summary ribbon
        expected_status_badges = [
            "discord-status-badge",
            "telegram-status-badge",
            "webhook-status-badge",
            "pushover-status-badge",
            "ntfy-status-badge",
            "gotify-status-badge",
        ]
        for badge_id in expected_status_badges:
            self.assertIn(f'id="{badge_id}"', html, f"Missing channel badge #{badge_id} in index.html")

        # 2. Modal containers and primary action buttons
        modal_dom_ids = [
            "modal-notif-channels",
            "btn-close-notif-channels",
            "btn-cancel-notif-channels",
            "save-discord-btn",
            "notif-test-channel-select",
            "send-test-notification-btn",
        ]
        for elem_id in modal_dom_ids:
            self.assertIn(f'id="{elem_id}"', html, f"Missing modal control #{elem_id} in index.html")

        # 3. Channel enable toggle switches
        channels = ["discord", "telegram", "webhook", "pushover", "ntfy", "gotify"]
        for ch in channels:
            toggle_id = f"notif-channel-{ch}-enable"
            self.assertIn(f'id="{toggle_id}"', html, f"Missing enable toggle #{toggle_id} in index.html")

        # 4. Token status badges for credential masking feedback
        token_badges = [
            "discord-token-status",
            "notif-telegram-token-badge",
            "notif-pushover-user-key-badge",
            "notif-pushover-token-badge",
            "notif-ntfy-token-badge",
            "notif-gotify-token-badge",
        ]
        for badge_id in token_badges:
            self.assertIn(f'id="{badge_id}"', html, f"Missing token status badge #{badge_id} in index.html")

        # 5. Routing checkboxes for all 6 channels
        routing_events = ["raid-alerts", "raid-weekly", "cron-failures", "cron-successes"]
        for ch in channels:
            for event in routing_events:
                chk_id = f"notif-{ch}-route-{event}"
                self.assertIn(f'id="{chk_id}"', html, f"Missing routing checkbox #{chk_id} in index.html")

        # 6. Test target selector options for all 6 channels plus 'all'
        target_options = ['value="all"', 'value="discord"', 'value="telegram"', 'value="webhook"', 'value="pushover"', 'value="ntfy"', 'value="gotify"']
        for opt in target_options:
            self.assertIn(opt, html, f"Missing test channel option {opt} in index.html")

    def test_multichannel_status_badge_rendering_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  return {{
    id: id,
    style: {{}},
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) classes.delete(cls); else classes.add(cls);
        }} else if (force) {{
          classes.add(cls);
        }} else {{
          classes.delete(cls);
        }}
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    placeholder: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    clearTimeout: () => {{}},
    setTimeout: setTimeout,
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

// Test 1: Direct updateChannelStatusBadge behavior for ACTIVE, DISABLED, and NOT CONFIGURED
const testBadgeId = 'pushover-status-badge';

// Not Configured
ctx.updateChannelStatusBadge(testBadgeId, false, false);
let badgeEl = getEl(testBadgeId);
if (badgeEl.textContent !== 'NOT CONFIGURED') throw new Error("Expected NOT CONFIGURED text, got: " + badgeEl.textContent);
if (!badgeEl.classList.contains('status-not-configured') || !badgeEl.classList.contains('status-degraded')) {{
  throw new Error("Missing not-configured classes");
}}
if (badgeEl.getAttribute('data-state') !== 'not-configured') throw new Error("Expected data-state not-configured");

// Configured and Active
ctx.updateChannelStatusBadge(testBadgeId, true, true);
if (badgeEl.textContent !== 'ACTIVE') throw new Error("Expected ACTIVE text, got: " + badgeEl.textContent);
if (!badgeEl.classList.contains('status-active') || !badgeEl.classList.contains('status-clean')) {{
  throw new Error("Missing active classes");
}}
if (badgeEl.getAttribute('data-state') !== 'active') throw new Error("Expected data-state active");

// Configured but Disabled
ctx.updateChannelStatusBadge(testBadgeId, true, false);
if (badgeEl.textContent !== 'DISABLED') throw new Error("Expected DISABLED text, got: " + badgeEl.textContent);
if (!badgeEl.classList.contains('status-disabled')) throw new Error("Missing status-disabled class");
if (badgeEl.getAttribute('data-state') !== 'disabled') throw new Error("Expected data-state disabled");

// Test 2: Full renderNotificationCenterConfig rendering for all 6 channels simultaneously
const channels = ['discord', 'telegram', 'webhook', 'pushover', 'ntfy', 'gotify'];

// Scenario A: All 6 Channels Configured and Active
const allActiveConfig = {{
  channels: {{
    discord: {{ enabled: true, token_configured: true, channel_id: "123456" }},
    telegram: {{ enabled: true, token_configured: true, chat_id: "654321" }},
    webhook: {{ enabled: true, url: "https://webhook.site/test" }},
    pushover: {{ enabled: true, user_key_configured: true, api_token_configured: true }},
    ntfy: {{ enabled: true, topic: "homelab-alerts" }},
    gotify: {{ enabled: true, server_url: "https://gotify.net", app_token_configured: true }}
  }}
}};
ctx.renderNotificationCenterConfig(allActiveConfig);
for (const ch of channels) {{
  const b = getEl(`${{ch}}-status-badge`);
  if (b.textContent !== 'ACTIVE' || b.getAttribute('data-state') !== 'active') {{
    throw new Error(`Channel ${{ch}} badge not ACTIVE in Scenario A: text=${{b.textContent}}, state=${{b.getAttribute('data-state')}}`);
  }}
}}

// Scenario B: All 6 Channels Configured but Disabled
const allDisabledConfig = {{
  channels: {{
    discord: {{ enabled: false, token_configured: true, channel_id: "123456" }},
    telegram: {{ enabled: false, token_configured: true, chat_id: "654321" }},
    webhook: {{ enabled: false, url: "https://webhook.site/test" }},
    pushover: {{ enabled: false, user_key_configured: true, api_token_configured: true }},
    ntfy: {{ enabled: false, topic: "homelab-alerts" }},
    gotify: {{ enabled: false, server_url: "https://gotify.net", app_token_configured: true }}
  }}
}};
ctx.renderNotificationCenterConfig(allDisabledConfig);
for (const ch of channels) {{
  const b = getEl(`${{ch}}-status-badge`);
  if (b.textContent !== 'DISABLED' || b.getAttribute('data-state') !== 'disabled') {{
    throw new Error(`Channel ${{ch}} badge not DISABLED in Scenario B: text=${{b.textContent}}, state=${{b.getAttribute('data-state')}}`);
  }}
}}

// Scenario C: All 6 Channels Unconfigured
const allUnconfigured = {{ channels: {{}} }};
ctx.renderNotificationCenterConfig(allUnconfigured);
for (const ch of channels) {{
  const b = getEl(`${{ch}}-status-badge`);
  if (b.textContent !== 'NOT CONFIGURED' || b.getAttribute('data-state') !== 'not-configured') {{
    throw new Error(`Channel ${{ch}} badge not NOT CONFIGURED in Scenario C: text=${{b.textContent}}, state=${{b.getAttribute('data-state')}}`);
  }}
}}

console.log("ALL_NODE_MULTICHANNEL_STATUS_BADGE_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_MULTICHANNEL_STATUS_BADGE_VM_CHECKS_PASSED", res.stdout)

    def test_channel_modal_and_credential_masking_population_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  return {{
    id: id,
    style: {{}},
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) classes.delete(cls); else classes.add(cls);
        }} else if (force) {{
          classes.add(cls);
        }} else {{
          classes.delete(cls);
        }}
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    placeholder: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    clearTimeout: () => {{}},
    setTimeout: setTimeout,
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

// 1. Test Modal Open / Close Controllers
const modal = getEl('modal-notif-channels');
modal.classList.add('hidden');

ctx.openNotifChannelsModal();
if (modal.classList.contains('hidden')) throw new Error("openNotifChannelsModal failed to remove 'hidden' class");

ctx.closeNotifChannelsModal();
if (!modal.classList.contains('hidden')) throw new Error("closeNotifChannelsModal failed to add 'hidden' class");

// 2. Test Form Population with Masked Credentials and Routing Rules
const payloadConfig = {{
  channels: {{
    discord: {{
      enabled: true,
      token_configured: true,
      token_masked: "••••1234",
      channel_id: "discord-chan-42",
      routing: {{ raid_alerts: true, raid_weekly: false, cron_failures: true, cron_successes: false }}
    }},
    telegram: {{
      enabled: true,
      token_configured: true,
      token_masked: "••••5678",
      chat_id: "telegram-chat-88",
      routing: {{ raid_alerts: false, raid_weekly: true, cron_failures: false, cron_successes: true }}
    }},
    webhook: {{
      enabled: true,
      url: "https://webhook.site/endpoint-custom",
      routing: {{ raid_alerts: true, raid_weekly: true, cron_failures: false, cron_successes: false }}
    }},
    pushover: {{
      enabled: true,
      user_key_configured: true,
      user_key_masked: "••••9999",
      api_token_configured: true,
      token_masked: "••••8888",
      priority: 1,
      routing: {{ raid_alerts: false, raid_weekly: false, cron_failures: true, cron_successes: true }}
    }},
    ntfy: {{
      enabled: true,
      server_url: "https://ntfy.example.com",
      topic: "homelab-ntfy-topic",
      access_token_configured: true,
      token_masked: "••••7777",
      priority: 4,
      routing: {{ raid_alerts: true, raid_weekly: false, cron_failures: false, cron_successes: true }}
    }},
    gotify: {{
      enabled: true,
      server_url: "https://gotify.example.com",
      app_token_configured: true,
      token_masked: "••••6666",
      priority: 8,
      routing: {{ raid_alerts: false, raid_weekly: true, cron_failures: true, cron_successes: false }}
    }}
  }}
}};

ctx.renderNotificationCenterConfig(payloadConfig);

// Verify Discord credentials masking and routing
if (!getEl('discord-token-status').textContent.includes('••••1234')) throw new Error("Discord token badge missing masked preview");
if (!getEl('notif-discord-token-input').placeholder.includes('••••1234')) throw new Error("Discord input placeholder missing masked preview");
if (getEl('notif-discord-channel-input').value !== 'discord-chan-42') throw new Error("Discord channel input mismatch");
if (getEl('notif-discord-route-raid-alerts').checked !== true) throw new Error("Discord raid-alerts routing mismatch");
if (getEl('notif-discord-route-raid-weekly').checked !== false) throw new Error("Discord raid-weekly routing mismatch");
if (getEl('notif-discord-route-cron-failures').checked !== true) throw new Error("Discord cron-failures routing mismatch");
if (getEl('notif-discord-route-cron-successes').checked !== false) throw new Error("Discord cron-successes routing mismatch");

// Verify Telegram credentials masking and routing
if (!getEl('notif-telegram-token-badge').textContent.includes('••••5678')) throw new Error("Telegram token badge missing masked preview");
if (!getEl('notif-telegram-token-input').placeholder.includes('••••5678')) throw new Error("Telegram input placeholder missing masked preview");
if (getEl('notif-telegram-chat-id-input').value !== 'telegram-chat-88') throw new Error("Telegram chat_id mismatch");
if (getEl('notif-telegram-route-raid-alerts').checked !== false) throw new Error("Telegram raid-alerts routing mismatch");
if (getEl('notif-telegram-route-raid-weekly').checked !== true) throw new Error("Telegram raid-weekly routing mismatch");
if (getEl('notif-telegram-route-cron-failures').checked !== false) throw new Error("Telegram cron-failures routing mismatch");
if (getEl('notif-telegram-route-cron-successes').checked !== true) throw new Error("Telegram cron-successes routing mismatch");

// Verify Webhook routing
if (getEl('notif-webhook-url-input').value !== 'https://webhook.site/endpoint-custom') throw new Error("Webhook URL mismatch");
if (getEl('notif-webhook-route-raid-alerts').checked !== true) throw new Error("Webhook raid-alerts routing mismatch");
if (getEl('notif-webhook-route-raid-weekly').checked !== true) throw new Error("Webhook raid-weekly routing mismatch");
if (getEl('notif-webhook-route-cron-failures').checked !== false) throw new Error("Webhook cron-failures routing mismatch");
if (getEl('notif-webhook-route-cron-successes').checked !== false) throw new Error("Webhook cron-successes routing mismatch");

// Verify Pushover credentials masking and routing
if (!getEl('notif-pushover-user-key-badge').textContent.includes('••••9999')) throw new Error("Pushover user key badge missing masked preview");
if (!getEl('notif-pushover-token-badge').textContent.includes('••••8888')) throw new Error("Pushover token badge missing masked preview");
if (!getEl('notif-pushover-user-key-input').placeholder.includes('••••9999')) throw new Error("Pushover user key placeholder missing masked preview");
if (!getEl('notif-pushover-token-input').placeholder.includes('••••8888')) throw new Error("Pushover token placeholder missing masked preview");
if (getEl('notif-pushover-priority-select').value !== '1') throw new Error("Pushover priority mismatch");
if (getEl('notif-pushover-route-raid-alerts').checked !== false) throw new Error("Pushover raid-alerts routing mismatch");
if (getEl('notif-pushover-route-raid-weekly').checked !== false) throw new Error("Pushover raid-weekly routing mismatch");
if (getEl('notif-pushover-route-cron-failures').checked !== true) throw new Error("Pushover cron-failures routing mismatch");
if (getEl('notif-pushover-route-cron-successes').checked !== true) throw new Error("Pushover cron-successes routing mismatch");

// Verify Ntfy credentials masking and routing
if (!getEl('notif-ntfy-token-badge').textContent.includes('••••7777')) throw new Error("Ntfy token badge missing masked preview");
if (!getEl('notif-ntfy-token-input').placeholder.includes('••••7777')) throw new Error("Ntfy token placeholder missing masked preview");
if (getEl('notif-ntfy-url-input').value !== 'https://ntfy.example.com') throw new Error("Ntfy URL mismatch");
if (getEl('notif-ntfy-topic-input').value !== 'homelab-ntfy-topic') throw new Error("Ntfy topic mismatch");
if (getEl('notif-ntfy-priority-select').value !== '4') throw new Error("Ntfy priority mismatch");
if (getEl('notif-ntfy-route-raid-alerts').checked !== true) throw new Error("Ntfy raid-alerts routing mismatch");
if (getEl('notif-ntfy-route-raid-weekly').checked !== false) throw new Error("Ntfy raid-weekly routing mismatch");
if (getEl('notif-ntfy-route-cron-failures').checked !== false) throw new Error("Ntfy cron-failures routing mismatch");
if (getEl('notif-ntfy-route-cron-successes').checked !== true) throw new Error("Ntfy cron-successes routing mismatch");

// Verify Gotify credentials masking and routing
if (!getEl('notif-gotify-token-badge').textContent.includes('••••6666')) throw new Error("Gotify token badge missing masked preview");
if (!getEl('notif-gotify-token-input').placeholder.includes('••••6666')) throw new Error("Gotify token placeholder missing masked preview");
if (getEl('notif-gotify-url-input').value !== 'https://gotify.example.com') throw new Error("Gotify URL mismatch");
if (getEl('notif-gotify-priority-select').value !== '8') throw new Error("Gotify priority mismatch");
if (getEl('notif-gotify-route-raid-alerts').checked !== false) throw new Error("Gotify raid-alerts routing mismatch");
if (getEl('notif-gotify-route-raid-weekly').checked !== true) throw new Error("Gotify raid-weekly routing mismatch");
if (getEl('notif-gotify-route-cron-failures').checked !== true) throw new Error("Gotify cron-failures routing mismatch");
if (getEl('notif-gotify-route-cron-successes').checked !== false) throw new Error("Gotify cron-successes routing mismatch");

console.log("ALL_NODE_CHANNEL_MODAL_AND_MASKING_POPULATION_VM_CHECKS_PASSED");
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_CHANNEL_MODAL_AND_MASKING_POPULATION_VM_CHECKS_PASSED", res.stdout)

    def test_channel_payload_construction_and_test_target_dispatch_in_node_vm(self):
        import subprocess

        root = Path(__file__).parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  return {{
    id: id,
    style: {{}},
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) classes.delete(cls); else classes.add(cls);
        }} else if (force) {{
          classes.add(cls);
        }} else {{
          classes.delete(cls);
        }}
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    placeholder: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
const capturedCalls = [];

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    clearTimeout: () => {{}},
    setTimeout: setTimeout,
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams
}});

vm.runInContext(appJs, ctx);

ctx.apiFetch = async function(url, options = {{}}) {{
  capturedCalls.push({{ url, options }});
  return {{
    ok: true,
    status: 200,
    json: async () => ({{ success: true, message: "OK" }})
  }};
}};

(async () => {{
  // 1. Payload construction when password inputs are blank (preserves existing tokens)
  getEl('notif-channel-pushover-enable').checked = true;
  getEl('notif-pushover-user-key-input').value = ''; // Left blank
  getEl('notif-pushover-token-input').value = '';    // Left blank
  getEl('notif-pushover-priority-select').value = '2';
  getEl('notif-pushover-route-raid-alerts').checked = true;
  getEl('notif-pushover-route-raid-weekly').checked = false;
  getEl('notif-pushover-route-cron-failures').checked = true;
  getEl('notif-pushover-route-cron-successes').checked = false;

  getEl('notif-channel-ntfy-enable').checked = true;
  getEl('notif-ntfy-url-input').value = 'https://ntfy.sh';
  getEl('notif-ntfy-topic-input').value = 'my-topic';
  getEl('notif-ntfy-token-input').value = '';        // Left blank
  getEl('notif-ntfy-priority-select').value = '5';
  getEl('notif-ntfy-route-raid-alerts').checked = false;
  getEl('notif-ntfy-route-raid-weekly').checked = true;
  getEl('notif-ntfy-route-cron-failures').checked = false;
  getEl('notif-ntfy-route-cron-successes').checked = true;

  getEl('notif-channel-gotify-enable').checked = true;
  getEl('notif-gotify-url-input').value = 'https://gotify.me';
  getEl('notif-gotify-token-input').value = '';      // Left blank
  getEl('notif-gotify-priority-select').value = '7';
  getEl('notif-gotify-route-raid-alerts').checked = true;
  getEl('notif-gotify-route-raid-weekly').checked = false;
  getEl('notif-gotify-route-cron-failures').checked = true;
  getEl('notif-gotify-route-cron-successes').checked = false;

  const payloadPreserved = ctx.getDeliveryChannelsPayload();
  if (payloadPreserved.pushover.user_key !== undefined) throw new Error("user_key should be omitted when empty to preserve masked secret");
  if (payloadPreserved.pushover.api_token !== undefined) throw new Error("api_token should be omitted when empty to preserve masked secret");
  if (payloadPreserved.pushover.priority !== 2) throw new Error("Pushover priority mismatch");
  if (payloadPreserved.pushover.routing.raid_alerts !== true || payloadPreserved.pushover.routing.raid_weekly !== false) {{
    throw new Error("Pushover routing mismatch in payload");
  }}

  if (payloadPreserved.ntfy.access_token !== undefined) throw new Error("access_token should be omitted when empty");
  if (payloadPreserved.ntfy.server_url !== 'https://ntfy.sh') throw new Error("ntfy server_url mismatch");
  if (payloadPreserved.ntfy.topic !== 'my-topic') throw new Error("ntfy topic mismatch");
  if (payloadPreserved.ntfy.priority !== 5) throw new Error("ntfy priority mismatch");

  if (payloadPreserved.gotify.app_token !== undefined) throw new Error("app_token should be omitted when empty");
  if (payloadPreserved.gotify.server_url !== 'https://gotify.me') throw new Error("gotify server_url mismatch");
  if (payloadPreserved.gotify.priority !== 7) throw new Error("gotify priority mismatch");

  // 2. Payload construction when new credentials are submitted
  getEl('notif-pushover-user-key-input').value = 'new_u_key_123';
  getEl('notif-pushover-token-input').value = 'new_a_token_456';
  getEl('notif-ntfy-token-input').value = 'new_tk_access_789';
  getEl('notif-gotify-token-input').value = 'new_gotify_token_999';

  const payloadUpdated = ctx.getDeliveryChannelsPayload();
  if (payloadUpdated.pushover.user_key !== 'new_u_key_123') throw new Error("New Pushover user_key not captured");
  if (payloadUpdated.pushover.api_token !== 'new_a_token_456') throw new Error("New Pushover api_token not captured");
  if (payloadUpdated.ntfy.access_token !== 'new_tk_access_789') throw new Error("New Ntfy access_token not captured");
  if (payloadUpdated.gotify.app_token !== 'new_gotify_token_999') throw new Error("New Gotify app_token not captured");

  // 3. Save Delivery Channels dispatch (handleSaveDiscordSettings)
  capturedCalls.length = 0;
  await ctx.handleSaveDiscordSettings();
  const saveCall = capturedCalls.find(c => c.url === '/api/notifications/center' && c.options.method === 'POST');
  if (!saveCall) throw new Error("handleSaveDiscordSettings did not POST to /api/notifications/center");
  const saveBody = JSON.parse(saveCall.options.body);
  if (!saveBody.channels || !saveBody.channels.pushover || !saveBody.channels.ntfy || !saveBody.channels.gotify) {{
    throw new Error("Channels structure missing in save payload: " + saveCall.options.body);
  }}

  // 4. Test Notification Target Selection Dispatch (handleSendTestNotification)
  // Scenario A: Default select option 'all'
  capturedCalls.length = 0;
  getEl('send-test-notification-btn').disabled = false;
  getEl('notif-test-channel-select').value = 'all';
  await ctx.handleSendTestNotification();
  let testCall = capturedCalls.find(c => c.url === '/api/notifications/test' && c.options.method === 'POST');
  if (!testCall) throw new Error("handleSendTestNotification did not POST to /api/notifications/test");
  let testBody = JSON.parse(testCall.options.body);
  if (testBody.channel !== 'all') throw new Error("Expected channel 'all', got: " + testBody.channel);

  // Scenario B: Selected channel option 'telegram'
  capturedCalls.length = 0;
  getEl('send-test-notification-btn').disabled = false;
  getEl('notif-test-channel-select').value = 'telegram';
  await ctx.handleSendTestNotification();
  testCall = capturedCalls.find(c => c.url === '/api/notifications/test');
  testBody = JSON.parse(testCall.options.body);
  if (testBody.channel !== 'telegram') throw new Error("Expected channel 'telegram', got: " + testBody.channel);

  // Scenario C: Explicit parameter override 'pushover'
  capturedCalls.length = 0;
  getEl('send-test-notification-btn').disabled = false;
  await ctx.handleSendTestNotification('pushover');
  testCall = capturedCalls.find(c => c.url === '/api/notifications/test');
  testBody = JSON.parse(testCall.options.body);
  if (testBody.channel !== 'pushover') throw new Error("Expected channel 'pushover', got: " + testBody.channel);

  // Scenario D: Explicit parameter override 'ntfy'
  capturedCalls.length = 0;
  getEl('send-test-notification-btn').disabled = false;
  await ctx.handleSendTestNotification('ntfy');
  testCall = capturedCalls.find(c => c.url === '/api/notifications/test');
  testBody = JSON.parse(testCall.options.body);
  if (testBody.channel !== 'ntfy') throw new Error("Expected channel 'ntfy', got: " + testBody.channel);

  // Scenario E: Explicit parameter override 'gotify'
  capturedCalls.length = 0;
  getEl('send-test-notification-btn').disabled = false;
  await ctx.handleSendTestNotification('gotify');
  testCall = capturedCalls.find(c => c.url === '/api/notifications/test');
  testBody = JSON.parse(testCall.options.body);
  if (testBody.channel !== 'gotify') throw new Error("Expected channel 'gotify', got: " + testBody.channel);

  console.log("ALL_NODE_CHANNEL_PAYLOAD_AND_TARGET_DISPATCH_VM_CHECKS_PASSED");
}})();
""";
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_CHANNEL_PAYLOAD_AND_TARGET_DISPATCH_VM_CHECKS_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
