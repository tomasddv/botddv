from __future__ import annotations

from datetime import datetime
from pathlib import Path
import gzip
import json
import re
import shutil
import threading
import unicodedata

import pandas as pd

from sources import ventas_actual_source

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = CACHE_DIR / "promotores_kpi_snapshot.json.gz"
RUNTIME_DIR = DATA_DIR / "promotores-kpi-runtime"

# Mismas fuentes usadas por promotores_kpi_dashboard del repo planificacion.
RUTAS_ID = "12REZlhQOVsQVIEIAKJ6mFSsrtNCSK7s8"
REPORTE_CLIENTES_ID = "1ZR9WOeqpaq9t-mJZM4f9AlUV7BIrKVo-"
PLANNER_SHEET_URL = "https://docs.google.com/spreadsheets/d/15ITRhsY5mvK3NSHeOKV2MymC078pT9TPAwKUdZDfjnI/export?format=xlsx"

SUPERVISORES = {
    "GASTON FABRE": "ISMAEL BRUNO",
    "MATIAS GARCIA": "ISMAEL BRUNO",
    "NICASTRO LUCAS": "ISMAEL BRUNO",
    "NICOLAS POCHETINO": "ISMAEL BRUNO",
    "SIRI MARTIN": "ISMAEL BRUNO",
    "VILLAGRA ENZO": "ISMAEL BRUNO",
    "PABLO ALVAREZ": "CASCO HERNAN",
    "ALEXANDER ROJAS": "CASCO HERNAN",
    "FERNANDO FIELG": "CASCO HERNAN",
    "JUAN MANUEL GIMENEZ": "CASCO HERNAN",
    "MENDEZ CARLOS": "CASCO HERNAN",
    "MARIANO HERRERA": "CASCO HERNAN",
    "FEDERICO BISS": "ISMAEL BRUNO",
}
DAY_GROUPS = {"LU": "LUJU", "JU": "LUJU", "MA": "MAVI", "VI": "MAVI", "MI": "MISA", "SA": "MISA", "DO": "DO"}

_snapshot = None
_last_error = None
_lock = threading.RLock()


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)


