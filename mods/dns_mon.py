"""Collector asincrono della salute di rete e DNS (catena AdGuard + Unbound).

Il modulo non esegue misure dentro ``collect()``: avvia un thread di
pianificazione che lancia le sonde alle frequenze configurate e aggiorna uno
stato aggregato. ``collect()`` si limita a garantire che il thread sia attivo e
``getData()`` restituisce l'ultima fotografia: il ciclo del daemon (1 minuto)
resta quindi indipendente dalla frequenza di campionamento.

Sonde (tutte su indirizzi IP, mai su nomi, per non dipendere dal DNS misurato):

  icmp   echo request IPv4 (socket "ping" non privilegiato, altrimenti raw;
         se nessuno dei due è disponibile ripiega su una connessione TCP/53)
  tcp    connessione TCP a ip:porta (es. 1.1.1.1:853, il canale DoT di Unbound)
  dns    query UDP A verso ip:porta con dnspython, senza resolver di sistema

Ogni sonda appartiene a un livello della catena, dal più basso al più alto:

  gateway < wan < dot_upstream < unbound < adguard

``failed_layer`` è il livello più basso in cui TUTTE le sonde con dati sono
"giù" (>= failures_before_down fallimenti consecutivi). Se cade una sola sonda
di un livello con più sonde (un solo peer pubblico irraggiungibile) il livello
non è guasto: lo stato è "degraded" e la sonda compare in ``degraded_probes``.

Un blackout è un'interruzione continua (livello guasto) lunga almeno
``blackout_threshold_seconds``. Quelle più brevi sono contate a parte
(``short_outages_24h``). L'inizio è l'istante in cui l'ultima sonda del livello
ha iniziato a fallire; la fine richiede una breve stabilità (debounce) per non
spezzare un'unica interruzione in più eventi. Solo i blackout conclusi sono
persistiti su disco; lo stato pubblicato è sempre derivato dal registro locale,
quindi un'interruzione di MQTT non fa perdere eventi (vengono pubblicati al
ciclo successivo che trova il broker raggiungibile).
"""

from __future__ import annotations

import copy
import errno
import ipaddress
import itertools
import json
import logging
import math
import os
import random
import re
import socket
import struct
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Any

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype

from mods.base_module import BaseModule

# Dal livello più basso al più alto della catena client -> AdGuard -> Unbound -> WAN.
LAYERS = ("gateway", "wan", "dot_upstream", "unbound", "adguard")
_KINDS = ("icmp", "tcp", "dns")

_DEFAULT_NETWORK_INTERVAL_SECONDS = 10
_DEFAULT_DNS_INTERVAL_SECONDS = 30
_DEFAULT_HEAVY_INTERVAL_MINUTES = 5
_DEFAULT_ADAPTIVE_INTERVAL_SECONDS = 3
_DEFAULT_ADAPTIVE_RECOVERY_CYCLES = 5
_DEFAULT_ADAPTIVE_MAX_MINUTES = 10
_DEFAULT_ICMP_TIMEOUT_SECONDS = 1.0
_DEFAULT_TCP_TIMEOUT_SECONDS = 2.0
_DEFAULT_DNS_TIMEOUT_SECONDS = 2.0
_DEFAULT_RETRIES = 1
_DEFAULT_FAILURES_BEFORE_DOWN = 3
_DEFAULT_BLACKOUT_THRESHOLD_SECONDS = 60
_DEFAULT_WINDOW_MINUTES = 15
_DEFAULT_MAX_PARALLEL_PROBES = 4
_DEFAULT_TEST_DOMAINS = ("example.com", "wikipedia.org", "cloudflare.com")
_DEFAULT_CACHE_MISS_ZONE = "example.com"
_DEFAULT_CACHE_MISS_LAYERS = ("unbound",)

_ICMP_FALLBACK_TCP_PORT = 53
_HEAVY_FAILURES_DEGRADED = 2
_SPEEDTEST_CHECK_SECONDS = 5.0
_SPEEDTEST_LOOKBACK_SECONDS = 120.0
_MAX_WAIT_SECONDS = 5.0
_MIN_WAIT_SECONDS = 0.05
_START_STAGGER_SECONDS = 0.1

_STATE_VERSION = 1
_MAX_EVENTS = 100
_MAX_SHORT_OUTAGES = 500
_DAY_SECONDS = 86400
_PROC_LOCKS = "/proc/locks"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_LOCK_ID_RE = re.compile(r"^[0-9a-fA-F]+:[0-9a-fA-F]+:(\d+)$")

_LOG = logging.getLogger("platformMonitor")


# ---------------------------------------------------------------------------
# Lettura configurazione
# ---------------------------------------------------------------------------

def _section(config: Any, name: str) -> Any:
    try:
        if config.has_section(name):
            return config[name]
    except (AttributeError, TypeError):
        pass
    return {}


def _get_str(section: Any, key: str, default: str = "") -> str:
    try:
        if hasattr(section, "getint"):  # SectionProxy
            value = section.get(key, fallback=default)
        else:
            value = section.get(key, default)
    except (TypeError, ValueError):
        return default
    return str(value).strip() if value is not None else default


def _bounded(key: str, value: float, minimum: float, maximum: float) -> float:
    if value < minimum or value > maximum:
        clamped = min(maximum, max(minimum, value))
        _LOG.warning(
            "dns_mon: %s=%s fuori dall'intervallo %s-%s; applicato %s",
            key, value, minimum, maximum, clamped,
        )
        return clamped
    return value


