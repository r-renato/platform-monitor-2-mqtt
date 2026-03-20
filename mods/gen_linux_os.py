"""
gen_linux_os.py — Informazioni sul sistema operativo Linux.

Raccoglie:
  - distribuzione (nome, versione, ID, ID_LIKE)   da /etc/os-release
  - versione kernel                                da /proc/version_signature o uname
  - hostname e FQDN                                da socket + fallback_domain
  - uptime                                         da /proc/uptime (secondi precisi)

Compatibilità:
  - Qualsiasi distribuzione Linux con /etc/os-release (FHS standard dal 2012)
  - Non dipende da APT, RPM o altri package manager
  - Non dipende da vcgencmd o hardware RPi-specifico
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

from mods.base_module import BaseModule


class GenericLinuxOS(BaseModule):

    def __init__(self, config) -> None:
        super().__init__(config)
        self._fallback_domain: str = (
            config["General"].get("fallback_domain", "").strip().lower()
        )
        # Le info sull'OS non cambiano a runtime: le raccogliamo una volta sola
        # nel __init__ e le manteniamo fisse tra un ciclo e l'altro.
        self._static: dict = self._collect_os_info()

    # ------------------------------------------------------------------
    # Interfaccia BaseModule
    # ------------------------------------------------------------------

    def collect(self) -> None:
        """
        Aggiorna hostname, FQDN e uptime (variano nel tempo).
        Le info statiche sull'OS vengono lette solo al primo avvio.
        """
        self._data = {
            **self._static,
            **self._collect_hostname(),
            **self._collect_uptime(),
        }

    def getData(self) -> dict:
        return self._data

    # ------------------------------------------------------------------
    # Raccolta dati statici (chiamata una sola volta nel __init__)
    # ------------------------------------------------------------------

    def _collect_os_info(self) -> dict:
        """
        Legge /etc/os-release direttamente in Python senza invocare shell.

        /etc/os-release è uno standard FHS presente su tutte le distro moderne:
        Debian, Ubuntu, Fedora, Arch, Alpine, Raspbian, ecc.
        Non fa affidamento su APT, /etc/apt/sources.list o altri strumenti
        specifici di una famiglia di distribuzioni.

        Formato del file:
            NAME="Ubuntu"
            VERSION_ID="22.04"
            ID=ubuntu
            ID_LIKE=debian
            ...
        Le righe sono coppie KEY=value, con value opzionalmente tra virgolette.
        """
        result: dict = {
            "linux_distribution_name": "",
            "linux_distribution_version": "",
            "linux_distribution_id": "",
            "linux_distribution_id_like": "",
            "linux_kernel": "",
        }

        os_release = self._parse_os_release()
        result["linux_distribution_name"]    = os_release.get("NAME", "")
        result["linux_distribution_version"] = os_release.get("VERSION_ID", os_release.get("VERSION", ""))
        result["linux_distribution_id"]      = os_release.get("ID", "")
        result["linux_distribution_id_like"] = os_release.get("ID_LIKE", "")

        result["linux_kernel"] = self._collect_kernel_version()

        return result

    def _parse_os_release(self) -> dict[str, str]:
        """
        Parsa /etc/os-release (o /usr/lib/os-release come fallback).
        Gestisce virgolette singole, doppie, e valori senza virgolette.
        """
        for candidate in ("/etc/os-release", "/usr/lib/os-release"):
            raw = self._read_file(candidate)
            if not raw:
                continue

            parsed: dict[str, str] = {}
            # Pattern: KEY="value", KEY='value', KEY=value
            pattern = re.compile(r'^([A-Z_][A-Z0-9_]*)=(["\']?)(.+?)\2\s*$')
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = pattern.match(line)
                if m:
                    parsed[m.group(1)] = m.group(3)
            return parsed

        self._logger.warning("gen_linux_os: /etc/os-release non trovato")
        return {}

    def _collect_kernel_version(self) -> str:
        """
        Legge la versione del kernel da /proc/version, che è disponibile
        su tutti i kernel Linux senza dipendere da uname(1).
        Esempio output: "6.1.21-v8+ #1642 SMP PREEMPT"
        """
        raw = self._read_file("/proc/version")
        if raw:
            # "Linux version 6.1.21-v8+ (dom@buildbot) #1642 SMP ..."
            # Estraiamo solo la stringa di versione (secondo token)
            parts = raw.split()
            if len(parts) >= 3:
                return parts[2]
        # Fallback: uname -r via _run_cmd sicuro
        return self._run_cmd(["/bin/uname", "-r"])

    # ------------------------------------------------------------------
    # Raccolta dati dinamici (chiamata a ogni ciclo)
    # ------------------------------------------------------------------

    def _collect_hostname(self) -> dict:
        """
        Risolve hostname e FQDN tramite il modulo socket della stdlib.

        Vantaggi rispetto a subprocess("hostname -f"):
          - Nessun fork, più veloce
          - Usa le stesse API che il sistema usa internamente (NSS)
          - Gestisce correttamente /etc/hosts, mDNS, ecc.

        Se socket non riesce a risolvere il FQDN (sistema non configurato),
        applica il fallback_domain dalla configurazione.
        """
        result: dict = {}

        short_hostname = socket.gethostname()
        result["rpi_hostname"] = short_hostname

        try:
            fqdn = socket.getfqdn(short_hostname)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("gen_linux_os: getfqdn() fallito: %s", exc)
            fqdn = short_hostname

        # socket.getfqdn() a volte restituisce solo l'hostname corto
        # se il sistema non ha un dominio configurato
        if "." not in fqdn and self._fallback_domain:
            fqdn = f"{short_hostname}.{self._fallback_domain}"

        result["rpi_fqdn"] = fqdn
        result["fqdn_raw"] = fqdn

        return result

    def _collect_uptime(self) -> dict:
        """
        Legge /proc/uptime direttamente: più preciso e più leggero
        di invocare uptime(1), che è un binario esterno che a sua
        volta legge /proc/uptime.

        Formato /proc/uptime: "<secondi_uptime> <secondi_idle_totale>"
        Il primo valore è un float con centesimi di secondo.

        Genera anche una stringa human-readable (es. "3 days, 4:12:07")
        per retrocompatibilità con chi consuma il topic MQTT esistente.
        """
        result: dict = {}

        raw = self._read_file("/proc/uptime")
        if not raw:
            self._logger.warning("gen_linux_os: impossibile leggere /proc/uptime")
            return result

        try:
            uptime_seconds = float(raw.split()[0])
        except (ValueError, IndexError) as exc:
            self._logger.error("gen_linux_os: parsing /proc/uptime fallito: %s", exc)
            return result

        result["uptime_seconds"] = int(uptime_seconds)
        result["uptime_since"]   = self._uptime_since(int(uptime_seconds))
        result["uptime"]         = self._uptime_human(int(uptime_seconds))

        return result

    # ------------------------------------------------------------------
    # Utilità private
    # ------------------------------------------------------------------

    @staticmethod
    def _uptime_human(seconds: int) -> str:
        """
        Converte secondi in stringa human-readable senza dipendenze esterne.
        Esempio: 277927 secondi → "3 days, 5:12:07"
        Retrocompatibile con il formato di uptime -p.
        """
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)

        if days:
            return f"up {days} {'day' if days == 1 else 'days'}, {hours:d}:{minutes:02d}:{secs:02d}"
        return f"up {hours:d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _uptime_since(uptime_seconds: int) -> str:
        """
        Calcola il timestamp di avvio del sistema.
        Equivalente a `uptime -s` ma senza fork.
        """
        from datetime import datetime, timezone, timedelta
        boot_time = datetime.now(timezone.utc) - timedelta(seconds=uptime_seconds)
        return boot_time.astimezone().strftime("%Y-%m-%d %H:%M:%S")