#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# unittest.sh — Controlli automatici per platform-monitor-2-mqtt
#
# Uso:
#   ./script/unittest.sh
#
# Esecuzione di un singolo modulo di test:
#   ./script/unittest.sh tests.test_monitor
#
# Esecuzione di un singolo test:
#   ./script/unittest.sh \
#       tests.test_speedtest_mon.SpeedtestMonTests.test_collect_is_asynchronous_and_completes
#
# Variabili opzionali:
#   PYTHON_BIN=/path/python
#   SKIP_GIT_CHECK=1
#   SKIP_CONFIG_CHECK=1
#   SKIP_COMPILE_CHECK=1
#   NO_COLOR=1
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

PYTHON_BIN="${PYTHON_BIN:-}"
SKIP_GIT_CHECK="${SKIP_GIT_CHECK:-0}"
SKIP_CONFIG_CHECK="${SKIP_CONFIG_CHECK:-0}"
SKIP_COMPILE_CHECK="${SKIP_COMPILE_CHECK:-0}"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD=""
    GREEN=""
    YELLOW=""
    RED=""
    RESET=""
fi

step() {
    printf '\n%s==> %s%s\n' "${BOLD}" "$1" "${RESET}"
}

ok() {
    printf '%sOK%s - %s\n' "${GREEN}" "${RESET}" "$1"
}

warn() {
    printf '%sATTENZIONE%s - %s\n' \
        "${YELLOW}" "${RESET}" "$1" >&2
}

fail() {
    printf '%sERRORE%s - %s\n' \
        "${RED}" "${RESET}" "$1" >&2
    exit 1
}

on_error() {
    local exit_code=$?

    printf '\n%sTest interrotti alla riga %s (exit code %s).%s\n' \
        "${RED}" \
        "${BASH_LINENO[0]:-?}" \
        "${exit_code}" \
        "${RESET}" >&2

    exit "${exit_code}"
}

trap on_error ERR

