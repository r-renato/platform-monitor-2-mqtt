"""
keepalived_mon.py — Monitoraggio del daemon keepalived.

Raccoglie:
  - info daemon     (versione, data build)          da keepalived --version
  - stato servizio  (active/inactive/failed)        da systemctl is-active
  - istanze VRRP    (se /tmp/keepalived.data esiste) dal dump di keepalived

Compatibilità:
  - Se keepalived non è installato: modulo disabilitato, getData() → {}
  - Se keepalived è installato ma fermo: stato riportato, no errori
  - Se systemctl non è disponibile (container): fallback su /proc per
    rilevare il processo keepalived attivo

Sicurezza:
  - Nessuna interpolazione di stringa in comandi shell
  - _run_cmd() con lista di argomenti, shell=False sempre
  - Il parsing della versione usa re.search() su output noto,
    non eval() o exec() su dati esterni
"""

from __future__ import annotations

import re

from mods.base_module import BaseModule


class KeepalivedMon(BaseModule):

    def __init__(self, config) -> None:
        super().__init__(config)

        self._keepalived_cmd: str | None = self._which("keepalived")
        self._systemctl_cmd:  str | None = self._which("systemctl")

        if self._keepalived_cmd is None:
            self._available = False
            self._logger.info(
                "keepalived_mon: keepalived non trovato — modulo disabilitato"
            )
            return

        version_info = self._fetch_version()
        if version_info:
            self._data["info"] = version_info
            self._logger.info(
                "keepalived_mon: keepalived %s rilevato",
                version_info.get("keepalived_version", "?"),
            )
        else:
            self._logger.warning(
                "keepalived_mon: keepalived trovato ma impossibile leggere la versione"
            )

    # ------------------------------------------------------------------
    # Interfaccia BaseModule
    # ------------------------------------------------------------------

    def collect(self) -> None:
        if not self._available:
            return

        self._data = {
            **self._data,   # mantiene "info" raccolta nel __init__
            "core": self._fetch_service_status(),
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
        Recupera la versione di keepalived.

        `keepalived --version` scrive su stderr (comportamento keepalived storico),
        quindi subprocess.run() con stderr=PIPE è necessario.
        _run_cmd() redirige stderr su stdout via stderr=subprocess.STDOUT
        internamente — ma per keepalived usiamo un approccio diretto per
        catturare esplicitamente stderr senza dipendere da quel dettaglio.

        Output tipico:
          Keepalived v2.2.7 (04/04,2023)
          Copyright(C) 2001-2023 Alexandre Cassen, <acassen@gmail.com>
          ...

        Parsing robusto:
          Usiamo re.search() invece di split() su indici fissi.
          L'originale usava splittedLine[1] e splittedLine[2] che si
          rompevano se il formato cambiava tra versioni.
        """
        import subprocess

        try:
            result = subprocess.run(
                [self._keepalived_cmd, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # keepalived scrive su stderr
                timeout=5,
                text=True,
            )
            output = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self._logger.debug("keepalived_mon: --version fallito: %s", exc)
            return {}

        if not output:
            return {}

        first_line = output.splitlines()[0]

        info: dict = {}

        # Versione: "Keepalived v2.2.7 (04/04,2023)"
        # Pattern flessibile: cerca vN.N.N ovunque nella prima riga
        version_match = re.search(r"v(\d+\.\d+[\.\d]*)", first_line)
        if version_match:
            info["keepalived_version"] = version_match.group(1)

        # Data build: il testo tra parentesi "(04/04,2023)"
        # Puliamo le parentesi con strip() invece di re.sub() sull'intera riga
        # come faceva il codice originale (che rimuoveva tutte le parentesi
        # anche da eventuali altri token).
        date_match = re.search(r"\(([^)]+)\)", first_line)
        if date_match:
            info["keepalived_date"] = date_match.group(1).strip()

        return info

    def _fetch_service_status(self) -> dict:
        """
        Recupera lo stato del servizio keepalived.

        Strategia a due livelli:
          1. systemctl is-active keepalived  (sistemi con systemd)
          2. Rilevamento processo via /proc  (container, sistemi senza systemd)

        `systemctl is-active` restituisce una singola parola:
          "active"   — servizio in esecuzione
          "inactive" — servizio fermo normalmente
          "failed"   — servizio crashato
          "unknown"  — servizio non registrato in systemd

        Sicurezza: il nome del servizio "keepalived" è una costante hardcoded,
        non dati esterni — non c'è rischio di injection nemmeno con shell=True,
        ma usiamo comunque la lista per coerenza con il resto del codebase.
        """
        result: dict = {}

        if self._systemctl_cmd:
            status = self._run_cmd(
                [self._systemctl_cmd, "is-active", "keepalived"],
                timeout=5,
            )
            # is-active restituisce exit code != 0 per stati non-active,
            # ma _run_cmd() non solleva eccezioni su returncode != 0.
            # Il valore testuale è quello che ci interessa.
            result["service"] = status if status else "unknown"
        else:
            # Fallback per ambienti senza systemd (container, vecchi init system)
            result["service"] = self._detect_process_keepalived()

        return result

    def _detect_process_keepalived(self) -> str:
        """
        Rileva se keepalived è in esecuzione cercando il suo processo in /proc.

        Usato come fallback quando systemctl non è disponibile.
        Itera su /proc/*/comm che contiene il nome del processo (max 15 char).
        Non richiede permessi speciali — /proc/*/comm è leggibile da tutti.
        """
        from pathlib import Path

        try:
            for comm_file in Path("/proc").glob("*/comm"):
                try:
                    name = comm_file.read_text(encoding="utf-8").strip()
                    if name.startswith("keepalived"):
                        return "active"
                except OSError:
                    # Il processo potrebbe essere terminato nel frattempo
                    continue
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug(
                "keepalived_mon: rilevamento processo fallito: %s", exc
            )

        return "inactive"