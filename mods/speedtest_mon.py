"""Collector asincrono per il client ufficiale Speedtest CLI di Ookla."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, TextIO

from mods.base_module import BaseModule

_DEFAULT_INTERVAL_MINUTES = 60
_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_RETRY_INTERVAL_MINUTES = 15
_DEFAULT_MAX_RETRY_INTERVAL_MINUTES = 240
_DEFAULT_STARTUP_DELAY_SECONDS = 60
_DEFAULT_JITTER_SECONDS = 300
_MIN_INTERVAL_MINUTES = 15
_WARN_INTERVAL_MINUTES = 30
_MIN_RETRY_INTERVAL_MINUTES = 5
_MIN_TIMEOUT_SECONDS = 10
_MAX_TIMEOUT_SECONDS = 600
_MAX_ERROR_LENGTH = 1000


class SpeedtestMon(BaseModule):
    """Esegue Speedtest in un worker senza bloccare la raccolta MQTT."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._lock = RLock()
        self._shutdown = Event()
        self._worker: Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._random = random.SystemRandom()

        self._speedtest_cmd: str | None = self._which("speedtest")
        if self._speedtest_cmd is None:
            self._available = False
            self._data = {
                "status": "disabled",
                "stale": True,
                "message": "Comando speedtest non trovato",
            }
            self._logger.info("speedtest_mon: speedtest non trovato — modulo disabilitato")
            return

        section = config["Speedtest"] if config.has_section("Speedtest") else {}
        requested_interval = self._get_int(
            section, "interval_in_minutes", _DEFAULT_INTERVAL_MINUTES
        )
        self._interval_minutes = max(_MIN_INTERVAL_MINUTES, requested_interval)
        if requested_interval < _MIN_INTERVAL_MINUTES:
            self._logger.warning(
                "speedtest_mon: intervallo %d min troppo basso; applicato minimo %d min",
                requested_interval,
                _MIN_INTERVAL_MINUTES,
            )
        elif self._interval_minutes < _WARN_INTERVAL_MINUTES:
            self._logger.warning(
                "speedtest_mon: intervallo %d min può generare traffico significativo",
                self._interval_minutes,
            )
        self._interval_seconds = self._interval_minutes * 60

        self._timeout_seconds = self._get_int(
            section,
            "timeout_seconds",
            _DEFAULT_TIMEOUT_SECONDS,
            minimum=_MIN_TIMEOUT_SECONDS,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        self._retry_interval_minutes = self._get_int(
            section,
            "retry_interval_minutes",
            _DEFAULT_RETRY_INTERVAL_MINUTES,
            minimum=_MIN_RETRY_INTERVAL_MINUTES,
        )
        self._max_retry_interval_minutes = self._get_int(
            section,
            "max_retry_interval_minutes",
            _DEFAULT_MAX_RETRY_INTERVAL_MINUTES,
            minimum=self._retry_interval_minutes,
        )
        self._startup_delay_seconds = self._get_int(
            section,
            "startup_delay_seconds",
            _DEFAULT_STARTUP_DELAY_SECONDS,
            minimum=0,
            maximum=86400,
        )
        self._jitter_seconds = self._get_int(
            section,
            "jitter_seconds",
            _DEFAULT_JITTER_SECONDS,
            minimum=0,
            maximum=3600,
        )

        self._server_id = self._get_positive_int(section, "server_id")
        self._interface = self._get_str(section, "interface")
        self._bind_ip = self._get_str(section, "ip")
        self._host = self._get_str(section, "host")
        self._run_on_start = self._get_bool(section, "run_on_start", False)
        self._accept_license = self._get_bool(section, "accept_license", True)
        self._accept_gdpr = self._get_bool(section, "accept_gdpr", True)

        default_store = Path(os.environ.get("PLATFORM_MONITOR_STORE_DIR", "./store"))
        cache_value = self._get_str(
            section, "cache_file", str(default_store / "speedtest.json")
        )
        lock_value = self._get_str(
            section, "lock_file", str(default_store / "speedtest.lock")
        )
        self._cache_file: Path | None = Path(cache_value) if cache_value else None
        self._lock_file: Path | None = Path(lock_value) if lock_value else None

        self._client_version = self._read_client_version()
        now_epoch = time.time()
        self._next_due_epoch = now_epoch
        cache_loaded = self._load_cache()
        self._restore_or_create_schedule(now_epoch, cache_loaded)
        with self._lock:
            self._set_schedule_fields_locked(now_epoch)

        self._logger.info(
            "speedtest_mon: client %s — intervallo %d min, timeout %ds, run_on_start=%s",
            self._client_version or "sconosciuto",
            self._interval_minutes,
            self._timeout_seconds,
            self._run_on_start,
        )

    def collect(self) -> None:
        if not self._available or self._shutdown.is_set():
            return

        now_epoch = time.time()
        with self._lock:
            self._refresh_age_locked(now_epoch)
            if self._worker is not None and self._worker.is_alive():
                self._set_schedule_fields_locked(now_epoch)
                return
            self._worker = None
            if now_epoch < self._next_due_epoch:
                self._set_schedule_fields_locked(now_epoch)
                return
            self._start_worker_locked(now_epoch)

    def getData(self) -> dict:
        if not self._available:
            return {}
        with self._lock:
            now_epoch = time.time()
            self._refresh_age_locked(now_epoch)
            self._set_schedule_fields_locked(now_epoch)
            return copy.deepcopy(self._data)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    def close(self) -> None:
        self._shutdown.set()
        with self._lock:
            process = self._process
            worker = self._worker
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if worker is not None:
            worker.join(timeout=5)
        if worker is not None and worker.is_alive():
            with self._lock:
                process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            worker.join(timeout=2)

    def _start_worker_locked(self, now_epoch: float) -> None:
        attempted_at = datetime.fromtimestamp(now_epoch, tz=timezone.utc).astimezone()
        previous = dict(self._data)
        had_success = bool(previous.get("last_success_at"))
        previous.update(
            {
                "status": "running",
                "stale": had_success,
                "interval_minutes": self._interval_minutes,
                "last_attempt_at": attempted_at.isoformat(),
                "message": "Speedtest in corso",
            }
        )
        previous.pop("error", None)
        previous.pop("next_run_at", None)
        previous.pop("seconds_until_next_run", None)
        self._data = previous
        # Pianificazione provvisoria persistita prima dell'avvio del processo:
        # in caso di crash/restart non viene lanciato subito un nuovo test.
        self._next_due_epoch = now_epoch + self._jittered_delay(self._interval_seconds)
        worker = Thread(
            target=self._worker_main,
            args=(attempted_at,),
            daemon=True,
            name="SpeedtestWorker",
        )
        self._worker = worker
        self._save_cache_locked()
        worker.start()

    def _worker_main(self, attempted_at: datetime) -> None:
        success = False
        try:
            success = self._execute_test(attempted_at)
        except Exception as exc:  # pylint: disable=broad-except
            self._finish_error(
                attempted_at,
                0.0,
                "unexpected_error",
                str(exc),
            )
        finally:
            now_epoch = time.time()
            with self._lock:
                if success:
                    self._schedule_success_locked(now_epoch)
                elif self._data.get("status") not in {"error", "cancelled"}:
                    self._schedule_failure_locked(now_epoch)
                self._worker = None
                self._refresh_age_locked(now_epoch)
                self._set_schedule_fields_locked(now_epoch)
                self._save_cache_locked()

    def _execute_test(self, attempted_at: datetime) -> bool:
        started = time.monotonic()
        lock_handle = self._acquire_process_lock()
        if self._lock_file is not None and lock_handle is None:
            self._finish_error(
                attempted_at,
                0.0,
                "already_running",
                "Un altro processo sta già eseguendo speedtest",
            )
            return False

        try:
            try:
                process = subprocess.Popen(
                    self._build_command(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                with self._lock:
                    self._process = process
                try:
                    stdout, stderr = process.communicate(timeout=self._timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    extra_stdout, extra_stderr = process.communicate()
                    duration = round(time.monotonic() - started, 3)
                    detail = extra_stderr or extra_stdout or self._decode_timeout_output(exc)
                    self._finish_error(
                        attempted_at,
                        duration,
                        "timeout",
                        f"Speedtest non completato entro {self._timeout_seconds} secondi",
                        detail=detail,
                    )
                    return False
            except (FileNotFoundError, PermissionError, OSError) as exc:
                duration = round(time.monotonic() - started, 3)
                self._finish_error(
                    attempted_at, duration, "execution_error", str(exc)
                )
                return False
            finally:
                with self._lock:
                    self._process = None

            duration = round(time.monotonic() - started, 3)
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()

            if self._shutdown.is_set():
                self._finish_cancelled(attempted_at, duration)
                return False

            if process.returncode != 0:
                self._finish_error(
                    attempted_at,
                    duration,
                    "command_failed",
                    f"speedtest terminato con codice {process.returncode}",
                    detail=stderr or stdout or f"return code {process.returncode}",
                    return_code=process.returncode,
                )
                return False

            try:
                parsed = self._normalise_result(self._parse_json_output(stdout))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self._finish_error(
                    attempted_at,
                    duration,
                    "invalid_output",
                    str(exc),
                    detail=stdout or stderr,
                    return_code=process.returncode,
                )
                return False

            completed_at = datetime.now().astimezone()
            with self._lock:
                self._data = {
                    "status": "ok",
                    "stale": False,
                    "interval_minutes": self._interval_minutes,
                    "last_attempt_at": attempted_at.isoformat(),
                    "last_success_at": completed_at.isoformat(),
                    "duration_seconds": duration,
                    "consecutive_failures": 0,
                    **parsed,
                }
            self._logger.info(
                "speedtest_mon: completato — download %.2f Mbps, upload %.2f Mbps, ping %.2f ms",
                self._nested_number(parsed, "download", "mbps"),
                self._nested_number(parsed, "upload", "mbps"),
                self._nested_number(parsed, "ping", "latency_ms"),
            )
            return True
        finally:
            self._release_process_lock(lock_handle)

    def _finish_error(
        self,
        attempted_at: datetime,
        duration_seconds: float,
        error_type: str,
        message: str,
        *,
        detail: str = "",
        return_code: int | None = None,
    ) -> None:
        with self._lock:
            previous = dict(self._data)
            had_success = bool(previous.get("last_success_at"))
            failures = int(previous.get("consecutive_failures", 0)) + 1
            error: dict[str, Any] = {
                "type": error_type,
                "message": self._truncate(message),
            }
            if detail:
                error["detail"] = self._truncate(detail)
            if return_code is not None:
                error["return_code"] = return_code
            previous.update(
                {
                    "status": "error",
                    "stale": had_success,
                    "interval_minutes": self._interval_minutes,
                    "last_attempt_at": attempted_at.isoformat(),
                    "duration_seconds": duration_seconds,
                    "consecutive_failures": failures,
                    "error": error,
                }
            )
            previous.pop("message", None)
            self._data = previous
            self._schedule_failure_locked(time.time())
        self._logger.warning("speedtest_mon: test fallito (%s) — %s", error_type, message)

    def _finish_cancelled(self, attempted_at: datetime, duration_seconds: float) -> None:
        with self._lock:
            previous = dict(self._data)
            previous.update(
                {
                    "status": "cancelled",
                    "stale": bool(previous.get("last_success_at")),
                    "last_attempt_at": attempted_at.isoformat(),
                    "duration_seconds": duration_seconds,
                    "message": "Speedtest interrotto durante l'arresto",
                }
            )
            self._data = previous
            self._next_due_epoch = time.time() + self._retry_interval_minutes * 60

    def _schedule_success_locked(self, now_epoch: float) -> None:
        self._data["consecutive_failures"] = 0
        self._data.pop("retry_delay_minutes", None)
        self._next_due_epoch = now_epoch + self._jittered_delay(self._interval_seconds)

    def _schedule_failure_locked(self, now_epoch: float) -> None:
        failures = max(1, int(self._data.get("consecutive_failures", 1)))
        exponent = min(failures - 1, 16)
        retry_minutes = min(
            self._max_retry_interval_minutes,
            self._retry_interval_minutes * (2 ** exponent),
        )
        self._data["retry_delay_minutes"] = retry_minutes
        self._next_due_epoch = now_epoch + self._jittered_delay(retry_minutes * 60)

    def _restore_or_create_schedule(self, now_epoch: float, cache_loaded: bool) -> None:
        if cache_loaded and self._data.get("status") == "running":
            had_success = bool(self._data.get("last_success_at"))
            failures = int(self._data.get("consecutive_failures", 0)) + 1
            self._data.update({
                "status": "error",
                "stale": had_success,
                "consecutive_failures": failures,
                "error": {
                    "type": "interrupted",
                    "message": "Il precedente speedtest è stato interrotto dal riavvio del processo",
                },
            })
            self._data.pop("message", None)

        if cache_loaded:
            cached_next = self._parse_datetime(self._data.get("next_run_at"))
            if cached_next is not None and cached_next.timestamp() > now_epoch:
                self._next_due_epoch = cached_next.timestamp()
                return

        if self._run_on_start:
            delay = self._jittered_delay(self._startup_delay_seconds, minimum=0)
        else:
            delay = self._jittered_delay(self._interval_seconds)
        self._next_due_epoch = now_epoch + delay

        if not self._data:
            self._data = {
                "status": "waiting",
                "stale": True,
                "interval_minutes": self._interval_minutes,
                "message": "In attesa del primo test pianificato",
                "consecutive_failures": 0,
            }

    def _jittered_delay(self, base_seconds: int, *, minimum: int = 60) -> int:
        jitter = (
            self._random.randint(-self._jitter_seconds, self._jitter_seconds)
            if self._jitter_seconds
            else 0
        )
        return max(minimum, base_seconds + jitter)

    def _refresh_age_locked(self, now_epoch: float) -> None:
        last_success = self._parse_datetime(self._data.get("last_success_at"))
        if last_success is None:
            return
        self._data["result_age_seconds"] = max(
            0, int(now_epoch - last_success.timestamp())
        )
        if self._data.get("status") != "ok":
            self._data["stale"] = True

    def _set_schedule_fields_locked(self, now_epoch: float) -> None:
        if self._data.get("status") == "running":
            self._data.pop("next_run_at", None)
            self._data.pop("seconds_until_next_run", None)
            return
        next_epoch = max(now_epoch, self._next_due_epoch)
        next_dt = datetime.fromtimestamp(next_epoch, tz=timezone.utc).astimezone()
        self._data["next_run_at"] = next_dt.isoformat()
        self._data["seconds_until_next_run"] = max(
            0, int(round(next_epoch - now_epoch))
        )

    def _load_cache(self) -> bool:
        if self._cache_file is None or not self._cache_file.exists():
            return False
        try:
            cached = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                raise TypeError("cache non valida: oggetto JSON atteso")
            self._data = cached
            self._data["loaded_from_cache"] = True
            return True
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            self._logger.warning(
                "speedtest_mon: impossibile caricare cache %s: %s",
                self._cache_file,
                exc,
            )
            return False

    def _save_cache_locked(self) -> None:
        if self._cache_file is None:
            return
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = copy.deepcopy(self._data)
            next_dt = datetime.fromtimestamp(
                self._next_due_epoch, tz=timezone.utc
            ).astimezone()
            data["next_run_at"] = next_dt.isoformat()
            data.pop("seconds_until_next_run", None)
            temporary = self._cache_file.with_name(self._cache_file.name + ".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self._cache_file)
        except OSError as exc:
            self._logger.warning(
                "speedtest_mon: impossibile salvare cache %s: %s",
                self._cache_file,
                exc,
            )

    def _acquire_process_lock(self) -> TextIO | None:
        if self._lock_file is None:
            return None
        try:
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_file.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            return handle
        except (OSError, BlockingIOError):
            try:
                handle.close()  # type: ignore[possibly-undefined]
            except (NameError, OSError):
                pass
            return None

    @staticmethod
    def _release_process_lock(handle: TextIO | None) -> None:
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _build_command(self) -> list[str]:
        command = [self._speedtest_cmd, "--format=json", "--progress=no"]
        if self._accept_license:
            command.append("--accept-license")
        if self._accept_gdpr:
            command.append("--accept-gdpr")
        if self._server_id is not None:
            command.extend(["--server-id", str(self._server_id)])
        if self._interface:
            command.extend(["--interface", self._interface])
        if self._bind_ip:
            command.extend(["--ip", self._bind_ip])
        if self._host:
            command.extend(["--host", self._host])
        return command

    @staticmethod
    def _parse_json_output(stdout: str) -> dict[str, Any]:
        if not stdout:
            raise ValueError("speedtest non ha prodotto output JSON")
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
            for line in reversed(stdout.splitlines()):
                candidate = line.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    parsed = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                raise
        if not isinstance(parsed, dict):
            raise TypeError("l'output speedtest JSON non è un oggetto")
        if parsed.get("type") == "error":
            raise ValueError(str(parsed.get("message") or "speedtest ha restituito un errore"))
        return parsed

    def _normalise_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        download = self._dict(raw.get("download"))
        upload = self._dict(raw.get("upload"))
        ping = self._dict(raw.get("ping"))
        interface = self._dict(raw.get("interface"))
        server = self._dict(raw.get("server"))
        result = self._dict(raw.get("result"))
        download_bandwidth = self._number(download.get("bandwidth"))
        upload_bandwidth = self._number(upload.get("bandwidth"))
        return self._compact(
            {
                "source_timestamp": raw.get("timestamp"),
                "client": self._compact(
                    {
                        "version": self._client_version,
                        "isp": raw.get("isp"),
                        "interface_name": interface.get("name"),
                        "internal_ip": interface.get("internalIp"),
                        "external_ip": interface.get("externalIp"),
                        "mac_address": interface.get("macAddr"),
                        "is_vpn": interface.get("isVpn"),
                    }
                ),
                "server": self._compact(
                    {
                        "id": server.get("id"),
                        "name": server.get("name"),
                        "location": server.get("location"),
                        "country": server.get("country"),
                        "host": server.get("host"),
                        "port": server.get("port"),
                        "ip": server.get("ip"),
                    }
                ),
                "ping": self._compact(
                    {
                        "latency_ms": self._number(ping.get("latency")),
                        "jitter_ms": self._number(ping.get("jitter")),
                        "low_ms": self._number(ping.get("low")),
                        "high_ms": self._number(ping.get("high")),
                    }
                ),
                "download": self._normalise_transfer(download, download_bandwidth),
                "upload": self._normalise_transfer(upload, upload_bandwidth),
                "packet_loss_percent": self._number(raw.get("packetLoss")),
                "result": self._compact(
                    {
                        "id": result.get("id"),
                        "url": result.get("url"),
                        "persisted": result.get("persisted"),
                    }
                ),
            }
        )

    def _normalise_transfer(
        self,
        transfer: dict[str, Any],
        bandwidth_bytes_per_second: float | None,
    ) -> dict[str, Any]:
        latency = self._dict(transfer.get("latency"))
        return self._compact(
            {
                "mbps": (
                    round(bandwidth_bytes_per_second * 8 / 1_000_000, 3)
                    if bandwidth_bytes_per_second is not None
                    else None
                ),
                "bandwidth_bytes_per_second": bandwidth_bytes_per_second,
                "bytes": self._number(transfer.get("bytes"), integer=True),
                "elapsed_ms": self._number(transfer.get("elapsed"), integer=True),
                "latency": self._compact(
                    {
                        "iqm_ms": self._number(latency.get("iqm")),
                        "low_ms": self._number(latency.get("low")),
                        "high_ms": self._number(latency.get("high")),
                        "jitter_ms": self._number(latency.get("jitter")),
                    }
                ),
            }
        )

    def _read_client_version(self) -> str:
        raw = self._run_cmd([self._speedtest_cmd, "--version"], timeout=5)
        if not raw:
            return ""
        if "Version:" in raw:
            for line in raw.splitlines():
                if line.strip().startswith("Version:"):
                    return line.partition(":")[2].strip()
        first_line = raw.splitlines()[0].strip()
        for token in reversed(first_line.split()):
            if token and token[0].isdigit() and "." in token:
                return token.strip()
        return first_line

    @staticmethod
    def _decode_timeout_output(exc: subprocess.TimeoutExpired) -> str:
        for value in (exc.stderr, exc.stdout):
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace").strip()
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _truncate(value: Any) -> str:
        return str(value).strip()[:_MAX_ERROR_LENGTH]

    @staticmethod
    def _nested_number(data: dict[str, Any], *keys: str) -> float:
        value: Any = data
        for key in keys:
            if not isinstance(value, dict):
                return 0.0
            value = value.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _get_int(
        cls,
        section,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            getter = getattr(section, "getint", None)
            if callable(getter):
                value = getter(key, fallback=default)
            else:
                value = int(cls._get_str(section, key, str(default)))
        except (ValueError, TypeError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    @classmethod
    def _get_positive_int(cls, section, key: str) -> int | None:
        raw = cls._get_str(section, key)
        if not raw:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _get_str(section, key: str, default: str = "") -> str:
        try:
            getter = getattr(section, "get", None)
            if callable(getter):
                value = getter(key, fallback=default)
            else:
                value = section.get(key, default)
        except (TypeError, ValueError):
            value = default
        return str(value).strip() if value is not None else default

    @classmethod
    def _get_bool(cls, section, key: str, default: bool) -> bool:
        try:
            getter = getattr(section, "getboolean", None)
            if callable(getter):
                return getter(key, fallback=default)
        except (ValueError, TypeError):
            return default
        raw = cls._get_str(section, key, str(default)).lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _number(value: Any, integer: bool = False) -> int | float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if integer else number

    @staticmethod
    def _compact(data: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in data.items() if value is not None}
