"""
base_module.py — Classe base per tutti i moduli di monitoraggio.

Fornisce:
  - _run_cmd()   esecuzione sicura di sottoprocessi (no shell=True, no string interpolation)
  - _read_file() lettura sicura di file di sistema (es. /proc, /sys)
  - collect()    interfaccia astratta da implementare nei moduli
  - getData()    interfaccia astratta da implementare nei moduli
  - is_available() flag di disponibilità del modulo sull'host corrente
"""

from __future__ import annotations

import logging
import subprocess
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseModule(ABC):
    """
    Classe base astratta per i moduli di monitoraggio.

    Ogni modulo concreto deve implementare:
      - collect()  — raccoglie i dati aggiornati e li salva internamente
      - getData()  — restituisce i dati raccolti come dizionario serializzabile in JSON

    Convenzioni:
      - _run_cmd() NON usa shell=True. Il comando deve essere una lista di stringhe.
        Questo elimina qualsiasi rischio di command injection, indipendentemente
        da cosa contengano i dati letti dal sistema.
      - Se un comando non è disponibile o fallisce, il modulo registra l'errore
        nel logger e restituisce un dict parziale anziché propagare l'eccezione.
      - I moduli che dipendono da tool opzionali (vcgencmd, docker, keepalived)
        devono impostare self._available = False nel __init__ se il tool non è
        presente, e restituire {} da getData() in quel caso.
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._logger = logging.getLogger("platformMonitor")
        self._data: dict = {}
        self._available: bool = True

    # ------------------------------------------------------------------
    # Interfaccia pubblica — da implementare nei moduli concreti
    # ------------------------------------------------------------------

    @abstractmethod
    def collect(self) -> None:
        """
        Raccoglie i dati aggiornati dal sistema e li salva in self._data.
        Deve essere idempotente: chiamate successive sovrascrivono i dati
        precedenti senza accumulare stato.
        Non deve propagare eccezioni: cattura, logga e restituisce dati parziali.
        """

    @abstractmethod
    def getData(self) -> dict:
        """
        Restituisce i dati raccolti dall'ultima chiamata a collect().
        Deve restituire un dizionario serializzabile in JSON.
        Se il modulo non è disponibile sull'host, restituisce {}.
        """

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Attende eventuali attività asincrone del modulo.

        I moduli sincroni sono sempre inattivi; i collector con worker dedicati
        possono sovrascrivere il metodo.
        """
        return True

    def close(self) -> None:
        """Rilascia risorse e interrompe eventuali worker del modulo."""
        return None

    @property
    def is_available(self) -> bool:
        """True se il modulo è operativo sull'host corrente."""
        return self._available

    # ------------------------------------------------------------------
    # Utilità protette — usate dai moduli concreti
    # ------------------------------------------------------------------

    def _run_cmd(
        self,
        args: list[str],
        timeout: int = 10,
        check: bool = False,
    ) -> str:
        """
        Esegue un sottoprocesso in modo sicuro e restituisce stdout come stringa.

        Args:
            args:    Lista di stringhe [comando, arg1, arg2, ...].
                     MAI una stringa singola con shell=True.
                     MAI costruita con .format() o f-string da dati esterni.
            timeout: Secondi prima di terminare il processo (default 10).
            check:   Se True, solleva CalledProcessError su returncode != 0.

        Returns:
            stdout del processo, stripped. Stringa vuota in caso di errore
            (se check=False).

        Raises:
            subprocess.CalledProcessError: solo se check=True e returncode != 0.

        Esempio corretto:
            self._run_cmd(["/sbin/ifconfig", iface_name])  # iface_name non viene interpretata

        Esempio SBAGLIATO (non fare mai):
            subprocess.Popen(f"ifconfig {iface_name}", shell=True, ...)
        """
        if not args:
            self._logger.warning("_run_cmd() chiamato con lista vuota")
            return ""

        try:
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=check,
                text=True,
            )
            if result.returncode != 0:
                self._logger.debug(
                    "_run_cmd() returncode=%d cmd=%s stderr=%s",
                    result.returncode,
                    args[0],
                    result.stderr.strip(),
                )
            return result.stdout.strip()

        except FileNotFoundError:
            self._logger.debug("_run_cmd() comando non trovato: %s", args[0])
            return ""
        except subprocess.TimeoutExpired:
            self._logger.warning("_run_cmd() timeout (%ds): %s", timeout, args[0])
            return ""
        except subprocess.CalledProcessError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error("_run_cmd() errore inatteso cmd=%s: %s", args[0], exc)
            return ""

    def _read_file(self, path: str | Path, default: str = "") -> str:
        """
        Legge un file di testo (tipicamente /proc/* o /sys/*) in modo sicuro.

        Preferire questo metodo a subprocess per la lettura di pseudo-file del
        kernel: è più veloce (niente fork), non ha rischi di injection, e
        gestisce correttamente i file /proc che terminano con \\x00.

        Args:
            path:    Percorso del file da leggere.
            default: Valore restituito se il file non esiste o non è leggibile.

        Returns:
            Contenuto del file stripped, oppure default.
        """
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            return content.strip().replace("\x00", "")
        except (OSError, PermissionError) as exc:
            self._logger.debug("_read_file() %s: %s", path, exc)
            return default

    def _which(self, command: str) -> str | None:
        """
        Restituisce il percorso assoluto di un eseguibile, oppure None.
        Wrapper su shutil.which() per leggibilità nei moduli.

        Esempio:
            docker_path = self._which("docker")
            if docker_path is None:
                self._available = False
                return
        """
        return shutil.which(command)
