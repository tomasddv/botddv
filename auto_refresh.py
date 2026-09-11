from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any

from sources import repago_source, frescura_source, grupos_source

_INTERVAL_MINUTES = max(5, int(os.getenv("AUTO_REFRESH_MINUTES", "30") or 30))
_INTERVAL_SECONDS = _INTERVAL_MINUTES * 60

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_initial_attempted = False
_state: dict[str, Any] = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "next_run": None,
    "results": {},
}

_SOURCES = (
    ("Repagos / EDF", repago_source),
    ("Frescura", frescura_source),
    ("Grupo de clientes", grupos_source),
)


def interval_minutes() -> int:
    return _INTERVAL_MINUTES


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cycle() -> dict[str, str]:
    with _lock:
        if _state.get("running"):
            return dict(_state.get("results") or {})
        _state["running"] = True
        _state["last_started"] = _stamp()

    results: dict[str, str] = {}
    try:
        for label, module in _SOURCES:
            try:
                module.refresh(force=True)
                results[label] = "OK"
            except Exception as exc:
                results[label] = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            _state["running"] = False
            _state["last_finished"] = _stamp()
            _state["results"] = results
            _state["next_run"] = datetime.fromtimestamp(time.time() + _INTERVAL_SECONDS).strftime("%Y-%m-%d %H:%M:%S")
    return results


def ensure_initial_ready() -> dict[str, str]:
    """Carga automáticamente cualquier fuente que aún no tenga snapshot.

    Los snapshots empaquetados permiten responder al instante. Si una fuente falta,
    sólo esa fuente se actualiza de forma automática antes de habilitar su consulta.
    """
    global _initial_attempted
    with _lock:
        if _initial_attempted:
            return {}
        _initial_attempted = True

    results: dict[str, str] = {}
    for label, module in _SOURCES:
        try:
            status = module.status()
            if status.get("ok") is True:
                continue
            module.refresh(force=True)
            results[label] = "OK"
        except Exception as exc:
            results[label] = f"{type(exc).__name__}: {exc}"
    return results


def _worker() -> None:
    # Primera actualización completa poco después de arrancar. Mientras tanto,
    # las consultas usan el snapshot local empaquetado y siguen siendo instantáneas.
    if _stop_event.wait(2.0):
        return
    while not _stop_event.is_set():
        _cycle()
        if _stop_event.wait(_INTERVAL_SECONDS):
            break


def start_background_updater() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_worker, name="ddv-auto-refresh", daemon=True)
        _thread.start()


def force_refresh_async() -> bool:
    """Fallback opcional: dispara una actualización sin bloquear la interfaz."""
    with _lock:
        if _state.get("running"):
            return False
    threading.Thread(target=_cycle, name="ddv-force-refresh", daemon=True).start()
    return True


def state() -> dict[str, Any]:
    with _lock:
        out = dict(_state)
    out["interval_minutes"] = _INTERVAL_MINUTES
    return out
