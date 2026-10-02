# platform-monitor-2-mqtt

Daemon Python che raccoglie metriche di sistema da macchine Linux e le
pubblica su un broker MQTT in formato JSON a intervalli configurabili.

Progettato per Raspberry Pi ma compatibile con qualsiasi distribuzione
Linux su x86 o ARM. I moduli opzionali (Docker, Keepalived) si disabilitano
automaticamente se il tool non è installato.

---

## Requisiti

- Python 3.10+
- `paho-mqtt` 2.x (installato tramite `requirements.txt`)
- `dnspython` (installato tramite `requirements.txt`; usato dal modulo `dns_mon`)
- Broker MQTT raggiungibile (Mosquitto, EMQX, HiveMQ, ecc.)
- `pip` e `venv` (inclusi nella maggior parte delle distribuzioni)

Dipendenze Python: vedi `requirements.txt`.

---

## Installazione

Il codice di progetto non viene mai modificato: tutto ciò che è personale
(configurazione, credenziali, virtualenv) vive in `/etc/platform-monitor/`.

| Percorso | Contenuto |
|---|---|
| `/opt/platform-monitor-2-mqtt/` | Codice, sostituito a ogni installazione |
| `/etc/platform-monitor/monitor.ini` | Configurazione (creata da `monitor.dist`, mai sovrascritta) |
| `/etc/platform-monitor/env` | Variabili d'ambiente opzionali (`EnvironmentFile`) |
| `/etc/platform-monitor/credentials/` | Credenziali MQTT cifrate con `systemd-creds` |
| `/etc/platform-monitor/venv/` | Virtualenv Python |
| `/etc/systemd/system/p-monitor-2-mqtt.service` | Unit del progetto |
| `/etc/systemd/system/p-monitor-2-mqtt.service.d/10-installer.conf` | Override generato (non modificare) |
| `/etc/systemd/system/p-monitor-2-mqtt.service.d/90-local.conf` | Override personali (mai toccato) |
| `/var/lib/platform-monitor/` | Stato runtime |

### 1 — Scarica il codice

```bash
sudo git clone https://github.com/r-renato/platform-monitor-2-mqtt.git \
               /usr/local/src/platform-monitor-2-mqtt
cd /usr/local/src/platform-monitor-2-mqtt
```

### 2 — Esegui l'installer

```bash
sudo ./scripts/install.sh
```

Lo script, idempotente, controlla i prerequisiti (Python ≥ 3.10, `venv`,
`systemd-creds`), copia il codice in `/opt`, crea il virtualenv e installa
`requirements.txt`, crea `monitor.ini` e `env` se mancano, chiede username e
password MQTT (senza eco) e le cifra, poi installa l'unit e l'override.
Nulla di ciò che hai già personalizzato viene sovrascritto.

Il virtualenv usa `/usr/bin/python3` (modificabile con `PYTHON_BIN=...`). Un
interprete sotto `/root` o `/home`, come quelli di pyenv o uv, viene rifiutato:
il servizio gira con `ProtectHome=true` e non potrebbe avviarlo (errore
`203/EXEC`). Un virtualenv esistente con questo problema viene ricreato.

Opzioni utili (`./scripts/install.sh --help`):

| Opzione | Effetto |
|---|---|
| `--enable-now` | Abilita e avvia (o riavvia) il servizio al termine |
| `--mqtt-user NOME --password-stdin` | Credenziali non interattive (la password da stdin) |
| `--reset-credentials` | Ricrea le credenziali cifrate |
| `--skip-credentials` | Non gestisce le credenziali cifrate |
| `--skip-pip` | Non installa le dipendenze Python |
| `--dry-run` | Mostra le azioni senza eseguirle |
| `--prefix DIR` | Installa sotto `DIR` invece che nella radice (prove) |

### 3 — Personalizza la configurazione e avvia

```bash
sudo nano /etc/platform-monitor/monitor.ini   # broker, topic, intervallo, moduli
sudo systemctl enable --now p-monitor-2-mqtt.service
```

