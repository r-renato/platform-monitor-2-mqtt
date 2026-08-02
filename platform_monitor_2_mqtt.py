"""
platform_monitor_2_mqtt.py — Daemon principale di platform-monitor-2-mqtt.

Raccoglie metriche di sistema tramite moduli pluggabili e le pubblica
su un broker MQTT in formato JSON a intervalli configurabili.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import logging.config
import os
import signal
import socket
import ssl
import sys
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

import paho.mqtt.client as mqtt
import sdnotify

SCRIPT_VERSION = "2.2.0"
SCRIPT_NAME = "platform-monitor-2-mqtt"
PROJECT_URL = "https://github.com/r-renato/platform-monitor-2-mqtt"

config: ConfigParser | None = None
logger: logging.Logger | None = None
_stop_event = Event()


def _sigint_handler(signum, frame) -> None:  # noqa: ARG001
    if logger:
        logger.info("SIGINT ricevuto — arresto in corso.")
    _stop_event.set()


def _sigterm_handler(signum, frame) -> None:  # noqa: ARG001
    if logger:
        logger.info("SIGTERM ricevuto — arresto in corso.")
    _stop_event.set()


def _reason_code_value(reason_code: Any) -> int:
    """Normalizza i ReasonCode Paho VERSION2 a un intero."""
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class Monitor2MQTT(Thread):
    """Raccoglie metriche dai moduli configurati e le pubblica su MQTT."""

    _DEFAULT_INTERVAL_MINUTES = 1
    _DEFAULT_BASE_TOPIC = "home/nodes"
    _DEFAULT_SENSOR_NAME = "rpi-reporter"
    _DEFAULT_QOS = 1
    _DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
    _DEFAULT_PUBLISH_TIMEOUT_SECONDS = 10.0
    _DEFAULT_RECONNECT_MIN_SECONDS = 1
    _DEFAULT_RECONNECT_MAX_SECONDS = 60

    _LWT_ONLINE = "online"
    _LWT_OFFLINE = "offline"

    def __init__(self, stop_event: Event) -> None:
        if config is None or logger is None:
            raise RuntimeError("config e logger devono essere inizializzati")

        super().__init__(daemon=True, name="Monitor2MQTT")
        self.stopped = stop_event

        self._interval_minutes = max(
            self._DEFAULT_INTERVAL_MINUTES,
            config["Daemon"].getint(
                "interval_in_minutes", self._DEFAULT_INTERVAL_MINUTES
            ),
        )

        base_topic = config["MQTT topic"].get(
            "base_topic", self._DEFAULT_BASE_TOPIC
        ).lower().strip().strip("/")
        sensor_name = config["MQTT topic"].get(
            "sensor_name", self._DEFAULT_SENSOR_NAME
        ).lower().strip().strip("/")
        sensor_name = sensor_name.replace("{hostname}", socket.gethostname().lower())
        self._topic = f"{base_topic}/{sensor_name}"

        self._qos = min(2, max(0, config["MQTT"].getint("qos", self._DEFAULT_QOS)))
        self._connect_timeout = max(
            1.0,
            config["MQTT"].getfloat(
                "connect_timeout_seconds", self._DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
        )
        self._publish_timeout = max(
            1.0,
            config["MQTT"].getfloat(
                "publish_timeout_seconds", self._DEFAULT_PUBLISH_TIMEOUT_SECONDS
            ),
        )
        reconnect_min = max(
            1,
            config["MQTT"].getint(
                "reconnect_min_delay_seconds", self._DEFAULT_RECONNECT_MIN_SECONDS
            ),
        )
        reconnect_max = max(
            reconnect_min,
            config["MQTT"].getint(
                "reconnect_max_delay_seconds", self._DEFAULT_RECONNECT_MAX_SECONDS
            ),
        )

        self._modules: dict[str, Any] = {}
        self._connected = False
        self._connected_event = Event()
        self._connect_result_event = Event()
        self._connect_reason_code: int | None = None
        self._network_started = False
        self._closing = False
        self._closed = False
        self._close_lock = Lock()

        # La patch richiede Paho 2.x per evitare callback ambigue tra API v1/v2.
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_publish = self._on_publish
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(
            min_delay=reconnect_min,
            max_delay=reconnect_max,
        )
        self._client.will_set(
            f"{self._topic}/availability",
            payload=self._LWT_OFFLINE,
            qos=self._qos,
            retain=True,
        )

        if config["MQTT"].getboolean("tls", False):
            self._configure_tls()

        username = os.environ.get("MQTT_USERNAME") or config["MQTT"].get("username")
        password = os.environ.get("MQTT_PASSWORD") or config["MQTT"].get("password")
        if username:
            self._client.username_pw_set(username, password)

        logger.info(
            "Monitor configurato — topic=%s, polling=%d min, qos=%d",
            self._topic,
            self._interval_minutes,
            self._qos,
        )

    def connect(self) -> bool:
        """Apre la connessione e attende l'esito della callback on_connect."""
        if config is None or logger is None:
            return False

        hostname = os.environ.get("MQTT_HOSTNAME") or config["MQTT"].get(
            "hostname", "localhost"
        )
        port = int(os.environ.get("MQTT_PORT") or config["MQTT"].get("port", "1883"))
        keepalive = config["MQTT"].getint("keepalive", 60)

        self._connect_result_event.clear()
        self._connect_reason_code = None
        try:
            self._client.connect(hostname, port=port, keepalive=keepalive)
            self._client.loop_start()
            self._network_started = True
        except (OSError, ValueError) as exc:
            logger.error(
                "Impossibile connettersi al broker MQTT %s:%d — %s",
                hostname,
                port,
                exc,
            )
            return False

        if not self._connect_result_event.wait(self._connect_timeout):
            logger.error(
                "Timeout connessione MQTT dopo %.1fs: on_connect non ricevuto",
                self._connect_timeout,
            )
            self.close(graceful=False)
            return False

        if not self._connected:
            rc = self._connect_reason_code
            logger.error("Connessione MQTT rifiutata (rc=%s)", rc)
            self.close(graceful=False)
            return False

        return True

    def _configure_tls(self) -> None:
        if config is None or logger is None:
            return
        ca_cert = config["MQTT"].get("tls_ca_cert") or None
        keyfile = config["MQTT"].get("tls_keyfile") or None
        certfile = config["MQTT"].get("tls_certfile") or None
        self._client.tls_set(
            ca_certs=ca_cert,
            keyfile=keyfile,
            certfile=certfile,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        logger.debug("TLS configurato (ca_cert=%s)", ca_cert or "sistema")

    def _on_connect(
        self, client, userdata, flags, reason_code, properties=None
    ) -> None:  # noqa: ARG002
        rc = _reason_code_value(reason_code)
        self._connect_reason_code = rc
        if rc == 0:
            self._connected = True
            self._connected_event.set()
            if logger:
                logger.info("Connessione MQTT stabilita — topic: %s", self._topic)
            # Non attendere wait_for_publish nella callback del network loop.
            self._publish_availability(self._LWT_ONLINE, wait=False)
            self._publish_timestamp(wait=False)
        else:
            self._connected = False
            self._connected_event.clear()
            if logger:
                logger.error("Errore connessione MQTT: rc=%d", rc)
        self._connect_result_event.set()

    def _on_publish(
        self, client, userdata, mid, reason_code=None, properties=None
    ) -> None:  # noqa: ARG002
        if logger:
            logger.debug("Messaggio MQTT pubblicato (mid=%d)", mid)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ) -> None:  # noqa: ARG002
        rc = _reason_code_value(reason_code)
        self._connected = False
        self._connected_event.clear()
        if rc != 0 and not self._closing and logger:
            logger.warning(
                "Disconnessione MQTT inattesa (rc=%d) — riconnessione automatica attiva",
                rc,
            )

    def _collect_data(self) -> dict[str, Any]:
        if config is None or logger is None:
            raise RuntimeError("config e logger non inizializzati")

        data: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        }

        for section_key, module_spec in config.items("Modules"):
            parts = [part.strip() for part in module_spec.split(",")]
            if len(parts) != 2:
                logger.warning(
                    "Configurazione modulo malformata per '%s': '%s'",
                    section_key,
                    module_spec,
                )
                continue

            module_file, class_name = parts
            instance = self._modules.get(section_key)
            if instance is None:
                instance = self._load_module(module_file, class_name)
                if instance is None:
                    continue
                self._modules[section_key] = instance

            try:
                instance.collect()
                data[section_key] = instance.getData()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Errore nel modulo '%s' durante collect()/getData(): %s",
                    section_key,
                    exc,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                data[section_key] = {}

        return data

    def _load_module(self, module_file: str, class_name: str):
        if config is None or logger is None:
            return None

        full_module_path = f"mods.{module_file}"
        try:
            module = importlib.import_module(full_module_path)
        except ModuleNotFoundError as exc:
            if exc.name in {full_module_path, module_file}:
                logger.error(
                    "Modulo '%s' non trovato; verificare mods/%s.py",
                    full_module_path,
                    module_file,
                )
            else:
                logger.error(
                    "Dipendenza '%s' mancante durante il caricamento di '%s'",
                    exc.name or "sconosciuta",
                    full_module_path,
                )
            return None
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Errore durante l'import di '%s': %s", full_module_path, exc)
            return None

        cls = getattr(module, class_name, None)
        if cls is None:
            logger.error("Classe '%s' non trovata in '%s'", class_name, full_module_path)
            return None

        try:
            return cls(config)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Errore nell'istanziare %s.%s: %s", full_module_path, class_name, exc)
            return None

    def execute(self) -> bool:
        """Esegue una raccolta e pubblica values, availability e timestamp."""
        if logger is None:
            return False
        try:
            payload = json.dumps(self._collect_data(), ensure_ascii=False)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Errore durante la raccolta dati: %s", exc)
            return False

        published = self._publish(
            f"{self._topic}/values", payload, retain=True, wait=True
        )
        published = self._publish_availability(
            self._LWT_ONLINE, wait=True
        ) and published
        published = self._publish_timestamp(wait=True) and published

        if config and config["General"].getboolean("save_json", False):
            self._save_json(payload)

        logger.debug(
            "Ciclo completato — %d bytes, pubblicazione=%s",
            len(payload),
            "ok" if published else "fallita",
        )
        return published

    def _publish(
        self,
        topic: str,
        payload: str,
        *,
        retain: bool,
        wait: bool,
    ) -> bool:
        if logger is None:
            return False
        if not self._connected:
            logger.warning("Pubblicazione saltata: client MQTT non connesso (%s)", topic)
            return False

        try:
            info = self._client.publish(
                topic,
                payload=payload,
                qos=self._qos,
                retain=retain,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Pubblicazione MQTT fallita su %s: %s", topic, exc)
            return False

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Pubblicazione MQTT rifiutata su %s: rc=%d", topic, info.rc)
            return False

        if wait:
            try:
                info.wait_for_publish(timeout=self._publish_timeout)
            except (RuntimeError, ValueError) as exc:
                logger.error("Conferma pubblicazione MQTT fallita su %s: %s", topic, exc)
                return False
            if hasattr(info, "is_published") and not info.is_published():
                logger.error(
                    "Timeout conferma pubblicazione MQTT su %s dopo %.1fs",
                    topic,
                    self._publish_timeout,
                )
                return False
        return True

    def _publish_availability(self, status: str, *, wait: bool) -> bool:
        return self._publish(
            f"{self._topic}/availability",
            status,
            retain=True,
            wait=wait,
        )

    def _publish_timestamp(self, *, wait: bool) -> bool:
        return self._publish(
            f"{self._topic}/timestamp",
            datetime.now().astimezone().isoformat(),
            retain=False,
            wait=wait,
        )

    def _save_json(self, payload: str) -> None:
        if logger is None:
            return
        try:
            store_dir = Path(os.environ.get("PLATFORM_MONITOR_STORE_DIR", "./store"))
            out_file = store_dir / "last.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logger.warning("Impossibile salvare last.json: %s", exc)

    def wait_for_background_modules(self, timeout: float) -> bool:
        """Attende i worker dei moduli nelle modalità one-shot."""
        deadline = monotonic() + max(0.0, timeout)
        all_idle = True
        for name, instance in list(self._modules.items()):
            wait_method = getattr(instance, "wait_for_idle", None)
            if not callable(wait_method):
                continue
            remaining = max(0.0, deadline - monotonic())
            try:
                if not wait_method(remaining):
                    all_idle = False
                    if logger:
                        logger.warning("Timeout attesa worker modulo '%s'", name)
            except Exception as exc:  # pylint: disable=broad-except
                all_idle = False
                if logger:
                    logger.warning("Errore attesa worker modulo '%s': %s", name, exc)
        return all_idle

    def _close_modules(self) -> None:
        for name, instance in list(self._modules.items()):
            close_method = getattr(instance, "close", None)
            if not callable(close_method):
                continue
            try:
                close_method()
            except Exception as exc:  # pylint: disable=broad-except
                if logger:
                    logger.warning("Errore chiusura modulo '%s': %s", name, exc)

    def close(self, graceful: bool = True) -> None:
        """Chiude moduli, connessione MQTT e network loop in modo idempotente."""
        with self._close_lock:
            if self._closed:
                return
            self._closing = True

            if graceful and self._connected:
                self._publish_availability(self._LWT_OFFLINE, wait=True)

            self._close_modules()

            if self._network_started:
                try:
                    self._client.disconnect()
                except (OSError, RuntimeError) as exc:
                    if logger:
                        logger.warning("Errore durante disconnect MQTT: %s", exc)
                finally:
                    self._client.loop_stop()
                    self._network_started = False

            self._connected = False
            self._connected_event.clear()
            self._closed = True
            if logger:
                logger.info("Monitor MQTT arrestato")

    def run(self) -> None:
        interval_seconds = 60 * self._interval_minutes
        if logger:
            logger.info("Prima raccolta dati all'avvio...")
        self.execute()
        while not self.stopped.wait(interval_seconds):
            self.execute()
        if logger:
            logger.info("Loop daemon terminato.")


def _load_configuration(config_dir: str) -> tuple[ConfigParser, Path]:
    loaded = ConfigParser(
        delimiters=("=",),
        inline_comment_prefixes=("#",),
        interpolation=None,
    )
    loaded.optionxform = str
    config_path = Path(config_dir) / "monitor.ini"
    with config_path.open(encoding="utf-8") as file_handle:
        loaded.read_file(file_handle)
    logging.config.fileConfig(config_path)
    return loaded, config_path


def main() -> int:
    global config, logger

    parser = argparse.ArgumentParser(
        description=SCRIPT_NAME,
        epilog=f"Per dettagli: {PROJECT_URL}",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log INFO")
    parser.add_argument("-d", "--debug", action="store_true", help="log DEBUG")
    parser.add_argument("-t", "--test", action="store_true", help="esegui una volta e termina")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="raccoglie e stampa il JSON senza connettersi al broker MQTT",
    )
    parser.add_argument(
        "-c",
        "--config_dir",
        default=sys.path[0],
        help="directory di monitor.ini (default: directory dello script)",
    )
    args = parser.parse_args()

    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION}\n")

    try:
        config, config_path = _load_configuration(args.config_dir)
    except FileNotFoundError:
        print(f'Errore: file di configurazione non trovato: "{Path(args.config_dir) / "monitor.ini"}"')
        print("Copiare monitor.dist in monitor.ini e personalizzarlo.")
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Errore nel caricamento della configurazione: {exc}")
        return 1

    logger = logging.getLogger("platformMonitor")
    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.verbose:
        logger.setLevel(logging.INFO)

    one_shot_wait = max(
        0.0,
        config["General"].getfloat("one_shot_wait_seconds", 180.0),
    )

    if args.dry_run:
        class _DryRunMonitor(Monitor2MQTT):
            def __init__(self) -> None:
                Thread.__init__(self, daemon=True, name="DryRun")
                self.stopped = Event()
                self._interval_minutes = 1
                self._topic = "dry-run/local"
                self._modules = {}
                self._connected = False

        dry = _DryRunMonitor()
        try:
            data = dry._collect_data()
            if dry.wait_for_background_modules(one_shot_wait):
                data = dry._collect_data()
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0
        except Exception as exc:  # pylint: disable=broad-except
            logger.critical("Errore in dry-run: %s", exc, exc_info=True)
            return 1
        finally:
            dry._close_modules()

    sd = sdnotify.SystemdNotifier()
    daemon_enabled = config["Daemon"].getboolean("enabled", True)
    monitor: Monitor2MQTT | None = None
    exit_code = 0

    try:
        monitor = Monitor2MQTT(_stop_event)
        if not monitor.connect():
            return 1

        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGTERM, _sigterm_handler)

        if args.test or not daemon_enabled:
            logger.info("Esecuzione singola (--test o daemon disabled)")
            monitor.execute()
            if monitor.wait_for_background_modules(one_shot_wait):
                # Ripubblica l'esito prodotto dai worker asincroni.
                monitor.execute()
        else:
            logger.info("Avvio daemon")
            monitor.start()
            sd.notify("READY=1")
            _stop_event.wait()
            monitor.join(timeout=15)
            if monitor.is_alive():
                logger.error("Il thread monitor non si è arrestato entro 15 secondi")
                exit_code = 1
    except Exception as exc:  # pylint: disable=broad-except
        logger.critical("Errore fatale: %s", exc, exc_info=True)
        exit_code = 1
    finally:
        if monitor is not None:
            monitor.close(graceful=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
