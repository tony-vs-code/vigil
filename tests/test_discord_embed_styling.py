# tests/test_discord_embed_styling.py
"""Dedicated test suite validating visual progress chart Discord notifications
and modern embed styling invariants in Vigil.
"""

import json
import urllib.parse
from unittest.mock import patch, MagicMock

import pytest
import discord

import main


class TestTelemetryChartUrl:
    """Test QuickChart generation logic, threshold coloring, and capacity labels."""

    def test_quickchart_url_structure_and_dark_theme(self):
        url = main.generate_telemetry_chart_url(
            cpu_pct=25.0,
            ram_pct=50.0,
            ram_label="8/16 GB",
            pool_pct=65.0,
            pool_label="4/10 TB",
            root_pct=40.0,
            root_label="40/100 GB",
        )
        assert url.startswith("https://quickchart.io/chart?")
        parsed = main.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        # Check theme & dimensions
        assert params.get("bkg") == ["#1e1f22"]
        assert params.get("w") == ["540"]
        assert params.get("h") == ["160"]
        assert params.get("devicePixelRatio") == ["2.0"]

        # Check chart configuration payload
        assert "c" in params
        chart_cfg = json.loads(params["c"][0])
        assert chart_cfg["type"] == "horizontalBar"
        assert chart_cfg["options"]["legend"]["display"] is False
        assert chart_cfg["options"]["scales"]["xAxes"][0]["ticks"]["min"] == 0
        assert chart_cfg["options"]["scales"]["xAxes"][0]["ticks"]["max"] == 100
        assert chart_cfg["options"]["scales"]["xAxes"][0]["ticks"]["fontColor"] == "#949ba4"
        assert chart_cfg["options"]["scales"]["yAxes"][0]["ticks"]["fontColor"] == "#f2f3f5"

    @pytest.mark.parametrize(
        "val, expected_color",
        [
            (0.0, "#22c55e"),      # normal green
            (50.0, "#22c55e"),     # normal green
            (74.9, "#22c55e"),     # boundary just under 75% -> green
            (75.0, "#f59e0b"),     # boundary at 75% -> amber
            (85.0, "#f59e0b"),     # amber
            (89.9, "#f59e0b"),     # boundary just under 90% -> amber
            (90.0, "#ef4444"),     # boundary at 90% -> red
            (95.5, "#ef4444"),     # red
            (100.0, "#ef4444"),    # red
        ],
    )
    def test_threshold_color_selection(self, val, expected_color):
        assert main._get_metric_bar_color(val) == expected_color

    def test_dynamic_colors_across_multiple_datasets(self):
        # cpu=30% (green), ram=76% (amber), pool=92% (red), root=10% (green)
        url = main.generate_telemetry_chart_url(
            cpu_pct=30.0,
            ram_pct=76.0,
            ram_label="12/16 GB",
            pool_pct=92.0,
            pool_label="9.2/10 TB",
            root_pct=10.0,
            root_label="10/100 GB",
        )
        parsed = main.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        chart_cfg = json.loads(params["c"][0])
        colors = chart_cfg["data"]["datasets"][0]["backgroundColor"]
        assert colors == ["#22c55e", "#f59e0b", "#ef4444", "#22c55e"]

    def test_human_readable_capacity_labels_and_edge_values(self):
        url = main.generate_telemetry_chart_url(
            cpu_pct=-5.0,  # Clamped to 0.0
            ram_pct=80.123,
            ram_label="6.7/16 GB",
            pool_pct=95.0,
            pool_label="8.4/16 TB",
            root_pct=50.0,
            root_label="42/250 GB",
        )
        parsed = main.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        chart_cfg = json.loads(params["c"][0])
        labels = chart_cfg["data"]["labels"]
        assert labels[0] == "CPU Usage (0.0%)"
        assert labels[1] == "Memory (80.1% - 6.7/16 GB)"
        assert labels[2] == "Data Pool (95.0% - 8.4/16 TB)"
        assert labels[3] == "Root Disk (50.0% - 42/250 GB)"


class TestCapacityPairFormatting:
    """Validate human-readable capacity pair formatting helper."""

    def test_capacity_pair_formatting(self):
        assert main._format_capacity_pair(6.7 * 1024**3, 16 * 1024**3) == "6.7/16 GB"
        assert main._format_capacity_pair(8.4 * 1024**4, 16 * 1024**4) == "8.4/16 TB"
        assert main._format_capacity_pair(42 * 1024**3, 250 * 1024**3) == "42/250 GB"
        assert main._format_capacity_pair(500 * 1024**2, 1000 * 1024**2) == "500/1000 MB"
        assert main._format_capacity_pair(0, 0) == "0 B / 0 B"
        assert main._format_capacity_pair(-10, 0) == "0 B / 0 B"


