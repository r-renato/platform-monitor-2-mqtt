from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

from mods.speedtest_mon import SpeedtestMon


SAMPLE_RESULT = {
    "type": "result",
    "timestamp": "2026-08-02T08:00:00Z",
    "ping": {"jitter": 1.2, "latency": 12.34, "low": 10.0, "high": 20.0},
    "download": {
        "bandwidth": 100_000_000,
        "bytes": 500_000_000,
        "elapsed": 5000,
        "latency": {"iqm": 15.0, "low": 10.0, "high": 25.0, "jitter": 2.0},
    },
    "upload": {
        "bandwidth": 12_500_000,
        "bytes": 62_500_000,
        "elapsed": 5000,
        "latency": {"iqm": 18.0, "low": 12.0, "high": 30.0, "jitter": 3.0},
    },
    "packetLoss": 0.5,
    "isp": "Example ISP",
    "interface": {
        "internalIp": "192.0.2.10",
        "name": "eth0",
        "macAddr": "00:11:22:33:44:55",
        "isVpn": False,
        "externalIp": "198.51.100.10",
    },
    "server": {
        "id": 1234,
        "host": "speed.example.net",
        "port": 8080,
        "name": "Example",
        "location": "Rome",
        "country": "Italy",
        "ip": "203.0.113.10",
    },
    "result": {"id": "result-id", "url": "https://example/result", "persisted": True},
}


def build_config(cache_file: str = "", **overrides: str) -> ConfigParser:
    config = ConfigParser(interpolation=None)
    config["General"] = {}
    values = {
        "interval_in_minutes": "60",
        "timeout_seconds": "90",
        "retry_interval_minutes": "15",
        "max_retry_interval_minutes": "240",
        "run_on_start": "true",
        "startup_delay_seconds": "0",
        "jitter_seconds": "0",
        "server_id": "1234",
        "interface": "eth0",
        "ip": "192.0.2.10",
        "host": "speed.example.net",
        "accept_license": "true",
        "accept_gdpr": "true",
        "cache_file": cache_file,
        "lock_file": "",
    }
    values.update(overrides)
    config["Speedtest"] = values
    return config


class FakePopen:
    instances: list["FakePopen"] = []

    def __init__(self, command, stdout=None, stderr=None, text=None, *, result=None):
        self.command = command
        self.returncode = 0
        self.result = result or (json.dumps(SAMPLE_RESULT), "")
        self.terminated = False
        self.killed = False
        self.__class__.instances.append(self)

    def communicate(self, timeout=None):
        return self.result

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class BlockingPopen(FakePopen):
    started = threading.Event()
    release = threading.Event()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.returncode = None

    def communicate(self, timeout=None):
        self.__class__.started.set()
        self.__class__.release.wait(timeout=2)
        if self.returncode is None:
            self.returncode = 0
        return self.result


