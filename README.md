# platform-monitor-2-mqtt

Daemon Python che raccoglie metriche di sistema da macchine Linux e le
pubblica su un broker MQTT in formato JSON a intervalli configurabili.

Progettato per Raspberry Pi ma compatibile con qualsiasi distribuzione
Linux su x86 o ARM. I moduli opzionali (Docker, Keepalived) si disabilitano
automaticamente se il tool non è installato.

---

## Requisiti

- Python 3.9+
- Broker MQTT raggiungibile (Mosquitto, EMQX, HiveMQ, ecc.)
- `pip` e `venv` (inclusi nella maggior parte delle distribuzioni)

Dipendenze Python: vedi `requirements.txt`.

---

## Installazione

### 1 — Clona il repository

```bash
sudo git clone https://github.com/r-renato/platform-monitor-2-mqtt.git \
               /opt/platform-monitor-2-mqtt
cd /opt/platform-monitor-2-mqtt
```

### 2 — Crea un virtualenv e installa le dipendenze

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> **Perché il virtualenv?**
> Installa le dipendenze in uno spazio isolato senza toccare i pacchetti
> di sistema. Evita conflitti con altri programmi Python sulla macchina
> e rende semplice aggiornare o rimuovere le dipendenze del daemon.

### 3 — Crea il file di configurazione

```bash
sudo cp monitor.dist monitor.ini
sudo nano monitor.ini   # personalizzare hostname broker, topic, intervallo
```

Il file `monitor.ini` non viene committato (è in `.gitignore`).
Vedere la sezione [Configurazione](#configurazione) per i dettagli.

### 4 — Installa il servizio systemd

```bash
sudo ln -s /opt/platform-monitor-2-mqtt/p-monitor-2-mqtt.service \
           /etc/systemd/system/p-monitor-2-mqtt.service

sudo systemctl daemon-reload
sudo systemctl enable --now p-monitor-2-mqtt.service
```

Verifica:

```bash
systemctl status p-monitor-2-mqtt.service
journalctl -u p-monitor-2-mqtt -f
```

---

## Aggiornamento

```bash
sudo systemctl stop p-monitor-2-mqtt.service
cd /opt/platform-monitor-2-mqtt
sudo git pull
.venv/bin/pip install -r requirements.txt   # aggiorna dipendenze se cambiate
sudo systemctl start p-monitor-2-mqtt.service
systemctl status p-monitor-2-mqtt.service
```

---

## Configurazione

### File monitor.ini

Copiare `monitor.dist` in `monitor.ini` e modificare le sezioni necessarie.
`monitor.dist` contiene la documentazione inline di ogni opzione.

Sezioni principali:

| Sezione | Contenuto |
|---|---|
| `[General]` | `fallback_domain`, `save_json` |
| `[Modules]` | Moduli abilitati e relativi nomi di classe |
| `[MQTT]` | Hostname, porta, credenziali, TLS |
| `[MQTT topic]` | `base_topic`, `sensor_name` |
| `[Daemon]` | `enabled`, `interval_in_minutes` |
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

### Credenziali sicure con systemd

Per non mettere le credenziali in `monitor.ini`:

```bash
sudo install -d -o root -g daemon -m 750 /etc/platform-monitor
sudo install -o root -g daemon -m 640 /dev/null /etc/platform-monitor/env

printf 'MQTT_USERNAME=myuser\nMQTT_PASSWORD=mysecret\n' | \
    sudo tee /etc/platform-monitor/env > /dev/null
```

Decommentare in `p-monitor-2-mqtt.service`:

```ini
EnvironmentFile=/etc/platform-monitor/env
```

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
.venv/bin/python platform_monitor_2_mqtt.py --test

# Log verbosi
.venv/bin/python platform_monitor_2_mqtt.py --test --verbose

# Log di debug (molto dettagliato)
.venv/bin/python platform_monitor_2_mqtt.py --test --debug

# Config in directory custom
.venv/bin/python platform_monitor_2_mqtt.py -c /etc/platform-monitor/
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

## Architettura

```
platform_monitor_2_mqtt.py   — daemon principale, loop MQTT
mods/
  base_module.py             — classe base: _run_cmd(), _read_file(), _which()
  gen_linux_os.py            — OS, hostname, uptime
  rpi_device.py              — CPU, RAM, storage, rete, temperatura
  docker_mon.py              — container e immagini Docker
  keepalived_mon.py          — stato daemon keepalived
monitor.dist                 — template di configurazione (copiare in monitor.ini)
requirements.txt             — dipendenze Python
p-monitor-2-mqtt.service     — unit systemd con hardening
```

---

## Licenza

MIT — vedi `LICENSE`.


sudo git clone https://github.com/r-renato/platform-monitor-2-mqtt.git /opt/platform-monitor-2-mqtt

cd /opt/platform-monitor-2-mqtt
sudo pip3 install -r requirements.txt


sudo cp /opt/platform-monitor-2-mqtt/monitor.{ini.dist,ini}


sudo ln -s /opt/platform-monitor-2-mqtt/p-monitor-2-mqtt.service /etc/systemd/system/p-monitor-2-mqtt.service

sudo systemctl daemon-reload
sudo systemctl enable p-monitor-2-mqtt.service