def _get_int(section: Any, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = _get_str(section, key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning("dns_mon: %s=%r non è un intero; uso %s", key, raw, default)
        return default
    return int(_bounded(key, value, minimum, maximum))


def _get_float(
    section: Any, key: str, default: float, minimum: float, maximum: float
) -> float:
    raw = _get_str(section, key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("dns_mon: %s=%r non è un numero; uso %s", key, raw, default)
        return default
    return float(_bounded(key, value, minimum, maximum))


def _get_bool(section: Any, key: str, default: bool) -> bool:
    raw = _get_str(section, key).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    _LOG.warning("dns_mon: %s=%r non è un booleano; uso %s", key, raw, default)
    return default


def _get_list(section: Any, key: str, default: tuple[str, ...]) -> list[str]:
    raw = _get_str(section, key)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Strutture dati
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeSpec:
    """Sonda dichiarata in ``[DnsMonitor probes]``."""

    name: str
    kind: str
    host: str
    port: int | None
    layer: str


@dataclass(frozen=True)
class ProbeResult:
    """Esito di una sonda, dopo gli eventuali tentativi ripetuti."""

    ok: bool
    latency_ms: float | None = None
    error: str | None = None
    rcode: str | None = None


class _Track:
    """Serie storica e contatori di una singola misura."""

    __slots__ = (
        "samples", "consecutive_failures", "consecutive_successes",
        "streak_started", "last_ok", "last_latency_ms", "last_error",
        "last_rcode", "last_at",
    )

    def __init__(self) -> None:
        self.samples: deque[tuple[float, bool, float | None]] = deque()
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.streak_started: float | None = None
        self.last_ok: bool | None = None
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None
        self.last_rcode: str | None = None
        self.last_at: float | None = None

    def record(
        self,
        result: ProbeResult,
        started_wall: float,
        now_wall: float,
        now_mono: float,
        window_seconds: float,
    ) -> None:
        self.last_at = now_wall
        self.last_ok = result.ok
        self.last_latency_ms = result.latency_ms if result.ok else None
        self.last_error = None if result.ok else (result.error or "failed")
        self.last_rcode = result.rcode
        if result.ok:
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            self.streak_started = None
        else:
            if self.consecutive_failures == 0:
                self.streak_started = started_wall
            self.consecutive_failures += 1
            self.consecutive_successes = 0
        self.samples.append((now_mono, result.ok, result.latency_ms))
        self.prune(now_mono, window_seconds)

    def prune(self, now_mono: float, window_seconds: float) -> None:
        limit = now_mono - window_seconds
        while self.samples and self.samples[0][0] < limit:
            self.samples.popleft()

    def success_pct(self) -> float | None:
        if not self.samples:
            return None
        ok = sum(1 for _, success, _ in self.samples if success)
        return round(100.0 * ok / len(self.samples), 1)

    def percentile(self, pct: float) -> float | None:
        values = sorted(
            lat for _, success, lat in self.samples if success and lat is not None
        )
        if not values:
            return None
        rank = max(1, math.ceil(pct / 100.0 * len(values)))
        return round(values[rank - 1], 2)


class _Job:
    """Una misura pianificata: la sonda regolare o il suo cache-miss."""

    __slots__ = ("probe", "heavy", "track", "next_due", "inflight")

    def __init__(self, probe: "_Probe", heavy: bool) -> None:
        self.probe = probe
        self.heavy = heavy
        self.track = _Track()
        self.next_due = 0.0
        self.inflight = False


class _Probe:
    __slots__ = ("spec", "main", "cache_miss")

    def __init__(self, spec: ProbeSpec, with_cache_miss: bool) -> None:
        self.spec = spec
        self.main = _Job(self, heavy=False)
        self.cache_miss = _Job(self, heavy=True) if with_cache_miss else None


def _error_label(exc: BaseException) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError, dns.exception.Timeout)):
        return "timeout"
    if isinstance(exc, OSError) and exc.errno:
        return errno.errorcode.get(exc.errno, "oserror").lower()
    return type(exc).__name__.lower()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# ICMP senza processi esterni
# ---------------------------------------------------------------------------

class _IcmpPing:
    """Echo request ICMP IPv4 con socket "ping" non privilegiato oppure raw."""

    _PAYLOAD = b"pm2m-dns-mon-" + bytes(range(19))

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self._random = random.SystemRandom()
        self.mode: str | None = self._detect()

    @staticmethod
    def _open(mode: str) -> socket.socket:
        kind = socket.SOCK_DGRAM if mode == "dgram" else socket.SOCK_RAW
        return socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP)

    @classmethod
    def _detect(cls) -> str | None:
        # "dgram" non richiede privilegi (net.ipv4.ping_group_range) e il
        # kernel filtra le risposte per socket; "raw" richiede CAP_NET_RAW.
        for mode in ("dgram", "raw"):
            try:
                cls._open(mode).close()
                return mode
            except OSError:
                continue
        return None

    @staticmethod
    def _checksum(data: bytes) -> int:
        if len(data) % 2:
            data += b"\x00"
        total = sum(struct.unpack(f"!{len(data) // 2}H", data))
        total = (total >> 16) + (total & 0xFFFF)
        total += total >> 16
        return ~total & 0xFFFF

    @classmethod
    def build_echo(cls, ident: int, seq: int) -> bytes:
        header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
        checksum = cls._checksum(header + cls._PAYLOAD)
        return struct.pack("!BBHHH", 8, 0, checksum, ident, seq) + cls._PAYLOAD

    @staticmethod
    def is_reply(
        mode: str, data: bytes, source: str, host: str, ident: int, seq: int
    ) -> bool:
        if mode == "raw":
            # Il socket raw riceve ogni ICMP in arrivo, header IP incluso:
            # servono sorgente, identificativo e sequenza per escludere il resto.
            if len(data) < 20 or source != host:
                return False
            data = data[(data[0] & 0x0F) * 4:]
        if len(data) < 8:
            return False
        icmp_type, _code, _checksum, reply_id, reply_seq = struct.unpack(
            "!BBHHH", data[:8]
        )
        if icmp_type != 0 or reply_seq != seq:
            return False
        return mode == "dgram" or reply_id == ident

    def ping(self, host: str, timeout: float) -> ProbeResult:
        mode = self.mode
        if mode is None:
            return ProbeResult(False, error="icmp_unavailable")
        ident = self._random.randrange(1, 0x10000)
        seq = next(self._seq) & 0xFFFF
        packet = self.build_echo(ident, seq)
        try:
            sock = self._open(mode)
        except OSError as exc:
            return ProbeResult(False, error=_error_label(exc))
        with sock:
            try:
                started = time.monotonic()
                sock.sendto(packet, (host, 0))
                deadline = started + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ProbeResult(False, error="timeout")
                    sock.settimeout(remaining)
                    data, address = sock.recvfrom(2048)
                    if self.is_reply(mode, data, address[0], host, ident, seq):
                        latency = (time.monotonic() - started) * 1000.0
                        return ProbeResult(True, latency_ms=latency)
            except OSError as exc:  # include socket.timeout
                return ProbeResult(False, error=_error_label(exc))


