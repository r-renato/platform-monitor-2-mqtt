"""
platform_monitor_2_mqtt.py — Daemon principale di platform-monitor-2-mqtt.

Raccoglie metriche di sistema tramite moduli pluggabili e le pubblica
su un broker MQTT in formato JSON a intervalli configurabili.

Uso:
  python3 platform_monitor_2_mqtt.py [-v] [-d] [-t] [-c /path/to/config/dir]

  -v / --verbose   abilita logging INFO (default: WARNING)
  -d / --debug     abilita logging DEBUG
  -t / --test      esegue un solo ciclo e termina (non avvia il daemon)
  -c / --config_dir  directory dove cercare monitor.ini (default: directory dello script)

Argomenti anche leggibili da variabili d'ambiente MQTT_*:
  MQTT_HOSTNAME, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import logging.config
import os
import signal
import ssl
import sys
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from time import sleep

import paho.mqtt.client as mqtt
import sdnotify

# ---------------------------------------------------------------------------
# Metadati
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "2.0.0"
SCRIPT_NAME    = "platform-monitor-2-mqtt"
PROJECT_URL    = "https://github.com/r-renato/platform-monitor-2-mqtt"

# ---------------------------------------------------------------------------
# Stato globale minimo
# ---------------------------------------------------------------------------

config: ConfigParser | None = None
logger: logging.Logger | None = None

# Event usato per segnalare al daemon di fermarsi
_stop_event = Event()


# ---------------------------------------------------------------------------
# Gestione segnali
# ---------------------------------------------------------------------------

def _sigint_handler(signum, frame) -> None:       # noqa: ARG001
    logger.info("SIGINT ricevuto — arresto in corso.")
    _stop_event.set()


def _sigterm_handler(signum, frame) -> None:      # noqa: ARG001
    logger.info("SIGTERM ricevuto — arresto in corso.")
    _stop_event.set()


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class Monitor2MQTT(Thread):
    """
    Thread daemon che raccoglie metriche e le pubblica su MQTT.

    Ciclo di vita:
      1. __init__: configura il client MQTT (TLS, credenziali, LWT)
      2. connect(): apre la connessione e attende on_connect
      3. execute(): raccoglie dati da tutti i moduli e pubblica
      4. run(): loop su Event.wait() — si sveglia ogni N minuti

    Differenze rispetto alla versione originale:
      - importlib.import_module() al posto di pydoc.locate() per caricare i moduli
      - execute() chiamato subito all'avvio (prima iterazione non ritardata)
      - availability topic pubblicato con retain=True (simmetrico con LWT offline)
      - ssl.PROTOCOL_TLS_CLIENT al posto di ssl.PROTOCOL_SSLv23 (deprecato in 3.12)
      - Loop robusto: eccezioni nei moduli non fermano il daemon
      - Timestamp con datetime.now().astimezone() (no dipendenza tzlocal)
    """

    _DEFAULT_INTERVAL_MINUTES = 1
    _DEFAULT_BASE_TOPIC       = "home/nodes"
    _DEFAULT_SENSOR_NAME      = "rpi-reporter"

    _LWT_ONLINE  = "online"
    _LWT_OFFLINE = "offline"

    def __init__(self, stop_event: Event) -> None:
        super().__init__(daemon=True, name="Monitor2MQTT")
        self.stopped = stop_event

        self._interval_minutes: int = max(
            self._DEFAULT_INTERVAL_MINUTES,
            config["Daemon"].getint("interval_in_minutes", self._DEFAULT_INTERVAL_MINUTES),
        )
        logger.info("Intervallo di polling: %d minuti", self._interval_minutes)

        # Costruzione del topic base
        base_topic   = config["MQTT topic"].get("base_topic",   self._DEFAULT_BASE_TOPIC).lower().strip()
        sensor_name  = config["MQTT topic"].get("sensor_name",  self._DEFAULT_SENSOR_NAME).lower().strip()
        self._topic  = f"{base_topic}/{sensor_name}"
        logger.debug("Topic MQTT base: %s", self._topic)

        # Cache dei moduli istanziati (lazy init in _collect_data)
        self._modules: dict = {}

        # Flag di connessione — aggiornato dalla callback on_connect
        self._connected = False

        # Configurazione client MQTT
        # CallbackAPIVersion.VERSION2 elimina il DeprecationWarning di paho >= 1.6.
        # La firma delle callback cambia leggermente (reason_code al posto di rc
        # in on_connect/on_disconnect), ma il comportamento è identico.
        # Se si usa paho < 1.6, fare fallback a mqtt.Client() senza argomenti.
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            # paho-mqtt < 1.6: CallbackAPIVersion non esiste ancora
            self._client = mqtt.Client()  # type: ignore[call-arg]
        self._client.on_connect = self._on_connect
        self._client.on_publish  = self._on_publish
        self._client.on_disconnect = self._on_disconnect

        # LWT: il broker pubblica "offline" se la connessione cade
        self._client.will_set(
            f"{self._topic}/availability",
            payload=self._LWT_OFFLINE,
            retain=True,
        )

        # TLS
        if config["MQTT"].getboolean("tls", False):
            self._configure_tls()

        # Credenziali (env ha precedenza su ini)
        username = os.environ.get("MQTT_USERNAME") or config["MQTT"].get("username")
        password = os.environ.get("MQTT_PASSWORD") or config["MQTT"].get("password")
        if username:
            self._client.username_pw_set(username, password)

    # ------------------------------------------------------------------
    # Connessione MQTT
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Apre la connessione al broker e attende on_connect.

        Restituisce True se la connessione è avvenuta con successo,
        False in caso di errore (il main può quindi uscire con codice != 0).
        """
        hostname = os.environ.get("MQTT_HOSTNAME") or config["MQTT"].get("hostname", "localhost")
        port     = int(os.environ.get("MQTT_PORT")  or config["MQTT"].get("port", "1883"))
        keepalive = config["MQTT"].getint("keepalive", 60)

        logger.debug("Connessione a %s:%d (keepalive=%ds)", hostname, port, keepalive)

        try:
            self._client.connect(hostname, port=port, keepalive=keepalive)
        except OSError as exc:
            logger.error(
                "Impossibile connettersi al broker MQTT %s:%d — %s\n"
                "Verificare hostname, porta e che il broker sia in ascolto.",
                hostname, port, exc,
            )
            return False

        self._client.loop_start()

        # Attesa attiva con timeout per non bloccare indefinitamente
        timeout = 10
        waited  = 0
        while not self._connected and waited < timeout:
            sleep(0.5)
            waited += 0.5

        if not self._connected:
            logger.error(
                "Timeout connessione MQTT dopo %ds. "
                "Broker raggiungibile ma on_connect non ricevuto.",
                timeout,
            )
            self._client.loop_stop()
            return False

        # Pubblica disponibilità con retain=True
        # CORREZIONE rispetto all'originale: retain=True è fondamentale
        # perché un subscriber che si connette DOPO l'avvio del daemon
        # riceva comunque lo stato "online" dal broker (retained message).
        # Con retain=False, il messaggio online era perso per chiunque
        # si connettesse in ritardo — rendendo il meccanismo asimmetrico
        # e di fatto inutile per il monitoraggio della disponibilità.
        self._publish_availability(self._LWT_ONLINE)
        self._publish_timestamp()

        return True

    def _configure_tls(self) -> None:
        """
        Configura TLS sul client MQTT.

        ssl.PROTOCOL_TLS_CLIENT (Python 3.10+):
          - Abilita automaticamente SNI e verifica del certificato
          - Seleziona il protocollo più sicuro supportato da entrambi gli endpoint
          - Sostituisce ssl.PROTOCOL_SSLv23 (deprecato in 3.10, rimosso in 3.12)

        Se tls_ca_cert non è specificato, ssl usa il bundle di CA di sistema
        (equivalente al comportamento di curl/wget senza --cacert).
        """
        ca_cert  = config["MQTT"].get("tls_ca_cert")  or None
        keyfile  = config["MQTT"].get("tls_keyfile")  or None
        certfile = config["MQTT"].get("tls_certfile") or None

        try:
            self._client.tls_set(
                ca_certs=ca_cert,
                keyfile=keyfile,
                certfile=certfile,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            logger.debug("TLS configurato (ca_cert=%s)", ca_cert or "sistema")
        except ssl.SSLError as exc:
            logger.error("Configurazione TLS fallita: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Callbacks MQTT
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:  # noqa: ARG002
        # VERSION2: reason_code è un oggetto ReasonCode, non un intero.
        # .value restituisce l'intero sottostante; == 0 significa successo.
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc == 0:
            self._connected = True
            logger.info("Connessione MQTT stabilita — topic: %s", self._topic)
        else:
            self._connected = False
            logger.error(
                "Errore connessione MQTT: rc=%d (%s)",
                rc, mqtt.connack_string(rc),
            )
            os._exit(1)

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:  # noqa: ARG002
        logger.debug("Messaggio MQTT pubblicato (mid=%d)", mid)

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None) -> None:  # noqa: ARG002
        rc = 0
        if reason_code is not None:
            rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc != 0:
            logger.warning(
                "Disconnessione MQTT inattesa (rc=%d) — paho tenterà la riconnessione",
                rc,
            )
        self._connected = False

    # ------------------------------------------------------------------
    # Raccolta dati
    # ------------------------------------------------------------------

    def _collect_data(self) -> dict:
        """
        Istanzia (la prima volta) e interroga tutti i moduli configurati.

        Caricamento moduli con importlib.import_module():
          L'originale usava pydoc.locate(), uno strumento pensato per la
          documentazione interattiva, non per il dynamic import. Se un modulo
          non veniva trovato, pydoc.locate() restituiva None e la chiamata
          successiva None(config) sollevava TypeError con messaggio criptico.

          importlib.import_module() è l'API ufficiale Python per il dynamic
          import: errori espliciti (ModuleNotFoundError, AttributeError),
          integrazione con il sistema di import standard (sys.path, __init__.py),
          comportamento prevedibile su tutti i Python >= 3.1.

        Formato configurazione moduli (monitor.ini):
          [Modules]
          operative_system = gen_linux_os,GenericLinuxOS
          device           = rpi_device,RPIDevice
          docker           = docker_mon,DockerMon
          keepalived       = keepalived_mon,KeepalivedMon

          Ogni riga: chiave = nome_modulo,NomeClasse
          Il modulo viene cercato in `mods.<nome_modulo>`

        Robustezza:
          Se un modulo solleva un'eccezione durante collect() o getData(),
          l'errore viene loggato ma il ciclo continua con gli altri moduli.
          Il valore per quella chiave nel JSON sarà {} invece di dati parziali.
        """
        data: dict = {
            "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        }

        for section_key, module_spec in config.items("Modules"):
            parts = [p.strip() for p in module_spec.split(",")]
            if len(parts) != 2:
                logger.warning(
                    "Configurazione modulo malformata per '%s': '%s' "
                    "(formato atteso: nome_modulo,NomeClasse)",
                    section_key, module_spec,
                )
                continue

            module_file, class_name = parts

            # Lazy init: il modulo viene istanziato solo al primo ciclo
            instance = self._modules.get(section_key)
            if instance is None:
                instance = self._load_module(module_file, class_name)
                if instance is None:
                    continue
                self._modules[section_key] = instance

            # collect() e getData() con protezione da eccezioni non gestite
            try:
                instance.collect()
                data[section_key] = instance.getData()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Errore nel modulo '%s' durante collect()/getData(): %s",
                    section_key, exc,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                data[section_key] = {}

        return data

    def _load_module(self, module_file: str, class_name: str):
        """
        Carica dinamicamente un modulo da mods/<module_file>.py
        e restituisce un'istanza di <class_name>.

        Restituisce None se il modulo non può essere caricato,
        loggando un errore esplicito con il motivo.
        """
        full_module_path = f"mods.{module_file}"
        try:
            module = importlib.import_module(full_module_path)
        except ModuleNotFoundError as exc:
            logger.error(
                "Modulo '%s' non trovato: %s\n"
                "Verificare che il file mods/%s.py esista.",
                full_module_path, exc, module_file,
            )
            return None
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Errore durante l'import di '%s': %s",
                full_module_path, exc,
            )
            return None

        cls = getattr(module, class_name, None)
        if cls is None:
            logger.error(
                "Classe '%s' non trovata in '%s'.\n"
                "Verificare il nome della classe in monitor.ini.",
                class_name, full_module_path,
            )
            return None

        try:
            return cls(config)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Errore nell'istanziare %s.%s: %s",
                full_module_path, class_name, exc,
            )
            return None

    # ------------------------------------------------------------------
    # Pubblicazione MQTT
    # ------------------------------------------------------------------

    def execute(self) -> None:
        """
        Esegue un ciclo completo: raccoglie dati e li pubblica su MQTT.

        Chiamato:
          - Subito all'avvio del daemon (prima iterazione non ritardata)
          - Ogni N minuti dal loop in run()
          - Una volta in modalità --test
        """
        try:
            data     = self._collect_data()
            payload  = json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Errore durante la raccolta dati: %s", exc)
            return

        self._client.publish(
            f"{self._topic}/values",
            payload=payload,
            retain=True,
        )
        self._publish_availability(self._LWT_ONLINE)
        self._publish_timestamp()

        if config["General"].getboolean("save_json", False):
            self._save_json(payload)

        logger.debug("Ciclo completato — %d bytes pubblicati", len(payload))

    def _publish_availability(self, status: str) -> None:
        self._client.publish(
            f"{self._topic}/availability",
            payload=status,
            retain=True,   # retain=True: i subscriber che si connettono dopo
                           # ricevono l'ultimo stato noto dal broker
        )

    def _publish_timestamp(self) -> None:
        self._client.publish(
            f"{self._topic}/timestamp",
            payload=datetime.now().astimezone().isoformat(),
            retain=False,
        )

    def _save_json(self, payload: str) -> None:
        """Salva l'ultimo JSON raccolto su file (opzionale, da config)."""
        try:
            out_file = Path("./store/last.json")
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logger.warning("Impossibile salvare last.json: %s", exc)

    # ------------------------------------------------------------------
    # Loop daemon
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Loop principale del thread daemon.

        CORREZIONE rispetto all'originale:
          Il vecchio codice usava:
            while not self.stopped.wait(60 * interval):
                self.execute()

          Event.wait(timeout) blocca per `timeout` secondi PRIMA di
          ritornare False (timeout scaduto senza che l'evento fosse settato).
          Questo significava che il daemon aspettava l'intero intervallo
          prima di pubblicare qualsiasi dato — i topic restavano vuoti
          per i primi N minuti dopo l'avvio.

          Il nuovo pattern:
            1. execute() immediatamente (dati disponibili subito su MQTT)
            2. poi wait(interval) per ogni ciclo successivo

          Così i topic sono popolati entro pochi secondi dall'avvio del
          servizio, non dopo il primo intervallo.
        """
        interval_seconds = 60 * self._interval_minutes

        # Prima esecuzione immediata
        logger.info("Prima raccolta dati all'avvio...")
        self.execute()

        # Loop successivi ogni N minuti
        while not self.stopped.wait(interval_seconds):
            self.execute()

        logger.info("Loop daemon terminato.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Argparse
    parser = argparse.ArgumentParser(
        description=SCRIPT_NAME,
        epilog=f"Per dettagli: {PROJECT_URL}",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log INFO")
    parser.add_argument("-d", "--debug",   action="store_true", help="log DEBUG")
    parser.add_argument("-t", "--test",    action="store_true", help="esegui una volta e termina")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "raccoglie i dati e stampa il JSON su stdout, "
            "senza connettersi al broker MQTT. "
            "Utile per verificare i moduli senza un broker disponibile."
        ),
    )
    parser.add_argument(
        "-c", "--config_dir",
        default=sys.path[0],
        help="directory di monitor.ini (default: directory dello script)",
    )
    args = parser.parse_args()

    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION}\n")

    # Caricamento configurazione
    config = ConfigParser(
        delimiters=("=",),
        inline_comment_prefixes=("#",),
        interpolation=None,
    )
    config.optionxform = str  # preserva il case delle chiavi

    config_path = Path(args.config_dir) / "monitor.ini"
    try:
        with open(config_path) as f:
            config.read_file(f)
        logging.config.fileConfig(config_path)
    except FileNotFoundError:
        print(f'Errore: file di configurazione non trovato: "{config_path}"')
        print("Copiare monitor.dist in monitor.ini e personalizzarlo.")
        sys.exit(1)
    except Exception as exc:
        print(f"Errore nel caricamento della configurazione: {exc}")
        sys.exit(1)

    # Logger
    logger = logging.getLogger("platformMonitor")

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.debug("Modalità debug attiva")
    elif args.verbose:
        logger.setLevel(logging.INFO)
        logger.info("Modalità verbose attiva")

    if args.dry_run:
        logger.info("Modalità dry-run — nessuna connessione MQTT")
    if args.test:
        logger.info("Modalità test — esecuzione singola")

    # Notifica systemd
    sd = sdnotify.SystemdNotifier()

    # Daemon mode flag
    daemon_enabled = config["Daemon"].getboolean("enabled", True)

    # --dry-run: raccoglie dati e stampa JSON senza toccare MQTT
    if args.dry_run:
        try:
            # Crea un'istanza minimale solo per accedere a _collect_data()
            # senza inizializzare il client MQTT
            class _DryRunMonitor(Monitor2MQTT):
                def __init__(self):
                    # Bypass del __init__ completo: non serve il client MQTT
                    from threading import Event
                    Thread.__init__(self, daemon=True, name="DryRun")
                    self.stopped   = Event()
                    self._interval_minutes = 1
                    self._topic    = "dry-run/local"
                    self._modules  = {}
                    self._connected = False
                    self._client   = None  # non inizializzato

            dry = _DryRunMonitor()
            data = dry._collect_data()
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.critical("Errore in dry-run: %s", exc, exc_info=True)
            sys.exit(1)
        sys.exit(0)

    # Avvio normale (con MQTT)
    try:
        monitor = Monitor2MQTT(_stop_event)

        if not monitor.connect():
            sys.exit(1)

        signal.signal(signal.SIGINT,  _sigint_handler)
        signal.signal(signal.SIGTERM, _sigterm_handler)

        if args.test or not daemon_enabled:
            logger.info("Esecuzione singola (--test o daemon disabled)")
            monitor.execute()
            logger.info("Completato.")
        else:
            logger.info("Avvio daemon")
            monitor.start()
            sd.notify("READY=1")

            # Il thread principale attende che _stop_event venga settato
            # dai signal handler (SIGINT/SIGTERM) o dal thread monitor.
            # sleep(10000) dell'originale sostituito con wait() pulito.
            _stop_event.wait()

            logger.info("Attendo la fine del thread monitor...")
            monitor.join(timeout=15)

    except Exception as exc:
        if logger:
            logger.critical("Errore fatale: %s", exc, exc_info=True)
        else:
            print(f"Errore fatale: {exc}")
        sys.exit(1)
