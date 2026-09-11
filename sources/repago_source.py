from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import re
import threading
import time

from .remote_module import load_remote_module

REPO = "tomasddv/repagos"
PATH = "edf_importer.py"
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = CACHE_DIR / "repago_snapshot.json"
PROCESS_DATA_DIR = ROOT / "data" / f"repago-runtime-{os.getpid()}"

_snapshot = None
_last_error = None
_lock = threading.RLock()


def _norm_code(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.lstrip("0") or ("0" if text else "")


def _business(value) -> str:
    text = str(value or "").upper()
    if "RED BULL" in text or text.strip() == "RB":
        return "RB"
    if "AGUA" in text or "ECO" in text:
        return "AGUAS"
    if "CERV" in text or "CMQ" in text or "CZA" in text:
        return "CZA"
    if "UNG" in text or "GASE" in text or "ISOT" in text or "ENERG" in text:
        return "UNG"
    return text.strip() or "—"


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


def _available_periods(db: dict) -> list[str]:
    periods = set()
    for c in db.get("customers", []):
        monthly = c.get("monthlySalesByBusiness") or {}
        for values in monthly.values():
            if isinstance(values, dict):
                periods.update(
                    str(k) for k in values.keys()
                    if re.match(r"^\d{4}-\d{2}$", str(k))
                )
    return sorted(periods)


def _latest_complete_period(db: dict) -> str:
    """Último mes calendario completo para responder consultas de compra mensual."""
    periods = _available_periods(db)
    now = datetime.now()
    current = f"{now.year:04d}-{now.month:02d}"
    completed = [p for p in periods if p < current]
    if completed:
        return completed[-1]
    return periods[-1] if periods else ""


def _repago_periods(db: dict) -> list[str]:
    """Mismo período predeterminado del dashboard original: últimos 3 períodos disponibles."""
    periods = _available_periods(db)
    return periods[-3:]


def _monthly_hl(customer: dict, business: str, period: str) -> float:
    monthly = customer.get("monthlySalesByBusiness") or {}
    values = monthly.get(business) or {}
    try:
        return float(values.get(period) or 0)
    except Exception:
        return 0.0


def _average_hl(customer: dict, business: str, periods: list[str]) -> float:
    if not periods:
        return 0.0
    total = sum(_monthly_hl(customer, business, p) for p in periods)
    return round(total / len(periods), 4)


def _band(pct_value: float, hl: float) -> str:
    if hl <= 0:
        return "Venta 0"
    if pct_value < 25:
        return "0%-25%"
    if pct_value < 50:
        return "25%-50%"
    if pct_value < 75:
        return "50%-75%"
    if pct_value < 100:
        return "75%-99%"
    return "100%+"


def _norm_serial(value) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _snapshot_from_db(db: dict) -> dict:
    """Snapshot alineado con el dashboard original de Repago.

    Repago predeterminado = promedio mensual de los últimos 3 períodos disponibles,
    repartido de forma SECUENCIAL entre EDF del mismo cliente + negocio, ordenados
    por activo/serie. Esto replica period_repayment_map(..., average=True) del repo
    tomasddv/repagos.
    """
    monthly_period = _latest_complete_period(db)
    repago_periods = _repago_periods(db)

    customer_db = {}
    customers = {}
    for c in db.get("customers", []):
        cid = _norm_code(c.get("id"))
        if not cid:
            continue
        customer_db[cid] = c

        monthly_by_business = {}
        monthly_total = 0.0
        repago_avg_by_business = {}
        for business in ("CZA", "UNG", "AGUAS", "RB", "OTROS"):
            month_value = _monthly_hl(c, business, monthly_period) if monthly_period else 0.0
            monthly_by_business[business] = round(month_value, 4)
            monthly_total += month_value
            repago_avg_by_business[business] = _average_hl(c, business, repago_periods)

        customers[cid] = {
            "id": cid,
            "name": c.get("fantasyName") or c.get("name") or c.get("legalName") or "",
            "legal_name": c.get("legalName") or "",
            "seller": c.get("seller") or "",
            "route": c.get("route") or "",
            "address": c.get("address") or "",
            "city": c.get("city") or "",
            "monthly_period": monthly_period,
            "monthly_hl": round(monthly_total, 4),
            "monthly_by_business": monthly_by_business,
            "repago_periods": repago_periods,
            "repago_avg_by_business": repago_avg_by_business,
        }

    # Todos los EDF para poder consultar una serie aunque no esté colocada en un cliente.
    all_edfs = []
    raw_by_group = {}
    for edf in db.get("edfs", []):
        cid = _norm_code(edf.get("customerId"))
        customer = customers.get(cid) or {}
        repayment = edf.get("repayment") or {}
        business = _business(edf.get("business") or "")
        target = float(repayment.get("target") or 0)
        status = str(edf.get("status") or "")
        serial = str(edf.get("serial") or "").strip()
        asset = str(edf.get("asset") or "").strip()

        location_row = {
            "asset": asset or "—",
            "serial": serial,
            "model": edf.get("model") or "—",
            "business": business,
            "status": status,
            "deposit": edf.get("deposit") or "",
            "customer_id": cid or "",
            "customer_name": customer.get("name") or "",
            "legal_name": customer.get("legal_name") or "",
            "address": customer.get("address") or "",
            "city": customer.get("city") or "",
        }
        all_edfs.append(location_row)

        if not cid:
            continue
        row = {
            **location_row,
            "cid": cid,
            "target": target,
        }
        raw_by_group.setdefault((cid, business), []).append(row)

    # Repago secuencial, igual al commit actual del repo original.
    rows_by_customer = {}
    for (cid, business), group in raw_by_group.items():
        group.sort(key=lambda r: (r.get("asset") or "", r.get("serial") or ""))
        available_avg = float((customers.get(cid, {}).get("repago_avg_by_business") or {}).get(business) or 0)
        remaining = available_avg

        for raw in group:
            target = float(raw.get("target") or 0)
            if raw.get("status") == "PDV" and target > 0:
                assigned = min(target, max(remaining, 0))
                remaining -= assigned
                pct_value = round((assigned / target) * 100) if target else 0
            else:
                assigned = 0.0
                pct_value = 0

            rows_by_customer.setdefault(cid, []).append({
                "asset": raw["asset"],
                "serial": raw.get("serial") or "",
                "model": raw["model"],
                "business": business,
                "status": raw.get("status"),
                "deposit": raw.get("deposit") or "",
                "hl": round(assigned, 4),
                "business_avg_hl": round(available_avg, 4),
                "repago_periods": repago_periods,
                "target": target,
                "pct": pct_value,
                "band": _band(pct_value, assigned),
            })

    return {
        "schema_version": 2,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repago_periods": repago_periods,
        "monthly_period": monthly_period,
        "source": "Repagos EDF · trimestre promedio · secuencial · snapshot local",
        "customers": customers,
        "rows_by_customer": rows_by_customer,
        "all_edfs": all_edfs,
    }

def _prepare_module():
    module = load_remote_module(REPO, PATH, force=True)
    source_dir = PROCESS_DATA_DIR / "drive-source"
    db_path = PROCESS_DATA_DIR / "db.json"
    manifest_path = source_dir / "_sync_manifest.json"
    module.ROOT = PROCESS_DATA_DIR
    module.SOURCE_DIR = source_dir
    module.DB_PATH = db_path
    module.SYNC_MANIFEST_PATH = manifest_path
    PROCESS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return module


def refresh(force=True):
    """Actualización pesada explícita. Las consultas nunca llaman a esta función."""
    global _last_error
    with _lock:
        module = _prepare_module()
        last_exc = None
        for attempt in range(2):
            try:
                db = module.import_data(sync=True)
                snap = _snapshot_from_db(db)
                _last_error = None
                return _save(snap)
            except PermissionError as exc:
                last_exc = exc
                time.sleep(2)
            except Exception as exc:
                last_exc = exc
                break
        _last_error = last_exc
        raise last_exc


def status():
    snap = _load_disk()
    if snap and int(snap.get("schema_version") or 0) >= 2:
        return {
            "ok": True,
            "name": "Repagos EDF",
            "detail": f"{len(snap.get('customers', {}))} clientes · repago trim. promedio {', '.join(snap.get('repago_periods') or []) or '—'}",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if snap:
        return {
            "ok": None,
            "name": "Repagos EDF",
            "detail": "Actualizando snapshot al cálculo secuencial de trimestre promedio...",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Repagos EDF", "detail": str(_last_error), "loaded_at": "—"}
    return {"ok": None, "name": "Repagos EDF", "detail": "Sin snapshot. Se actualizará automáticamente.", "loaded_at": "—"}


def customer(customer_id: str):
    snap = _load_disk()
    if not snap:
        return None
    return snap.get("customers", {}).get(_norm_code(customer_id))


def customer_repayments(customer_id: str):
    snap = _load_disk()
    if not snap:
        return []
    return list(snap.get("rows_by_customer", {}).get(_norm_code(customer_id), []))


def _norm_text(value) -> str:
    import unicodedata
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def search_customers(text: str, limit: int = 8):
    snap = _load_disk()
    if not snap:
        return []
    q = _norm_text(text)
    if not q:
        return []
    exact_code = _norm_code(text) if str(text or "").strip().isdigit() else ""
    scored = []
    tokens = [t for t in q.split() if t]
    for cid, c in snap.get("customers", {}).items():
        fantasy = _norm_text(c.get("name", ""))
        legal = _norm_text(c.get("legal_name", ""))
        hay = _norm_text(f"{cid} {fantasy} {legal}")
        score = 0
        if exact_code and cid == exact_code:
            score = 120
        elif q == fantasy or q == legal:
            score = 100
        elif q in fantasy or q in legal:
            score = 85
        elif tokens and all(t in hay for t in tokens):
            score = 65
        if score:
            scored.append((score, int(cid) if cid.isdigit() else 999999999, cid, c))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [c for _, _, _, c in scored[:limit]]


def edf_by_serial(serial: str):
    """Busca un EDF por número de serie exacto, tolerando espacios y guiones."""
    snap = _load_disk()
    if not snap:
        return None
    key = _norm_serial(serial)
    if not key:
        return None
    for row in snap.get("all_edfs", []):
        if _norm_serial(row.get("serial")) == key:
            return dict(row)
    # Compatibilidad: snapshots anteriores podían guardar serie dentro de rows_by_customer.
    for cid, rows in (snap.get("rows_by_customer") or {}).items():
        for row in rows or []:
            if _norm_serial(row.get("serial")) == key:
                customer_info = snap.get("customers", {}).get(cid) or {}
                return {
                    **row,
                    "customer_id": cid,
                    "customer_name": customer_info.get("name") or "",
                    "legal_name": customer_info.get("legal_name") or "",
                    "address": customer_info.get("address") or "",
                    "city": customer_info.get("city") or "",
                }
    return None


def search_edf_serial(serial: str, limit: int = 8):
    snap = _load_disk()
    if not snap:
        return []
    key = _norm_serial(serial)
    if not key:
        return []
    out = []
    for row in snap.get("all_edfs", []):
        serial_key = _norm_serial(row.get("serial"))
        if key in serial_key:
            out.append(dict(row))
            if len(out) >= limit:
                break
    return out


_load_disk()

def customer_edfs(customer_id: str):
    """Todos los EDF/heladeras asignados al cliente, sin forzar una lectura de repago."""
    snap = _load_disk()
    if not snap:
        return []
    rows = list(snap.get("rows_by_customer", {}).get(_norm_code(customer_id), []))
    # Evita dobles conteos si una fuente repitiera el mismo activo/serie.
    unique = []
    seen = set()
    for row in rows:
        asset = str(row.get("asset") or "").strip()
        model = str(row.get("model") or "").strip()
        key = (asset, model) if asset and asset != "—" else (asset, model, len(unique))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def count_customer_edfs(customer_id: str) -> int:
    return len(customer_edfs(customer_id))