# ---------------------------------------------------------------------------
# Modulo
# ---------------------------------------------------------------------------

class DnsMon(BaseModule):
    """Monitora gateway, WAN, canale DoT, Unbound e AdGuard con sonde dedicate."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._lock = RLock()
        self._cond = Condition(self._lock)
        self._shutdown = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._random = random.SystemRandom()
        # Orologi iniettabili: i test li sostituiscono con orologi simulati.
        self._time = time.time
        self._monotonic = time.monotonic

        self._probes: list[_Probe] = []
        self._jobs: list[_Job] = []
        self._by_layer: dict[str, list[_Probe]] = {}

        section = _section(config, "DnsMonitor")
        self._read_settings(section)

        specs = self._parse_probes(_section(config, "DnsMonitor probes"))
        if not specs:
            self._available = False
            self._logger.warning(
                "dns_mon: nessuna sonda valida in [DnsMonitor probes] — modulo disabilitato"
            )
            return

        cache_miss_layers = set(self._cache_miss_layers)
        for spec in specs:
            with_cache_miss = (
                spec.kind == "dns"
                and spec.layer in cache_miss_layers
                and bool(self._cache_miss_zone)
            )
            probe = _Probe(spec, with_cache_miss)
            self._probes.append(probe)
            self._jobs.append(probe.main)
            if probe.cache_miss is not None:
                self._jobs.append(probe.cache_miss)
            self._by_layer.setdefault(spec.layer, []).append(probe)

        self._icmp = _IcmpPing()
        if any(p.spec.kind == "icmp" for p in self._probes):
            if self._icmp.mode is None:
                self._logger.warning(
                    "dns_mon: ICMP non disponibile (serve CAP_NET_RAW oppure "
                    "net.ipv4.ping_group_range che includa il gruppo del servizio): "
                    "le sonde icmp ripiegano su connessioni TCP/%d",
                    _ICMP_FALLBACK_TCP_PORT,
                )

        self._domain_index = 0
        self._last_probe_at: float | None = None
        self._failed_layer: str | None = None
        self._degraded: list[str] = []
        self._status = "unknown"

        self._adaptive_active = False
        self._adaptive_since = 0.0
        self._adaptive_ignored: set[str] = set()

        self._outage: dict[str, Any] | None = None
        self._recovery_since: float | None = None
        self._events: list[dict[str, Any]] = []
        self._short_outages: list[dict[str, Any]] = []
        self._load_state()

        self._speedtest_checked = -math.inf
        self._speedtest_cached = False
        self._speedtest_last_seen = -math.inf

        self._logger.info(
            "dns_mon: %d sonde, rete ogni %ds, dns ogni %ds, cache-miss ogni %d min, "
            "adattivo=%s, ICMP=%s",
            len(self._probes),
            self._network_interval,
            self._dns_interval,
            self._heavy_interval // 60,
            self._adaptive_enabled,
            self._icmp.mode or f"tcp/{_ICMP_FALLBACK_TCP_PORT}",
        )

    # ------------------------------------------------------------------
    # Configurazione
    # ------------------------------------------------------------------

    def _read_settings(self, section: Any) -> None:
        self._network_interval = _get_int(
            section, "network_interval_seconds", _DEFAULT_NETWORK_INTERVAL_SECONDS, 2, 3600
        )
        self._dns_interval = _get_int(
            section, "dns_interval_seconds", _DEFAULT_DNS_INTERVAL_SECONDS, 5, 3600
        )
        self._heavy_interval = 60 * _get_int(
            section, "heavy_interval_minutes", _DEFAULT_HEAVY_INTERVAL_MINUTES, 1, 1440
        )
        self._adaptive_enabled = _get_bool(section, "adaptive_enabled", True)
        self._adaptive_interval = _get_int(
            section, "adaptive_interval_seconds", _DEFAULT_ADAPTIVE_INTERVAL_SECONDS, 1, 60
        )
        self._adaptive_recovery_cycles = _get_int(
            section, "adaptive_recovery_cycles", _DEFAULT_ADAPTIVE_RECOVERY_CYCLES, 1, 100
        )
        self._adaptive_max_seconds = 60 * _get_int(
            section, "adaptive_max_minutes", _DEFAULT_ADAPTIVE_MAX_MINUTES, 1, 1440
        )
        self._icmp_timeout = _get_float(
            section, "icmp_timeout_seconds", _DEFAULT_ICMP_TIMEOUT_SECONDS, 0.2, 30
        )
        self._tcp_timeout = _get_float(
            section, "tcp_timeout_seconds", _DEFAULT_TCP_TIMEOUT_SECONDS, 0.2, 30
        )
        self._dns_timeout = _get_float(
            section, "dns_timeout_seconds", _DEFAULT_DNS_TIMEOUT_SECONDS, 0.2, 30
        )
        self._retries = _get_int(section, "retries", _DEFAULT_RETRIES, 0, 5)
        self._failures_before_down = _get_int(
            section, "failures_before_down", _DEFAULT_FAILURES_BEFORE_DOWN, 1, 100
        )
        self._blackout_threshold = _get_int(
            section, "blackout_threshold_seconds", _DEFAULT_BLACKOUT_THRESHOLD_SECONDS, 5, 86400
        )
        self._window_minutes = _get_int(
            section, "window_minutes", _DEFAULT_WINDOW_MINUTES, 1, 180
        )
        self._window_seconds = float(self._window_minutes * 60)
        self._max_parallel = _get_int(
            section, "max_parallel_probes", _DEFAULT_MAX_PARALLEL_PROBES, 1, 16
        )

        self._test_domains = self._valid_domains(
            _get_list(section, "test_domains", _DEFAULT_TEST_DOMAINS)
        ) or list(_DEFAULT_TEST_DOMAINS)
        zone = _get_str(section, "cache_miss_zone", _DEFAULT_CACHE_MISS_ZONE).lower()
        self._cache_miss_zone = zone if self._valid_domains([zone]) else ""
        if zone and not self._cache_miss_zone:
            self._logger.warning("dns_mon: cache_miss_zone=%r non valida: cache-miss disattivato", zone)
        layers = [
            layer.lower()
            for layer in _get_list(section, "cache_miss_layers", _DEFAULT_CACHE_MISS_LAYERS)
        ]
        unknown = [layer for layer in layers if layer not in LAYERS]
        if unknown:
            self._logger.warning("dns_mon: cache_miss_layers: livelli sconosciuti ignorati: %s", unknown)
        self._cache_miss_layers = [layer for layer in layers if layer in LAYERS]

        # Devono coincidere con quelli di speedtest_mon, che usa lo stesso default.
        store = Path(os.environ.get("PLATFORM_MONITOR_STORE_DIR", "./store"))
        lock_value = _get_str(section, "speedtest_lock_file", str(store / "speedtest.lock"))
        state_value = _get_str(section, "state_file", str(store / "dns_monitor.json"))
        self._speedtest_lock_file: Path | None = Path(lock_value) if lock_value else None
        self._state_file: Path | None = Path(state_value) if state_value else None

    def _valid_domains(self, domains: list[str]) -> list[str]:
        valid = []
        for domain in domains:
            try:
                dns.name.from_text(domain)
            except dns.exception.DNSException:
                self._logger.warning("dns_mon: dominio non valido ignorato: %r", domain)
                continue
            if domain:
                valid.append(domain)
        return valid

    def _parse_probes(self, section: Any) -> list[ProbeSpec]:
        specs: list[ProbeSpec] = []
        seen: set[str] = set()
        try:
            items = list(section.items())
        except AttributeError:
            items = []
        for name, raw in items:
            try:
                spec = self._parse_probe(name, str(raw))
            except ValueError as exc:
                self._logger.warning("dns_mon: sonda '%s' ignorata: %s", name, exc)
                continue
            if spec.name in seen:
                self._logger.warning("dns_mon: sonda '%s' duplicata, ignorata", name)
                continue
            seen.add(spec.name)
            specs.append(spec)
        return specs

    @classmethod
    def _parse_probe(cls, name: str, raw: str) -> ProbeSpec:
        if not _NAME_RE.match(name):
            raise ValueError("nome non valido (ammessi lettere, cifre, _ e -)")
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("formato atteso: tipo,destinazione,livello")
        kind, target, layer = parts[0].lower(), parts[1], parts[2].lower()
        if kind not in _KINDS:
            raise ValueError(f"tipo '{kind}' sconosciuto (ammessi: {', '.join(_KINDS)})")
        if layer not in LAYERS:
            raise ValueError(f"livello '{layer}' sconosciuto (ammessi: {', '.join(LAYERS)})")
        host, port = cls._split_target(target, kind)
        return ProbeSpec(name=name, kind=kind, host=host, port=port, layer=layer)

    @staticmethod
    def _split_target(target: str, kind: str) -> tuple[str, int | None]:
        host, port_text = target, None
        if target.startswith("["):
            end = target.find("]")
            if end < 0:
                raise ValueError("indirizzo IPv6 tra parentesi non chiuso")
            host, rest = target[1:end], target[end + 1:]
            if rest:
                if not rest.startswith(":"):
                    raise ValueError("dopo ']' è atteso ':porta'")
                port_text = rest[1:]
        elif target.count(":") == 1:
            host, port_text = target.split(":")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(
                f"'{host}' non è un indirizzo IP (i nomi DNS non sono ammessi)"
            ) from None

        port: int | None = None
        if port_text is not None:
            try:
                port = int(port_text)
            except ValueError:
                raise ValueError(f"porta '{port_text}' non valida") from None
            if not 1 <= port <= 65535:
                raise ValueError(f"porta {port} fuori dall'intervallo 1-65535")

        if kind == "icmp":
            if address.version != 4:
                raise ValueError("icmp supporta solo IPv4")
            if port is not None:
                raise ValueError("icmp non ammette una porta")
        elif kind == "tcp" and port is None:
            raise ValueError("tcp richiede ip:porta")
        elif kind == "dns" and port is None:
            port = 53
        return str(address), port

    # ------------------------------------------------------------------
    # Interfaccia BaseModule
    # ------------------------------------------------------------------

    def collect(self) -> None:
        if not self._available or self._shutdown.is_set():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._start_locked()

    def getData(self) -> dict:
        if not self._available:
            return {}
        with self._lock:
            return copy.deepcopy(self._build_data_locked())

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Attende che ogni sonda abbia un esito e nessuna sia in fase incerta.

        Serve alle esecuzioni singole (--test, --dry-run): dopo una sola misura
        una sonda fallita avrebbe ``consecutive_failures=1`` e lo stato
        risulterebbe ancora "ok". Si attende quindi che i fallimenti siano
        confermati (o smentiti) dalle misure ravvicinate della modalità adattiva.
        """
        if not self._available or self._thread is None:
            return True
        with self._cond:
            return self._cond.wait_for(
                lambda: self._shutdown.is_set() or self._settled_locked(), timeout
            )

    def close(self) -> None:
        self._shutdown.set()
        self._wake.set()
        if not self._available:
            return
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Pianificazione
    # ------------------------------------------------------------------

    def _start_locked(self) -> None:
        now = self._monotonic()
        for index, job in enumerate(self._jobs):
            job.inflight = False
            job.next_due = now + index * _START_STAGGER_SECONDS
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_parallel, thread_name_prefix="DnsProbe"
            )
        self._thread = Thread(
            target=self._run_loop, name="DnsMonitorScheduler", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._shutdown.is_set():
            # clear() prima di pianificare: un completamento durante il tick
            # imposta l'evento e l'attesa successiva termina subito.
            self._wake.clear()
            try:
                wait = self._tick()
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.error("dns_mon: errore nel ciclo di pianificazione: %s", exc)
                wait = 1.0
            self._wake.wait(wait)

    def _tick(self) -> float:
        now = self._monotonic()
        due: list[_Job] = []
        wait = _MAX_WAIT_SECONDS
        with self._lock:
            # Aggiorna (al massimo ogni pochi secondi) la memoria dell'ultimo
            # speedtest, anche quando tutto funziona: serve a datare un guasto
            # che compare poco dopo la fine di un test.
            self._speedtest_running(now)
            for job in self._jobs:
                if job.inflight:
                    continue
                if job.next_due <= now:
                    job.inflight = True
                    due.append(job)
                else:
                    wait = min(wait, job.next_due - now)
        executor = self._executor
        for job in due:
            try:
                executor.submit(self._run_job, job)
            except (RuntimeError, AttributeError):  # executor chiuso
                with self._lock:
                    job.inflight = False
                break
        return max(_MIN_WAIT_SECONDS, wait)

    def _interval_for(self, job: _Job) -> float:
        if job.heavy:
            return float(self._heavy_interval)
        base = self._dns_interval if job.probe.spec.kind == "dns" else self._network_interval
        if self._adaptive_active:
            return float(min(base, self._adaptive_interval))
        return float(base)

    def _run_job(self, job: _Job) -> None:
        started_wall = self._time()
        try:
            result = self._execute(job)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "dns_mon: errore inatteso nella sonda %s: %s", job.probe.spec.name, exc
            )
            result = ProbeResult(False, error="internal_error")
        if self._shutdown.is_set():
            return
        self._on_result(job, result, started_wall)

    def _execute(self, job: _Job) -> ProbeResult:
        result = ProbeResult(False, error="not_run")
        for _ in range(1 + self._retries):
            if self._shutdown.is_set():
                break
            result = self._probe_once(job)
            if result.ok:
                break
        return result

    def _probe_once(self, job: _Job) -> ProbeResult:
        spec = job.probe.spec
        if spec.kind == "icmp":
            if self._icmp.mode is None:
                # Un RST (connessione rifiutata) prova comunque che l'host risponde.
                return self._probe_tcp(
                    spec.host, _ICMP_FALLBACK_TCP_PORT, self._tcp_timeout, refused_ok=True
                )
            return self._icmp.ping(spec.host, self._icmp_timeout)
        if spec.kind == "tcp":
            return self._probe_tcp(spec.host, spec.port, self._tcp_timeout)
        qname = self._cache_miss_name() if job.heavy else self._next_domain()
        return self._probe_dns(spec.host, spec.port, qname, self._dns_timeout)

    def _next_domain(self) -> str:
        with self._lock:
            domain = self._test_domains[self._domain_index % len(self._test_domains)]
            self._domain_index += 1
        return domain

    def _cache_miss_name(self) -> str:
        # Etichetta casuale: non può essere in cache. NXDOMAIN è una risposta valida.
        return f"pm{self._random.getrandbits(40):010x}.{self._cache_miss_zone}"

    # ------------------------------------------------------------------
    # Sonde
    # ------------------------------------------------------------------

    def _probe_tcp(
        self, host: str, port: int, timeout: float, refused_ok: bool = False
    ) -> ProbeResult:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        started = time.monotonic()
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((host, port))
        except ConnectionRefusedError:
            if refused_ok:
                return ProbeResult(True, latency_ms=(time.monotonic() - started) * 1000.0)
            return ProbeResult(False, error="refused")
        except OSError as exc:
            return ProbeResult(False, error=_error_label(exc))
        return ProbeResult(True, latency_ms=(time.monotonic() - started) * 1000.0)

    def _probe_dns(self, host: str, port: int, qname: str, timeout: float) -> ProbeResult:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            query = dns.message.make_query(qname, dns.rdatatype.A)
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                # Socket connesso: se il servizio è fermo l'ICMP "porta
                # irraggiungibile" emerge come "econnrefused" invece di un
                # timeout, distinguendo un AdGuard/Unbound spento da pacchetti
                # persi. dnspython richiede un socket non bloccante.
                sock.setblocking(False)
                sock.connect((host, port))
                started = time.monotonic()
                response = dns.query.udp(query, host, port=port, timeout=timeout, sock=sock)
        except (dns.exception.DNSException, OSError, ValueError) as exc:
            return ProbeResult(False, error=_error_label(exc))
        latency = (time.monotonic() - started) * 1000.0
        rcode = response.rcode()
        text = dns.rcode.to_text(rcode)
        ok = rcode in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN)
        return ProbeResult(ok, latency_ms=latency, error=None if ok else text.lower(), rcode=text)

    # ------------------------------------------------------------------
    # Valutazione dello stato
    # ------------------------------------------------------------------

    def _on_result(self, job: _Job, result: ProbeResult, started_wall: float) -> None:
        now_mono = self._monotonic()
        now_wall = self._time()
        with self._lock:
            name = job.probe.spec.name
            before = None if job.heavy else self._probe_state(job.probe)
            job.track.record(result, started_wall, now_wall, now_mono, self._window_seconds)
            job.inflight = False
            job.next_due = now_mono + self._interval_for(job)
            self._last_probe_at = now_wall
            if not job.heavy:
                after = self._probe_state(job.probe)
                if before != after:
                    if after == "down":
                        self._logger.warning(
                            "dns_mon: sonda %s giù (%s)", name, job.track.last_error
                        )
                    elif before == "down":
                        self._logger.info("dns_mon: sonda %s ripristinata", name)
            self._evaluate_locked(now_mono, now_wall)
            self._cond.notify_all()
        self._wake.set()

    def _probe_state(self, probe: _Probe) -> str:
        track = probe.main.track
        if track.last_ok is None:
            return "unknown"
        if track.consecutive_failures >= self._failures_before_down:
            return "down"
        return "up"

    def _settled_locked(self) -> bool:
        for job in self._jobs:
            if job.track.last_ok is None:
                return False
            if not job.heavy and 0 < job.track.consecutive_failures < self._failures_before_down:
                return False
        return True

    def _evaluate_locked(self, now_mono: float, now_wall: float) -> None:
        failed_layer: str | None = None
        degraded: list[str] = []
        for layer in LAYERS:
            probes = self._by_layer.get(layer)
            if not probes:
                continue
            states = {p.spec.name: self._probe_state(p) for p in probes}
            known = [name for name, state in states.items() if state != "unknown"]
            down = [name for name in known if states[name] == "down"]
            if not known:
                continue
            if len(down) == len(known):
                if failed_layer is None:
                    failed_layer = layer
            elif down:
                degraded.extend(down)
        for probe in self._probes:
            cache_miss = probe.cache_miss
            if (
                cache_miss is not None
                and cache_miss.track.consecutive_failures >= _HEAVY_FAILURES_DEGRADED
                and probe.spec.name not in degraded
            ):
                degraded.append(probe.spec.name)

        if failed_layer != self._failed_layer:
            if failed_layer is not None:
                self._logger.warning("dns_mon: livello guasto: %s", failed_layer)
            elif self._failed_layer is not None:
                self._logger.info("dns_mon: livello %s ripristinato", self._failed_layer)
        self._failed_layer = failed_layer
        self._degraded = degraded

        if not any(probe.main.track.last_ok is not None for probe in self._probes):
            self._status = "unknown"
        elif failed_layer is not None:
            self._status = "down"
        elif degraded:
            self._status = "degraded"
        else:
            self._status = "ok"

        self._update_adaptive_locked(now_mono)
        self._update_outage_locked(now_wall)

    def _regular_jobs(self) -> list[_Job]:
        return [probe.main for probe in self._probes]

    def _update_adaptive_locked(self, now_mono: float) -> None:
        """Infittisce il campionamento durante un guasto, entro un tempo massimo."""
        if not self._adaptive_enabled:
            return
        jobs = self._regular_jobs()
        failing = {j.probe.spec.name for j in jobs if j.track.consecutive_failures >= 1}
        # Una sonda già "ignorata" che si riprende torna rilevante per il guasto successivo.
        self._adaptive_ignored &= failing
        relevant = [j for j in jobs if j.probe.spec.name not in self._adaptive_ignored]
        if not self._adaptive_active:
            if any(j.track.consecutive_failures >= 1 for j in relevant):
                self._adaptive_active = True
                self._adaptive_since = now_mono
                for job in jobs:
                    if not job.inflight:
                        job.next_due = min(job.next_due, now_mono + self._adaptive_interval)
                self._logger.info(
                    "dns_mon: campionamento adattivo attivo (ogni %ds)", self._adaptive_interval
                )
            return
        if all(j.track.consecutive_successes >= self._adaptive_recovery_cycles for j in relevant):
            self._adaptive_active = False
            self._logger.info("dns_mon: campionamento adattivo concluso")
        elif now_mono - self._adaptive_since >= self._adaptive_max_seconds:
            # Un guasto permanente (es. un peer che filtra l'ICMP) non deve mantenere
            # il campionamento veloce per sempre: quelle sonde vengono ignorate
            # finché non si riprendono.
            self._adaptive_active = False
            self._adaptive_ignored |= failing
            self._logger.warning(
                "dns_mon: campionamento adattivo concluso per timeout (sonde ancora in errore: %s)",
                ", ".join(sorted(failing)) or "-",
            )

    def _layer_onset(self, layer: str, default: float) -> float:
        """Inizio del guasto: quando l'ultima sonda del livello ha iniziato a fallire.

        Si usa il massimo e non il minimo: una sonda con un guasto proprio e
        permanente (peer che filtra l'ICMP) altrimenti sposterebbe l'inizio di
        ore. Con il campionamento adattivo lo scarto reale è di pochi secondi.
        """
        starts = [
            p.main.track.streak_started
            for p in self._by_layer.get(layer, [])
            if self._probe_state(p) == "down" and p.main.track.streak_started is not None
        ]
        return max(starts) if starts else default

    def _update_outage_locked(self, now_wall: float) -> None:
        failed = self._failed_layer
        if failed is not None:
            self._recovery_since = None
            self._speedtest_running(self._monotonic())
            outage = self._outage
            if outage is None:
                outage = {
                    "start": self._layer_onset(failed, now_wall),
                    "layer": failed,
                    "confirmed": False,
                    "during_speedtest": False,
                }
                self._outage = outage
            elif LAYERS.index(failed) < LAYERS.index(outage["layer"]):
                outage["layer"] = failed
            # Speedtest in corso durante il guasto o nei due minuti che lo precedono.
            if self._speedtest_last_seen >= outage["start"] - _SPEEDTEST_LOOKBACK_SECONDS:
                outage["during_speedtest"] = True
            if not outage["confirmed"] and now_wall - outage["start"] >= self._blackout_threshold:
                outage["confirmed"] = True
                self._logger.warning(
                    "dns_mon: blackout confermato (livello %s, dal %s%s)",
                    outage["layer"],
                    _iso(outage["start"]),
                    ", durante uno speedtest" if outage["during_speedtest"] else "",
                )
            return

        outage = self._outage
        if outage is None:
            return
        if self._recovery_since is None:
            self._recovery_since = now_wall
            return
        # Debounce: la fine è confermata solo se il livello resta sano abbastanza a lungo.
        debounce = max(15.0, 2.0 * self._network_interval)
        if now_wall - self._recovery_since >= debounce:
            self._close_outage_locked(self._recovery_since)

    def _close_outage_locked(self, end: float) -> None:
        outage = self._outage
        self._outage = None
        self._recovery_since = None
        if outage is None:
            return
        start = outage["start"]
        duration = max(0.0, end - start)
        if outage["confirmed"]:
            self._events.append(
                {
                    "start": start,
                    "end": end,
                    "layer": outage["layer"],
                    "during_speedtest": bool(outage["during_speedtest"]),
                }
            )
            del self._events[:-_MAX_EVENTS]
            self._logger.warning(
                "dns_mon: blackout terminato dopo %ds (livello %s)",
                int(duration), outage["layer"],
            )
        else:
            self._short_outages.append(
                {"start": start, "duration": duration, "layer": outage["layer"]}
            )
            del self._short_outages[:-_MAX_SHORT_OUTAGES]
            self._logger.info(
                "dns_mon: interruzione breve di %ds (livello %s)", int(duration), outage["layer"]
            )
        self._save_state_locked()

    # ------------------------------------------------------------------
    # Speedtest in corso
    # ------------------------------------------------------------------

    def _speedtest_running(self, now_mono: float) -> bool:
        if self._speedtest_lock_file is None:
            return False
        if now_mono - self._speedtest_checked < _SPEEDTEST_CHECK_SECONDS:
            return self._speedtest_cached
        self._speedtest_checked = now_mono
        self._speedtest_cached = self._flock_held(self._speedtest_lock_file)
        if self._speedtest_cached:
            self._speedtest_last_seen = self._time()
        return self._speedtest_cached

    @staticmethod
    def _flock_held(path: Path, locks_file: str = _PROC_LOCKS) -> bool:
        """True se un processo tiene un flock esclusivo sul file.

        speedtest_mon tiene il lock solo mentre il test gira, ma il file resta
        sul disco: la sua sola presenza non dice nulla. Provare ad acquisire il
        lock rischierebbe di far fallire lo speedtest con "already_running";
        /proc/locks permette invece una lettura passiva. Si confronta solo
        l'inode: st_dev non coincide con il dispositivo mostrato in /proc/locks
        su alcuni filesystem (es. btrfs).
        """
        try:
            inode = path.stat().st_ino
            content = Path(locks_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in content.splitlines():
            fields = line.split()
            if "FLOCK" not in fields or "WRITE" not in fields or "->" in fields:
                continue  # "->" indica un processo in attesa, non il possessore
            for field in fields:
                match = _LOCK_ID_RE.match(field)
                if match and int(match.group(1)) == inode:
                    return True
        return False

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    def _load_state(self) -> None:
        path = self._state_file
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("stato non valido: oggetto JSON atteso")
            events: list[dict[str, Any]] = []
            for item in raw.get("events") or []:
                start = self._number(item.get("start"))
                end = self._number(item.get("end"))
                if start is None or end is None or end < start:
                    continue
                events.append(
                    {
                        "start": start,
                        "end": end,
                        "layer": str(item.get("layer") or "unknown"),
                        "during_speedtest": bool(item.get("during_speedtest", False)),
                    }
                )
            short: list[dict[str, Any]] = []
            for item in raw.get("short_outages") or []:
                start = self._number(item.get("start"))
                duration = self._number(item.get("duration"))
                if start is None or duration is None or duration < 0:
                    continue
                short.append(
                    {"start": start, "duration": duration, "layer": str(item.get("layer") or "unknown")}
                )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._logger.warning("dns_mon: impossibile caricare lo stato %s: %s", path, exc)
            return
        self._events = events[-_MAX_EVENTS:]
        self._short_outages = short[-_MAX_SHORT_OUTAGES:]

    def _save_state_locked(self) -> None:
        path = self._state_file
        if path is None:
            return
        horizon = self._time() - _DAY_SECONDS
        payload = {
            "version": _STATE_VERSION,
            "saved_at": self._time(),
            "events": self._events[-_MAX_EVENTS:],
            "short_outages": [s for s in self._short_outages if s["start"] >= horizon],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            self._logger.warning("dns_mon: impossibile salvare lo stato %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _build_data_locked(self) -> dict[str, Any]:
        now_mono = self._monotonic()
        now_wall = self._time()
        return {
            "status": self._status,
            "failed_layer": self._failed_layer,
            "degraded_probes": list(self._degraded),
            "speedtest_running": self._speedtest_running(now_mono),
            "last_probe": _iso(self._last_probe_at) if self._last_probe_at else None,
            "window_minutes": self._window_minutes,
            "probes": {
                probe.spec.name: self._probe_data(probe, now_mono) for probe in self._probes
            },
            "blackouts": self._blackout_data(now_wall),
        }

    def _probe_data(self, probe: _Probe, now_mono: float) -> dict[str, Any]:
        spec = probe.spec
        track = probe.main.track
        track.prune(now_mono, self._window_seconds)
        fallback = spec.kind == "icmp" and self._icmp.mode is None
        if fallback:
            kind, target = "tcp_fallback", self._format_target(spec.host, _ICMP_FALLBACK_TCP_PORT)
        elif spec.kind == "icmp":
            kind, target = "icmp", spec.host
        else:
            kind, target = spec.kind, self._format_target(spec.host, spec.port)
        prefix = "rtt_ms" if spec.kind == "icmp" else "latency_ms"

        data: dict[str, Any] = {
            "type": kind,
            "target": target,
            "layer": spec.layer,
            "state": self._probe_state(probe),
            "ok": track.last_ok,
            "consecutive_failures": track.consecutive_failures,
            "success_pct": track.success_pct(),
            f"{prefix}_p50": track.percentile(50),
            f"{prefix}_p95": track.percentile(95),
        }
        if spec.kind == "dns" and track.last_rcode:
            data["rcode"] = track.last_rcode
        if track.last_ok is False and track.last_error:
            data["error"] = track.last_error
        cache_miss = probe.cache_miss
        if cache_miss is not None:
            cm_track = cache_miss.track
            data["cache_miss_ok"] = cm_track.last_ok
            data["cache_miss_latency_ms"] = (
                round(cm_track.last_latency_ms, 2) if cm_track.last_latency_ms is not None else None
            )
            if cm_track.last_ok is False and cm_track.last_error:
                data["cache_miss_error"] = cm_track.last_error
        return data

    @staticmethod
    def _format_target(host: str, port: int | None) -> str:
        if port is None:
            return host
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    def _blackout_data(self, now_wall: float) -> dict[str, Any]:
        window_start = now_wall - _DAY_SECONDS
        count = 0
        total = 0.0
        for event in self._events:
            if event["end"] < window_start:
                continue
            count += 1
            total += max(0.0, event["end"] - max(event["start"], window_start))
        outage = self._outage
        in_progress = bool(outage and outage["confirmed"])
        if in_progress:
            count += 1
            total += max(0.0, now_wall - max(outage["start"], window_start))

        data: dict[str, Any] = {
            "in_progress": in_progress,
            "count_24h": count,
            "total_seconds_24h": int(total),
            "short_outages_24h": sum(
                1 for s in self._short_outages if s["start"] >= window_start
            ),
            "last": None,
        }
        if self._events:
            last = self._events[-1]
            data["last"] = {
                "start": _iso(last["start"]),
                "end": _iso(last["end"]),
                "duration_seconds": int(last["end"] - last["start"]),
                "failed_layer": last["layer"],
                "during_speedtest": last["during_speedtest"],
            }
        if in_progress:
            data["current_start"] = _iso(outage["start"])
            data["current_seconds"] = int(max(0.0, now_wall - outage["start"]))
            data["current_layer"] = outage["layer"]
            data["current_during_speedtest"] = bool(outage["during_speedtest"])
        return data