class SpeedtestMonTests(unittest.TestCase):
    def setUp(self):
        FakePopen.instances.clear()
        BlockingPopen.started.clear()
        BlockingPopen.release.clear()

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    def test_normalises_bandwidth_to_mbps(self, _version, _which):
        module = SpeedtestMon(build_config())
        parsed = module._normalise_result(SAMPLE_RESULT)
        self.assertEqual(parsed["download"]["mbps"], 800.0)
        self.assertEqual(parsed["upload"]["mbps"], 100.0)
        self.assertEqual(parsed["download"]["bandwidth_bytes_per_second"], 100_000_000.0)
        self.assertEqual(parsed["ping"]["latency_ms"], 12.34)
        self.assertEqual(parsed["packet_loss_percent"], 0.5)

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    def test_builds_safe_argument_list(self, _version, _which):
        module = SpeedtestMon(build_config())
        command = module._build_command()
        self.assertEqual(command[0], "/usr/bin/speedtest")
        self.assertIn("--format=json", command)
        self.assertIn("--accept-license", command)
        self.assertIn("--accept-gdpr", command)
        self.assertEqual(command[command.index("--server-id") + 1], "1234")
        self.assertEqual(command[command.index("--interface") + 1], "eth0")

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    @patch("mods.speedtest_mon.subprocess.Popen", side_effect=FakePopen)
    def test_collect_is_asynchronous_and_completes(self, _popen, _version, _which):
        module = SpeedtestMon(build_config())
        module._next_due_epoch = 0
        module.collect()
        self.assertIn(module.getData()["status"], {"running", "ok"})
        self.assertTrue(module.wait_for_idle(1))
        data = module.getData()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["stale"])
        self.assertEqual(data["download"]["mbps"], 800.0)
        self.assertGreater(data["seconds_until_next_run"], 0)

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    @patch("mods.speedtest_mon.subprocess.Popen", side_effect=BlockingPopen)
    def test_does_not_start_concurrent_workers(self, popen, _version, _which):
        module = SpeedtestMon(build_config())
        module._next_due_epoch = 0
        started_at = time.monotonic()
        module.collect()
        self.assertLess(time.monotonic() - started_at, 0.2)
        self.assertTrue(BlockingPopen.started.wait(1))
        module.collect()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(module.getData()["status"], "running")
        BlockingPopen.release.set()
        self.assertTrue(module.wait_for_idle(1))

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    @patch("mods.speedtest_mon.subprocess.Popen")
    def test_failure_preserves_last_success_and_applies_backoff(self, popen, _version, _which):
        failed = FakePopen([], result=("", "network unavailable"))
        failed.returncode = 2
        popen.return_value = failed
        module = SpeedtestMon(build_config())
        module._data = {
            "status": "ok",
            "last_success_at": "2026-08-02T08:00:00+00:00",
            "download": {"mbps": 800.0},
            "consecutive_failures": 0,
        }
        module._next_due_epoch = 0
        module.collect()
        self.assertTrue(module.wait_for_idle(1))
        data = module.getData()
        self.assertEqual(data["status"], "error")
        self.assertTrue(data["stale"])
        self.assertEqual(data["download"]["mbps"], 800.0)
        self.assertEqual(data["error"]["type"], "command_failed")
        self.assertEqual(data["consecutive_failures"], 1)
        self.assertEqual(data["retry_delay_minutes"], 15)

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    def test_minimum_interval_and_safe_start_default(self, _version, _which):
        config = build_config(interval_in_minutes="1", run_on_start="false")
        module = SpeedtestMon(config)
        self.assertEqual(module._interval_minutes, 15)
        self.assertEqual(module.getData()["status"], "waiting")
        self.assertGreater(module.getData()["seconds_until_next_run"], 0)

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    def test_cache_roundtrip_preserves_next_run(self, _version, _which):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "speedtest.json")
            first = SpeedtestMon(build_config(cache, run_on_start="false"))
            with first._lock:
                first._data = {
                    "status": "ok",
                    "last_attempt_at": "2026-08-02T08:00:00+00:00",
                    "last_success_at": "2026-08-02T08:00:00+00:00",
                    "download": {"mbps": 800.0},
                }
                first._next_due_epoch = time.time() + 1800
                first._save_cache_locked()

            second = SpeedtestMon(build_config(cache, run_on_start="false"))
            self.assertEqual(second.getData()["download"]["mbps"], 800.0)
            self.assertTrue(second.getData()["loaded_from_cache"])
            self.assertGreater(second.getData()["seconds_until_next_run"], 1700)


    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    def test_running_cache_is_marked_interrupted_after_restart(self, _version, _which):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "speedtest.json"
            cache.write_text(json.dumps({
                "status": "running",
                "last_attempt_at": "2026-08-02T08:00:00+00:00",
                "next_run_at": "2099-08-02T09:00:00+00:00",
                "consecutive_failures": 0,
            }))
            module = SpeedtestMon(build_config(str(cache)))
            data = module.getData()
            self.assertEqual(data["status"], "error")
            self.assertEqual(data["error"]["type"], "interrupted")
            self.assertEqual(data["consecutive_failures"], 1)

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    @patch("mods.speedtest_mon.SpeedtestMon._acquire_process_lock", return_value=None)
    def test_process_lock_prevents_second_execution(self, _lock, _version, _which):
        module = SpeedtestMon(build_config(lock_file="/tmp/speedtest-test.lock"))
        module._next_due_epoch = 0
        module.collect()
        self.assertTrue(module.wait_for_idle(1))
        self.assertEqual(module.getData()["error"]["type"], "already_running")

    @patch("mods.speedtest_mon.SpeedtestMon._which", return_value="/usr/bin/speedtest")
    @patch("mods.speedtest_mon.SpeedtestMon._read_client_version", return_value="1.2.0.84")
    @patch("mods.speedtest_mon.subprocess.Popen", side_effect=BlockingPopen)
    def test_close_terminates_running_process(self, _popen, _version, _which):
        module = SpeedtestMon(build_config())
        module._next_due_epoch = 0
        module.collect()
        self.assertTrue(BlockingPopen.started.wait(1))
        process = FakePopen.instances[-1]
        module.close()
        BlockingPopen.release.set()
        self.assertTrue(process.terminated or process.killed)


if __name__ == "__main__":
    unittest.main()
