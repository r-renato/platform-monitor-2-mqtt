"""
rpi_device.py — Metriche hardware del dispositivo.

Raccoglie:
  - info dispositivo   (board, CPU model, core count)    da /proc/cpuinfo
  - memoria RAM        (totale, usata, libera, available) via psutil
  - CPU usage          (percentuale reale, doppio sample) via psutil
  - storage            (per mount point, valori reali GB) via psutil
  - rete               (interfacce UP, IP, MAC, RX/TX)   via psutil
  - temperatura        CPU da /sys/class/thermal          sempre
                       GPU da vcgencmd                    solo se disponibile

Compatibilità:
  - Linux generico x86/ARM: tutte le metriche tranne quelle RPi-specifiche
  - Raspberry Pi: tutte le metriche inclusa GPU temp via vcgencmd
  - Container (Docker/K8s): metriche disponibili nel namespace del container

Note sul CPU usage:
  Il vecchio codice leggeva i tick cumulativi di /proc/stat una sola volta
  e calcolava idle% sull'intera vita del processo — un numero privo di
  significato operativo. psutil.cpu_percent(interval=N) esegue correttamente
  due letture distanziate di N secondi e calcola la percentuale sul delta,
  esattamente come fa top(1). L'interval è configurabile per non bloccare
  il ciclo di raccolta più del necessario.

Note sullo storage:
  Il vecchio codice applicava next_power_of_2() a tutti i valori, arrotondando
  un disco da 59 GB a 64 GB e falsificando tutti i calcoli derivati.
  Il nuovo codice usa i valori reali di psutil in GB con 1 decimale.
"""

from __future__ import annotations

import psutil

from mods.base_module import BaseModule


# Filesystem da escludere dal report storage —
# sono pseudo-filesystem o mount temporanei che non interessano il monitoraggio
_EXCLUDED_FS_TYPES = frozenset({
    "tmpfs", "devtmpfs", "devfs", "overlay",
    "aufs", "squashfs", "nsfs", "cgroup",
    "cgroup2", "sysfs", "proc", "debugfs",
    "tracefs", "securityfs", "pstore",
})

# Mount point da escludere esplicitamente
_EXCLUDED_MOUNT_POINTS = frozenset({
    "/boot", "/boot/efi", "/boot/firmware",
})

# Intervallo in secondi per il campionamento CPU (doppio sample psutil)
# Valori più alti = misura più precisa, ma blocca collect() per quel tempo
_CPU_SAMPLE_INTERVAL = 1.0


