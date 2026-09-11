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


def _latest_complete_period(db: dict) -> str:
    """Último mes calendario completo disponible en la base de ventas."""
    periods = set()
    for c in db.get("customers", []):
        monthly = c.get("monthlySalesByBusiness") or {}
        for values in monthly.values():
            if isinstance(values, dict):
                periods.update(
                    str(k) for k in values.keys()
                    if re.match(r"^\d{4}-\d{2}$", str(k))
                )
    now = datetime.now()
    current = f"{now.year:04d}-{now.month:02d}"
    completed = sorted(p for p in periods if p < current)
    if completed:
        return completed[-1]
    return sorted(periods)[-1] if periods else ""


def _monthly_hl(customer: dict, business: str, period: str) -> float:
    monthly = customer.get("monthlySalesByBusiness") or {}
    values = monthly.get(business) or {}
    try:
        return float(values.get(period) or 0)
    except Exception:
        return 0.0


def _snapshot_from_db(db: dict) -> dict:
    """Crea snapshot de repago usando el último mes completo y asignación por EDF.

    Replica la lógica de reparto del dashboard original, pero reemplaza
    salesByBusiness (acumulado) por monthlySalesByBusiness[último mes completo].
    """
    period = _latest_complete_period(db)
    customer_db = {}
    customers = {}
    for c in db.get("customers", []):
        cid = _norm_code(c.get("id"))
        if not cid:
            continue
        customer_db[cid] = c
        monthly_by_business = {}
        monthly_total = 0.0
        for business in ("CZA", "UNG", "AGUAS", "RB", "OTROS"):
            value = _monthly_hl(c, business, period) if period else 0.0
            monthly_by_business[business] = round(value, 4)
            monthly_total += value
        customers[cid] = {
            "id": cid,
            "name": c.get("fantasyName") or c.get("name") or c.get("legalName") or "",
            "legal_name": c.get("legalName") or "",
            "seller": c.get("seller") or "",
            "route": c.get("route") or "",
            "monthly_period": period,
            "monthly_hl": round(monthly_total, 4),
            "monthly_by_business": monthly_by_business,
        }

    # Agrupar EDF colocados por cliente + unidad de negocio para repartir el volumen
    # del mes en proporción al objetivo de cada equipo, igual que el dashboard base.
    raw_rows = []
    grouped_targets = {}
    for edf in db.get("edfs", []):
        cid = _norm_code(edf.get("customerId"))
        if not cid:
            continue
        repayment = edf.get("repayment") or {}
        business = _business(edf.get("business") or "")
        target = float(repayment.get("target") or 0)
        status = str(edf.get("status") or "")
        row = {
            "cid": cid,
            "asset": edf.get("asset") or edf.get("serial") or "—",
            "model": edf.get("model") or "—",
            "business": business,
            "status": status,
            "target": target,
            "hl_source_accumulated": float(repayment.get("hl") or 0),
            "pct_source_accumulated": float(repayment.get("pct") or 0),
        }
        raw_rows.append(row)
        if status == "PDV":
            grouped_targets[(cid, business)] = grouped_targets.get((cid, business), 0.0) + target

    rows_by_customer = {}
    for raw in raw_rows:
        cid = raw["cid"]
        business = raw["business"]
        target = raw["target"]
        available = float((customers.get(cid, {}).get("monthly_by_business") or {}).get(business) or 0)
        total_target = float(grouped_targets.get((cid, business)) or 0)

        if raw.get("status") == "PDV" and target > 0 and total_target > 0:
            assigned = min(target, available * (target / total_target))
            pct_month = (assigned / target) * 100.0
        else:
            assigned = 0.0
            pct_month = 0.0

        rows_by_customer.setdefault(cid, []).append({
            "asset": raw["asset"],
            "model": raw["model"],
            "business": business,
            "status": raw.get("status"),
            "hl": round(assigned, 4),
            "business_month_hl": round(available, 4),
            "business_target_hl": round(total_target, 4),
            "hl_period": period,
            "target": target,
            "pct": pct_month,
            "band": "Repaga" if pct_month >= 100 else "75% o más" if pct_month >= 75 else "No repaga",
            "hl_source_accumulated": raw["hl_source_accumulated"],
            "pct_source_accumulated": raw["pct_source_accumulated"],
        })

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repago_period": period,
        "source": "Repagos EDF · último mes completo · snapshot local",
        "customers": customers,
        "rows_by_customer": rows_by_customer,
    }


def _prepare_module():
    module = load_remote_module(REPO, PATH, force=False)
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
    if snap:
        return {
            "ok": True,
            "name": "Repagos EDF",
            "detail": f"{len(snap.get('customers', {}))} clientes · repago mes {snap.get('repago_period') or '—'}",
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

