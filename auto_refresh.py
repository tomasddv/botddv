from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from datetime import datetime
from typing import Any

from sources import repago_source, frescura_source, grupos_source, planificacion_source, ventas_actual_source

_INTERVAL_MINUTES = max(5, int(os.getenv("AUTO_REFRESH_MINUTES", "30") or 30))
_INTERVAL_SECONDS = _INTERVAL_MINUTES * 60
_SALES_RETRY_MINUTES = max(5, int(os.getenv("SALES_REFRESH_MINUTES", "5") or 5))
_SALES_RETRY_SECONDS = _SALES_RETRY_MINUTES * 60

_lock = threading.RLock()
_thread: threading.Thread | None = None
_sales_thread: threading.Thread | None = None
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
    ("Topes Planificación", planificacion_source),
    ("Venta mes actual", ventas_actual_source),
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
        with ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
            pending = {pool.submit(module.refresh, force=True): label for label, module in _SOURCES}
            for future in as_completed(pending):
                label = pending[future]
                try:
                    future.result()
                    results[label] = "OK"
                except Exception as exc:
                    results[label] = f"{type(exc).__name__}: {exc}"
                with _lock:
                    _state["results"] = dict(results)
    finally:
        with _lock:
            _state["running"] = False
            _state["last_finished"] = _stamp()
            _state["results"] = results
            _state["next_run"] = datetime.fromtimestamp(time.time() + _INTERVAL_SECONDS).strftime("%Y-%m-%d %H:%M:%S")
    return results


def ensure_initial_ready() -> dict[str, str]:
    """Start background refresh without delaying the first chat render."""
    start_background_updater()
    return {}


def _worker() -> None:
    if _stop_event.wait(2.0):
        return
    while not _stop_event.is_set():
        _cycle()
        if _stop_event.wait(_INTERVAL_SECONDS):
            break


def _sales_worker() -> None:
    """Después de las 16, reintenta Venta diaria cada 5 min hasta tener el corte del día."""
    if _stop_event.wait(8.0):
        return
    while not _stop_event.is_set():
        try:
            waiting = ventas_actual_source.needs_refresh()
        except Exception:
            waiting = True

        if waiting:
            try:
                ventas_actual_source.refresh(force=True)
                with _lock:
                    current = dict(_state.get("results") or {})
                    current["Venta mes actual"] = "OK"
                    _state["results"] = current
            except Exception as exc:
                with _lock:
                    current = dict(_state.get("results") or {})
                    current["Venta mes actual"] = f"{type(exc).__name__}: {exc}"
                    _state["results"] = current
            wait_seconds = _SALES_RETRY_SECONDS
        else:
            # Ya está al día: no castigar Drive; el ciclo general sigue cada 30 min.
            wait_seconds = _INTERVAL_SECONDS

        if _stop_event.wait(wait_seconds):
            break


def start_background_updater() -> None:
    global _thread, _sales_thread
    with _lock:
        _stop_event.clear()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="ddv-auto-refresh", daemon=True)
            _thread.start()
        if _sales_thread is None or not _sales_thread.is_alive():
            _sales_thread = threading.Thread(target=_sales_worker, name="ddv-sales-refresh", daemon=True)
            _sales_thread.start()


def force_refresh_async() -> bool:
    with _lock:
        if _state.get("running"):
            return False
    threading.Thread(target=_cycle, name="ddv-force-refresh", daemon=True).start()
    return True


def state() -> dict[str, Any]:
    with _lock:
        out = dict(_state)
    out["interval_minutes"] = _INTERVAL_MINUTES
    out["sales_retry_minutes"] = _SALES_RETRY_MINUTES
    try:
        out["sales_freshness"] = ventas_actual_source.freshness()
    except Exception:
        out["sales_freshness"] = {}
    return out
