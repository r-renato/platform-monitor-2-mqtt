# mods/debian/Device.py

import os
import re
import json
import logging
import subprocess
from typing import Any, Dict, List, Optional


class DebianDevice:
    """
    Raccoglie metriche di sistema su Debian/derivati senza affidarsi a grep/cut/awk
    (usa /proc, /sys e comandi con output stabile/JSON: ip -j, df --output).
    """

    _logger: logging.Logger
    _modDict: Dict[str, Any]

    def __init__(self, config) -> None:
        self._logger = logging.getLogger("platformMonitor")
        self._modDict = {}
        self._modDict["info"] = self._getDeviceInfo()

    # ---- API pubblica -----------------------------------------------------

    def collect(self):
        self._modDict["memory"] = self._getMemoryInfo()
        self._modDict["cpu"] = self._getCPUInfo()
        self._modDict["storage"] = self._getStorageInfo()
        self._modDict["network"] = self._getNetworkInfo()
        self._modDict["temperature"] = self._getDeviceTemperature()

        # fs_total_gb per la root
        for dev in self._modDict.get("storage", []):
            if dev.get("mount_point") == "/":
                self._modDict["info"]["fs_total_gb"] = dev.get("size_total_gb")
                break

    def getData(self):
        return self._modDict

    # ---- Utilità ----------------------------------------------------------

    def _run_cmd(self, cmd: List[str], timeout: int = 5) -> str:
        try:
            res = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                text=True,
            )
            return (res.stdout or "").strip()
        except Exception as e:
            self._logger.debug("Command failed %s: %s", cmd, e)
            return ""

    @staticmethod
    def _round_up_pow2_gib_from_megabytes(mb: int) -> int:
        """
        Approx. come l'originale: dato MB, restituisce la potenza di 2 in GiB
        arrotondata per eccesso.
        Esempio: 59998 MB -> 64 GiB.
        """
        if mb <= 0:
            return 0
        # da MB a GiB float
        gib = mb / 1024.0
        n = int(gib)
        if gib > n:
            n += 1
        # arrotonda alla potenza di 2 successiva
        p = 1
        while p < n:
            p <<= 1
        return p

    # ---- Info dispositivo -------------------------------------------------

    def _getDeviceInfo(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {}

        # Board / vendor / product da DMI, se disponibile
        dmi_base = "/sys/devices/virtual/dmi/id"
        def _read(path: str) -> Optional[str]:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    v = f.read().strip()
                    return v if v else None
            except Exception:
                return None

        product_name = _read(os.path.join(dmi_base, "product_name"))
        product_version = _read(os.path.join(dmi_base, "product_version"))
        sys_vendor = _read(os.path.join(dmi_base, "sys_vendor"))
        product_serial = _read(os.path.join(dmi_base, "product_serial"))

        if sys_vendor or product_name:
            info["board"] = " ".join(x for x in [sys_vendor, product_name, product_version] if x)
        else:
            # fallback: prima riga con "model name" da /proc/cpuinfo
            info["board"] = self._cpu_model_fallback()

        info["board_hardware"] = product_name or ""
        info["board_revision"] = product_version or ""
        info["board_serial"] = product_serial or self._machine_id()

        # CPU model
        info["processor"] = self._cpu_model_fallback()

        # Core logici
        try:
            info["processor_cores"] = os.cpu_count() or 1
        except Exception:
            info["processor_cores"] = 1

        # RAM totale (MB reali)
        mem_total_kb = self._meminfo().get("MemTotal", 0)
        info["ram_total_mb"] = int(mem_total_kb / 1024)

        return info

    def _cpu_model_fallback(self) -> str:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "model name" in line or "Hardware" in line:
                        return line.split(":", 1)[-1].strip()
        except Exception:
            pass
        # fallback su lsb_release -ds se presente
        lsb = self._run_cmd(["lsb_release", "-ds"])
        return lsb or "Unknown CPU/Board"

    def _machine_id(self) -> str:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    v = f.read().strip()
                    if v:
                        return v
            except Exception:
                continue
        return ""

    # ---- Temperature ------------------------------------------------------

    def _getDeviceTemperature(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        # CPU/package via thermal zones (/sys/class/thermal/thermal_zoneX)
        cpu_temp = self._read_cpu_temp_from_sys()
        if cpu_temp is not None:
            out["cpu"] = round(cpu_temp, 1)

        # GPU (opzionale) via nvidia-smi, se presente
        nvidia = self._run_cmd(
            ["bash", "-lc", "command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits | head -1"]
        )
        if nvidia.isdigit():
            out["gpu"] = float(nvidia)

        if out:
            out["measurement"] = "°C"
        return out

    def _read_cpu_temp_from_sys(self) -> Optional[float]:
        tz_base = "/sys/class/thermal"
        try:
            zones = [z for z in os.listdir(tz_base) if z.startswith("thermal_zone")]
        except Exception:
            return None

        preferred_types = {"x86_pkg_temp", "cpu-thermal", "cpu_thermal", "acpitz", "soc_thermal"}

        def read_type(zpath: str) -> Optional[str]:
            try:
                with open(os.path.join(zpath, "type"), "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except Exception:
                return None

        def read_temp(zpath: str) -> Optional[float]:
            try:
                with open(os.path.join(zpath, "temp"), "r", encoding="utf-8", errors="ignore") as f:
                    v = f.read().strip()
                    if not v:
                        return None
                    # in milligradi C
                    t = int(v) / 1000.0
                    return t
            except Exception:
                return None

        # prova le preferred
        for z in zones:
            zpath = os.path.join(tz_base, z)
            ztype = read_type(zpath) or ""
            if ztype in preferred_types or ztype.lower().startswith("cpu"):
                t = read_temp(zpath)
                if t is not None:
                    return t

        # fallback: prima valida
        for z in zones:
            t = read_temp(os.path.join(tz_base, z))
            if t is not None:
                return t
        return None

    # ---- Storage ----------------------------------------------------------

    def _getStorageInfo(self) -> List[Dict[str, Any]]:
        """
        Usa 'df' con output stabile (POSIX) e filtri per tipi effimeri.
        Misure: converte a GiB arrotondando alla potenza di 2 (compat con logica originale).
        """
        # -P: posix output, -m o -k non necessari se usiamo --output
        # Usiamo byte per evitare errori di locale, poi convertiamo.
        cmd = [
            "bash",
            "-lc",
            "df -B1 --output=source,size,used,avail,pcent,target "
            "-x tmpfs -x devtmpfs -x overlay -x squashfs | tail -n +2",
        ]
        raw = self._run_cmd(cmd)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        drivers: List[Dict[str, Any]] = []

        for ln in lines:
            # df --output garantisce 6 colonne; la 6a (mountpoint) può contenere spazi, ma è l'ultima.
            parts = ln.split()
            if len(parts) < 6:
                self._logger.debug("Skip malformed df line: %s", ln)
                continue
            # ricostruisci mountpoint unendo gli ultimi campi
            source = parts[0]
            size_b = self._to_int(parts[1])
            used_b = self._to_int(parts[2])
            avail_b = self._to_int(parts[3])
            used_pct = int(parts[4].rstrip("%")) if parts[4].endswith("%") else self._to_int(parts[4])
            mount_point = " ".join(parts[5:])

            size_mb = int(size_b / (1024 * 1024))
            used_mb = int(used_b / (1024 * 1024))

            dev: Dict[str, Any] = {
                "device": source,
                "mount_point": mount_point,
                # compat con logica originale: potenza di 2 in GiB
                "size_total_gb": self._round_up_pow2_gib_from_megabytes(size_mb),
                "used_gb": self._round_up_pow2_gib_from_megabytes(used_mb),
                "available_gb": 0,  # calcolato sotto
                "used_percentage": used_pct,
            }
            dev["available_gb"] = max(dev["size_total_gb"] - dev["used_gb"], 0)

            if dev["size_total_gb"] > 0:
                drivers.append(dev)

        return drivers

    @staticmethod
    def _to_int(s: Any) -> int:
        try:
            return int(str(s))
        except Exception:
            return 0

    # ---- Memoria ----------------------------------------------------------

    def _meminfo(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    m = re.search(r"(\d+)\s*kB", v)
                    if m:
                        out[k.strip()] = int(m.group(1))
        except Exception:
            pass
        return out

    def _getMemoryInfo(self) -> Dict[str, int]:
        mi = self._meminfo()
        total = mi.get("MemTotal", 0)
        free = mi.get("MemFree", 0)
        available = mi.get("MemAvailable", 0)
        used = total - free  # semplice, coerente con originale
        return {
            "ram_total_kb": total,
            "ram_used_kb": used,
            "ram_free_kb": free,
            "ram_available_kb": available,
        }

    # ---- Rete -------------------------------------------------------------

    def _getNetworkInfo(self) -> Dict[str, Any]:
        """
        Usa `ip -j addr show up` per interfacce UP (esclude lo) e
        counters da /sys/class/net/<if>/statistics.
        """
        out: Dict[str, Any] = {}
        raw = self._run_cmd(["ip", "-j", "addr", "show", "up"])
        if not raw:
            return out

        try:
            data = json.loads(raw)
        except Exception:
            self._logger.debug("ip -j parse failure")
            return out

        for iface in data:
            name = iface.get("ifname")
            if not name or name == "lo":
                continue

            net: Dict[str, Any] = {}

            # IPv4/IPv6
            for addr in iface.get("addr_info", []):
                fam = addr.get("family")
                if fam == "inet":
                    net["ip"] = addr.get("local")
                    net["mask"] = addr.get("prefixlen")  # prefixlen, non netmask
                    net["broadcast"] = addr.get("broadcast")
                elif fam == "inet6":
                    net["ip6"] = addr.get("local")

            # MAC
            mac = iface.get("address")
            if mac:
                net["mac"] = mac

            # RX/TX counters
            stats_path = os.path.join("/sys/class/net", name, "statistics")
            rx = {
                "packets": self._read_int(os.path.join(stats_path, "rx_packets")),
                "bytes": self._read_int(os.path.join(stats_path, "rx_bytes")),
                "errors": self._read_int(os.path.join(stats_path, "rx_errors")),
                "dropped": self._read_int(os.path.join(stats_path, "rx_dropped")),
                "overruns": self._read_int(os.path.join(stats_path, "rx_missed_errors")),  # approx
                "frame": self._read_int(os.path.join(stats_path, "rx_frame_errors")),
            }
            tx = {
                "packets": self._read_int(os.path.join(stats_path, "tx_packets")),
                "bytes": self._read_int(os.path.join(stats_path, "tx_bytes")),
                "errors": self._read_int(os.path.join(stats_path, "tx_errors")),
                "dropped": self._read_int(os.path.join(stats_path, "tx_dropped")),
                "overruns": self._read_int(os.path.join(stats_path, "tx_aborted_errors")),  # approx
                "carrier": self._read_int(os.path.join(stats_path, "tx_carrier_errors")),
                "collisions": self._read_int(os.path.join(stats_path, "collisions")),
            }
            net["rx"] = rx
            net["tx"] = tx

            out[name] = net

        return out

    @staticmethod
    def _read_int(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return int(f.read().strip())
        except Exception:
            return 0

    # ---- CPU --------------------------------------------------------------

    def _getCPUInfo(self) -> Dict[str, int | float]:
        """
        Come l'originale: legge la riga 'cpu ' da /proc/stat (cumulative jiffies).
        Nota: la % idle qui rappresenta il rapporto cumulativo, non l'uso istantaneo.
        """
        local: Dict[str, int | float] = {}
        try:
            with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
                line = next((ln for ln in f if ln.startswith("cpu ")), "")
        except Exception:
            line = ""

        parts = line.split()
        #     cpu  user nice system idle iowait irq softirq steal guest guest_nice
        # idx:  0    1    2     3     4      5   6      7       8     9     10
        def gi(i: int) -> int:
            try:
                return int(parts[i])
            except Exception:
                return 0

        local["normal_processes_user_mode"] = gi(1)
        local["nice_processes_user_mode"] = gi(2)
        local["system_processes_kernel_mode"] = gi(3)
        local["idle_processes"] = gi(4)
        local["iowait_processes"] = gi(5)
        local["irq_processes"] = gi(6)
        local["softirq_processes"] = gi(7)
        local["steal_processes"] = gi(8)
        local["guest_processes"] = gi(9)
        local["guest_nice_processes"] = gi(10)

        total = (
            local["normal_processes_user_mode"]
            + local["nice_processes_user_mode"]
            + local["system_processes_kernel_mode"]
            + local["idle_processes"]
            + local["iowait_processes"]
            + local["irq_processes"]
            + local["softirq_processes"]
        )
        if total > 0:
            local["average_idle_percentage"] = round(local["idle_processes"] * 100.0 / total, 1)
        else:
            local["average_idle_percentage"] = 0.0

        return local
