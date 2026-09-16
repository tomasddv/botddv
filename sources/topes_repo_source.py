from __future__ import annotations

from datetime import datetime
from pathlib import Path
import gzip
import json
import re
import shutil
import threading
import unicodedata

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = CACHE_DIR / "topes_repo_snapshot.json.gz"
RUNTIME_DIR = DATA_DIR / "topes-repo-runtime"

# Misma fuente / lógica de dashboard_bultos_accion.py del repo tomasddv/planificacion.
PLANIFICACION_FOLDER_URL = "https://drive.google.com/drive/folders/1cukgXLUaPsEDK_yD7tSwgaBFZAbiDUot"
AUXILIARES_ID = "1zXhbWtT7K1tY43MmYz7oTTYifMgmLyFT"
REPORTE_CLIENTES_ID = "1ZR9WOeqpaq9t-mJZM4f9AlUV7BIrKVo-"
PLANNER_SHEET_URL = "https://docs.google.com/spreadsheets/d/15ITRhsY5mvK3NSHeOKV2MymC078pT9TPAwKUdZDfjnI/export?format=xlsx"
EXTENSION_SHEET_NAME = "BD_EXTENSION_TOPES"

TOPES_CANAL = {
    "K+T": 200.0,
    "MAYORISTA": 1000.0,
    "AUTOSERVICIO": 500.0,
    "AS": 500.0,
}
CANAL_ACCION_ALIAS = {"AUTOSERVICIO": "AS"}
CORE_BRAND_TERMS = ("QUILMES", "BRAHMA", "BUDWEISER")
VALUE_BRAND_TERMS = ("QUILMES 1890", "1890")

_snapshot = None
_last_error = None
_lock = threading.RLock()


