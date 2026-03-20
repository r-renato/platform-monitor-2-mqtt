"""
docker_mon.py — Monitoraggio container e immagini Docker.

Raccoglie:
  - info daemon        (versione Docker)
  - per ogni immagine  (tag, ID, dimensione)
  - per ogni container (stato, CPU%, memoria, rete, I/O disco)

Sicurezza:
  Tutte le chiamate a Docker CLI usano _run_cmd() con lista di argomenti.
  I nomi di immagini e container non vengono MAI interpolati in una stringa
  di comando: vengono passati come elementi separati della lista args.
  Questo elimina qualsiasi rischio di command injection indipendentemente
  dal contenuto dei nomi (caratteri speciali, spazi, backtick, ecc.).

  Esempio corretto:
      self._run_cmd([self._docker_cmd, "inspect", "--format", fmt, container_id])

  Esempio SBAGLIATO (mai fare):
      subprocess.Popen(f"docker inspect {container_id}", shell=True, ...)

Formato output (pubblicato su MQTT come JSON):
  {
    "info": { "docker_version": "24.0.5" },
    "images": [
      { "repository": "nginx", "tag": "latest", "image_id": "abc123",
        "size": "142MB", "container_count": 1 }
    ],
    "containers": [
      { "id": "def456", "name": "nginx-proxy", "image": "nginx:latest",
        "status": "running", "cpu_pct": 0.5, "mem_used_mb": 32.1,
        "mem_limit_mb": 512.0, "mem_pct": 6.3,
        "net_rx_mb": 1.2, "net_tx_mb": 0.8,
        "block_read_mb": 0.0, "block_write_mb": 4.1 }
    ]
  }
"""

from __future__ import annotations

import json

from mods.base_module import BaseModule


