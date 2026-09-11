"""Availability and refresh health are independent; dates use Argentina time."""
from datetime import datetime, timedelta, timezone

AR = timezone(timedelta(hours=-3))


def stamp():
    return datetime.now(AR).isoformat(timespec="seconds")


def describe(name, snapshot, error=None, compatible=True, detail="", max_age_hours=24):
    available = bool(snapshot) and compatible
    updated = (snapshot or {}).get("updated_at")
    stale = False
    if updated:
        try:
            when = datetime.fromisoformat(updated)
            if when.tzinfo is None:
                when = when.replace(tzinfo=AR)
            stale = datetime.now(AR) - when > timedelta(hours=max_age_hours)
        except (TypeError, ValueError):
            stale = True
    elif available:
        stale = True
    warning = ""
    if error:
        warning = "Falló la actualización; se conserva la última copia disponible."
    if stale:
        warning += " Los datos tienen más de 24 horas o no tienen fecha verificable."
    return {
        "name": name, "ok": True if available else (False if error else None),
        "available": available, "loaded_at": updated or "—",
        "data_as_of": (snapshot or {}).get("data_date") or (snapshot or {}).get("as_of") or updated,
        "stale": stale, "refresh_error": str(error) if error else None,
        "warning": warning.strip(),
        "detail": detail if available else ("No se pudo preparar la fuente." if error else "Preparando datos compatibles en segundo plano."),
    }