def _clean(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ").strip())


def _norm(value) -> str:
    text = unicodedata.normalize("NFD", _clean(value).lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _code(value) -> str:
    text = _clean(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if re.fullmatch(r"\d+", text):
        return text.lstrip("0") or "0"
    return text


def _num(value) -> float:
    text = _clean(value)
    if not text:
        return 0.0
    text = text.replace("%", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except Exception:
        return 0.0


def _bool(value) -> bool:
    return _norm(value) in {"1", "true", "si", "yes", "y", "activo", "activa"}


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
    target.unlink(missing_ok=True)
    tmp.replace(target)
    return target


def _drive_compact_name(value) -> str:
    return _norm(Path(str(value or "")).name).replace(" ", "")


def _download_current_bultos(target: Path):
    """Busca por nombre el archivo vigente de venta diaria en bultos.

    Igual que Venta diaria HL, no depende de un ID fijo para soportar reemplazos
    del archivo en Drive.
    """
    import gdown

    candidates = []
    try:
        metadata = gdown.download_folder(
            url=PLANIFICACION_FOLDER_URL,
            output=str(RUNTIME_DIR / "_drive_index"),
            quiet=True,
            use_cookies=False,
            skip_download=True,
        )
        for item in metadata or []:
            name = Path(str(getattr(item, "path", "") or "")).name
            compact = _drive_compact_name(name)
            suffix = Path(name).suffix.lower()
            if suffix not in {".txt", ".csv"}:
                continue
            exact = compact in {"ventadiariabultostxt", "ventadiariabultoscsv"}
            broad = "venta" in compact and "diaria" in compact and "bulto" in compact
            if exact or broad:
                candidates.append((0 if exact else 1, len(name), str(getattr(item, "id", "") or ""), name))
    except Exception:
        candidates = []

    if not candidates:
        raise RuntimeError("No encontré ventadiaria bultos en la carpeta de Planificación.")
    candidates.sort(key=lambda x: (x[0], x[1], x[3].lower()))
    last_error = None
    for _, _, file_id, _ in candidates:
        if not file_id:
            continue
        try:
            return _download_by_id(file_id, target), file_id
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("No se pudo descargar ventadiaria bultos.")


def _read_tabular(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    # Los TXT de CHESS actuales son tabulados Latin-1.
    try:
        return pd.read_csv(path, sep="\t", encoding="latin-1", dtype=str, low_memory=False)
    except Exception:
        return pd.read_csv(path, sep=None, engine="python", encoding="latin-1", dtype=str)


def _col_by_pos(df: pd.DataFrame, label: str):
    index = 0
    for char in label.upper():
        if char.isalpha():
            index = index * 26 + ord(char) - ord("A") + 1
    index -= 1
    return df.columns[index] if 0 <= index < len(df.columns) else None


def _find_col(df: pd.DataFrame, *terms: str):
    normalized = [(_norm(c), c) for c in df.columns]
    for term in terms:
        nt = _norm(term)
        for name, raw in normalized:
            if name == nt:
                return raw
    for term in terms:
        nt = _norm(term)
        for name, raw in normalized:
            if nt and nt in name:
                return raw
    return None


def _parse_period(value):
    text = _norm(value)
    m = re.search(r"(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[a-z]*\s+(\d{2,4})", text)
    months = {"ene":1,"feb":2,"mar":3,"abr":4,"may":5,"jun":6,"jul":7,"ago":8,"sep":9,"set":9,"oct":10,"nov":11,"dic":12}
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return pd.Timestamp(year=year, month=months[m.group(2)], day=int(m.group(1)))
        except Exception:
            pass
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return parsed.normalize() if not pd.isna(parsed) else pd.NaT


def _classify_customer_channel(text: str) -> str:
    value = _norm(text).upper()
    if "AUTOSERV" in value:
        return "AUTOSERVICIO"
    if "MAYOR" in value:
        return "MAYORISTA"
    if "REFRIG" in value:
        return "REF"
    if any(term in value for term in ("TRADICIONAL", "KIOSCO", "CADENITA", "LISTA UNICA")):
        return "K+T"
    return "NO"


def _customer_channels(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        df = pd.read_excel(path, dtype=str)
    except Exception:
        return {}
    if df.empty:
        return {}
    customer_col = _find_col(df, "Cliente", "Cod Cliente", "Codigo Cliente")
    if customer_col is None:
        return {}
    text_cols = []
    for col in df.columns:
        nc = _norm(col)
        if any(term in nc for term in (
            "descripcion lista", "lista de precios", "descripcion subcanal", "subcanal",
            "descripcion ramo", "ramo",
        )):
            text_cols.append(col)
    if not text_cols:
        return {}
    out = {}
    for _, row in df.iterrows():
        cid = _code(row.get(customer_col))
        if not cid:
            continue
        text = " ".join(_clean(row.get(c)) for c in text_cols)
        out[cid] = _classify_customer_channel(text)
    return out


def _brand_segments(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        df = pd.read_excel(path, sheet_name="PIVOT", dtype=str)
    except Exception:
        return {}
    out = {}
    for _, row in df.iterrows():
        brand = _clean(row.get("MARCA"))
        segment = _clean(row.get("SEGMENTO")).upper()
        if brand and segment:
            out[_norm(brand)] = segment
    return out


def _brand_action(brand: str, segment_map: dict[str, str]) -> str:
    n = _norm(brand)
    seg = str(segment_map.get(n) or "").upper()
    if seg == "VALUE":
        return "VALUE"
    if seg == "CORE":
        return "CORE"
    up = _clean(brand).upper()
    if any(term in up for term in VALUE_BRAND_TERMS):
        return "VALUE"
    if any(term in up for term in CORE_BRAND_TERMS):
        return "CORE"
    return ""


def _normalize_bultos(path: Path, customer_path: Path | None, aux_path: Path | None) -> pd.DataFrame:
    raw = _read_tabular(path)
    if raw.empty:
        return pd.DataFrame()

    date_col = _find_col(raw, "Periodos") or _col_by_pos(raw, "A")
    customer_col = _find_col(raw, "Cod. Cliente", "Cod Cliente") or _col_by_pos(raw, "E")
    name_col = _col_by_pos(raw, "F")
    route_code_col = _col_by_pos(raw, "I")
    route_name_col = _col_by_pos(raw, "J")
    seller_col = _col_by_pos(raw, "P")
    brand_col = _col_by_pos(raw, "U")
    business_col = _col_by_pos(raw, "AJ")
    quantity_col = _find_col(raw, "Cantidades Totales")
    if customer_col is None or brand_col is None or business_col is None or quantity_col is None:
        raise RuntimeError("ventadiaria bultos no tiene las columnas esperadas para Topes.")

    channels = _customer_channels(customer_path)
    segments = _brand_segments(aux_path)
    rows = []
    for _, r in raw.iterrows():
        fecha = _parse_period(r.get(date_col))
        if pd.isna(fecha):
            continue
        cid = _code(r.get(customer_col))
        if not cid:
            continue
        brand = _clean(r.get(brand_col))
        action = _brand_action(brand, segments)
        business = _clean(r.get(business_col))
        if not action or not ("CERVEZ" in _norm(business).upper() or "CZA" in _norm(business).upper()):
            continue
        channel = channels.get(cid, "NO")
        channel_action = CANAL_ACCION_ALIAS.get(channel, channel)
        top = TOPES_CANAL.get(channel)
        if top is None:
            top = TOPES_CANAL.get(channel_action)
        if top is None:
            continue
        route_code = _code(r.get(route_code_col)) if route_code_col is not None else ""
        route_name = _clean(r.get(route_name_col)) if route_name_col is not None else ""
        rows.append({
            "fecha": fecha,
            "cliente_codigo": cid,
            "cliente": _clean(r.get(name_col)) if name_col is not None else "",
            "canal": channel_action,
            "ruta": (route_code + (" - " + route_name if route_name else "")).strip(" -"),
            "vendedor": _clean(r.get(seller_col)) if seller_col is not None else "",
            "marca": brand,
            "segmento": action,
            "bultos": _num(r.get(quantity_col)),
            "tope_bultos": float(top),
        })
    return pd.DataFrame(rows)


def _load_extensions(path_or_url) -> pd.DataFrame:
    try:
        workbook = pd.read_excel(path_or_url, sheet_name=None, dtype=str)
        sheet = next((df for name, df in workbook.items() if _norm(name) == _norm(EXTENSION_SHEET_NAME)), None)
    except Exception:
        return pd.DataFrame()
    if sheet is None or sheet.empty:
        return pd.DataFrame()
    cols = {_norm(c): c for c in sheet.columns}
    code_col = cols.get("cliente codigo") or cols.get("cod cliente") or cols.get("codigo cliente")
    action_col = cols.get("accion")
    if code_col is None or action_col is None:
        return pd.DataFrame()
    rows = []
    for _, r in sheet.iterrows():
        cid = _code(r.get(code_col))
        action = _clean(r.get(action_col)).upper()
        if not cid or action not in {"CORE", "VALUE"}:
            continue
        date_col = cols.get("fecha extension")
        ext_date = pd.to_datetime(r.get(date_col), errors="coerce") if date_col else pd.NaT
        if not pd.isna(ext_date):
            ext_date = pd.Timestamp(ext_date).normalize()
        active_col = cols.get("activa")
        active = _bool(r.get(active_col)) if active_col else True
        active = bool(active and not pd.isna(ext_date))
        rows.append({
            "cliente_codigo": cid,
            "cliente": _clean(r.get(cols.get("cliente"))) if cols.get("cliente") else "",
            "canal": _clean(r.get(cols.get("canal"))).upper() if cols.get("canal") else "",
            "segmento": action,
            "fecha_extension": ext_date,
            "primer_tope_sheet": _num(r.get(cols.get("primer tope"))) if cols.get("primer tope") else 0.0,
            "segundo_tope_sheet": _num(r.get(cols.get("segundo tope"))) if cols.get("segundo tope") else 0.0,
            "extension_activa": active,
            "comentario_extension": _clean(r.get(cols.get("comentario"))) if cols.get("comentario") else "",
            "actualizado_extension": _clean(r.get(cols.get("actualizado"))) if cols.get("actualizado") else "",
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.drop_duplicates(["cliente_codigo", "segmento"], keep="last")


def _build_snapshot(
    bultos_path: Path,
    customer_path: Path | None = None,
    aux_path: Path | None = None,
    planner_path_or_url=PLANNER_SHEET_URL,
    drive_file_id: str = "",
) -> dict:
    data = _normalize_bultos(bultos_path, customer_path, aux_path)
    if data.empty:
        raise RuntimeError("La fuente de Topes no produjo filas Core/Value válidas.")

    # Igual que Venta diaria: el snapshot representa el mes de la última fecha disponible.
    max_date = pd.to_datetime(data["fecha"], errors="coerce").max()
    data = data[(data["fecha"].dt.year == max_date.year) & (data["fecha"].dt.month == max_date.month)].copy()

    grouped = (
        data.groupby(["cliente_codigo", "segmento"], as_index=False)
        .agg(
            cliente=("cliente", "max"), canal=("canal", "first"), ruta=("ruta", "max"),
            vendedor=("vendedor", "max"), bultos_comprados=("bultos", "sum"),
            tope_bultos=("tope_bultos", "first"),
        )
    )

    ext = _load_extensions(planner_path_or_url)
    if ext.empty:
        ext = pd.DataFrame(columns=[
            "cliente_codigo", "segmento", "fecha_extension", "extension_activa",
            "comentario_extension", "actualizado_extension",
        ])
    keep_ext = [c for c in [
        "cliente_codigo", "segmento", "fecha_extension", "extension_activa",
        "comentario_extension", "actualizado_extension",
    ] if c in ext.columns]
    grouped = grouped.merge(ext[keep_ext], on=["cliente_codigo", "segmento"], how="left")
    grouped["extension_activa"] = grouped.get("extension_activa", False).map(lambda x: bool(x) if pd.notna(x) else False)

    # Segundo tramo: exactamente las compras desde la fecha de extensión.
    second = {}
    for _, erow in ext.iterrows() if not ext.empty else []:
        if not bool(erow.get("extension_activa")) or pd.isna(erow.get("fecha_extension")):
            continue
        cid = str(erow.get("cliente_codigo"))
        action = str(erow.get("segmento"))
        e_date = pd.Timestamp(erow.get("fecha_extension")).normalize()
        mask = (
            data["cliente_codigo"].eq(cid)
            & data["segmento"].eq(action)
            & pd.to_datetime(data["fecha"], errors="coerce").dt.normalize().ge(e_date)
        )
        second[(cid, action)] = float(data.loc[mask, "bultos"].sum())

    rows = []
    for _, row in grouped.iterrows():
        total = float(row.get("bultos_comprados") or 0.0)
        top = float(row.get("tope_bultos") or 0.0)
        active = bool(row.get("extension_activa"))
        key = (str(row.get("cliente_codigo")), str(row.get("segmento")))
        second_bought = float(second.get(key, 0.0)) if active else 0.0
        if active:
            first_bought = min(max(total - second_bought, 0.0), top)
            second_top = top
            second_left = second_top - second_bought
        else:
            first_bought = min(max(total, 0.0), top)
            second_top = None
            second_left = None
        remaining = top - total
        advance = (total / top * 100.0) if top > 0 else None
        if advance is not None and advance >= 100:
            status = "COMPLETO"
        elif advance is not None and advance >= 80:
            status = "CERCA DEL TOPE"
        else:
            status = "PENDIENTE"
        rows.append({
            "cliente_codigo": str(row.get("cliente_codigo") or ""),
            "cliente": _clean(row.get("cliente")),
            "segmento": str(row.get("segmento") or ""),
            "canal": _clean(row.get("canal")),
            "ruta": _clean(row.get("ruta")),
            "vendedor": _clean(row.get("vendedor")),
            "bultos_comprados": total,
            "tope_bultos": top,
            "avance_pct": advance,
            "restante_bultos": remaining,
            "estado_tope": status,
            "primer_tope_comprado": first_bought,
            "extension_activa": active,
            "fecha_extension": "" if pd.isna(row.get("fecha_extension")) else pd.Timestamp(row.get("fecha_extension")).strftime("%Y-%m-%d"),
            "segundo_tope_bultos": second_top,
            "segundo_tramo_comprado": second_bought,
            "restante_segundo_bultos": second_left,
            "comentario_extension": _clean(row.get("comentario_extension")),
            "actualizado_extension": _clean(row.get("actualizado_extension")),
        })

    return {
        "schema_version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "planificacion/dashboard_bultos_accion.py · ventadiaria bultos · BD_EXTENSION_TOPES",
        "drive_file_id": drive_file_id,
        "period_start": data["fecha"].min().strftime("%Y-%m-%d"),
        "period_end": data["fecha"].max().strftime("%Y-%m-%d"),
        "rows": rows,
        "clients_count": int(pd.Series([r["cliente_codigo"] for r in rows]).nunique()) if rows else 0,
    }


def refresh(force=True):
    global _last_error
    with _lock:
        try:
            if RUNTIME_DIR.exists():
                shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            bultos_path, file_id = _download_current_bultos(RUNTIME_DIR / "ventadiaria bultos.txt")
            aux_path = None
            customer_path = None
            try:
                aux_path = _download_by_id(AUXILIARES_ID, RUNTIME_DIR / "AUXILIARES.xlsx")
            except Exception:
                pass
            try:
                customer_path = _download_by_id(REPORTE_CLIENTES_ID, RUNTIME_DIR / "reporte de clientes.xlsx")
            except Exception:
                pass
            snap = _build_snapshot(
                bultos_path=bultos_path,
                customer_path=customer_path,
                aux_path=aux_path,
                planner_path_or_url=PLANNER_SHEET_URL,
                drive_file_id=file_id,
            )
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
            "name": "Topes Core / Value",
            "detail": f"{snap.get('clients_count', 0)} clientes · corte {snap.get('period_end', '—')} · extensiones incluidas",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Topes Core / Value", "detail": str(_last_error), "loaded_at": "—"}
    return {"ok": None, "name": "Topes Core / Value", "detail": "Sin snapshot. Se actualizará automáticamente.", "loaded_at": "—"}