usage() {
    cat <<'USAGE'
Uso:
  ./script/unittest.sh

  ./script/unittest.sh tests.test_monitor

  ./script/unittest.sh \
      tests.test_speedtest_mon.SpeedtestMonTests.test_collect_is_asynchronous_and_completes

Senza argomenti viene eseguita la discovery completa dei test presenti
nella directory tests/.

Gli eventuali argomenti vengono passati a:

  python -m unittest -v

Variabili d'ambiente:

  PYTHON_BIN=/path/python
      Interprete Python da utilizzare.

  SKIP_GIT_CHECK=1
      Salta il controllo degli errori di whitespace nel diff Git.

  SKIP_CONFIG_CHECK=1
      Salta la validazione del template monitor.dist.

  SKIP_COMPILE_CHECK=1
      Salta il controllo sintattico tramite compileall.

  NO_COLOR=1
      Disabilita i colori nell'output.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

# ---------------------------------------------------------------------------
# Individuazione dell'interprete
# ---------------------------------------------------------------------------

step "Individuazione dell'ambiente Python"

if [[ -z "${PYTHON_BIN}" ]]; then
    if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
        PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    else
        fail "Python 3 non trovato. Crea .venv oppure imposta PYTHON_BIN."
    fi
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    fail "Interprete Python non eseguibile: ${PYTHON_BIN}"
fi

"${PYTHON_BIN}" - <<'PY'
import sys

minimum = (3, 10)

if sys.version_info < minimum:
    raise SystemExit(
        f"Richiesto Python {minimum[0]}.{minimum[1]}+, "
        f"trovato {sys.version.split()[0]}"
    )

print(f"Python {sys.version.split()[0]} ({sys.executable})")
PY

ok "Versione Python compatibile"

# ---------------------------------------------------------------------------
# Dipendenze
# ---------------------------------------------------------------------------

step "Verifica delle dipendenze runtime"

"${PYTHON_BIN}" - <<'PY'
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

dependencies = {
    "paho.mqtt.client": "paho-mqtt",
    "psutil": "psutil",
    "sdnotify": "sdnotify",
    "dns": "dnspython",
}

missing = []
versions = {}

for module_name, package_name in dependencies.items():
    try:
        import_module(module_name)

        try:
            installed_version = version(package_name)
        except PackageNotFoundError:
            installed_version = "versione sconosciuta"

        versions[package_name] = installed_version
        print(f"{package_name}: {installed_version}")

    except ImportError as exc:
        missing.append(f"{package_name} ({exc})")

if missing:
    raise SystemExit(
        "Dipendenze mancanti: "
        + ", ".join(missing)
        + ". Eseguire: .venv/bin/pip install -r requirements.txt"
    )

paho_version = versions.get("paho-mqtt", "0")
try:
    paho_major = int(paho_version.split(".", maxsplit=1)[0])
except ValueError:
    paho_major = 0

if paho_major < 2:
    raise SystemExit(
        f"È richiesto paho-mqtt 2.x; versione trovata: {paho_version}"
    )
PY

ok "Dipendenze disponibili"

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

if [[ "${SKIP_GIT_CHECK}" != "1" ]]; then
    step "Controllo formattazione del diff Git"

    if command -v git >/dev/null 2>&1 \
        && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then

        # Modifiche non ancora in staging.
        git diff --check

        # Modifiche già aggiunte allo staging.
        git diff --cached --check

        ok "Nessun errore di whitespace nel diff"
    else
        warn "Directory non gestita da Git: controllo saltato"
    fi
fi

# ---------------------------------------------------------------------------
# Sintassi Python
# ---------------------------------------------------------------------------

if [[ "${SKIP_COMPILE_CHECK}" != "1" ]]; then
    step "Controllo sintattico Python"

    "${PYTHON_BIN}" -m compileall -q \
        platform_monitor_2_mqtt.py \
        mods \
        test

    ok "Compilazione sintattica completata"
fi

# ---------------------------------------------------------------------------
# Script shell
# ---------------------------------------------------------------------------

if [[ "${SKIP_COMPILE_CHECK}" != "1" ]]; then
    step "Controllo sintattico degli script shell"

    for shell_script in scripts/install.sh scripts/uninstall.sh; do
        bash -n "${shell_script}"
    done

    if command -v shellcheck >/dev/null 2>&1; then
        shellcheck -S warning scripts/install.sh scripts/uninstall.sh
        ok "bash -n e shellcheck superati"
    else
        warn "shellcheck non installato: eseguito solo bash -n"
        ok "bash -n superato"
    fi
fi

# ---------------------------------------------------------------------------
# Validazione monitor.dist
# ---------------------------------------------------------------------------

if [[ "${SKIP_CONFIG_CHECK}" != "1" ]]; then
    step "Validazione del template monitor.dist"

    "${PYTHON_BIN}" - "${PROJECT_ROOT}/monitor.dist" <<'PY'
from __future__ import annotations

import importlib
import sys
from configparser import ConfigParser, Error
from pathlib import Path

from mods.base_module import BaseModule


config_path = Path(sys.argv[1])

if not config_path.is_file():
    raise SystemExit(f"File non trovato: {config_path}")

config = ConfigParser(
    delimiters=("=",),
    inline_comment_prefixes=("#",),
    interpolation=None,
)
config.optionxform = str

try:
    loaded_files = config.read(config_path, encoding="utf-8")
except Error as exc:
    raise SystemExit(f"INI non valido: {exc}") from exc

if not loaded_files:
    raise SystemExit(f"Impossibile leggere {config_path}")

required_sections = {
    "General",
    "Modules",
    "MQTT",
    "MQTT topic",
    "Daemon",
    "Speedtest",
    "DnsMonitor",
    "DnsMonitor probes",
    "Logger sessions",
    "loggers",
    "handlers",
    "formatters",
    "logger_root",
    "logger_platformMonitor",
    "handler_consoleHandler",
    "formatter_simpleFormatter",
}

missing_sections = sorted(
    required_sections.difference(config.sections())
)

if missing_sections:
    raise SystemExit(
        "Sezioni mancanti in monitor.dist: "
        + ", ".join(missing_sections)
    )

for module_key, declaration in config["Modules"].items():
    parts = [
        part.strip()
        for part in declaration.split(",", maxsplit=1)
    ]

    if len(parts) != 2 or not all(parts):
        raise SystemExit(
            "Dichiarazione modulo non valida: "
            f"{module_key} = {declaration!r}"
        )

    module_name, class_name = parts

    try:
        module = importlib.import_module(f"mods.{module_name}")
    except ImportError as exc:
        raise SystemExit(
            f"Impossibile importare mods/{module_name}.py: {exc}"
        ) from exc

    module_class = getattr(module, class_name, None)

    if module_class is None:
        raise SystemExit(
            f"Classe {class_name!r} non trovata "
            f"in mods/{module_name}.py"
        )

    if not isinstance(module_class, type):
        raise SystemExit(
            f"{class_name!r} in mods/{module_name}.py "
            "non è una classe"
        )

    if not issubclass(module_class, BaseModule):
        raise SystemExit(
            f"La classe {class_name!r} non estende BaseModule"
        )

print(
    f"monitor.dist valido: {len(config.sections())} sezioni, "
    f"{len(config['Modules'])} moduli attivi"
)
PY

    ok "Template di configurazione valido"
fi

# ---------------------------------------------------------------------------
# Unit test
# ---------------------------------------------------------------------------

step "Esecuzione degli unit test"

if (( $# > 0 )); then
    "${PYTHON_BIN}" -m unittest -v "$@"
else
    "${PYTHON_BIN}" -m unittest discover \
        --start-directory test \
        --pattern 'test_*.py' \
        --verbose
fi

printf '\n%sTutti i controlli sono terminati con successo.%s\n' \
    "${GREEN}${BOLD}" \
    "${RESET}"
