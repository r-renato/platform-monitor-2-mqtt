#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# uninstall.sh — Rimuove platform-monitor-2-mqtt installato con install.sh
#
# Rimuove: servizio, unit, override generato, codice in /opt e virtualenv.
# Conserva: monitor.ini, env, credenziali cifrate, 90-local.conf e lo stato
# in /var/lib/platform-monitor, salvo --purge.
#
# Uso:
#   sudo ./scripts/uninstall.sh [--purge] [--prefix DIR] [--dry-run]
# ---------------------------------------------------------------------------

SERVICE_NAME="p-monitor-2-mqtt"
PREFIX=""
PURGE=0
DRY_RUN=0
ASSUME_YES=0

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step() { printf '\n%s==> %s%s\n' "${BOLD}" "$1" "${RESET}"; }
ok()   { printf '%sOK%s - %s\n' "${GREEN}" "${RESET}" "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '%sATTENZIONE%s - %s\n' "${YELLOW}" "${RESET}" "$1" >&2; }
fail() { printf '%sERRORE%s - %s\n' "${RED}" "${RESET}" "$1" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Uso:
  sudo ./scripts/uninstall.sh [opzioni]

Opzioni:
  --purge        rimuove anche /etc/platform-monitor (configurazione,
                 credenziali, override locali) e /var/lib/platform-monitor
  -y, --yes      non chiede conferma per --purge
  --prefix DIR   opera sotto DIR invece che nella radice (test)
  --dry-run      mostra le azioni senza eseguirle
  -h, --help     mostra questo aiuto
USAGE
}

while (( $# > 0 )); do
    case "$1" in
        --purge) PURGE=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        --prefix)
            (( $# >= 2 )) || fail "--prefix richiede un valore"
            PREFIX="${2%/}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Opzione sconosciuta: $1" ;;
    esac
    shift
done

APP_DIR="${PREFIX}/opt/platform-monitor-2-mqtt"
CONF_DIR="${PREFIX}/etc/platform-monitor"
STATE_DIR="${PREFIX}/var/lib/platform-monitor"
UNIT_DIR="${PREFIX}/etc/systemd/system"
UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"
DROPIN_DIR="${UNIT_DIR}/${SERVICE_NAME}.service.d"

run() {
    if (( DRY_RUN )); then
        printf '    [dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

use_systemd() { [[ -z "${PREFIX}" ]] && (( ! DRY_RUN )); }

if [[ -z "${PREFIX}" ]] && (( EUID != 0 )) && (( ! DRY_RUN )); then
    fail "Servono privilegi di root: eseguire con sudo (oppure usare --dry-run o --prefix)."
fi

if (( PURGE && ! ASSUME_YES && ! DRY_RUN )); then
    warn "--purge elimina configurazione, credenziali cifrate e stato."
    [[ -r /dev/tty ]] || fail "Nessun terminale per la conferma: usare --yes."
    read -r -p "    Digitare 'si' per confermare: " answer </dev/tty
    [[ "${answer}" == "si" ]] || fail "Operazione annullata."
fi

step "Servizio"
if use_systemd; then
    systemctl disable --now "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
fi
ok "Servizio fermato e disabilitato"

step "Unit systemd"
run rm -f "${UNIT_FILE}" "${DROPIN_DIR}/10-installer.conf"
if (( PURGE )); then
    run rm -rf "${DROPIN_DIR}"
else
    # Rimuove la directory solo se vuota: 90-local.conf viene conservato.
    run rmdir "${DROPIN_DIR}" 2>/dev/null || true
fi
if use_systemd; then
    systemctl daemon-reload
    systemctl reset-failed "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
fi
ok "Unit rimossa"

step "Codice e virtualenv"
run rm -rf "${APP_DIR}" "${APP_DIR}.new" "${APP_DIR}.old" "${CONF_DIR}/venv"
ok "Codice e virtualenv rimossi"

step "Configurazione e stato"
if (( PURGE )); then
    run rm -rf "${CONF_DIR}" "${STATE_DIR}"
    ok "Configurazione, credenziali e stato rimossi"
else
    info "Conservati: ${CONF_DIR#"${PREFIX}"} e ${STATE_DIR#"${PREFIX}"}."
    info "Per eliminarli: ./scripts/uninstall.sh --purge"
fi

printf '\n%sDisinstallazione terminata.%s\n' "${GREEN}${BOLD}" "${RESET}"