class TestDictToDiscordEmbed:
    """Validate conversion of dictionary specifications into rich discord.Embed objects."""

    def test_thumbnail_and_image_assignment(self):
        # Case 1: Nested dict structure for thumbnail and image
        embed_dict = {
            "title": "Test Title",
            "description": "Test Description",
            "thumbnail": {"url": "https://example.com/thumb.png"},
            "image": {"url": "https://quickchart.io/chart?test=1"},
        }
        embed = main.dict_to_discord_embed(embed_dict)
        assert isinstance(embed, discord.Embed)
        assert embed.thumbnail.url == "https://example.com/thumb.png"
        assert embed.image.url == "https://quickchart.io/chart?test=1"

        # Case 2: Direct string URL for thumbnail and image
        embed_dict_str = {
            "title": "Direct URL Test",
            "thumbnail": "https://example.com/icon.ico",
            "image": "https://quickchart.io/chart?test=2",
        }
        embed_str = main.dict_to_discord_embed(embed_dict_str)
        assert embed_str.thumbnail.url == "https://example.com/icon.ico"
        assert embed_str.image.url == "https://quickchart.io/chart?test=2"

    def test_full_embed_attributes(self):
        embed_dict = {
            "title": "Comprehensive Embed",
            "description": "System Status Nominal",
            "color": 0x2ECC71,
            "author": {
                "name": "Vigil System Monitor — node-prod",
                "icon_url": "https://example.com/favicon.ico",
                "url": "https://example.com",
            },
            "thumbnail": {"url": "https://example.com/favicon.ico"},
            "image": {"url": "https://quickchart.io/chart?bkg=%231e1f22"},
            "footer": {
                "text": "Vigil v0.9.6 • Linux • node-prod",
                "icon_url": "https://example.com/footer.ico",
            },
            "timestamp": "2026-11-09T18:00:00Z",
            "fields": [
                {"name": "Field 1", "value": "Value 1", "inline": True},
                {"name": "Field 2", "value": "Value 2", "inline": False},
            ],
        }
        embed = main.dict_to_discord_embed(embed_dict)
        assert embed.title == "Comprehensive Embed"
        assert embed.description == "System Status Nominal"
        assert embed.color.value == 0x2ECC71
        assert embed.author.name == "Vigil System Monitor — node-prod"
        assert embed.author.icon_url == "https://example.com/favicon.ico"
        assert embed.author.url == "https://example.com"
        assert embed.thumbnail.url == "https://example.com/favicon.ico"
        assert embed.image.url == "https://quickchart.io/chart?bkg=%231e1f22"
        assert embed.footer.text == "Vigil v0.9.6 • Linux • node-prod"
        assert embed.footer.icon_url == "https://example.com/footer.ico"
        assert len(embed.fields) == 2
        assert embed.fields[0].name == "Field 1"
        assert embed.fields[0].value == "Value 1"
        assert embed.fields[0].inline is True


