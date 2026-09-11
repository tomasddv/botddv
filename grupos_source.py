from __future__ import annotations

from datetime import datetime
from io import StringIO
from pathlib import Path
import json
import re
import threading
import unicodedata

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = CACHE_DIR / "grupos_clientes_snapshot.json"

CLIENTS_URL = (
    "https://raw.githubusercontent.com/tomasddv/grupo-de-clientes-/main/"
    "trade_spend_dashboard/data/processed/client_percentages.csv"
)
GROUPS_URL = (
    "https://raw.githubusercontent.com/tomasddv/grupo-de-clientes-/main/"
    "trade_spend_dashboard/data/processed/group_summary.csv"
)

_snapshot = None
_last_error = None
_lock = threading.RLock()


def _norm_text(value: object) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _norm_code(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits.lstrip("0") or ("0" if digits else "")


def _load_disk():
    global _snapshot
    if _snapshot is None and SNAPSHOT_PATH.exists():
        try:
            _snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            _snapshot = None
    return _snapshot


def _save(snapshot: dict):
    global _snapshot
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SNAPSHOT_PATH)
    _snapshot = snapshot
    return snapshot


def _download_csv(url: str) -> pd.DataFrame:
    response = requests.get(
        url,
        timeout=25,
        headers={"Cache-Control": "no-cache", "User-Agent": "DDV-fast-assistant/1.0"},
    )
    response.raise_for_status()
    return pd.read_csv(StringIO(response.text))


def _build_snapshot(clients: pd.DataFrame, groups: pd.DataFrame) -> dict:
    if clients.empty:
        raise RuntimeError("client_percentages.csv está vacío")

    clients = clients.copy()
    clients.columns = [str(c).strip() for c in clients.columns]
    if "cliente" not in clients.columns:
        raise RuntimeError("client_percentages.csv no contiene la columna cliente")

    rows_by_customer: dict[str, list[dict]] = {}
    customers: dict[str, dict] = {}

    for row in clients.to_dict("records"):
        cid = _norm_code(row.get("cliente"))
        if not cid:
            continue
        segmento = str(row.get("segmento") or "").strip().upper()
        subsegmento = str(row.get("subsegmento") or "").strip().upper()
        try:
            porcentaje = float(pd.to_numeric(row.get("porcentaje_total"), errors="coerce"))
            if pd.isna(porcentaje):
                porcentaje = 0.0
        except Exception:
            porcentaje = 0.0

        item = {
            "segmento": segmento,
            "subsegmento": subsegmento,
            "porcentaje_total": porcentaje,
            "grupos": str(row.get("grupos") or "").strip(),
            "fantasia": str(row.get("fantasia") or "").strip(),
            "promotor": str(row.get("promotor") or "").strip(),
            "ruta": str(row.get("ruta") or "").strip(),
            "origen": str(row.get("origen") or "").strip(),
            "fecha": str(row.get("fecha") or "").strip(),
        }
        rows_by_customer.setdefault(cid, []).append(item)
        if cid not in customers:
            customers[cid] = {
                "id": cid,
                "name": item["fantasia"],
                "promotor": item["promotor"],
                "ruta": item["ruta"],
            }
        else:
            if not customers[cid].get("name") and item["fantasia"]:
                customers[cid]["name"] = item["fantasia"]

    group_rows = []
    if not groups.empty:
        groups = groups.copy()
        groups.columns = [str(c).strip() for c in groups.columns]
        for row in groups.to_dict("records"):
            group_rows.append({
                "segmento": str(row.get("segmento") or "").strip().upper(),
                "accion_id": str(row.get("accion_id") or "").strip(),
                "promo_compania": str(row.get("promo_compania") or "").strip(),
                "descripcion": str(row.get("descripcion") or "").strip(),
                "grupo": str(row.get("grupo") or "").strip(),
                "fecha": str(row.get("fecha") or "").strip(),
            })

    latest = ""
    if "fecha" in clients.columns:
        try:
            latest = str(clients["fecha"].dropna().astype(str).max())
        except Exception:
            latest = ""

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": latest,
        "source": "Grupo de clientes · GitHub snapshot local",
        "customers": customers,
        "rows_by_customer": rows_by_customer,
        "groups": group_rows,
    }


def refresh(force=True):
    """Actualiza el snapshot desde GitHub. Las consultas nunca llaman a esta función."""
    global _last_error
    with _lock:
        try:
            clients = _download_csv(CLIENTS_URL)
            groups = _download_csv(GROUPS_URL)
            snapshot = _build_snapshot(clients, groups)
            _last_error = None
            return _save(snapshot)
        except Exception as exc:
            _last_error = exc
            raise


def status():
    snap = _load_disk()
    if snap:
        data_date = snap.get("data_date") or "—"
        return {
            "ok": True,
            "name": "Grupo de clientes",
            "detail": f"{len(snap.get('customers', {}))} clientes · datos {data_date}",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Grupo de clientes", "detail": str(_last_error), "loaded_at": "—"}
    return {
        "ok": None,
        "name": "Grupo de clientes",
        "detail": "Sin snapshot. Se actualizará automáticamente.",
        "loaded_at": "—",
    }


def customer(customer_id: str):
    snap = _load_disk()
    if not snap:
        return None
    return snap.get("customers", {}).get(_norm_code(customer_id))


def discounts(customer_id: str, segment: str | None = None):
    snap = _load_disk()
    if not snap:
        return []
    rows = list(snap.get("rows_by_customer", {}).get(_norm_code(customer_id), []))
    if not segment:
        return rows
    wanted = str(segment).strip().upper()
    if wanted == "CORE":
        return [r for r in rows if r.get("segmento") == "CORE"]
    if wanted in {"VALUE LITRO", "LITRO"}:
        return [r for r in rows if r.get("segmento") == "VALUE" and r.get("subsegmento") == "LITRO"]
    if wanted in {"VALUE LATA", "LATA"}:
        return [r for r in rows if r.get("segmento") == "VALUE" and r.get("subsegmento") == "LATA"]
    if wanted == "VALUE":
        return [r for r in rows if r.get("segmento") == "VALUE"]
    return [r for r in rows if r.get("segmento") == wanted]


def search_customers(text: str, limit: int = 8):
    snap = _load_disk()
    if not snap:
        return []
    q = _norm_text(text)
    if not q:
        return []

    exact_code = _norm_code(text) if str(text or "").strip().isdigit() else ""
    scored = []
    for cid, customer_row in snap.get("customers", {}).items():
        name = str(customer_row.get("name") or "")
        hay = _norm_text(f"{cid} {name}")
        score = 0
        if exact_code and cid == exact_code:
            score = 100
        elif q == _norm_text(name):
            score = 90
        elif q in _norm_text(name):
            score = 70
        elif all(token in hay for token in q.split()):
            score = 50
        if score:
            scored.append((score, cid, customer_row))
    scored.sort(key=lambda x: (-x[0], int(x[1]) if x[1].isdigit() else 999999999, x[1]))
    return [row for _, _, row in scored[:limit]]


def data_date():
    snap = _load_disk()
    return (snap or {}).get("data_date") or ""


_load_disk()
