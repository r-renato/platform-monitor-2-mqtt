from __future__ import annotations

import fcntl
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import dns.message
import dns.rcode
import dns.rrset

from mods.dns_mon import DnsMon, ProbeResult, _IcmpPing, _Track


# Topologia di prova che ricalca la catena reale (indirizzi di documentazione).
PROBES = {
    "gateway": "icmp,192.0.2.1,gateway",
    "wan_a": "icmp,198.51.100.1,wan",
    "wan_b": "icmp,198.51.100.2,wan",
    "dns_public": "dns,198.51.100.1:53,wan",
    "dot_upstream": "tcp,198.51.100.1:853,dot_upstream",
    "unbound": "dns,192.0.2.1:5353,unbound",
    "adguard": "dns,192.0.2.1:53,adguard",
}
ALL = list(PROBES)


def build_config(probes: dict | None = None, **settings: str) -> ConfigParser:
    config = ConfigParser(
        delimiters=("=",), inline_comment_prefixes=("#",), interpolation=None
    )
    config.optionxform = str
    values = {"state_file": "", "speedtest_lock_file": ""}
    values.update(settings)
    config["DnsMonitor"] = values
    config["DnsMonitor probes"] = PROBES if probes is None else probes
    return config


class FakeClock:
    """Orologio simulato: ``time()`` e ``monotonic()`` avanzano insieme."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.origin = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now - self.origin + 100.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeIcmp:
    mode = "raw"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def ping(self, host: str, timeout: float) -> ProbeResult:
        self.calls.append(host)
        if self.ok:
            return ProbeResult(True, latency_ms=1.0)
        return ProbeResult(False, error="timeout")


class DnsMonTestCase(unittest.TestCase):
    # I test non devono dipendere dai privilegi della macchina che li esegue:
    # la disponibilità ICMP è sempre simulata (di default "raw").
    ICMP_MODE: str | None = "raw"

    def make(self, probes=None, **settings) -> tuple[DnsMon, FakeClock]:
        clock = FakeClock()
        with patch.object(_IcmpPing, "_detect", return_value=self.ICMP_MODE):
            module = DnsMon(build_config(probes, **settings))
        module._time = clock.time
        module._monotonic = clock.monotonic
        self.addCleanup(module.close)
        return module, clock

    @staticmethod
    def probe(module: DnsMon, name: str):
        return next(p for p in module._probes if p.spec.name == name)

    @staticmethod
    def iso(timestamp: float) -> str:
        from datetime import datetime

        return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")

    def feed(self, module, clock, name, ok, *, heavy=False, latency=1.0, error="timeout"):
        probe = self.probe(module, name)
        job = probe.cache_miss if heavy else probe.main
        result = (
            ProbeResult(True, latency_ms=latency)
            if ok
            else ProbeResult(False, error=error)
        )
        module._on_result(job, result, clock.time())

    def round(self, module, clock, failing, step=5.0, names=None):
        """Un giro di sonde: quelle in ``failing`` falliscono, le altre riescono."""
        for name in names or ALL:
            self.feed(module, clock, name, ok=name not in failing)
        clock.advance(step)

    def rounds(self, module, clock, failing, count, step=5.0):
        for _ in range(count):
            self.round(module, clock, failing, step)


class ConfigTests(DnsMonTestCase):
    def test_missing_icmp_is_logged_once_and_probes_fall_back_to_tcp(self):
        probes = {"gw": "icmp,192.0.2.1,gateway", "dns": "dns,192.0.2.1,adguard"}
        with patch.object(_IcmpPing, "_detect", return_value=None):
            with self.assertLogs("platformMonitor", level="WARNING") as logs:
                module = DnsMon(build_config(probes))
        self.addCleanup(module.close)
        self.assertIsNone(module._icmp.mode)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("ICMP non disponibile", logs.records[0].getMessage())

    def test_no_icmp_warning_without_icmp_probes(self):
        probes = {"dns": "dns,192.0.2.1,adguard"}
        with patch.object(_IcmpPing, "_detect", return_value=None):
            with self.assertNoLogs("platformMonitor", level="WARNING"):
                module = DnsMon(build_config(probes))
        self.addCleanup(module.close)

    def test_parses_valid_probes_and_rejects_invalid_ones(self):
        probes = {
            "ok_icmp": "icmp,192.0.2.1,gateway",
            "ok_tcp": "tcp,198.51.100.1:853,dot_upstream",
            "ok_dns": "dns,192.0.2.1,adguard",
            "bad name": "icmp,192.0.2.1,gateway",
            "bad_kind": "http,192.0.2.1,gateway",
            "bad_layer": "icmp,192.0.2.1,cloud",
            "hostname": "icmp,example.com,wan",
            "icmp_port": "icmp,192.0.2.1:53,wan",
            "tcp_no_port": "tcp,192.0.2.1,wan",
            "bad_port": "tcp,192.0.2.1:70000,wan",
            "too_short": "icmp,192.0.2.1",
            "icmp_v6": "icmp,2001:db8::1,wan",
        }
        with self.assertLogs("platformMonitor", level="WARNING") as logs:
            module, _ = self.make(probes)
        self.assertEqual(
            [p.spec.name for p in module._probes], ["ok_icmp", "ok_tcp", "ok_dns"]
        )
        self.assertEqual(self.probe(module, "ok_dns").spec.port, 53)
        self.assertEqual(len(logs.records), 9)

    def test_ipv6_targets(self):
        probes = {
            "t": "tcp,[2001:db8::1]:853,dot_upstream",
            "d1": "dns,[2001:db8::1]:5353,unbound",
            "d2": "dns,2001:db8::2,adguard",
        }
        module, _ = self.make(probes)
        self.assertEqual(self.probe(module, "t").spec.host, "2001:db8::1")
        self.assertEqual(self.probe(module, "t").spec.port, 853)
        self.assertEqual(self.probe(module, "d1").spec.port, 5353)
        self.assertEqual(self.probe(module, "d2").spec.port, 53)
        data = module.getData()
        self.assertEqual(data["probes"]["t"]["target"], "[2001:db8::1]:853")

    def test_inline_comments_and_case_are_handled_like_the_daemon_does(self):
        config = ConfigParser(
            delimiters=("=",), inline_comment_prefixes=("#",), interpolation=None
        )
        config.optionxform = str
        config.read_string(
            "[DnsMonitor]\nstate_file =\nspeedtest_lock_file =\n"
            "[DnsMonitor probes]\nGateway = ICMP, 192.0.2.1 , Gateway  # nota\n"
        )
        module = DnsMon(config)
        self.addCleanup(module.close)
        self.assertEqual(self.probe(module, "Gateway").spec.layer, "gateway")
        self.assertEqual(self.probe(module, "Gateway").spec.host, "192.0.2.1")

    def test_no_valid_probe_disables_module(self):
        with self.assertLogs("platformMonitor", level="WARNING"):
            module, _ = self.make({"x": "bogus"})
        self.assertFalse(module.is_available)
        self.assertEqual(module.getData(), {})
        module.collect()  # non deve avviare nulla né sollevare
        self.assertTrue(module.wait_for_idle(0.1))
        module.close()

    def test_defaults(self):
        module, _ = self.make()
        self.assertEqual(module._network_interval, 10)
        self.assertEqual(module._dns_interval, 30)
        self.assertEqual(module._heavy_interval, 300)
        self.assertEqual(module._failures_before_down, 3)
        self.assertEqual(module._blackout_threshold, 60)
        self.assertEqual(module._window_minutes, 15)
        self.assertTrue(module._adaptive_enabled)
        # Cache-miss solo sulle sonde dns del livello unbound.
        names = [j.probe.spec.name for j in module._jobs if j.heavy]
        self.assertEqual(names, ["unbound"])

    def test_out_of_range_values_are_clamped_with_a_warning(self):
        with self.assertLogs("platformMonitor", level="WARNING") as logs:
            module, _ = self.make(network_interval_seconds="0", retries="99")
        self.assertEqual(module._network_interval, 2)
        self.assertEqual(module._retries, 5)
        self.assertTrue(any("network_interval_seconds" in r.getMessage() for r in logs.records))

    def test_invalid_numbers_fall_back_to_default(self):
        with self.assertLogs("platformMonitor", level="WARNING"):
            module, _ = self.make(window_minutes="molti")
        self.assertEqual(module._window_minutes, 15)

    def test_cache_miss_layers_is_configurable_and_can_be_disabled(self):
        module, _ = self.make(cache_miss_layers="unbound, adguard")
        self.assertEqual(sum(1 for j in module._jobs if j.heavy), 2)
        module, _ = self.make(cache_miss_zone="")
        self.assertEqual(sum(1 for j in module._jobs if j.heavy), 0)


class LayerAttributionTests(DnsMonTestCase):
    def test_status_is_unknown_before_any_result(self):
        module, _ = self.make()
        data = module.getData()
        self.assertEqual(data["status"], "unknown")
        self.assertIsNone(data["last_probe"])
        self.assertIsNone(data["probes"]["gateway"]["ok"])
        self.assertEqual(data["probes"]["gateway"]["state"], "unknown")

    def test_all_probes_up(self):
        module, clock = self.make()
        self.rounds(module, clock, failing=set(), count=2)
        data = module.getData()
        self.assertEqual(data["status"], "ok")
        self.assertIsNone(data["failed_layer"])
        self.assertEqual(data["degraded_probes"], [])
        self.assertEqual(data["probes"]["gateway"]["success_pct"], 100.0)

    def test_single_failure_is_not_enough(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"gateway"}, count=2)
        data = module.getData()
        self.assertEqual(data["probes"]["gateway"]["consecutive_failures"], 2)
        self.assertEqual(data["probes"]["gateway"]["state"], "up")
        self.assertEqual(data["status"], "ok")

    def test_gateway_is_the_lowest_failed_layer(self):
        module, clock = self.make()
        self.rounds(module, clock, failing=set(ALL), count=3)
        data = module.getData()
        self.assertEqual(data["status"], "down")
        self.assertEqual(data["failed_layer"], "gateway")
        self.assertEqual(data["probes"]["gateway"]["error"], "timeout")

    def test_wan_failure_when_gateway_is_fine(self):
        module, clock = self.make()
        wan_and_above = set(ALL) - {"gateway"}
        self.rounds(module, clock, failing=wan_and_above, count=3)
        data = module.getData()
        self.assertEqual(data["failed_layer"], "wan")
        self.assertEqual(data["status"], "down")

    def test_one_failing_public_peer_only_degrades(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"wan_a"}, count=3)
        data = module.getData()
        self.assertEqual(data["status"], "degraded")
        self.assertIsNone(data["failed_layer"])
        self.assertEqual(data["degraded_probes"], ["wan_a"])

    def test_dot_channel_failure(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"dot_upstream", "unbound", "adguard"}, count=3)
        self.assertEqual(module.getData()["failed_layer"], "dot_upstream")

    def test_unbound_failure_with_adguard_still_up(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"unbound"}, count=3)
        data = module.getData()
        self.assertEqual(data["failed_layer"], "unbound")
        self.assertEqual(data["status"], "down")

    def test_adguard_only_failure(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"adguard"}, count=3)
        self.assertEqual(module.getData()["failed_layer"], "adguard")

    def test_recovery_clears_the_failed_layer(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"adguard"}, count=3)
        self.rounds(module, clock, failing=set(), count=1)
        data = module.getData()
        self.assertEqual(data["status"], "ok")
        self.assertIsNone(data["failed_layer"])
        self.assertEqual(data["probes"]["adguard"]["consecutive_failures"], 0)

    def test_cache_miss_failure_marks_the_probe_degraded(self):
        module, clock = self.make()
        self.rounds(module, clock, failing=set(), count=1)
        self.feed(module, clock, "unbound", True, heavy=True, latency=31.0)
        probe = module.getData()["probes"]["unbound"]
        self.assertTrue(probe["cache_miss_ok"])
        self.assertEqual(probe["cache_miss_latency_ms"], 31.0)
        self.assertNotIn("cache_miss_error", probe)

        self.feed(module, clock, "unbound", False, heavy=True, error="servfail")
        self.assertEqual(module.getData()["status"], "ok")  # una sola volta non basta
        self.feed(module, clock, "unbound", False, heavy=True, error="servfail")
        data = module.getData()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["degraded_probes"], ["unbound"])
        self.assertFalse(data["probes"]["unbound"]["cache_miss_ok"])
        self.assertIsNone(data["probes"]["unbound"]["cache_miss_latency_ms"])
        self.assertEqual(data["probes"]["unbound"]["cache_miss_error"], "servfail")

    def test_output_is_json_serialisable_with_expected_keys(self):
        module, clock = self.make()
        self.rounds(module, clock, failing=set(), count=1)
        data = json.loads(json.dumps(module.getData()))
        self.assertIn("rtt_ms_p50", data["probes"]["gateway"])
        self.assertIn("latency_ms_p95", data["probes"]["adguard"])
        self.assertNotIn("rtt_ms_p50", data["probes"]["adguard"])
        self.assertEqual(data["probes"]["gateway"]["type"], "icmp")
        self.assertEqual(data["probes"]["dot_upstream"]["target"], "198.51.100.1:853")
        self.assertEqual(data["window_minutes"], 15)

    def test_icmp_probe_is_reported_as_tcp_fallback_without_icmp(self):
        module, clock = self.make()
        module._icmp.mode = None
        data = module.getData()
        self.assertEqual(data["probes"]["gateway"]["type"], "tcp_fallback")
        self.assertEqual(data["probes"]["gateway"]["target"], "192.0.2.1:53")
        with patch.object(DnsMon, "_probe_tcp", return_value=ProbeResult(True, 1.0)) as tcp:
            result = module._probe_once(self.probe(module, "gateway").main)
        self.assertTrue(result.ok)
        self.assertEqual(tcp.call_args.args[:2], ("192.0.2.1", 53))
        self.assertTrue(tcp.call_args.kwargs["refused_ok"])


class WindowStatisticsTests(DnsMonTestCase):
    def test_percentiles_and_success_rate(self):
        track = _Track()
        for index in range(1, 21):
            track.record(ProbeResult(True, latency_ms=float(index)), 0, 0, float(index), 1000)
        track.record(ProbeResult(False, error="timeout"), 0, 0, 21.0, 1000)
        self.assertEqual(track.percentile(50), 10.0)
        self.assertEqual(track.percentile(95), 19.0)
        self.assertEqual(track.success_pct(), round(100 * 20 / 21, 1))

    def test_samples_outside_the_window_are_dropped(self):
        module, clock = self.make(window_minutes="1")
        self.feed(module, clock, "gateway", ok=False)
        clock.advance(30)
        self.feed(module, clock, "gateway", ok=True)
        self.assertEqual(module.getData()["probes"]["gateway"]["success_pct"], 50.0)
        clock.advance(45)  # il primo campione (fallito) ha ora 75 s
        self.assertEqual(module.getData()["probes"]["gateway"]["success_pct"], 100.0)


class BlackoutTests(DnsMonTestCase):
    def test_short_outage_is_counted_but_is_not_a_blackout(self):
        module, clock = self.make()
        self.rounds(module, clock, failing={"gateway"}, count=3)
        self.assertEqual(module.getData()["failed_layer"], "gateway")
        self.rounds(module, clock, failing=set(), count=5)
        blackouts = module.getData()["blackouts"]
        self.assertFalse(blackouts["in_progress"])
        self.assertEqual(blackouts["count_24h"], 0)
        self.assertEqual(blackouts["short_outages_24h"], 1)
        self.assertIsNone(blackouts["last"])

    def test_blackout_is_confirmed_after_the_threshold(self):
        module, clock = self.make()
        start = clock.time()
        self.rounds(module, clock, failing=set(ALL), count=8)  # 40 s
        blackouts = module.getData()["blackouts"]
        self.assertFalse(blackouts["in_progress"])
        self.rounds(module, clock, failing=set(ALL), count=6)  # 70 s
        data = module.getData()
        self.assertEqual(data["status"], "down")
        self.assertTrue(data["blackouts"]["in_progress"])
        self.assertEqual(data["blackouts"]["count_24h"], 1)
        self.assertEqual(data["blackouts"]["current_layer"], "gateway")
        self.assertGreaterEqual(data["blackouts"]["current_seconds"], 60)
        self.assertEqual(
            data["blackouts"]["current_start"],
            self.iso(start),
        )

    def test_blackout_is_closed_with_start_end_and_layer(self):
        module, clock = self.make()
        start = clock.time()
        self.rounds(module, clock, failing=set(ALL), count=20)  # 100 s di guasto
        end = clock.time()
        self.rounds(module, clock, failing=set(), count=5)  # ripresa + debounce
        data = module.getData()
        blackouts = data["blackouts"]
        self.assertEqual(data["status"], "ok")
        self.assertFalse(blackouts["in_progress"])
        self.assertEqual(blackouts["count_24h"], 1)
        self.assertEqual(blackouts["total_seconds_24h"], 100)
        last = blackouts["last"]
        self.assertEqual(last["start"], self.iso(start))
        self.assertEqual(last["end"], self.iso(end))
        self.assertEqual(last["duration_seconds"], 100)
        self.assertEqual(last["failed_layer"], "gateway")
        self.assertFalse(last["during_speedtest"])

    def test_blackout_start_uses_the_last_probe_of_the_layer_to_fail(self):
        # Una sonda con un guasto proprio e di vecchia data non deve anticipare l'inizio.
        module, clock = self.make()
        self.feed(module, clock, "wan_a", ok=False)  # filtra l'ICMP da sempre
        clock.advance(3600)
        onset = clock.time()
        self.rounds(module, clock, failing=set(ALL) - {"gateway"}, count=20)
        self.rounds(module, clock, failing=set(ALL) - {"gateway"}, count=0)
        data = module.getData()
        self.assertEqual(data["blackouts"]["current_start"], self.iso(onset))

    def test_relapse_during_debounce_keeps_a_single_event(self):
        module, clock = self.make()
        self.rounds(module, clock, failing=set(ALL), count=20)
        self.rounds(module, clock, failing=set(), count=1)  # un respiro...
        self.rounds(module, clock, failing=set(ALL), count=4)  # ...poi di nuovo giù
        self.assertTrue(module.getData()["blackouts"]["in_progress"])
        self.assertEqual(len(module._events), 0)
        self.rounds(module, clock, failing=set(), count=5)
        blackouts = module.getData()["blackouts"]
        self.assertEqual(blackouts["count_24h"], 1)
        self.assertFalse(blackouts["in_progress"])

    def test_lower_layer_failure_replaces_the_reported_layer(self):
        module, clock = self.make()
        failing_wan = set(ALL) - {"gateway"}
        self.rounds(module, clock, failing=failing_wan, count=15)
        self.assertEqual(module.getData()["blackouts"]["current_layer"], "wan")
        self.rounds(module, clock, failing=set(ALL), count=4)
        self.assertEqual(module.getData()["blackouts"]["current_layer"], "gateway")

    def test_threshold_is_configurable(self):
        module, clock = self.make(blackout_threshold_seconds="30")
        self.rounds(module, clock, failing=set(ALL), count=10)
        self.assertTrue(module.getData()["blackouts"]["in_progress"])

    def test_events_are_tagged_when_a_speedtest_was_running(self):
        module, clock = self.make(speedtest_lock_file="/nonexistent/speedtest.lock")
        with patch.object(DnsMon, "_flock_held", return_value=True):
            self.rounds(module, clock, failing=set(ALL), count=3)
            self.assertTrue(module.getData()["speedtest_running"])
        self.rounds(module, clock, failing=set(ALL), count=17)
        self.rounds(module, clock, failing=set(), count=5)
        data = module.getData()
        self.assertFalse(data["speedtest_running"])
        self.assertTrue(data["blackouts"]["last"]["during_speedtest"])

    def test_speedtest_ended_shortly_before_the_outage_is_still_tagged(self):
        module, clock = self.make(speedtest_lock_file="/nonexistent/speedtest.lock")
        with patch.object(DnsMon, "_flock_held", return_value=True):
            self.rounds(module, clock, failing=set(), count=1)
            module._speedtest_running(clock.monotonic())  # lo fa _tick() a regime
        clock.advance(30)  # lo speedtest è finito 30 s prima del guasto
        self.rounds(module, clock, failing=set(ALL), count=20)
        self.rounds(module, clock, failing=set(), count=5)
        self.assertTrue(module.getData()["blackouts"]["last"]["during_speedtest"])

    def test_no_speedtest_no_tag(self):
        module, clock = self.make(speedtest_lock_file="/nonexistent/speedtest.lock")
        self.rounds(module, clock, failing=set(ALL), count=20)
        self.rounds(module, clock, failing=set(), count=5)
        self.assertFalse(module.getData()["blackouts"]["last"]["during_speedtest"])

    def test_24h_window_clips_and_excludes_old_events(self):
        module, clock = self.make()
        now = clock.time()
        day = 86400
        module._events = [
            {"start": now - 3 * day, "end": now - 3 * day + 100, "layer": "wan", "during_speedtest": False},
            {"start": now - day - 50, "end": now - day + 150, "layer": "wan", "during_speedtest": False},
            {"start": now - 1000, "end": now - 900, "layer": "gateway", "during_speedtest": True},
        ]
        blackouts = module.getData()["blackouts"]
        self.assertEqual(blackouts["count_24h"], 2)
        self.assertEqual(blackouts["total_seconds_24h"], 150 + 100)
        self.assertEqual(blackouts["last"]["failed_layer"], "gateway")


class PersistenceTests(DnsMonTestCase):
    def test_closed_blackouts_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "dns_monitor.json")
            first, clock = self.make(state_file=state)
            self.rounds(first, clock, failing=set(ALL), count=20)
            self.rounds(first, clock, failing=set(), count=5)
            self.assertTrue(Path(state).exists())

            second, _ = self.make(state_file=state)
            self.assertEqual(second._events, first._events)
            self.assertEqual(second.getData()["blackouts"]["last"]["duration_seconds"], 100)

    def test_in_progress_outage_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "dns_monitor.json"
            module, clock = self.make(state_file=str(state))
            self.rounds(module, clock, failing=set(ALL), count=20)
            self.assertTrue(module.getData()["blackouts"]["in_progress"])
            self.assertFalse(state.exists())

    def test_corrupt_or_partial_state_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "dns_monitor.json"
            state.write_text("{non json", encoding="utf-8")
            with self.assertLogs("platformMonitor", level="WARNING"):
                module, _ = self.make(state_file=str(state))
            self.assertEqual(module._events, [])

            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "events": [
                            {"start": 10, "end": 20, "layer": "wan", "during_speedtest": True},
                            {"start": 30, "end": 20},
                            {"start": "x", "end": 5},
                            {"start": True, "end": 5},
                        ],
                        "short_outages": [{"start": 1, "duration": 2}, {"start": 1}],
                    }
                ),
                encoding="utf-8",
            )
            module, _ = self.make(state_file=str(state))
            self.assertEqual(len(module._events), 1)
            self.assertTrue(module._events[0]["during_speedtest"])
            self.assertEqual(len(module._short_outages), 1)

    def test_unwritable_state_does_not_break_the_module(self):
        module, clock = self.make(state_file="/proc/forbidden/dns.json")
        with self.assertLogs("platformMonitor", level="WARNING"):
            self.rounds(module, clock, failing=set(ALL), count=20)
            self.rounds(module, clock, failing=set(), count=5)
        self.assertEqual(module.getData()["blackouts"]["count_24h"], 1)


class AdaptiveSamplingTests(DnsMonTestCase):
    def test_failure_speeds_up_sampling_and_recovery_slows_it_down(self):
        module, clock = self.make()
        job = self.probe(module, "adguard").main
        self.assertEqual(module._interval_for(job), 30)
        self.assertEqual(module._interval_for(self.probe(module, "gateway").main), 10)
        self.round(module, clock, failing={"gateway"}, step=1)
        self.assertTrue(module._adaptive_active)
        self.assertEqual(module._interval_for(job), 3)
        self.assertEqual(module._interval_for(self.probe(module, "gateway").main), 3)
        for _ in range(5):
            self.round(module, clock, failing=set(), step=1)
        self.assertFalse(module._adaptive_active)
        self.assertEqual(module._interval_for(job), 30)

    def test_failure_pulls_forward_the_next_runs(self):
        module, clock = self.make()
        for job in module._jobs:
            job.next_due = clock.monotonic() + 30
        self.feed(module, clock, "gateway", ok=False)
        for probe in module._probes:
            self.assertLessEqual(probe.main.next_due, clock.monotonic() + 3)
        heavy = self.probe(module, "unbound").cache_miss
        self.assertGreater(heavy.next_due, clock.monotonic() + 3)  # il cache-miss non accelera

    def test_adaptive_can_be_disabled(self):
        module, clock = self.make(adaptive_enabled="false")
        self.round(module, clock, failing={"gateway"})
        self.assertFalse(module._adaptive_active)
        self.assertEqual(module._interval_for(self.probe(module, "gateway").main), 10)

    def test_permanent_failure_does_not_keep_fast_sampling_forever(self):
        module, clock = self.make()
        for _ in range(25):  # 12,5 minuti con wan_a sempre in errore
            self.round(module, clock, failing={"wan_a"}, step=30)
        self.assertFalse(module._adaptive_active)
        self.assertIn("wan_a", module._adaptive_ignored)
        # Un nuovo guasto riattiva comunque il campionamento veloce.
        self.round(module, clock, failing={"wan_a", "unbound"}, step=1)
        self.assertTrue(module._adaptive_active)
        # Quando wan_a si riprende torna rilevante per il guasto successivo.
        self.round(module, clock, failing=set(), step=1)
        self.assertNotIn("wan_a", module._adaptive_ignored)


class SpeedtestDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "speedtest.lock"

    def test_real_flock_is_detected_without_taking_the_lock(self):
        handle = self.lock.open("a+")
        self.addCleanup(handle.close)
        self.assertFalse(DnsMon._flock_held(self.lock))  # file presente ma non bloccato
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(DnsMon._flock_held(self.lock))
        # Il rilevamento è passivo: il lock resta nostro e un secondo tentativo fallisce.
        other = self.lock.open("a+")
        self.addCleanup(other.close)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertFalse(DnsMon._flock_held(self.lock))

    def test_missing_file_or_locks_table(self):
        self.assertFalse(DnsMon._flock_held(self.lock))
        self.lock.write_text("")
        self.assertFalse(DnsMon._flock_held(self.lock, locks_file="/nonexistent/locks"))

    def test_parsing_of_proc_locks_lines(self):
        self.lock.write_text("")
        inode = os.stat(self.lock).st_ino
        table = Path(self.tmp.name) / "locks"

        def held(*lines: str) -> bool:
            table.write_text("\n".join(lines) + "\n")
            return DnsMon._flock_held(self.lock, locks_file=str(table))

        self.assertTrue(held(f"1: FLOCK  ADVISORY  WRITE 97 fe:00:{inode} 0 EOF"))
        self.assertFalse(held(f"1: FLOCK  ADVISORY  READ 97 fe:00:{inode} 0 EOF"))
        self.assertFalse(held(f"1: -> FLOCK  ADVISORY  WRITE 97 fe:00:{inode} 0 EOF"))
        self.assertFalse(held(f"1: POSIX  ADVISORY  WRITE 97 fe:00:{inode} 0 EOF"))
        self.assertFalse(held(f"1: FLOCK  ADVISORY  WRITE 97 fe:00:{inode + 1} 0 EOF"))
        self.assertFalse(held("garbage", ""))
        self.assertTrue(
            held(
                f"1: FLOCK  ADVISORY  WRITE 11 fe:00:{inode + 7} 0 EOF",
                f"2: FLOCK  ADVISORY  WRITE 12 fe:00:{inode} 0 EOF",
            )
        )

    def test_module_reports_speedtest_running_and_caches_the_check(self):
        clock = FakeClock()
        module = DnsMon(build_config(speedtest_lock_file=str(self.lock)))
        module._time, module._monotonic = clock.time, clock.monotonic
        self.addCleanup(module.close)
        with patch.object(DnsMon, "_flock_held", side_effect=[True, False]) as held:
            self.assertTrue(module._speedtest_running(clock.monotonic()))
            clock.advance(2)
            self.assertTrue(module._speedtest_running(clock.monotonic()))  # da cache
            self.assertEqual(held.call_count, 1)
            clock.advance(10)
            self.assertFalse(module._speedtest_running(clock.monotonic()))
            self.assertEqual(held.call_count, 2)

    def test_empty_lock_path_disables_detection(self):
        module = DnsMon(build_config(speedtest_lock_file=""))
        self.addCleanup(module.close)
        with patch.object(DnsMon, "_flock_held") as held:
            self.assertFalse(module._speedtest_running(0.0))
        held.assert_not_called()


class FakeDnsServer:
    """Server DNS UDP su loopback con comportamento selezionabile."""

    def __init__(self, behaviour: str = "noerror") -> None:
        self.behaviour = behaviour
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.queries: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                data, address = self.sock.recvfrom(4096)
            except OSError:
                return
            query = dns.message.from_wire(data)
            name = query.question[0].name.to_text()
            self.queries.append(name)
            if self.behaviour == "silent":
                continue
            response = dns.message.make_response(query)
            if self.behaviour == "servfail":
                response.set_rcode(dns.rcode.SERVFAIL)
            elif self.behaviour == "nxdomain":
                response.set_rcode(dns.rcode.NXDOMAIN)
            else:
                response.answer.append(dns.rrset.from_text(name, 60, "IN", "A", "192.0.2.9"))
            self.sock.sendto(response.to_wire(), address)

    def close(self) -> None:
        self.sock.close()


class ProbeTransportTests(DnsMonTestCase):
    def server(self, behaviour: str) -> FakeDnsServer:
        server = FakeDnsServer(behaviour)
        self.addCleanup(server.close)
        return server

    def test_dns_probe_noerror_and_nxdomain_are_both_valid_answers(self):
        module, _ = self.make()
        for behaviour, rcode in (("noerror", "NOERROR"), ("nxdomain", "NXDOMAIN")):
            server = self.server(behaviour)
            result = module._probe_dns("127.0.0.1", server.port, "example.com", 1.0)
            self.assertTrue(result.ok, behaviour)
            self.assertEqual(result.rcode, rcode)
            self.assertIsNotNone(result.latency_ms)

    def test_dns_probe_servfail_is_a_failure_with_the_rcode_as_error(self):
        module, _ = self.make()
        server = self.server("servfail")
        result = module._probe_dns("127.0.0.1", server.port, "example.com", 1.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "servfail")
        self.assertEqual(result.rcode, "SERVFAIL")

    def test_dns_probe_timeout(self):
        module, _ = self.make()
        server = self.server("silent")
        started = time.monotonic()
        result = module._probe_dns("127.0.0.1", server.port, "example.com", 0.3)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")
        self.assertLess(time.monotonic() - started, 2)

    def test_dns_probe_distinguishes_a_stopped_service_from_lost_packets(self):
        module, _ = self.make()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Nessuno ascolta: l'ICMP "porta irraggiungibile" diventa "econnrefused"...
        stopped = module._probe_dns("127.0.0.1", port, "example.com", 1.0)
        self.assertFalse(stopped.ok)
        self.assertEqual(stopped.error, "econnrefused")
        # ...mentre un server che riceve e non risponde è un timeout.
        silent = self.server("silent")
        lost = module._probe_dns("127.0.0.1", silent.port, "example.com", 0.3)
        self.assertEqual(lost.error, "timeout")

    def test_tcp_probe(self):
        module, _ = self.make()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        ok = module._probe_tcp("127.0.0.1", port, 1.0)
        self.assertTrue(ok.ok)
        self.assertIsNotNone(ok.latency_ms)
        listener.close()
        refused = module._probe_tcp("127.0.0.1", port, 1.0)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.error, "refused")
        reachable = module._probe_tcp("127.0.0.1", port, 1.0, refused_ok=True)
        self.assertTrue(reachable.ok)

    def test_dns_names_rotate_and_cache_miss_names_are_unique(self):
        module, _ = self.make(test_domains="a.example, b.example")
        self.assertEqual(
            [module._next_domain() for _ in range(5)],
            ["a.example", "b.example", "a.example", "b.example", "a.example"],
        )
        names = {module._cache_miss_name() for _ in range(50)}
        self.assertEqual(len(names), 50)
        self.assertTrue(all(n.endswith(".example.com") for n in names))
        self.assertTrue(all(len(n.split(".")[0]) <= 63 for n in names))

    def test_cache_miss_query_uses_a_random_name_in_the_configured_zone(self):
        module, _ = self.make(cache_miss_zone="zone.test")
        job = self.probe(module, "unbound").cache_miss
        with patch.object(DnsMon, "_probe_dns", return_value=ProbeResult(True, 1.0)) as probe_dns:
            module._probe_once(job)
            module._probe_once(self.probe(module, "unbound").main)
        cache_miss_name, regular_name = (c.args[2] for c in probe_dns.call_args_list)
        self.assertTrue(cache_miss_name.startswith("pm"))
        self.assertTrue(cache_miss_name.endswith(".zone.test"))
        self.assertEqual(regular_name, "example.com")  # le sonde regolari usano i domini stabili


class IcmpTests(unittest.TestCase):
    def test_echo_request_has_a_valid_checksum(self):
        packet = _IcmpPing.build_echo(0x1234, 7)
        self.assertEqual(packet[0], 8)
        self.assertEqual(struct.unpack("!HH", packet[4:8]), (0x1234, 7))
        self.assertEqual(_IcmpPing._checksum(packet), 0)

    def test_reply_matching(self):
        ident, seq = 0x1234, 9
        reply = struct.pack("!BBHHH", 0, 0, 0, ident, seq) + b"payload!"
        ip_header = bytes([0x45]) + bytes(19)
        raw = ip_header + reply
        match = _IcmpPing.is_reply
        self.assertTrue(match("raw", raw, "192.0.2.1", "192.0.2.1", ident, seq))
        self.assertFalse(match("raw", raw, "192.0.2.2", "192.0.2.1", ident, seq))
        self.assertFalse(match("raw", raw, "192.0.2.1", "192.0.2.1", ident + 1, seq))
        self.assertFalse(match("raw", raw, "192.0.2.1", "192.0.2.1", ident, seq + 1))
        request = ip_header + struct.pack("!BBHHH", 8, 0, 0, ident, seq)
        self.assertFalse(match("raw", request, "192.0.2.1", "192.0.2.1", ident, seq))
        self.assertFalse(match("raw", b"short", "192.0.2.1", "192.0.2.1", ident, seq))
        # Il socket "ping" del kernel riscrive l'identificativo: conta solo la sequenza.
        self.assertTrue(match("dgram", reply, "192.0.2.1", "192.0.2.1", 0xBEEF, seq))
        self.assertFalse(match("dgram", reply, "192.0.2.1", "192.0.2.1", 0xBEEF, seq + 1))

    def test_live_ping_on_loopback(self):
        pinger = _IcmpPing()
        if pinger.mode is None:
            self.skipTest("socket ICMP non disponibili in questo ambiente")
        result = pinger.ping("127.0.0.1", 1.0)
        self.assertTrue(result.ok, result)
        self.assertLess(result.latency_ms, 500)

    def test_without_icmp_the_result_is_explicit(self):
        pinger = _IcmpPing()
        pinger.mode = None
        self.assertEqual(pinger.ping("127.0.0.1", 0.1).error, "icmp_unavailable")


class SchedulerTests(DnsMonTestCase):
    """Prove con il vero scheduler e orologi reali; le sonde sono simulate."""

    FAST = {
        "network_interval_seconds": "2",
        "dns_interval_seconds": "5",
        "adaptive_interval_seconds": "1",
    }

    def make_real(self, ok: bool = True, **settings):
        module = DnsMon(build_config(None, **{**self.FAST, **settings}))
        module._icmp = FakeIcmp(ok)
        self.addCleanup(module.close)

        def tcp(_self, host, port, timeout, refused_ok=False):
            return ProbeResult(ok, 1.0 if ok else None, None if ok else "timeout")

        def dns_probe(_self, host, port, qname, timeout):
            return ProbeResult(
                ok, 2.0 if ok else None, None if ok else "timeout", "NOERROR" if ok else None
            )

        for name, fake in (("_probe_tcp", tcp), ("_probe_dns", dns_probe)):
            patcher = patch.object(DnsMon, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return module

    def test_runs_all_probes_and_stops_cleanly(self):
        module = self.make_real(ok=True)
        module.collect()
        self.assertTrue(module.wait_for_idle(10))
        data = module.getData()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(all(p["ok"] for p in data["probes"].values()))
        self.assertIsNotNone(data["probes"]["unbound"]["cache_miss_latency_ms"])
        thread = module._thread
        module.close()
        self.assertFalse(thread.is_alive())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            t.name.startswith("DnsProbe") for t in threading.enumerate()
        ):
            time.sleep(0.05)
        self.assertFalse([t.name for t in threading.enumerate() if t.name.startswith("Dns")])

    def test_failures_are_confirmed_before_a_one_shot_run_returns(self):
        module = self.make_real(ok=False)
        module.collect()
        self.assertTrue(module.wait_for_idle(15))
        data = module.getData()
        self.assertEqual(data["status"], "down")
        self.assertEqual(data["failed_layer"], "gateway")
        self.assertTrue(all(p["consecutive_failures"] >= 3 for k, p in data["probes"].items()))

    def test_collect_is_non_blocking_and_idempotent(self):
        release = threading.Event()
        module = self.make_real(ok=True)

        def slow_dns(_self, host, port, qname, timeout):
            release.wait(2)
            return ProbeResult(True, 1.0, rcode="NOERROR")

        with patch.object(DnsMon, "_probe_dns", slow_dns):
            started = time.monotonic()
            module.collect()
            module.collect()
            self.assertLess(time.monotonic() - started, 0.3)
            thread = module._thread
            module.collect()
            self.assertIs(module._thread, thread)
            self.assertEqual(module.getData()["status"] in {"unknown", "ok"}, True)
            release.set()
            self.assertTrue(module.wait_for_idle(10))

    def test_a_blocked_probe_is_never_scheduled_twice(self):
        release = threading.Event()
        calls: list[str] = []
        module = self.make_real(ok=True)

        def blocking_tcp(_self, host, port, timeout, refused_ok=False):
            calls.append(host)
            release.wait(3)
            return ProbeResult(True, 1.0)

        with patch.object(DnsMon, "_probe_tcp", blocking_tcp):
            module.collect()
            time.sleep(1.5)  # molte volte l'intervallo di adattamento (1 s)
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertTrue(module.wait_for_idle(10))

    def test_probes_use_the_configured_retries(self):
        module, _ = self.make(retries="2")
        attempts = []

        def failing(_self, host, port, timeout, refused_ok=False):
            attempts.append(1)
            return ProbeResult(False, error="timeout")

        with patch.object(DnsMon, "_probe_tcp", failing):
            result = module._execute(self.probe(module, "dot_upstream").main)
        self.assertFalse(result.ok)
        self.assertEqual(len(attempts), 3)

        calls = []

        def flaky(_self, host, port, timeout, refused_ok=False):
            calls.append(1)
            return ProbeResult(len(calls) == 2, 1.0, None if len(calls) == 2 else "timeout")

        with patch.object(DnsMon, "_probe_tcp", flaky):
            result = module._execute(self.probe(module, "dot_upstream").main)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 2)

    def test_unexpected_probe_error_is_recorded_as_a_failure(self):
        module, _ = self.make()
        job = self.probe(module, "dot_upstream").main
        with patch.object(DnsMon, "_probe_tcp", side_effect=RuntimeError("boom")):
            with self.assertLogs("platformMonitor", level="ERROR"):
                module._run_job(job)
        self.assertFalse(job.track.last_ok)
        self.assertEqual(job.track.last_error, "internal_error")


if __name__ == "__main__":
    unittest.main()
