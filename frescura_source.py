from __future__ import annotations

from datetime import datetime, date
from pathlib import Path
import json
import re
import shutil
import threading
import unicodedata

import numpy as np
import pandas as pd

from forecast_engine import (
    load_history_base,
    load_current_bultos,
    combine_history,
    load_supermarket_dispatches,
    build_weekday_profiles,
    build_supermarket_weekday_profiles,
    combine_normal_and_supermarket_profiles,
    simulate_fefo,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = CACHE_DIR / "frescura_snapshot.json"
HISTORY_SEED = DATA_DIR / "historico_frescura_bultos.csv.gz"
SUPER_HISTORY_SEED = DATA_DIR / "historico_supermercados_bultos.csv.gz"
HISTORY_RUNTIME = CACHE_DIR / "historico_frescura_runtime.csv.gz"
SUPER_HISTORY_RUNTIME = CACHE_DIR / "historico_supermercados_runtime.csv.gz"
RUNTIME_DIR = DATA_DIR / "frescura-runtime"
DRIVE_URL = "https://drive.google.com/drive/folders/1cukgXLUaPsEDK_yD7tSwgaBFZAbiDUot"

_snapshot = None
_last_error = None
_lock = threading.RLock()


def _clean(value) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _code(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.lstrip("0") or ("0" if text else "")


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


def _parse_template(path: Path, city: str):
    df = pd.read_excel(path, header=1)
    products = []
    lots = []
    for _, row in df.iterrows():
        if len(row) < 12 or pd.isna(row.iloc[0]):
            continue
        code = _code(row.iloc[0])
        if not code:
            continue
        desc = "" if pd.isna(row.iloc[1]) else str(row.iloc[1])
        products.append({
            "ciudad": city,
            "codigo": code,
            "descripcion": desc,
            "politica_stock_dias": float(pd.to_numeric(row.iloc[2], errors="coerce") or 0),
            "stock_total": float(pd.to_numeric(row.iloc[5], errors="coerce") or 0),
            "venta_promedio": float(pd.to_numeric(row.iloc[6], errors="coerce") or 0),
        })
        for lot_no, (stock_i, date_i) in enumerate(((10, 11), (16, 17), (22, 23)), start=1):
            if len(row) <= date_i:
                continue
            stock = pd.to_numeric(row.iloc[stock_i], errors="coerce")
            expiry = pd.to_datetime(row.iloc[date_i], errors="coerce")
            if pd.notna(stock) and float(stock) > 0 and pd.notna(expiry):
                lots.append({
                    "ciudad": city,
                    "codigo": code,
                    "descripcion": desc,
                    "lote_nro": lot_no,
                    "stock_lote": float(stock),
                    "fecha_vencimiento": expiry,
                })
    return pd.DataFrame(products), pd.DataFrame(lots)


def _drive_items():
    import gdown
    return gdown.download_folder(
        url=DRIVE_URL, output=".", quiet=True, use_cookies=False, skip_download=True
    ) or []


def _pick(items, required_words, suffixes):
    found = []
    for item in items:
        name = Path(str(item.path)).name
        c = _clean(name)
        if all(w in c for w in required_words) and Path(name).suffix.lower() in suffixes:
            found.append(item)
    if not found:
        return None
    return sorted(found, key=lambda x: Path(str(x.path)).name.lower())[-1]


def _download(item, folder: Path):
    import gdown
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(str(item.path)).name
    target = folder / name
    tmp = folder / f"{name}.part"
    tmp.unlink(missing_ok=True)
    result = gdown.download(id=item.id, output=str(tmp), quiet=True, use_cookies=False)
    if not result or not tmp.exists() or tmp.stat().st_size <= 0:
        raise RuntimeError(f"No se pudo descargar {name}")
    target.unlink(missing_ok=True)
    tmp.replace(target)
    return target


def _json_value(v):
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (np.floating, float)):
        if not np.isfinite(v):
            return None
        return float(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    return v


def _records(df: pd.DataFrame):
    out = []
    for rec in df.to_dict("records"):
        out.append({k: _json_value(v) for k, v in rec.items()})
    return out


def _build_snapshot(trelew_path: Path, madryn_path: Path, customer_path: Path,
                    current_path: Path, supermarket_path: Path | None):
    p1, l1 = _parse_template(trelew_path, "Trelew")
    p2, l2 = _parse_template(madryn_path, "Madryn")
    products = pd.concat([p1, p2], ignore_index=True)
    lots = pd.concat([l1, l2], ignore_index=True)
    wanted = set(products["codigo"].astype(str).str.lstrip("0"))

    hist_path = HISTORY_RUNTIME if HISTORY_RUNTIME.exists() else HISTORY_SEED
    history = load_history_base(hist_path) if hist_path.exists() else pd.DataFrame(columns=["date","loc","sku","bultos"])
    current = load_current_bultos(current_path, customer_path, wanted)
    normal_daily = combine_history(history, current)
    normal_daily.to_csv(HISTORY_RUNTIME, index=False, compression="gzip")

    super_hist_path = SUPER_HISTORY_RUNTIME if SUPER_HISTORY_RUNTIME.exists() else SUPER_HISTORY_SEED
    super_history = load_history_base(super_hist_path) if super_hist_path.exists() else pd.DataFrame(columns=["date","loc","sku","bultos"])
    if supermarket_path and supermarket_path.exists():
        super_live = load_supermarket_dispatches(supermarket_path, wanted)
        supermarket_daily = combine_history(super_history, super_live)
    else:
        supermarket_daily = super_history
    supermarket_daily.to_csv(SUPER_HISTORY_RUNTIME, index=False, compression="gzip")

    source_dates = []
    if not current.empty:
        source_dates.append(current["date"].max().date())
    if not supermarket_daily.empty:
        source_dates.append(supermarket_daily["date"].max().date())
    if not source_dates and not normal_daily.empty:
        source_dates.append(normal_daily["date"].max().date())
    as_of = max(source_dates) if source_dates else datetime.now().date()
    local_today = pd.Timestamp.now(tz="America/Argentina/Buenos_Aires").date()
    as_of = min(as_of, local_today)

    normal_ddv = build_weekday_profiles(normal_daily, products, as_of, recent_occurrences=2, scope="DDV")
    super_ddv = build_supermarket_weekday_profiles(supermarket_daily, products, as_of, recent_occurrences=8, scope="DDV")
    profiles_ddv = combine_normal_and_supermarket_profiles(normal_ddv, super_ddv, as_of)
    forecast_ddv = simulate_fefo(lots, profiles_ddv, as_of, scope="DDV")

    normal_base = build_weekday_profiles(normal_daily, products, as_of, recent_occurrences=2, scope="BASE")
    super_base = build_supermarket_weekday_profiles(supermarket_daily, products, as_of, recent_occurrences=8, scope="BASE")
    profiles_base = combine_normal_and_supermarket_profiles(normal_base, super_base, as_of)
    forecast_base = simulate_fefo(lots, profiles_base, as_of, scope="BASE")

    product_map = {}
    for sku, grp in products.groupby("codigo"):
        product_map[str(sku)] = {
            "codigo": str(sku),
            "descripcion": str(grp["descripcion"].dropna().iloc[0]) if not grp["descripcion"].dropna().empty else str(sku),
            "stock_total_ddv": float(grp["stock_total"].sum()),
            "stock_trelew": float(grp.loc[grp["ciudad"].eq("Trelew"), "stock_total"].sum()),
            "stock_madryn": float(grp.loc[grp["ciudad"].eq("Madryn"), "stock_total"].sum()),
        }

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": as_of.isoformat(),
        "source": "Frescura Predictiva v9 · snapshot local",
        "products": product_map,
        "forecast_ddv": _records(forecast_ddv),
        "forecast_base": _records(forecast_base),
        "profiles_ddv": _records(profiles_ddv),
        "profiles_base": _records(profiles_base),
    }


def refresh(force=True):
    """Actualización pesada explícita. Las consultas nunca descargan ni recalculan."""
    global _last_error
    with _lock:
        try:
            items = _drive_items()
            tw = _pick(items, ("plantillafrescura", "trelew"), (".xlsx", ".xls"))
            pm = _pick(items, ("plantillafrescura", "madryn"), (".xlsx", ".xls"))
            customers = _pick(items, ("plantillaclientesar",), (".xlsx", ".xls"))
            current = _pick(items, ("ventadiaria", "bultos"), (".txt",))
            supermarket = _pick(items, ("reportecomprobantesdetallado",), (".xlsx", ".xls"))
            missing = [name for name, item in [
                ("Frescura Trelew", tw), ("Frescura Madryn", pm),
                ("PlantillaClientesAR", customers), ("ventadiaria bultos", current)
            ] if item is None]
            if missing:
                raise RuntimeError("Faltan fuentes en Drive: " + ", ".join(missing))

            if RUNTIME_DIR.exists():
                shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            tw_path = _download(tw, RUNTIME_DIR)
            pm_path = _download(pm, RUNTIME_DIR)
            customer_path = _download(customers, RUNTIME_DIR)
            current_path = _download(current, RUNTIME_DIR)
            super_path = _download(supermarket, RUNTIME_DIR) if supermarket else None
            snap = _build_snapshot(tw_path, pm_path, customer_path, current_path, super_path)
            _last_error = None
            return _save(snap)
        except Exception as exc:
            _last_error = exc
            raise


def status():
    snap = _load_disk()
    if snap:
        return {
            "ok": True,
            "name": "Frescura",
            "detail": f"{len(snap.get('products', {}))} SKU · cálculo al {snap.get('as_of','—')} · consulta local instantánea",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Frescura", "detail": str(_last_error), "loaded_at": "—"}
    return {"ok": None, "name": "Frescura", "detail": "Sin snapshot. Se actualizará automáticamente.", "loaded_at": "—"}


def product(code: str):
    snap = _load_disk()
    if not snap:
        return None
    return snap.get("products", {}).get(_code(code))


def search_products(text: str, limit: int = 8):
    snap = _load_disk()
    if not snap:
        return []
    q = _clean(text)
    if not q:
        return []
    out = []
    for sku, p in snap.get("products", {}).items():
        hay = _clean(f"{sku} {p.get('descripcion','')}")
        if q in hay:
            out.append(p)
            if len(out) >= limit:
                break
    return out


def forecast(code: str, scope: str = "DDV"):
    snap = _load_disk()
    if not snap:
        return []
    code = _code(code)
    scope = str(scope or "DDV").upper()
    if scope == "DDV":
        rows = snap.get("forecast_ddv", [])
        return [r for r in rows if _code(r.get("codigo")) == code]
    rows = snap.get("forecast_base", [])
    return [r for r in rows if _code(r.get("codigo")) == code and str(r.get("ciudad","")).upper() == scope]


def profile(code: str, scope: str = "DDV"):
    snap = _load_disk()
    if not snap:
        return None
    code = _code(code)
    scope = str(scope or "DDV").upper()
    rows = snap.get("profiles_ddv", []) if scope == "DDV" else snap.get("profiles_base", [])
    for r in rows:
        if _code(r.get("sku")) != code:
            continue
        if scope == "DDV" or str(r.get("loc", "")).upper() == scope:
            return r
    return None


def risk_list(scope: str = "DDV", state: str | None = None, limit: int = 15):
    snap = _load_disk()
    if not snap:
        return []
    scope = str(scope or "DDV").upper()
    if scope == "DDV":
        rows = list(snap.get("forecast_ddv", []))
    else:
        rows = [r for r in snap.get("forecast_base", []) if str(r.get("ciudad","")).upper() == scope]
    rows = [r for r in rows if float(r.get("stock_lote") or 0) >= 1.0]
    if state:
        rows = [r for r in rows if str(r.get("estado_predictivo", "")).upper() == state.upper()]
    else:
        rows = [r for r in rows if float(r.get("bultos_riesgo") or 0) > 0.05]
    rows.sort(key=lambda r: (-float(r.get("bultos_riesgo") or 0), str(r.get("fecha_vencimiento") or "")))
    return rows[:limit]


_load_disk()