class RPIDevice(BaseModule):

    def __init__(self, config) -> None:
        super().__init__(config)

        # vcgencmd è disponibile solo su Raspberry Pi con firmware Broadcom
        self._vcgencmd_path: str | None = self._which("vcgencmd")
        if self._vcgencmd_path is None:
            self._logger.info(
                "rpi_device: vcgencmd non trovato — temperatura GPU non disponibile"
            )

        # Le info statiche del dispositivo vengono raccolte una sola volta
        self._device_info: dict = self._collect_device_info()

    # ------------------------------------------------------------------
    # Interfaccia BaseModule
    # ------------------------------------------------------------------

    def collect(self) -> None:
        self._data = {
            "info":        {**self._device_info, **self._collect_fs_total()},
            "memory":      self._collect_memory(),
            "cpu":         self._collect_cpu(),
            "storage":     self._collect_storage(),
            "network":     self._collect_network(),
            "temperature": self._collect_temperature(),
        }

    def getData(self) -> dict:
        return self._data

    # ------------------------------------------------------------------
    # Info statiche dispositivo (raccolte una sola volta nel __init__)
    # ------------------------------------------------------------------

    def _collect_device_info(self) -> dict:
        """
        Legge /proc/cpuinfo direttamente tramite _read_file().

        I campi Model, Hardware, Revision, Serial esistono solo su ARM/RPi.
        Su x86 questi campi sono assenti: restituiamo stringa vuota senza errori.
        Il campo "model name" e il conteggio dei core sono disponibili su
        qualsiasi architettura Linux.
        """
        result: dict = {
            "board":           "",
            "board_hardware":  "",
            "board_revision":  "",
            "board_serial":    "",
            "processor":       "",
            "processor_cores": 0,
            "ram_total_mb":    0,
        }

        cpuinfo = self._read_file("/proc/cpuinfo")
        if cpuinfo:
            for line in cpuinfo.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key   = key.strip()
                value = value.strip()

                if key == "Model":
                    result["board"] = value
                elif key == "Hardware":
                    result["board_hardware"] = value
                elif key == "Revision":
                    result["board_revision"] = value
                elif key == "Serial":
                    result["board_serial"] = value
                elif key == "model name" and not result["processor"]:
                    # Prendiamo solo il primo "model name" (tutti i core sono uguali)
                    result["processor"] = value

        # psutil per core count e RAM totale: più affidabili di nproc e free
        result["processor_cores"] = psutil.cpu_count(logical=True) or 0

        mem = psutil.virtual_memory()
        # RAM totale in MB, arrotondata alla potenza di 2 più vicina
        # SOLO per il campo "ram_total_mb" nel device info (valore nominale,
        # es. 4096 MB per un RPi 4 da 4 GB).
        # I valori operativi in _collect_memory() usano i byte reali.
        result["ram_total_mb"] = self._round_to_power_of_2_mb(mem.total)

        return result

    def _collect_fs_total(self) -> dict:
        """
        Aggiunge fs_total_gb al device info: la dimensione del filesystem root.
        Separata da _collect_device_info perché dipende da psutil.disk_usage
        che potrebbe non essere disponibile in certi ambienti container.
        """
        try:
            usage = psutil.disk_usage("/")
            return {"fs_total_gb": round(usage.total / 1_073_741_824, 1)}
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("rpi_device: fs_total_gb non disponibile: %s", exc)
            return {"fs_total_gb": 0.0}

    # ------------------------------------------------------------------
    # Metriche dinamiche (raccolte a ogni ciclo)
    # ------------------------------------------------------------------

    def _collect_memory(self) -> dict:
        """
        Memoria RAM via psutil.virtual_memory().

        Valori in KB per retrocompatibilità con il formato originale.
        psutil restituisce byte: dividiamo per 1024.

        Campi:
          ram_total_kb    — RAM fisica totale
          ram_used_kb     — RAM usata (total - free - buffers - cache)
          ram_free_kb     — RAM libera (non usata da nulla)
          ram_available_kb — RAM disponibile per nuovi processi
                             (free + cache recuperabile; il valore più utile
                              operativamente — quello che usa `free -h`)
        """
        mem = psutil.virtual_memory()
        return {
            "ram_total_kb":     mem.total     // 1024,
            "ram_used_kb":      mem.used      // 1024,
            "ram_free_kb":      mem.free      // 1024,
            "ram_available_kb": mem.available // 1024,
        }

    def _collect_cpu(self) -> dict:
        """
        CPU usage via psutil.cpu_percent(interval=N).

        psutil esegue due letture di /proc/stat distanziate di `interval` secondi
        e calcola la percentuale sul delta — esattamente come fa top(1).

        DIFFERENZA CRITICA rispetto al codice originale:
          - Originale: legge /proc/stat UNA VOLTA, divide idle_ticks / total_ticks
            dall'avvio del sistema. Questo misura l'idle medio dall'accensione,
            non il carico corrente.
          - Nuovo: doppio sample su _CPU_SAMPLE_INTERVAL secondi. Misura il carico
            nell'ultimo secondo, non nella vita intera del processo.

        cpu_percent restituisce 100 - idle%, quindi:
          average_cpu_percentage  = percentuale di CPU utilizzata ORA
          average_idle_percentage = 100 - cpu_percentage (per retrocompatibilità)
        """
        cpu_pct = psutil.cpu_percent(interval=_CPU_SAMPLE_INTERVAL)

        result: dict = {
            "average_cpu_percentage":  round(cpu_pct, 1),
            "average_idle_percentage": round(100.0 - cpu_pct, 1),
        }

        # Frequenze CPU (opzionale — non disponibile su tutti i kernel/architetture)
        try:
            freq = psutil.cpu_freq()
            if freq:
                result["cpu_freq_mhz_current"] = round(freq.current, 0)
                result["cpu_freq_mhz_max"]     = round(freq.max, 0)
        except Exception:  # pylint: disable=broad-except
            pass

        # Load average (1m, 5m, 15m) — disponibile su tutti i Linux
        try:
            load1, load5, load15 = psutil.getloadavg()
            result["load_avg_1m"]  = round(load1,  2)
            result["load_avg_5m"]  = round(load5,  2)
            result["load_avg_15m"] = round(load15, 2)
        except Exception:  # pylint: disable=broad-except
            pass

        return result

    def _collect_storage(self) -> list[dict]:
        """
        Storage per mount point via psutil.disk_partitions().

        Valori reali in GB (1 decimale) — NON arrotondati a potenze di 2.

        Il vecchio codice applicava next_power_of_2() a tutti i valori:
          - Un disco da 59647 MB diventava 64 GB (nominale)
          - Usato da 3328 MB diventava 4 GB
          - available_gb = 64 - 4 = 60 GB (invece dei reali ~56 GB)
        Tutti i valori erano sistematicamente falsificati.

        Filtri applicati:
          - Esclusi i filesystem virtuali (tmpfs, devtmpfs, overlay, ecc.)
          - Esclusi /boot e /boot/efi (non interessanti per il monitoraggio)
          - Esclusi i dispositivi con dimensione totale = 0
        """
        drives: list[dict] = []

        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error("rpi_device: disk_partitions() fallito: %s", exc)
            return drives

        for part in partitions:
            if part.fstype in _EXCLUDED_FS_TYPES:
                continue
            if part.mountpoint in _EXCLUDED_MOUNT_POINTS:
                continue

            try:
                usage = psutil.disk_usage(part.mountpoint)
            except PermissionError:
                self._logger.debug(
                    "rpi_device: disk_usage() permesso negato su %s", part.mountpoint
                )
                continue
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.debug(
                    "rpi_device: disk_usage() fallito su %s: %s", part.mountpoint, exc
                )
                continue

            if usage.total == 0:
                continue

            drives.append({
                "device":          part.device,
                "mount_point":     part.mountpoint,
                "fstype":          part.fstype,
                "size_total_gb":   round(usage.total   / 1_073_741_824, 1),
                "used_gb":         round(usage.used    / 1_073_741_824, 1),
                "available_gb":    round(usage.free    / 1_073_741_824, 1),
                "used_percentage": usage.percent,
            })

        return drives

    def _collect_network(self) -> dict:
        """
        Interfacce di rete via psutil.net_if_addrs() e net_if_stats().

        Raccoglie solo le interfacce in stato UP e non loopback.
        Per ogni interfaccia: IPv4, IPv6, MAC, statistiche RX/TX.

        Rispetto all'originale (che invocava ifconfig 5-6 volte per interfaccia):
          - Una sola chiamata a net_if_addrs() per tutti gli indirizzi
          - Una sola chiamata a net_if_stats() per tutti gli stati
          - Una sola chiamata a net_io_counters() per tutte le statistiche
          - Nessun subprocess, nessun parsing di testo, nessun shell
        """
        result: dict = {}

        try:
            addrs   = psutil.net_if_addrs()
            stats   = psutil.net_if_stats()
            io_ctrs = psutil.net_io_counters(pernic=True)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error("rpi_device: net info non disponibile: %s", exc)
            return result

        import socket as _socket  # import locale per non inquinare il namespace

        AF_INET  = _socket.AF_INET
        AF_INET6 = _socket.AF_INET6
        AF_PACKET = getattr(_socket, "AF_PACKET", 17)  # Linux; 17 è il valore standard

        for iface, iface_stats in stats.items():
            # Salta loopback e interfacce DOWN
            if iface == "lo" or not iface_stats.isup:
                continue

            net: dict = {}

            # Indirizzi IP e MAC
            for addr in addrs.get(iface, []):
                if addr.family == AF_INET:
                    net["ip"]        = addr.address
                    net["mask"]      = addr.netmask or ""
                    net["broadcast"] = addr.broadcast or ""
                elif addr.family == AF_INET6:
                    # Prendiamo solo il primo IPv6 (di solito il link-local)
                    if "ip6" not in net:
                        net["ip6"] = addr.address.split("%")[0]  # rimuove %scope_id
                elif addr.family == AF_PACKET:
                    net["mac"] = addr.address

            # Statistiche RX/TX
            io = io_ctrs.get(iface)
            if io:
                net["rx"] = {
                    "packets":  io.packets_recv,
                    "bytes":    io.bytes_recv,
                    "errors":   io.errin,
                    "dropped":  io.dropin,
                }
                net["tx"] = {
                    "packets":  io.packets_sent,
                    "bytes":    io.bytes_sent,
                    "errors":   io.errout,
                    "dropped":  io.dropout,
                }

            result[iface] = net

        return result

    def _collect_temperature(self) -> dict:
        """
        Temperature del dispositivo.

        CPU: legge /sys/class/thermal/thermal_zone0/temp
             Disponibile su ARM (RPi, Jetson, ecc.) e su molti x86 con ACPI.
             Valore in milligradi Celsius: dividiamo per 1000.

        GPU: invoca vcgencmd measure_temp — SOLO su Raspberry Pi.
             Se vcgencmd non è disponibile, il campo gpu viene omesso
             senza errori (il modulo rimane fully operational).

        Sicurezza: _run_cmd([self._vcgencmd_path, "measure_temp"]) non usa shell,
                   non interpola stringhe — nessun rischio di injection.
        """
        result: dict = {"measurement": "°C"}

        # CPU temperature da /sys (nessun subprocess)
        raw_cpu = self._read_file("/sys/class/thermal/thermal_zone0/temp")
        if raw_cpu:
            try:
                result["cpu"] = round(int(raw_cpu) / 1000, 1)
            except ValueError:
                self._logger.debug("rpi_device: parsing temperatura CPU fallito: %r", raw_cpu)
        else:
            # Fallback: psutil.sensors_temperatures() se disponibile
            try:
                temps = psutil.sensors_temperatures()
                if temps:
                    # Prendiamo la prima sorgente disponibile (coretemp su x86,
                    # cpu_thermal su RPi se /sys non è leggibile)
                    for source, entries in temps.items():
                        if entries:
                            result["cpu"] = round(entries[0].current, 1)
                            result["cpu_source"] = source
                            break
            except (AttributeError, Exception):  # pylint: disable=broad-except
                pass

        # GPU temperature via vcgencmd (solo RPi)
        if self._vcgencmd_path:
            raw_gpu = self._run_cmd(
                [self._vcgencmd_path, "measure_temp"],
                timeout=3,
            )
            # Output: "temp=47.2'C"
            if raw_gpu and "=" in raw_gpu:
                try:
                    temp_str = raw_gpu.split("=")[1].replace("'C", "").strip()
                    result["gpu"] = round(float(temp_str), 1)
                except (ValueError, IndexError):
                    self._logger.debug(
                        "rpi_device: parsing temperatura GPU fallito: %r", raw_gpu
                    )

        return result

    # ------------------------------------------------------------------
    # Utilità private
    # ------------------------------------------------------------------

    @staticmethod
    def _round_to_power_of_2_mb(total_bytes: int) -> int:
        """
        Arrotonda la RAM totale alla potenza di 2 in MB più vicina.

        Usato SOLO per il campo ram_total_mb nel device info, che per
        convenzione riporta la dimensione nominale del modulo RAM
        (es. 4096 MB per un RPi con 4 GB di RAM, non 3927 MB reali).

        Questo è l'UNICO posto dove l'arrotondamento a potenza di 2
        ha senso. Tutte le altre metriche (storage, RAM operativa)
        usano i valori reali.
        """
        mb = total_bytes // (1024 * 1024)
        if mb <= 0:
            return 0
        # Trova la potenza di 2 >= mb
        power = 1
        while power < mb:
            power <<= 1
        return power