Vedere la sezione [Configurazione](#configurazione) per i dettagli.

Verifica:

```bash
systemctl status p-monitor-2-mqtt.service
journalctl -u p-monitor-2-mqtt -f
```

---

## Aggiornamento

```bash
cd /usr/local/src/platform-monitor-2-mqtt
sudo git pull
sudo ./scripts/install.sh --enable-now   # sostituisce il codice, aggiorna le dipendenze, riavvia
```

`monitor.ini`, `env`, le credenziali e `90-local.conf` restano invariati.

---

## Disinstallazione

```bash
sudo ./scripts/uninstall.sh          # rimuove servizio, codice e virtualenv
sudo ./scripts/uninstall.sh --purge  # rimuove anche configurazione, credenziali e stato
```

Senza `--purge` restano `/etc/platform-monitor/` (tranne il virtualenv) e
`/var/lib/platform-monitor/`.

---

## Configurazione

### File monitor.ini

Copiare `monitor.dist` in `monitor.ini` e modificare le sezioni necessarie.
`monitor.dist` contiene la documentazione inline di ogni opzione.

Sezioni principali:

| Sezione | Contenuto |
|---|---|
| `[General]` | `fallback_domain`, `save_json`, attesa worker one-shot |
| `[Modules]` | Moduli abilitati e relativi nomi di classe |
| `[MQTT]` | Hostname, porta, credenziali, TLS, QoS, timeout e reconnect |
| `[MQTT topic]` | `base_topic`, `sensor_name` |
| `[Daemon]` | `enabled`, `interval_in_minutes` |
| `[Speedtest]` | Periodicità, worker asincrono, backoff, jitter, binding, cache e lock |
| `[DnsMonitor]` | Frequenze di campionamento, modalità adattiva, timeout, soglie, domini di prova |
| `[DnsMonitor probes]` | Sonde `nome = tipo,destinazione,livello` del modulo `dns_mon` |
| `[Logger sessions]` | Configurazione logging Python standard |

### Variabili d'ambiente

Le variabili d'ambiente hanno **precedenza** sui valori di `monitor.ini`.
Utili per ambienti containerizzati o per evitare credenziali su disco.

| Variabile | Corrisponde a | Default |
|---|---|---|
| `MQTT_HOSTNAME` | `[MQTT] hostname` | `localhost` |
| `MQTT_PORT` | `[MQTT] port` | `1883` |
| `MQTT_USERNAME` | `[MQTT] username` | — |
| `MQTT_PASSWORD` | `[MQTT] password` | — |

### Credenziali MQTT

Per non tenere username e password in `monitor.ini` (un file che si
condivide facilmente per errore), il daemon le cerca in quest'ordine,
campo per campo:

1. **Credenziali systemd** `mqtt_username` e `mqtt_password`, esposte nella
   directory `$CREDENTIALS_DIRECTORY` del servizio.
2. **Variabili d'ambiente** `MQTT_USERNAME` e `MQTT_PASSWORD`.
3. **`monitor.ini`**, sezione `[MQTT]`, solo come ripiego.

Nel log compare solo la fonte usata, mai il valore.

**Credenziali cifrate (consigliato, systemd ≥ 250).** `scripts/install.sh` le
crea con `systemd-creds encrypt` in `/etc/platform-monitor/credentials/` e
genera in `10-installer.conf` le righe `LoadCredentialEncrypted=`. Il file
cifrato è legato alla chiave dell'host (o al TPM2, se presente): copiato su
un'altra macchina è inutilizzabile. Il servizio le riceve come file
temporanei, non come variabili d'ambiente, quindi non compaiono in
`systemctl show`. Per cambiarle:

```bash
sudo ./scripts/install.sh --reset-credentials --skip-pip
sudo systemctl restart p-monitor-2-mqtt.service
```

**File di environment (alternativa o ripiego).** L'override carica sempre
`EnvironmentFile=-/etc/platform-monitor/env`, creato vuoto (`640 root:daemon`):

```bash
printf 'MQTT_USERNAME=myuser\nMQTT_PASSWORD=mysecret\n' | sudo tee /etc/platform-monitor/env > /dev/null
sudo systemctl restart p-monitor-2-mqtt.service
```

Il segreto resta in chiaro in quel file, ma fuori da `monitor.ini` e leggibile
solo da root e dal gruppo del servizio.

**Limiti.** Chi è root sull'host può sempre leggere il segreto, perché il
daemon deve poterlo usare. Senza TPM2 la chiave dell'host si trova in
`/var/lib/systemd/credential.secret`: la cifratura protegge dalla copia del
file, non da un attaccante con privilegi di root. Se una password è stata
condivisa in chiaro, va cambiata sul broker.

---

## Topic MQTT

Il topic base è `{base_topic}/{sensor_name}` (default: `home/nodes/rpi-reporter`).

| Subtopic | Contenuto | Retain |
|---|---|---|
| `/values` | JSON con tutte le metriche | ✓ |
| `/availability` | `online` / `offline` | ✓ |
| `/timestamp` | ISO 8601 dell'ultimo ciclo | ✗ |

Il messaggio `/availability = offline` viene pubblicato automaticamente
dal broker (LWT — Last Will Testament) se la connessione cade in modo
inatteso. Entrambi i messaggi online/offline sono `retain=true`: un
subscriber che si connette dopo l'avvio del daemon riceve subito lo
stato corrente senza aspettare il ciclo successivo.

### Esempio payload `/values`

```json
{
  "timestamp": "2024-03-15 10:30:00",
  "operative_system": {
    "linux_distribution_name": "Raspberry Pi OS",
    "linux_distribution_version": "12",
    "linux_kernel": "6.1.21-v8+",
    "rpi_hostname": "raspberrypi",
    "rpi_fqdn": "raspberrypi.home.local",
    "uptime_seconds": 277927,
    "uptime": "up 3 days, 5:12:07"
  },
  "device": {
    "info": {
      "board": "Raspberry Pi 4 Model B Rev 1.4",
      "processor": "ARMv7 Processor rev 3",
      "processor_cores": 4,
      "ram_total_mb": 4096,
      "fs_total_gb": 59.6
    },
    "memory": {
      "ram_total_kb": 3790848,
      "ram_used_kb": 892416,
      "ram_free_kb": 1204224,
      "ram_available_kb": 2654208
    },
    "cpu": {
      "average_cpu_percentage": 12.3,
      "average_idle_percentage": 87.7,
      "load_avg_1m": 0.42,
      "load_avg_5m": 0.38,
      "load_avg_15m": 0.35
    },
    "storage": [
      {
        "device": "/dev/root",
        "mount_point": "/",
        "fstype": "ext4",
        "size_total_gb": 59.6,
        "used_gb": 9.1,
        "available_gb": 47.8,
        "used_percentage": 16
      }
    ],
    "temperature": {
      "cpu": 47.2,
      "gpu": 46.0,
      "measurement": "°C"
    }
  },
  "docker": {
    "info": { "docker_version": "24.0.5" },
    "images": [
      {
        "repository": "nginx",
        "tag": "latest",
        "image_id": "a8758716bb6a",
        "size": "142MB",
        "container_count": 1
      }
    ],
    "containers": [
      {
        "id": "3f4d2a1b9c8e",
        "name": "nginx-proxy",
        "image": "nginx:latest",
        "status": "running",
        "cpu_pct": 0.5,
        "mem_used_mb": 32.1,
        "mem_limit_mb": 512.0,
        "mem_pct": 6.3,
        "net_rx_mb": 1.2,
        "net_tx_mb": 0.8,
        "block_read_mb": 0.0,
        "block_write_mb": 4.1
      }
    ]
  }
}
```

---

## Esecuzione in Docker

```yaml
# docker-compose.yml
services:
  platform-monitor:
    image: python:3.11-slim
    container_name: platform-monitor-2-mqtt
    restart: unless-stopped
    network_mode: host          # necessario per accedere al broker locale
    pid: host                   # necessario per leggere /proc del sistema host
    volumes:
      - /opt/platform-monitor-2-mqtt:/app:ro
      - /opt/platform-monitor-2-mqtt/store:/app/store
      - /var/run/docker.sock:/var/run/docker.sock:ro   # rimuovere se docker_mon non serve
    working_dir: /app
    environment:
      MQTT_HOSTNAME: mosquitto
      MQTT_USERNAME: ${MQTT_USERNAME}
      MQTT_PASSWORD: ${MQTT_PASSWORD}
    command: >
      sh -c "pip install -r requirements.txt -q &&
             python -u platform_monitor_2_mqtt.py"
```

> **Note sulla modalità container:**
> - `network_mode: host` permette di raggiungere il broker MQTT sulla rete dell'host.
> - `pid: host` espone `/proc` dell'host al container, necessario per le metriche
>   di sistema reali (CPU, uptime, ecc.). Senza questo flag, i valori riflettono
>   solo il namespace del container.
> - Il socket Docker (`/var/run/docker.sock`) è necessario solo se il modulo
>   `docker_mon` è abilitato.

---

## Uso da riga di comando

```bash
# Esecuzione singola (test, senza avviare il daemon)
./venv/bin/python platform_monitor_2_mqtt.py --test

# Log verbosi
./venv/bin/python platform_monitor_2_mqtt.py --test --verbose

# Log di debug (molto dettagliato)
./venv/bin/python platform_monitor_2_mqtt.py --test --debug

# Config in directory custom
./venv/bin/python platform_monitor_2_mqtt.py -c /etc/platform-monitor/
```

---

## Aggiungere un modulo custom

1. Creare `mods/mio_modulo.py`:

```python
from mods.base_module import BaseModule

class MioModulo(BaseModule):

    def __init__(self, config):
        super().__init__(config)
        # Capability detection: disabilita il modulo se il tool non è disponibile
        if self._which("mio-tool") is None:
            self._available = False
            return

    def collect(self):
        if not self._available:
            return
        # Raccoglie i dati e li salva in self._data
        # Usare self._run_cmd([...]) — mai shell=True
        # Usare self._read_file("/proc/...") per pseudo-file del kernel
        self._data = {"valore": 42}

    def getData(self):
        if not self._available:
            return {}
        return self._data
```

2. Aggiungere in `monitor.ini`:

```ini
[Modules]
mio_modulo = mio_modulo,MioModulo
```

3. Riavviare il servizio:

```bash
sudo systemctl restart p-monitor-2-mqtt.service
```

---

## Speedtest opzionale

Il collector `speedtest_mon` usa il client ufficiale Ookla, interpreta il suo
output JSON e pubblica download, upload, latenza, jitter, packet loss, ISP,
interfaccia e server. Il test è eseguito in un worker dedicato: `collect()`
restituisce subito, quindi le altre metriche continuano a essere raccolte e
pubblicate mentre lo Speedtest è in corso.

Abilitazione e configurazione consigliata:

```ini
[Modules]
speedtest = speedtest_mon,SpeedtestMon

[Speedtest]
interval_in_minutes = 60
timeout_seconds = 120
run_on_start = false
startup_delay_seconds = 60
jitter_seconds = 300
retry_interval_minutes = 15
max_retry_interval_minutes = 240
accept_license = true
accept_gdpr = true
# Se omessi usano PLATFORM_MONITOR_STORE_DIR
#cache_file = /var/lib/platform-monitor/speedtest.json
#lock_file = /var/lib/platform-monitor/speedtest.lock
```

Durante l'esecuzione `status` vale `running`. L'ultimo risultato valido resta
disponibile in caso di errore con `status=error` e `stale=true`; gli errori
consecutivi applicano un backoff progressivo. La cache persistente conserva
anche la prossima esecuzione, mentre il lock impedisce test concorrenti tra
daemon, `--test` e `--dry-run`.

---

## Monitor di rete e DNS opzionale

Il collector `dns_mon` serve a capire **dove** si interrompe la catena
`client → AdGuard → Unbound → (DoT) → WAN` quando "il DNS non va": un
problema di rete, del canale verso l'upstream, di Unbound o di AdGuard
sembrano identici dal client. Richiede `dnspython` (già in `requirements.txt`).

Come per lo Speedtest, `collect()` restituisce subito: le misure sono fatte da
un thread di pianificazione con un piccolo pool di worker, alle frequenze
configurate e indipendentemente dal ciclo del daemon, che pubblica l'ultimo
stato aggregato. Le sonde non lanciano processi esterni e usano solo indirizzi
IP, mai nomi, per non dipendere dal DNS che misurano.

```ini
[Modules]
dns_monitor = dns_mon,DnsMon

[DnsMonitor probes]
# nome = tipo,destinazione,livello
gateway        = icmp,192.168.1.1,gateway
wan_cloudflare = icmp,1.1.1.1,wan
wan_quad9      = icmp,9.9.9.9,wan
dns_public     = dns,1.1.1.1:53,wan
dot_upstream   = tcp,1.1.1.1:853,dot_upstream
unbound        = dns,192.168.1.1:5353,unbound
adguard        = dns,192.168.1.1:53,adguard
```

Tutte le opzioni (frequenze, timeout, soglie, domini di prova) sono documentate
in `monitor.dist`, sezioni `[DnsMonitor]` e `[DnsMonitor probes]`.

**Sonde.** `icmp` (echo IPv4), `tcp` (connessione a `ip:porta`) e `dns` (query
UDP verso `ip[:porta]`, senza resolver di sistema). Una risposta `NOERROR` o
`NXDOMAIN` conta come valida; `SERVFAIL`, `REFUSED` e timeout no. Sulle sonde
`dns` del livello `unbound` viene eseguita anche una query su un nome casuale
(`cache_miss_latency_ms`), che non può essere in cache e attraversa tutta la
catena: molti guasti intermittenti emergono solo lì.

**Livelli e attribuzione.** Ogni sonda appartiene a un livello, dal più basso
al più alto: `gateway`, `wan`, `dot_upstream`, `unbound`, `adguard`.
`failed_layer` è il livello più basso in cui **tutte** le sonde sono giù
(almeno `failures_before_down` fallimenti consecutivi): se cade un solo peer
pubblico il livello `wan` non è guasto e lo stato è `degraded`, con la sonda in
`degraded_probes`. Stati: `ok`, `degraded`, `down` (`unknown` finché non c'è
alcun esito).

**Blackout.** Un livello guasto per almeno `blackout_threshold_seconds`
(default 60) è un blackout; le interruzioni più brevi sono contate in
`short_outages_24h`. L'inizio è l'istante in cui l'ultima sonda del livello ha
iniziato a fallire, la fine è confermata da una breve stabilità, così
un'unica interruzione non si spezza in più eventi. I blackout conclusi sono
salvati in `state_file` e sopravvivono ai riavvii; lo stato pubblicato è
sempre derivato da quel registro, quindi un'interruzione di MQTT non fa perdere
eventi. Un blackout ancora in corso al riavvio del servizio non viene
recuperato.

**Speedtest.** Lo Speedtest satura la WAN e può causare perdite e timeout. Il
modulo rileva se è in corso leggendo `/proc/locks` (non prende mai il lock di
`speedtest_mon`, quindi non può farlo fallire) ed espone `speedtest_running`;
i blackout avvenuti durante un test, o nei due minuti precedenti, hanno
`during_speedtest: true`.

**Campionamento adattivo.** A riposo il carico è minimo (rete ogni 10 s, DNS
ogni 30 s, cache-miss ogni 5 minuti). Quando una sonda fallisce le misure si
infittiscono (ogni 3 s) fino al ripristino, per datare con precisione il
guasto, ma al massimo per `adaptive_max_minutes`: un guasto permanente non
mantiene il ritmo veloce per sempre.

**Privilegi ICMP.** Viene usato un socket "ping" non privilegiato, che funziona
se `net.ipv4.ping_group_range` include il gruppo del servizio (default di
Debian/Proxmox), oppure un socket raw (CAP_NET_RAW, ad esempio con il servizio
eseguito come root). Se nessuno dei due è disponibile le sonde `icmp` ripiegano
su connessioni TCP/53 e il JSON le riporta come `tcp_fallback`.

Esempio di payload (estratto; `probes` contiene una voce per sonda):

```json
"dns_monitor": {
  "status": "ok",
  "failed_layer": null,
  "degraded_probes": [],
  "speedtest_running": false,
  "last_probe": "2026-09-21T16:21:40+02:00",
  "window_minutes": 15,
  "probes": {
    "unbound": {
      "type": "dns",
      "target": "192.168.1.1:5353",
      "layer": "unbound",
      "state": "up",
      "ok": true,
      "consecutive_failures": 0,
      "success_pct": 100.0,
      "latency_ms_p50": 1.2,
      "latency_ms_p95": 28.4,
      "rcode": "NOERROR",
      "cache_miss_ok": true,
      "cache_miss_latency_ms": 31.0
    }
  },
  "blackouts": {
    "in_progress": false,
    "count_24h": 1,
    "total_seconds_24h": 420,
    "short_outages_24h": 0,
    "last": {
      "start": "2026-09-21T16:13:50+02:00",
      "end": "2026-09-21T16:20:50+02:00",
      "duration_seconds": 420,
      "failed_layer": "wan",
      "during_speedtest": true
    }
  }
}
```

Durante un blackout confermato `blackouts` contiene anche `current_start`,
`current_seconds`, `current_layer` e `current_during_speedtest`. Le sonde icmp
espongono `rtt_ms_p50/p95`, le altre `latency_ms_p50/p95`; una sonda in errore
riporta anche `error` (`timeout`, `servfail`, `refused`, `enetunreach`, …).

Esempio di sensori Home Assistant, con `base_topic = sensors/machines` e
`sensor_name = pmx-{hostname}` (host `ppve`); da adattare ai propri topic:

```yaml
mqtt:
  sensor:
    - name: "Rete/DNS stato"
      state_topic: "sensors/machines/pmx-ppve/values"
      value_template: "{{ value_json.dns_monitor.status }}"
      json_attributes_topic: "sensors/machines/pmx-ppve/values"
      json_attributes_template: >-
        {{ {'failed_layer': value_json.dns_monitor.failed_layer,
            'blackouts_24h': value_json.dns_monitor.blackouts.count_24h} | tojson }}
      availability_topic: "sensors/machines/pmx-ppve/availability"
  binary_sensor:
    - name: "Blackout di rete in corso"
      state_topic: "sensors/machines/pmx-ppve/values"
      value_template: "{{ value_json.dns_monitor.blackouts.in_progress }}"
      payload_on: "True"
      payload_off: "False"
      availability_topic: "sensors/machines/pmx-ppve/availability"
```

Le notifiche conviene basarle su `status == down` (richiede più fallimenti
consecutivi) e non su `degraded`, che può comparire per una singola sonda.

---

## Architettura

```
platform_monitor_2_mqtt.py   — daemon principale, loop MQTT
mods/
  base_module.py             — classe base: _run_cmd(), _read_file(), _which()
  gen_linux_os.py            — OS, hostname, uptime
  rpi_device.py              — CPU, RAM, storage, rete, temperatura
  docker_mon.py              — container e immagini Docker
  keepalived_mon.py          — stato daemon keepalived
  speedtest_mon.py           — prestazioni Internet via Speedtest CLI Ookla
  dns_mon.py                 — sonde ICMP/TCP/DNS, livello guasto e blackout
scripts/
  install.sh                 — installa/aggiorna il servizio (codice in /opt, config in /etc)
  uninstall.sh               — rimuove il servizio (--purge elimina anche la config)
  unittest.sh                — controlli automatici (sintassi, template, script, test)
test/
  test_monitor.py            — test ciclo MQTT, reconnect, publish, shutdown e credenziali
  test_speedtest_mon.py      — test worker, scheduling, backoff, lock e cache
  test_dns_mon.py            — test sonde, attribuzione dei livelli, blackout, stato
monitor.dist                 — template di configurazione (install.sh lo copia in monitor.ini)
requirements.txt             — dipendenze Python
p-monitor-2-mqtt.service     — unit systemd con hardening
```

---

## Licenza

MIT — vedi `LICENSE`.
