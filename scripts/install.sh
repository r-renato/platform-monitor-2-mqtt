#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# install.sh — Installa o aggiorna platform-monitor-2-mqtt come servizio systemd
#
# Principio: il codice di progetto non viene mai modificato; tutto ciò che è
# personale vive in /etc/platform-monitor/.
#
#   /opt/platform-monitor-2-mqtt/        codice (sostituito a ogni esecuzione)
#   /etc/platform-monitor/monitor.ini    configurazione (creata se assente,
#                                        mai sovrascritta)
#   /etc/platform-monitor/env            variabili d'ambiente (opzione
#                                        EnvironmentFile, creata vuota)
#   /etc/platform-monitor/credentials/   credenziali cifrate (systemd-creds)
#   /etc/platform-monitor/venv/          virtualenv Python
#   /etc/systemd/system/p-monitor-2-mqtt.service            unit del progetto
#   /etc/systemd/system/p-monitor-2-mqtt.service.d/10-installer.conf
#                                        override generato (non modificare)
#   /etc/systemd/system/p-monitor-2-mqtt.service.d/90-local.conf
#                                        override personali (mai toccato)
#
# Lo script è idempotente: rieseguirlo equivale ad aggiornare.
#
# Uso:
#   sudo ./scripts/install.sh [opzioni]
#   Vedere --help.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

SERVICE_NAME="p-monitor-2-mqtt"
SERVICE_GROUP="daemon"

PREFIX=""
# Il servizio gira con ProtectHome=true: l'interprete del virtualenv non può
# stare sotto /root o /home (pyenv, uv, ecc.). Per default si usa quello di
# sistema, non il primo "python3" del PATH.
if [[ -z "${PYTHON_BIN:-}" ]]; then
    if [[ -x /usr/bin/python3 ]]; then
        PYTHON_BIN=/usr/bin/python3
    else
        PYTHON_BIN=python3
    fi
fi
DRY_RUN=0
ENABLE_NOW=0
SKIP_PIP=0
SKIP_CREDENTIALS=0
RESET_CREDENTIALS=0
PASSWORD_STDIN=0
MQTT_USER_ARG=""

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
  sudo ./scripts/install.sh [opzioni]

Opzioni:
  --enable-now           abilita e avvia (o riavvia) il servizio al termine
  --mqtt-user NOME       username MQTT da cifrare (altrimenti viene richiesto)
  --password-stdin       legge la password MQTT dallo standard input
  --reset-credentials    ricrea le credenziali anche se già presenti
  --skip-credentials     non gestire le credenziali cifrate
  --skip-pip             non installare requirements.txt nel virtualenv
  --prefix DIR           installa sotto DIR invece che nella radice (test)
  --dry-run              mostra le azioni senza eseguirle
  -h, --help             mostra questo aiuto

Variabili d'ambiente:
  PYTHON_BIN=/path/python   interprete per creare il virtualenv (≥ 3.10);
                            default /usr/bin/python3. Non può stare in /root
                            o /home: il servizio non lo vedrebbe.
  NO_COLOR=1                disabilita i colori

La password non va mai passata come argomento: viene richiesta senza eco
oppure letta da stdin con --password-stdin.
USAGE
}