def _norm(value) -> str:
    text = unicodedata.normalize("NFD", _clean(value).lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _code(value) -> str:
    text = _clean(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _load_disk():
    global _snapshot
    if _snapshot is None and SNAPSHOT_PATH.exists():
        try:
            with gzip.open(SNAPSHOT_PATH, "rt", encoding="utf-8") as fh:
                _snapshot = json.load(fh)
        except Exception:
            _snapshot = None
    return _snapshot


def _save(snapshot: dict):
    global _snapshot
    tmp = SNAPSHOT_PATH.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(SNAPSHOT_PATH)
    _snapshot = snapshot
    return snapshot


def _download_by_id(file_id: str, target: Path):
    import gdown
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.unlink(missing_ok=True)
    result = gdown.download(id=file_id, output=str(tmp), quiet=True, use_cookies=False)
    if not result or not tmp.exists() or tmp.stat().st_size <= 0:
        raise RuntimeError(f"No se pudo descargar {target.name}")
    tmp.replace(target)
    return target


def _first_existing_column(df: pd.DataFrame, patterns: tuple[str, ...]):
    normalized = {str(col).strip().upper(): col for col in df.columns}
    for pattern in patterns:
        p = pattern.upper()
        for name, col in normalized.items():
            if p in name:
                return col
    return None


def _excel_col_index(label: str) -> int:
    index = 0
    for char in label.upper():
        if char.isalpha():
            index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _route_flags(value) -> dict[str, str]:
    text = _clean(value).upper()
    flags = {"LU": "", "MA": "", "MI": "", "JU": "", "VI": "", "SA": "", "DO": ""}
    if not text:
        return flags
    replacements = {
        "LUNES": "LU", "LUN": "LU", "MARTES": "MA", "MAR": "MA",
        "MIERCOLES": "MI", "MIÉRCOLES": "MI", "MIE": "MI",
        "JUEVES": "JU", "JUE": "JU", "VIERNES": "VI", "VIE": "VI",
        "SABADO": "SA", "SÁBADO": "SA", "SAB": "SA", "DOMINGO": "DO", "DOM": "DO",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    for token in ("LUJU", "MAVI", "MISA"):
        if token in text:
            for day, group in DAY_GROUPS.items():
                if group == token:
                    flags[day] = "X"
    for day in flags:
        if re.search(rf"(^|[^A-Z]){day}([^A-Z]|$)", text):
            flags[day] = "X"
    return flags


def _sales_promoter_lookup() -> dict[str, tuple[str, str]]:
    snap = ventas_actual_source._load_disk() or {}
    out: dict[str, tuple[str, str]] = {}
    for row in snap.get("rows") or []:
        code = _code(row.get("vendedor_codigo"))
        promotor = _clean(row.get("vendedor"))
        supervisor = _clean(row.get("supervisor")) or SUPERVISORES.get(promotor.upper(), "OTROS")
        if code and promotor and code not in out:
            out[code] = (promotor, supervisor)
    return out


def _customer_lookup() -> dict[str, str]:
    snap = ventas_actual_source._load_disk() or {}
    out = {}
    for row in snap.get("customer_master") or []:
        cid = _code(row.get("cliente_codigo"))
        name = _clean(row.get("nombre_fantasia")) or _clean(row.get("razon_social"))
        if cid and name:
            out[cid] = name
    return out


def _rows_from_reporte(path: Path) -> list[dict]:
    try:
        df = pd.read_excel(path, dtype=str)
    except Exception:
        return []
    if df.empty:
        return []
    promotor_col = df.columns[_excel_col_index("CG")] if len(df.columns) > _excel_col_index("CG") else _first_existing_column(df, ("PROMOTOR", "VENDEDOR"))
    dia_col = df.columns[_excel_col_index("CH")] if len(df.columns) > _excel_col_index("CH") else _first_existing_column(df, ("DIA", "DÍA", "VISITA"))
    cliente_col = _first_existing_column(df, ("COD. CLIENTE", "COD CLIENTE", "CODIGO CLIENTE", "CÓDIGO CLIENTE", "CLIENTE"))
    razon_col = _first_existing_column(df, ("RAZON SOCIAL", "RAZÓN SOCIAL", "NOMBRE DE FANTASIA", "DESCRIPCION", "DESCRIPCIÓN"))
    ruta_col = _first_existing_column(df, ("RUTA DE VENTA", "RUTA"))
    alta_col = _first_existing_column(df, ("ALTA FECHA", "FECHA DE ALTA"))
    if promotor_col is None or dia_col is None or cliente_col is None:
        return []
    customers = _customer_lookup()
    rows = []
    for _, row in df.iterrows():
        cid = _code(row.get(cliente_col))
        promotor = _clean(row.get(promotor_col))
        if not cid or not promotor:
            continue
        flags = _route_flags(row.get(dia_col))
        for day, active in flags.items():
            if active != "X":
                continue
            name = _clean(row.get(razon_col)) if razon_col is not None else ""
            rows.append({
                "vendedor_codigo": "",
                "promotor": promotor,
                "supervisor": SUPERVISORES.get(promotor.upper(), "OTROS"),
                "ruta": _code(row.get(ruta_col)) if ruta_col is not None else "",
                "cliente_codigo": cid,
                "cliente": name or customers.get(cid, ""),
                "dia": day,
                "grupo_ruta": DAY_GROUPS.get(day, "OTROS"),
                "alta_fecha": str(pd.to_datetime(row.get(alta_col), errors="coerce").date()) if alta_col is not None and pd.notna(pd.to_datetime(row.get(alta_col), errors="coerce")) else "",
            })
    return rows


def _rows_from_rutas(path: Path) -> list[dict]:
    try:
        df = pd.read_excel(path, sheet_name="Browser", dtype=str)
    except Exception:
        return []
    prom_lookup = _sales_promoter_lookup()
    customers = _customer_lookup()
    rows = []
    day_cols = {"LU": "LU", "MA": "MA", "MI": "MI", "JU": "JU", "VI": "VI", "SA": "SA", "DO": "DO"}
    for _, row in df.iterrows():
        seller = _code(row.get("Vnd."))
        cid = _code(row.get("Cliente"))
        if not seller or not cid:
            continue
        promotor, supervisor = prom_lookup.get(seller, (f"VND {seller}", "OTROS"))
        for day, col in day_cols.items():
            if _clean(row.get(col)).upper() != "X":
                continue
            name = _clean(row.get("Razón Social")) or customers.get(cid, "")
            rows.append({
                "vendedor_codigo": seller,
                "promotor": promotor,
                "supervisor": supervisor,
                "ruta": _code(row.get("Ruta")),
                "cliente_codigo": cid,
                "cliente": name,
                "dia": day,
                "grupo_ruta": DAY_GROUPS.get(day, "OTROS"),
                "alta_fecha": "",
            })
    return rows


def _plan_rows() -> list[dict]:
    try:
        workbook = pd.read_excel(PLANNER_SHEET_URL, sheet_name=None, dtype=str)
        sheet = workbook.get("BD_KPI_PROMOTORES")
        if sheet is None or sheet.empty:
            return []
        sheet.columns = [str(c).strip().lower() for c in sheet.columns]
        required = {"fecha", "ruta", "kpi", "promotor", "planificado"}
        if not required.issubset(sheet.columns):
            return []
        rows = []
        for _, row in sheet.iterrows():
            fecha = pd.to_datetime(row.get("fecha"), errors="coerce")
            promotor = _clean(row.get("promotor"))
            kpi = _clean(row.get("kpi"))
            ruta = _clean(row.get("ruta"))
            if pd.isna(fecha) or not promotor or not kpi:
                continue
            try:
                planificado = float(str(row.get("planificado") or "0").replace(".", "").replace(",", "."))
            except Exception:
                planificado = 0.0
            rows.append({
                "fecha": fecha.strftime("%Y-%m-%d"),
                "ruta": ruta,
                "kpi": kpi,
                "promotor": promotor,
                "supervisor": SUPERVISORES.get(promotor.upper(), "OTROS"),
                "planificado": planificado,
            })
        return rows
    except Exception:
        return []


def _build_snapshot(rutas_path: Path | None, reporte_path: Path | None) -> dict:
    route_rows = []
    if rutas_path is not None and rutas_path.exists():
        route_rows.extend(_rows_from_rutas(rutas_path))
    if reporte_path is not None and reporte_path.exists():
        # Reporte de clientes es más actual y se agrega al maestro de rutas.
        route_rows.extend(_rows_from_reporte(reporte_path))
    if route_rows:
        df = pd.DataFrame(route_rows)
        # Si el reporte trae el mismo cliente/promotor/grupo, prioriza la fila con nombre real.
        df["_quality"] = df["cliente"].fillna("").str.len() + df["promotor"].fillna("").str.len()
        df = df.sort_values("_quality").drop_duplicates(["promotor", "cliente_codigo", "grupo_ruta"], keep="last").drop(columns="_quality")
        route_rows = df.to_dict("records")
    plan_rows = _plan_rows()
    return {
        "schema_version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "promotores_kpi_dashboard · RUTAS + reporte clientes + BD_KPI_PROMOTORES",
        "route_clients": route_rows,
        "plan_rows": plan_rows,
    }


def refresh(force=True):
    global _last_error
    with _lock:
        try:
            if RUNTIME_DIR.exists():
                shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            rutas_path = None
            reporte_path = None
            try:
                rutas_path = _download_by_id(RUTAS_ID, RUNTIME_DIR / "RUTAS 7-26.xlsx")
            except Exception:
                rutas_path = None
            try:
                reporte_path = _download_by_id(REPORTE_CLIENTES_ID, RUNTIME_DIR / "reporte de clientes.xlsx")
            except Exception:
                reporte_path = None
            snap = _build_snapshot(rutas_path, reporte_path)
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
            "name": "Promotores KPI",
            "detail": f"{len(snap.get('route_clients') or [])} asignaciones ruta · {len(snap.get('plan_rows') or [])} filas planificadas",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Promotores KPI", "detail": str(_last_error), "loaded_at": "—"}
    return {"ok": None, "name": "Promotores KPI", "detail": "Sin snapshot. Se actualizará automáticamente.", "loaded_at": "—"}