class TestPeriodicReportData:
    """Validate build_periodic_report_data contract and clean formatting."""

    @patch("main.check_raid_status", return_value=("clean", "md0 : active raid1 sda1[0] sdb1[1]\n2 devices"))
    @patch("main.psutil.cpu_percent", return_value=28.5)
    @patch("main.psutil.cpu_count", return_value=8)
    @patch("main.get_disk_usage_statvfs")
    @patch("main.docker_manager.get_containers")
    @patch("main.get_probes_summary")
    def test_clean_status_banner_and_clean_formatting(
        self,
        mock_probes,
        mock_containers,
        mock_statvfs,
        mock_cpu_count,
        mock_cpu_pct,
        mock_raid,
    ):
        mock_statvfs.return_value = {
            "percent": 45.0,
            "used": 45 * 1024**3,
            "total": 100 * 1024**3,
            "storage_info": "45GB/100GB (45%)",
        }
        mock_containers.return_value = [
            {"name": "web", "state": "running", "update_available": False},
            {"name": "db", "state": "running", "update_available": True},
        ]
        mock_probes.return_value = [
            {"id": "p1", "avg_response_time_ms": 12.4},
            {"id": "p2", "avg_response_time_ms": 18.6},
        ]

        title, desc, embed_data = main.build_periodic_report_data({
            "system_status": True,
            "disk_health": True,
            "raid_status": True,
            "services": True,
        })

        # Contract compatibility checks
        assert "Periodic Report" in title
        assert "All Systems Operational" in desc
        assert embed_data["color"] == 0x2ECC71

        # Check metadata elements
        assert "author" in embed_data
        assert "Vigil System Monitor" in embed_data["author"]["name"]
        assert "thumbnail" in embed_data
        assert embed_data["thumbnail"]["url"].endswith("favicon.ico")
        assert "image" in embed_data
        assert "quickchart.io" in embed_data["image"]["url"]
        assert "footer" in embed_data
        assert "Vigil v" in embed_data["footer"]["text"]

        # Validate that no raw codeblocks, block characters, or emojis leak into fields or description
        for field in embed_data["fields"]:
            assert "`" not in field["value"], f"Found raw backtick in field {field['name']}: {field['value']}"
            assert "```" not in field["value"]
            assert "🟢" not in field["value"]
            assert "🔴" not in field["value"]
            assert "▰" not in field["value"]
            assert "▱" not in field["value"]

        assert "`" not in desc
        assert "🟢" not in desc
        assert "🔴" not in desc

    @patch("main.check_raid_status", return_value=("degraded", "md0 : active raid1 sda1[0]\n1 devices, 1 degraded"))
    def test_degraded_status_banner_and_color(self, mock_raid):
        title, desc, embed_data = main.build_periodic_report_data({
            "system_status": False,
            "disk_health": False,
            "raid_status": True,
            "services": False,
        })
        assert "Periodic Report" in title
        assert "Attention: RAID Array DEGRADED" in desc
        assert embed_data["color"] == 0xEF4444


class TestRaidEmbed:
    """Validate build_raid_embed formatting, thumbnail, image, and title invariants."""

    @patch("main.psutil.cpu_percent", return_value=15.0)
    @patch("main.get_disk_usage_statvfs")
    def test_build_raid_embed_clean(self, mock_statvfs, mock_cpu):
        mock_statvfs.return_value = {
            "percent": 30.0,
            "used": 30 * 1024**3,
            "total": 100 * 1024**3,
            "storage_info": "30GB/100GB (30%)",
        }
        details = (
            "/dev/md0:\n"
            "        Version : 1.2\n"
            "        Raid Level : raid1\n"
            "        Array Size : 10000 (9.31 GiB)\n"
            "        State : clean\n"
            "        Active Devices : 2\n"
            "        Working Devices : 2\n"
            "        Failed Devices : 0\n"
            "        Spare Devices : 0\n"
        )
        embed = main.build_raid_embed("clean", details, "100GB 30GB 70GB 30%")

        # Title contract: RAID <level> | <STATUS>
        assert embed.title == "RAID 1 | CLEAN"
        assert embed.color.value == 0x2ECC71

        # Description without raw backticks or emojis
        assert "`" not in embed.description
        assert "```" not in embed.description
        assert "🟢" not in embed.description
        assert "Status: CLEAN" in embed.description

        # Thumbnail and image checks
        assert embed.thumbnail.url is not None
        assert "favicon.ico" in embed.thumbnail.url
        assert embed.image.url is not None
        assert "quickchart.io" in embed.image.url

        # Check fields without backticks or emojis
        assert len(embed.fields) >= 2
        field_names = [f.name for f in embed.fields]
        assert "Array Health" in field_names
        assert "Device Breakdown" in field_names

        for f in embed.fields:
            assert "`" not in f.value, f"Raw backtick found in field {f.name}"
            assert "🟢" not in f.value
            assert "🔴" not in f.value

    @patch("main.psutil.cpu_percent", return_value=20.0)
    @patch("main.get_disk_usage_statvfs")
    def test_build_raid_embed_degraded(self, mock_statvfs, mock_cpu):
        mock_statvfs.return_value = {
            "percent": 60.0,
            "used": 60 * 1024**3,
            "total": 100 * 1024**3,
            "storage_info": "60GB/100GB (60%)",
        }
        details = (
            "/dev/md0:\n"
            "        Version : 1.2\n"
            "        Raid Level : raid5\n"
            "        Array Size : 2048000 (1.95 GiB)\n"
            "        State : degraded\n"
            "        Active Devices : 2\n"
            "        Working Devices : 2\n"
            "        Failed Devices : 1\n"
            "        Spare Devices : 0\n"
        )
        embed = main.build_raid_embed("degraded", details, "100GB 60GB 40GB 60%")
        assert embed.title == "RAID 5 | DEGRADED"
        assert embed.color.value == 0xF39C12
        assert "Status: DEGRADED" in embed.description
        assert "`" not in embed.description
        assert "Array Health" in [f.name for f in embed.fields]