while (( $# > 0 )); do
    case "$1" in
        --enable-now) ENABLE_NOW=1 ;;
        --mqtt-user)
            (( $# >= 2 )) || fail "--mqtt-user richiede un valore"
            MQTT_USER_ARG="$2"; shift ;;
        --password-stdin) PASSWORD_STDIN=1 ;;
        --reset-credentials) RESET_CREDENTIALS=1 ;;
        --skip-credentials) SKIP_CREDENTIALS=1 ;;
        --skip-pip) SKIP_PIP=1 ;;
        --prefix)
            (( $# >= 2 )) || fail "--prefix richiede un valore"
            PREFIX="${2%/}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Opzione sconosciuta: $1" ;;
    esac
    shift
done

# Percorsi "reali" (scritti nei file di configurazione) e percorsi "fisici"
# (dove lo script scrive davvero: coincidono salvo --prefix).
APP_DIR_REAL="/opt/platform-monitor-2-mqtt"
CONF_DIR_REAL="/etc/platform-monitor"
UNIT_DIR_REAL="/etc/systemd/system"

APP_DIR="${PREFIX}${APP_DIR_REAL}"
CONF_DIR="${PREFIX}${CONF_DIR_REAL}"
UNIT_DIR="${PREFIX}${UNIT_DIR_REAL}"
VENV_DIR="${CONF_DIR}/venv"
CRED_DIR="${CONF_DIR}/credentials"
DROPIN_DIR="${UNIT_DIR}/${SERVICE_NAME}.service.d"

MANAGED_DROPIN="${DROPIN_DIR}/10-installer.conf"
UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"

CRED_NAMES=(mqtt_username mqtt_password)

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

systemd_version() {
    local v
    v="$(systemctl --version 2>/dev/null | awk 'NR==1 {print $2}')" || true
    [[ "${v}" =~ ^[0-9]+$ ]] && echo "${v}" || echo 0
}

group_exists() { getent group "${SERVICE_GROUP}" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Controlli preliminari
# ---------------------------------------------------------------------------

step "Controlli preliminari"

if [[ -z "${PREFIX}" ]] && (( EUID != 0 )) && (( ! DRY_RUN )); then
    fail "Servono privilegi di root: eseguire con sudo (oppure usare --dry-run o --prefix)."
fi

[[ -f "${SOURCE_DIR}/platform_monitor_2_mqtt.py" ]] \
    || fail "Sorgenti non trovati in ${SOURCE_DIR}"
[[ -f "${SOURCE_DIR}/requirements.txt" ]] \
    || fail "requirements.txt non trovato in ${SOURCE_DIR}"

command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
    || fail "Python non trovato (${PYTHON_BIN}). Impostare PYTHON_BIN."

"${PYTHON_BIN}" - <<'PY' || fail "Richiesto Python 3.10 o superiore."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
info "Python: $("${PYTHON_BIN}" --version 2>&1)"

# Vero percorso di un eseguibile, seguendo i link simbolici.
real_path() { readlink -f "$(command -v "$1" 2>/dev/null || echo "$1")" 2>/dev/null || true; }

# L'unit usa ProtectHome=true: un interprete sotto /root o /home è invisibile
# al servizio, che fallirebbe con status 203/EXEC.
in_home() {
    case "$(real_path "$1")" in
        /root/*|/home/*) return 0 ;;
        *) return 1 ;;
    esac
}

if in_home "${PYTHON_BIN}"; then
    fail "L'interprete $(real_path "${PYTHON_BIN}") sta nella home: il servizio (ProtectHome=true) non potrebbe avviarlo. Usare PYTHON_BIN=/usr/bin/python3."
fi

if ! "${PYTHON_BIN}" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    fail "Modulo venv/ensurepip assente. Su Debian/Proxmox: apt install python3-venv"
fi

HAVE_CREDS_TOOL=0
if (( SKIP_CREDENTIALS == 0 )); then
    if command -v systemd-creds >/dev/null 2>&1 \
        && (( $(systemd_version) >= 250 )); then
        HAVE_CREDS_TOOL=1
        info "systemd-creds disponibile (systemd $(systemd_version))"
    else
        warn "systemd-creds non disponibile (serve systemd ≥ 250): credenziali cifrate saltate."
        warn "Usare in alternativa ${CONF_DIR_REAL}/env (EnvironmentFile)."
    fi
fi

ok "Controlli superati"

# ---------------------------------------------------------------------------
# Codice di progetto
# ---------------------------------------------------------------------------

step "Installazione del codice in ${APP_DIR_REAL}"

if [[ "$(readlink -f "${SOURCE_DIR}")" == "$(readlink -f "${APP_DIR}" 2>/dev/null || true)" ]]; then
    info "Esecuzione già dalla directory di installazione: copia non necessaria."
else
    STAGING="${APP_DIR}.new"
    OLD="${APP_DIR}.old"
    run rm -rf "${STAGING}" "${OLD}"
    run mkdir -p "${STAGING}"
    if (( DRY_RUN )); then
        info "[dry-run] copia di ${SOURCE_DIR} in ${STAGING} (esclusi .git, test, venv, cache)"
    else
        (
            cd "${SOURCE_DIR}"
            tar --exclude='./.git' --exclude='./.venv' --exclude='./venv' \
                --exclude='./test' --exclude='./store' --exclude='./monitor.ini' \
                --exclude='__pycache__' --exclude='*.pyc' --exclude='*.zip' \
                -cf - .
        ) | tar -xf - -C "${STAGING}"
    fi
    if [[ -d "${APP_DIR}" ]]; then
        run mv "${APP_DIR}" "${OLD}"
    fi
    if run mv "${STAGING}" "${APP_DIR}"; then
        run rm -rf "${OLD}"
    else
        [[ -d "${OLD}" ]] && run mv "${OLD}" "${APP_DIR}"
        fail "Copia del codice non riuscita: versione precedente ripristinata."
    fi
    if group_exists && [[ -z "${PREFIX}" ]]; then
        run chown -R "root:${SERVICE_GROUP}" "${APP_DIR}"
    fi
    run chmod -R go-w,o-rwx "${APP_DIR}"
fi

ok "Codice installato"

# ---------------------------------------------------------------------------
# Directory di configurazione
# ---------------------------------------------------------------------------

step "Configurazione in ${CONF_DIR_REAL}"

run install -d -m 750 "${CONF_DIR}"
if group_exists; then
    run chown "root:${SERVICE_GROUP}" "${CONF_DIR}"
fi

if [[ -e "${CONF_DIR}/monitor.ini" ]]; then
    info "monitor.ini esistente: lasciato invariato."
else
    run install -m 640 "${SOURCE_DIR}/monitor.dist" "${CONF_DIR}/monitor.ini"
    group_exists && run chown "root:${SERVICE_GROUP}" "${CONF_DIR}/monitor.ini"
    info "monitor.ini creato da monitor.dist: va personalizzato."
fi

if [[ -e "${CONF_DIR}/env" ]]; then
    info "env esistente: lasciato invariato."
else
    run install -m 640 /dev/null "${CONF_DIR}/env"
    group_exists && run chown "root:${SERVICE_GROUP}" "${CONF_DIR}/env"
    info "env creato vuoto (MQTT_USERNAME / MQTT_PASSWORD opzionali)."
fi

# Le credenziali cifrate le legge solo systemd (root): nessun accesso al servizio.
run install -d -m 700 -o root -g root "${CRED_DIR}"

ok "Configurazione pronta"

# ---------------------------------------------------------------------------
# Virtualenv
# ---------------------------------------------------------------------------

step "Virtualenv in ${VENV_DIR}"

if [[ -e "${VENV_DIR}" || -L "${VENV_DIR}/bin/python" ]]; then
    if [[ -x "${VENV_DIR}/bin/python" ]] && ! in_home "${VENV_DIR}/bin/python"; then
        info "Virtualenv esistente: riutilizzato."
    else
        warn "Virtualenv esistente non utilizzabile dal servizio (interprete mancante o nella home): viene ricreato."
        run rm -rf "${VENV_DIR}"
    fi
fi
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    run "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
if (( ! DRY_RUN )); then
    [[ -x "${VENV_DIR}/bin/python" ]] || fail "Creazione del virtualenv non riuscita."
    if in_home "${VENV_DIR}/bin/python"; then
        fail "Il virtualenv punta a $(real_path "${VENV_DIR}/bin/python"), nella home: il servizio non potrebbe avviarlo."
    fi
fi

if (( SKIP_PIP )); then
    info "Installazione dipendenze saltata (--skip-pip)."
else
    run "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check \
        -r "${APP_DIR}/requirements.txt" \
        || fail "pip install fallito. Verificare la rete e i pacchetti di build (python3-dev)."
fi

if group_exists; then
    run chown -R "root:${SERVICE_GROUP}" "${VENV_DIR}"
fi
run chmod -R go-w,o-rwx "${VENV_DIR}"

ok "Virtualenv pronto"

# ---------------------------------------------------------------------------
# Credenziali cifrate
# ---------------------------------------------------------------------------

encrypt_credential() {
    local name="$1" value="$2"
    local target="${CRED_DIR}/${name}.cred"
    if (( DRY_RUN )); then
        info "[dry-run] cifratura di ${name} in ${target}"
        return 0
    fi
    # Il valore passa da stdin: non compare in argv né nell'ambiente.
    printf '%s' "${value}" \
        | systemd-creds encrypt --name="${name}" - "${target}" \
        || fail "Cifratura di ${name} non riuscita."
    chmod 600 "${target}"
}

have_all_credentials() {
    local name
    for name in "${CRED_NAMES[@]}"; do
        [[ -f "${CRED_DIR}/${name}.cred" ]] || return 1
    done
    return 0
}

if (( SKIP_CREDENTIALS )); then
    step "Credenziali cifrate"
    info "Saltate (--skip-credentials)."
elif (( HAVE_CREDS_TOOL )); then
    step "Credenziali cifrate (systemd-creds)"

    if have_all_credentials && (( RESET_CREDENTIALS == 0 )); then
        info "Credenziali già presenti: invariate (usare --reset-credentials per ricrearle)."
    else
        mqtt_user="${MQTT_USER_ARG}"
        mqtt_pass=""

        if (( PASSWORD_STDIN )); then
            [[ -n "${mqtt_user}" ]] || fail "Con --password-stdin serve anche --mqtt-user."
            IFS= read -r mqtt_pass || true
        elif (( DRY_RUN )); then
            info "[dry-run] verrebbero richiesti username e password MQTT."
        elif [[ -r /dev/tty ]]; then
            if [[ -z "${mqtt_user}" ]]; then
                read -r -p "    Username MQTT: " mqtt_user </dev/tty
            fi
            read -r -s -p "    Password MQTT (nessun eco): " mqtt_pass </dev/tty
            printf '\n'
        else
            warn "Nessun terminale: credenziali non create. Rieseguire con --mqtt-user e --password-stdin."
        fi

        if (( DRY_RUN )); then
            info "[dry-run] cifratura di ${CRED_NAMES[*]}"
        elif [[ -n "${mqtt_user}" && -n "${mqtt_pass}" ]]; then
            if [[ ! -e /var/lib/systemd/credential.secret ]] && [[ -z "${PREFIX}" ]]; then
                systemd-creds setup >/dev/null 2>&1 || true
            fi
            encrypt_credential mqtt_username "${mqtt_user}"
            encrypt_credential mqtt_password "${mqtt_pass}"
            info "Credenziali cifrate in ${CONF_DIR_REAL}/credentials."
        else
            warn "Username o password vuoti: credenziali non create."
        fi
        unset mqtt_pass
    fi
    ok "Gestione credenziali completata"
fi

# ---------------------------------------------------------------------------
# Unit systemd e override
# ---------------------------------------------------------------------------

step "Unit systemd"

run install -d -m 755 "${UNIT_DIR}" "${DROPIN_DIR}"
if [[ -L "${UNIT_FILE}" ]]; then
    # Installazioni precedenti usavano un link simbolico verso /opt.
    run rm -f "${UNIT_FILE}"
fi
run install -m 644 "${SOURCE_DIR}/p-monitor-2-mqtt.service" "${UNIT_FILE}"

if (( DRY_RUN )); then
    info "[dry-run] scrittura di ${MANAGED_DROPIN}"
else
    {
        echo "# Generato da scripts/install.sh — NON modificare."
        echo "# Per personalizzazioni usare 90-local.conf nella stessa directory."
        echo "[Service]"
        echo "EnvironmentFile=-${CONF_DIR_REAL}/env"
        for name in "${CRED_NAMES[@]}"; do
            if [[ -f "${CRED_DIR}/${name}.cred" ]]; then
                echo "LoadCredentialEncrypted=${name}:${CONF_DIR_REAL}/credentials/${name}.cred"
            fi
        done
    } > "${MANAGED_DROPIN}"
    chmod 644 "${MANAGED_DROPIN}"
fi

if [[ -e "${DROPIN_DIR}/90-local.conf" ]]; then
    info "90-local.conf presente: lasciato invariato."
fi

if use_systemd; then
    systemctl daemon-reload
fi

ok "Unit installata"

# ---------------------------------------------------------------------------
# Avvio
# ---------------------------------------------------------------------------

step "Servizio"

if (( ENABLE_NOW )); then
    if use_systemd; then
        systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1
        systemctl restart "${SERVICE_NAME}.service"
        sleep 1
        systemctl --no-pager --lines=0 status "${SERVICE_NAME}.service" || true
        ok "Servizio abilitato e avviato"
    else
        info "Avvio saltato (modalità --prefix/--dry-run)."
    fi
else
    info "Servizio non avviato. Dopo aver controllato ${CONF_DIR_REAL}/monitor.ini:"
    info "  sudo systemctl enable --now ${SERVICE_NAME}.service"
    info "Se era già attivo, per applicare il nuovo codice:"
    info "  sudo systemctl restart ${SERVICE_NAME}.service"
fi

printf '\n%sInstallazione terminata.%s\n' "${GREEN}${BOLD}" "${RESET}"