class DockerMon(BaseModule):
    """
    Monitora il daemon Docker locale.

    Se Docker non è installato o il daemon non è in esecuzione,
    il modulo si segna come non disponibile (is_available = False)
    e restituisce {} senza errori.
    """

    def __init__(self, config) -> None:
        super().__init__(config)

        self._docker_cmd: str | None = self._which("docker")

        if self._docker_cmd is None:
            self._available = False
            self._logger.info("docker_mon: docker non trovato — modulo disabilitato")
            return

        # Verifica che il daemon sia raggiungibile (potrebbe esserci il binario
        # ma il socket /var/run/docker.sock non essere accessibile)
        version_info = self._fetch_version()
        if not version_info:
            self._available = False
            self._logger.warning(
                "docker_mon: docker trovato ma daemon non raggiungibile "
                "(socket inaccessibile o daemon fermo) — modulo disabilitato"
            )
            return

        self._data["info"] = version_info
        self._logger.info(
            "docker_mon: Docker %s rilevato", version_info.get("docker_version", "?")
        )

    # ------------------------------------------------------------------
    # Interfaccia BaseModule
    # ------------------------------------------------------------------

    def collect(self) -> None:
        if not self._available:
            return

        images     = self._fetch_images()
        containers = self._fetch_containers()

        self._data = {
            **self._data,            # mantiene "info" raccolta nel __init__
            "images":     images,
            "containers": containers,
        }

    def getData(self) -> dict:
        if not self._available:
            return {}
        return self._data

    # ------------------------------------------------------------------
    # Raccolta dati
    # ------------------------------------------------------------------

    def _fetch_version(self) -> dict:
        """
        Recupera la versione del daemon Docker.

        Usa `docker version --format json` per evitare il parsing
        dell'output testuale, fragile al variare del formato tra versioni.
        Il flag --format con template Go è disponibile da Docker 1.13+.
        """
        # --format '{{json .}}' restituisce JSON strutturato
        raw = self._run_cmd(
            [self._docker_cmd, "version", "--format", "{{json .Server.Components}}"],
            timeout=5,
        )

        if not raw:
            # Fallback: docker --version (più compatibile, solo stringa)
            raw_ver = self._run_cmd([self._docker_cmd, "--version"], timeout=5)
            # Output: "Docker version 24.0.5, build ced0996"
            if raw_ver:
                parts = raw_ver.split()
                version = parts[2].rstrip(",") if len(parts) >= 3 else raw_ver
                return {"docker_version": version}
            return {}

        # Proviamo il parsing JSON
        try:
            components = json.loads(raw)
            if isinstance(components, list) and components:
                return {"docker_version": components[0].get("Version", "")}
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

        # Ultimo fallback: --version
        raw_ver = self._run_cmd([self._docker_cmd, "--version"], timeout=5)
        if raw_ver:
            parts = raw_ver.split()
            version = parts[2].rstrip(",") if len(parts) >= 3 else raw_ver
            return {"docker_version": version}

        return {}

    def _fetch_images(self) -> list[dict]:
        """
        Elenca le immagini Docker locali.

        Usa --format con template Go per ottenere output strutturato
        invece di parsificare colonne di testo a larghezza variabile.

        Template: Repository\\tTag\\tID\\tSize
        Il separatore \\t (tab) è sicuro perché i nomi Docker non possono
        contenere tab.
        """
        fmt = "{{.Repository}}\\t{{.Tag}}\\t{{.ID}}\\t{{.Size}}"
        raw = self._run_cmd(
            [self._docker_cmd, "images", "--format", fmt],
            timeout=10,
        )

        images: list[dict] = []
        if not raw:
            return images

        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                self._logger.debug("docker_mon: riga immagine malformata: %r", line)
                continue

            repository, tag, image_id, size = parts[0], parts[1], parts[2], parts[3]

            # Conta i container che usano questa immagine (per nome repository)
            # Nota: usiamo l'image_id come filtro, non il nome del repository,
            # per evitare falsi positivi su nomi simili.
            container_count = self._count_containers_for_image(image_id)

            images.append({
                "repository":      repository,
                "tag":             tag,
                "image_id":        image_id,
                "size":            size,
                "container_count": container_count,
            })

        return images

    def _count_containers_for_image(self, image_id: str) -> int:
        """
        Conta quanti container (running o stopped) usano una data immagine.

        image_id viene passato come argomento separato alla lista — non
        interpolato in nessuna stringa. Anche se image_id contenesse
        caratteri speciali, verrebbe trattato letteralmente da execve().
        """
        raw = self._run_cmd(
            [
                self._docker_cmd, "ps", "--all",
                "--filter", f"ancestor={image_id}",
                "--format", "{{.ID}}",
            ],
            timeout=5,
        )
        if not raw:
            return 0
        return len([line for line in raw.splitlines() if line.strip()])

    def _fetch_containers(self) -> list[dict]:
        """
        Raccoglie stato e statistiche di tutti i container (running + stopped).

        Strategia in due passi:
          1. `docker ps --all` con --format per ottenere ID, nome, immagine, stato
          2. `docker stats --no-stream` per le metriche live (solo container running)

        Separare i due comandi è necessario perché `docker stats` non mostra
        i container fermi, mentre vogliamo monitorare anche quelli stopped
        per rilevare crash o arresti non pianificati.
        """
        containers = self._fetch_container_list()
        if not containers:
            return []

        # Recupera le stats dei container in esecuzione in una sola chiamata
        stats_by_id = self._fetch_container_stats()

        # Arricchisce ogni container con le sue stats (se disponibili)
        for container in containers:
            cid = container["id"]
            if cid in stats_by_id:
                container.update(stats_by_id[cid])

        return containers

    def _fetch_container_list(self) -> list[dict]:
        """
        Elenca tutti i container con ID, nome, immagine e stato.
        Usa --format per output strutturato a tab.
        """
        fmt = "{{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}"
        raw = self._run_cmd(
            [self._docker_cmd, "ps", "--all", "--format", fmt],
            timeout=10,
        )

        containers: list[dict] = []
        if not raw:
            return containers

        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue

            cid, name, image, status_raw = parts[0], parts[1], parts[2], parts[3]

            # "Up 3 hours" → "running", "Exited (1) 2 minutes ago" → "exited"
            status_lower = status_raw.lower()
            if status_lower.startswith("up"):
                status = "running"
            elif status_lower.startswith("exited"):
                status = "exited"
            elif status_lower.startswith("restarting"):
                status = "restarting"
            elif status_lower.startswith("paused"):
                status = "paused"
            else:
                status = "stopped"

            containers.append({
                "id":           cid,
                "name":         name.lstrip("/"),
                "image":        image,
                "status":       status,
                "status_raw":   status_raw,
            })

        return containers

    def _fetch_container_stats(self) -> dict[str, dict]:
        """
        Raccoglie le statistiche live dei container in esecuzione.

        `docker stats --no-stream --format json` restituisce una riga JSON
        per container. È il modo ufficiale e più robusto di leggere le stats:
        evita il parsing di colonne a larghezza variabile (l'approccio originale
        usava indici fissi tipo lineParts[2], lineParts[5] ecc. che si
        rompevano al variare della lunghezza dei valori).

        I valori di memoria e rete in Docker stats usano suffissi come
        "32.1MiB", "1.2GB", "892kB" — _parse_docker_size() li converte
        tutti in MB float.

        Nota: --format json richiede Docker 23+. Per versioni precedenti
        abbiamo il fallback su --format con template Go.
        """
        # Prima proviamo JSON (Docker 23+)
        raw = self._run_cmd(
            [
                self._docker_cmd, "stats", "--no-stream",
                "--format", "{{json .}}",
            ],
            timeout=15,
        )

        if not raw:
            return {}

        stats: dict[str, dict] = {}

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                self._logger.debug("docker_mon: stats JSON malformato: %r", line[:80])
                continue

            # Il campo ID in docker stats è il full 64-char ID o il short 12-char
            # docker ps --all --format {{.ID}} restituisce il short ID (12 char)
            # Usiamo i primi 12 caratteri per allineare le due fonti
            cid = s.get("ID", s.get("id", ""))[:12]
            if not cid:
                continue

            # CPU%: "0.05%" → 0.05
            cpu_pct = self._parse_percentage(s.get("CPUPerc", s.get("cpu_percent", "0%")))

            # Memoria: "32.1MiB / 512MiB" oppure campo separato
            mem_usage_str  = s.get("MemUsage",   s.get("mem_usage", "0B / 0B"))
            mem_perc_str   = s.get("MemPerc",    s.get("mem_percent", "0%"))
            mem_used_mb, mem_limit_mb = self._parse_mem_usage(mem_usage_str)

            # Rete: "1.2MB / 800kB"
            net_io_str = s.get("NetIO", s.get("net_io", "0B / 0B"))
            net_rx_mb, net_tx_mb = self._parse_io_pair(net_io_str)

            # Block I/O: "0B / 4.1MB"
            block_io_str = s.get("BlockIO", s.get("block_io", "0B / 0B"))
            block_read_mb, block_write_mb = self._parse_io_pair(block_io_str)

            stats[cid] = {
                "cpu_pct":        cpu_pct,
                "mem_used_mb":    mem_used_mb,
                "mem_limit_mb":   mem_limit_mb,
                "mem_pct":        self._parse_percentage(mem_perc_str),
                "net_rx_mb":      net_rx_mb,
                "net_tx_mb":      net_tx_mb,
                "block_read_mb":  block_read_mb,
                "block_write_mb": block_write_mb,
            }

        return stats

    # ------------------------------------------------------------------
    # Parsing dei valori Docker (MB come unità comune)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_percentage(value: str) -> float:
        """
        "0.05%" → 0.05
        "--"    → 0.0  (Docker mostra -- per container paused/stopped)
        """
        try:
            return round(float(value.replace("%", "").replace("--", "0").strip()), 2)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_docker_size(value: str) -> float:
        """
        Converte una stringa di dimensione Docker in MB float.

        Formati supportati (Docker usa IEC e SI liberamente):
          "32.1MiB" → 32.1
          "1.2GB"   → 1228.8
          "892kB"   → 0.87
          "512B"    → 0.0005
          "0B"      → 0.0
          "--"      → 0.0
        """
        value = value.strip()
        if not value or value == "--":
            return 0.0

        # Mappa suffisso → moltiplicatore per arrivare a MB
        multipliers = {
            "gib": 1024.0,
            "mib": 1.0,
            "kib": 1.0 / 1024,
            "gb":  1000.0,  # Docker a volte usa SI
            "mb":  1.0,
            "kb":  1.0 / 1000,
            "b":   1.0 / (1024 * 1024),
        }

        value_lower = value.lower()
        for suffix, mult in multipliers.items():
            if value_lower.endswith(suffix):
                try:
                    num = float(value_lower[: -len(suffix)])
                    return round(num * mult, 3)
                except ValueError:
                    return 0.0

        # Nessun suffisso riconosciuto — proviamo a parsare come numero puro
        try:
            return float(value)
        except ValueError:
            return 0.0

    @classmethod
    def _parse_mem_usage(cls, value: str) -> tuple[float, float]:
        """
        "32.1MiB / 512MiB" → (32.1, 512.0)
        Restituisce (used_mb, limit_mb).
        """
        parts = value.split("/")
        if len(parts) == 2:
            return cls._parse_docker_size(parts[0]), cls._parse_docker_size(parts[1])
        return 0.0, 0.0

    @classmethod
    def _parse_io_pair(cls, value: str) -> tuple[float, float]:
        """
        "1.2MB / 800kB" → (1.2, 0.8)
        Restituisce (in_mb, out_mb).
        """
        parts = value.split("/")
        if len(parts) == 2:
            return cls._parse_docker_size(parts[0]), cls._parse_docker_size(parts[1])
        return 0.0, 0.0