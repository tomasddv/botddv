from __future__ import annotations

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
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
SNAPSHOT_PATH = CACHE_DIR / "ventas_actual_snapshot.json.gz"
RUNTIME_DIR = DATA_DIR / "ventas-actual-runtime"

# Mismas fuentes de Planificación / Venta diaria.
PLANIFICACION_FOLDER_URL = "https://drive.google.com/drive/folders/1cukgXLUaPsEDK_yD7tSwgaBFZAbiDUot"
# Fallback histórico: si no se puede listar la carpeta, intenta este ID.
VENTA_DIARIA_ID = "12c7hy-bTbg7P_1QYUyKKcooNLo4iog1x"
CLIENTES_ID = "1GuRrGKlb7SLjI9h81XssZTpWzgPUrpRb"
AUXILIARES_ID = "1zXhbWtT7K1tY43MmYz7oTTYifMgmLyFT"

EXCLUDED_VENDORS = {"701", "702", "703"}
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
BALANCED_BRANDS = {
    "STELLA ARTOIS PURE GOLD",
    "STELLA ARTOIS 0.0%",
    "CORONA CERO",
    "QUILMES 0.0%",
    "MICHELOB ULTRA",
}
NABS_DIVISIONS = {"AGUAS", "BEB ENERGIZANTES", "BEBIDAS SABORIZADAS", "GASEOSAS", "ISOTONICAS"}

_snapshot = None
_last_error = None
_lock = threading.RLock()


AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAILY_UPLOAD_HOUR = 16


def _now_ar() -> datetime:
    return datetime.now(AR_TZ)


def _previous_commercial_day(day: date) -> date:
    """En DDV hay venta de lunes a sábado; domingo no genera corte comercial."""
    candidate = day - timedelta(days=1)
    while candidate.weekday() == 6:  # domingo
        candidate -= timedelta(days=1)
    return candidate


def expected_cutoff(now: datetime | None = None) -> str:
    """Fecha que debería estar disponible según la rutina de carga diaria.

    Antes de las 16:00 se considera vigente el último día comercial cerrado.
    Desde las 16:00 se espera la venta del día actual. Los domingos se conserva sábado.
    """
    now = now or _now_ar()
    today = now.date()
    if today.weekday() == 6:  # domingo
        expected = _previous_commercial_day(today)
    elif now.hour >= DAILY_UPLOAD_HOUR:
        expected = today
    else:
        expected = _previous_commercial_day(today)
    return expected.isoformat()


def freshness(now: datetime | None = None) -> dict:
    snap = _load_disk() or {}
    actual = str(snap.get("period_end") or "")[:10]
    expected = expected_cutoff(now)
    current = bool(actual and actual >= expected)
    return {
        "period_end": actual,
        "expected_cutoff": expected,
        "is_current": current,
        "waiting_for_upload": not current,
        "upload_hour": DAILY_UPLOAD_HOUR,
    }


def needs_refresh(now: datetime | None = None) -> bool:
    """True mientras el corte esperado todavía no llegó al snapshot."""
    return not freshness(now).get("is_current", False)


def _drive_name(value) -> str:
    text = _norm(Path(str(value or "")).name)
    return re.sub(r"\s+", "", text)


def _download_current_venta(target: Path):
    """Busca ventadiaria.txt por nombre dentro de la carpeta de Planificación.

    Esto evita depender del ID fijo: si el archivo se reemplaza/sube de nuevo,
    el bot toma automáticamente el ID vigente.
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
            raw_path = str(getattr(item, "path", "") or "")
            name = Path(raw_path).name
            compact = _drive_name(name)
            suffix = Path(name).suffix.lower()
            if suffix not in {".txt", ".csv"}:
                continue
            if "bultos" in compact:
                continue
            # Preferencia exacta: ventadiaria.txt. Respaldo: cualquier VENTA + DIARIA.
            exact = compact in {"ventadiariatxt", "ventadiariacsv"}
            broad = "venta" in compact and "diaria" in compact
            if exact or broad:
                candidates.append((0 if exact else 1, len(raw_path), item, name))
    except Exception:
        candidates = []

    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1], x[3].lower()))
        last_error = None
        for _, _, item, _ in candidates:
            file_id = str(getattr(item, "id", "") or "")
            if not file_id:
                continue
            try:
                _download_by_id(file_id, target)
                return target, file_id
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error

    # Fallback si Drive no permite listar la carpeta.
    _download_by_id(VENTA_DIARIA_ID, target)
    return target, VENTA_DIARIA_ID


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
    return text.lstrip("0") or ("0" if text else "")


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


def _period_date(value):
    text = _clean(value).lower()
    # CHESS exporta: (001) 01 SEP 2026
    match = re.search(r"(\d{1,2})\s+([a-záéíóú]{3,})\s+(\d{4})", text)
    months = {
        "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
        "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    }
    if match:
        mon = _norm(match.group(2))[:3]
        if mon in months:
            return pd.Timestamp(date(int(match.group(3)), months[mon], int(match.group(1))))
    # Respaldo para Descripción Período: 1-sep-26
    match = re.search(r"(\d{1,2})[-/ ]([a-záéíóú]{3})[-/ ](\d{2,4})", text)
    if match:
        mon = _norm(match.group(2))[:3]
        year = int(match.group(3))
        if year < 100:
            year += 2000
        if mon in months:
            return pd.Timestamp(date(year, months[mon], int(match.group(1))))
    return pd.NaT


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


def _read_client_map(path: Path | None) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        df = pd.read_excel(path, sheet_name="Clientes", header=1, dtype=str)
    except Exception:
        df = pd.read_excel(path, header=1, dtype=str)
    if "Cliente" not in df.columns:
        return {}
    out = {}
    for _, row in df.iterrows():
        cid = _code(row.get("Cliente"))
        if not cid or cid.upper() == "ENTERO":
            continue
        grouping = _clean(row.get("Descripción Agrupación"))
        ng = _norm(grouping)
        if "madryn" in ng:
            base = "MADRYN"
        elif "trelew" in ng:
            base = "TRELEW"
        else:
            base = ""
        out[cid] = {
            "razon_social": _clean(row.get("Razón social")),
            "nombre_fantasia": _clean(row.get("Nombre de fantasia")),
            "agrupacion": grouping,
            "localidad_base": base,
            "lista_precios": _clean(row.get("Lista de precios")),
            "subcanal": _clean(row.get("Subcanal MKT")),
            "ramo_cliente": _clean(row.get("Descripción Negocio")),
        }
    return out


def _key(*parts) -> str:
    return "|".join(_clean(p).upper() for p in parts)


def _read_aux(path: Path | None):
    empty = ({}, {}, {})
    if path is None or not path.exists():
        return empty
    try:
        aux = pd.read_excel(path, sheet_name="PIVOT", dtype=str)
    except Exception:
        return empty
    brand_map, mix_map, caliber_map = {}, {}, {}
    for _, row in aux.iterrows():
        marca = _clean(row.get("MARCA"))
        if marca:
            brand_map[marca.upper()] = {
                "marca_unificada": _clean(row.get("MARCA UNIFICADA")) or marca,
                "segmento": _clean(row.get("SEGMENTO")),
                "segmento_2": _clean(row.get("SEGMENTO.2")),
                "segmento_3": _clean(row.get("SEGMENTO.3")),
            }
        mix_brand = _clean(row.get("Marca"))
        mix_div = _clean(row.get("División"))
        mix_cal = _clean(row.get("Calibre"))
        if mix_brand and mix_div and mix_cal:
            mix_map[_key(mix_brand, mix_div, mix_cal)] = {
                "ung_top": _clean(row.get("UNG TOP")),
                "calibres_cpr": _clean(row.get("CALIBRES CPR")),
            }
        unidad = _clean(row.get("Unidad de Negocio"))
        calibre = _clean(row.get("Calibre.1"))
        if unidad and calibre:
            caliber_map[_key(unidad, calibre)] = _clean(row.get("Calibre Unificado")) or calibre
    return brand_map, mix_map, caliber_map


def _focus_fields(marca: str, division: str, calibre_unificado: str) -> dict:
    m = _clean(marca).upper()
    d = _clean(division).upper()
    cal = _clean(calibre_unificado).upper()
    cza = d == "CERVEZAS"
    balanced = m in BALANCED_BRANDS
    core = cza and (
        m == "BRAHMA"
        or m.startswith("BUDWEISER")
        or ("QUILMES" in m and m != "QUILMES 1890" and not balanced)
    )
    value = cza and m == "QUILMES 1890"
    above = cza and (m.startswith("STELLA ARTOIS") or m.startswith("ANDES ORIGEN")) and not balanced
    premium = cza and (m.startswith("CORONA") or m.startswith("PATAGONIA")) and not balanced
    nabs = d in NABS_DIVISIONS
    if core:
        focus = "CORE"
    elif value:
        focus = "VALUE"
    elif above:
        focus = "ABOVE CORE"
    elif premium:
        focus = "PREMIUM"
    elif balanced:
        focus = "BALANCED CHOICES"
    elif cza:
        focus = "OTRAS CERVEZAS"
    elif nabs:
        focus = "NABS"
    else:
        focus = "OTROS"
    return {
        "foco_comercial": focus,
        "es_cza": int(cza),
        "es_core": int(core),
        "es_value": int(value),
        "es_above_core": int(above),
        "es_premium": int(premium),
        "es_balanced": int(cza and balanced),
        "es_nabs": int(nabs),
        "es_laton_710": int(cal == "LATON 710 CC"),
    }


def _json_value(value):
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if not np.isfinite(value):
            return None
        return float(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _records(df: pd.DataFrame):
    return [{k: _json_value(v) for k, v in rec.items()} for rec in df.to_dict("records")]


def _build_snapshot(venta_path: Path, clientes_path: Path | None = None, aux_path: Path | None = None, drive_file_id: str = ""):
    venta = pd.read_csv(venta_path, sep="\t", encoding="latin1", dtype=str, engine="python")
    required = {
        "Periodos", "Vendedor", "Descripción Vendedor", "Ruta", "Cod. Cliente", "Descripción",
        "Código", "Descripción.2", "Descripción.3", "Descripción.4", "Descripción.5",
        "Descripción.6", "Descripción.8", "Cantidades Totales", "Importes Netos",
        "Importes Finales", "Cantidad de Facturas",
    }
    missing = sorted(required - set(venta.columns))
    if missing:
        raise RuntimeError("ventadiaria.txt no tiene las columnas esperadas: " + ", ".join(missing))

    client_map = _read_client_map(clientes_path)
    brand_map, mix_map, caliber_map = _read_aux(aux_path)

    rows = []
    for _, r in venta.iterrows():
        fecha = _period_date(r.get("Periodos"))
        if pd.isna(fecha):
            fecha = _period_date(r.get("Descripción Período"))
        if pd.isna(fecha):
            continue
        vendedor = _code(r.get("Vendedor"))
        if vendedor in EXCLUDED_VENDORS:
            continue
        cid = _code(r.get("Cod. Cliente"))
        sku = _code(r.get("Código"))
        marca = _clean(r.get("Descripción.3"))
        calibre = _clean(r.get("Descripción.4"))
        division = _clean(r.get("Descripción.5"))
        unidad = _clean(r.get("Descripción.8"))
        cm = client_map.get(cid, {})
        bm = brand_map.get(marca.upper(), {})
        mm = mix_map.get(_key(marca, division, calibre), {})
        calibre_unificado = caliber_map.get(_key(unidad, calibre), calibre)
        focus = _focus_fields(marca, division, calibre_unificado)
        promotor = _clean(r.get("Descripción Vendedor")) or f"VND {vendedor}"
        row = {
            "fecha": fecha,
            "cliente_codigo": cid,
            "cliente": _clean(r.get("Descripción")),
            "razon_social": cm.get("razon_social", ""),
            "nombre_fantasia": cm.get("nombre_fantasia", ""),
            "agrupacion": cm.get("agrupacion", ""),
            "localidad_base": cm.get("localidad_base", ""),
            "lista_precios": cm.get("lista_precios", ""),
            "subcanal": cm.get("subcanal", ""),
            "vendedor_codigo": vendedor,
            "vendedor": promotor,
            "supervisor": SUPERVISORES.get(promotor.upper(), "OTROS"),
            "ruta": _code(r.get("Ruta")),
            "sku": sku,
            "producto": _clean(r.get("Descripción.2")),
            "marca": marca,
            "marca_unificada": bm.get("marca_unificada", marca),
            "segmento": bm.get("segmento", ""),
            "segmento_2": bm.get("segmento_2", ""),
            "segmento_3": bm.get("segmento_3", ""),
            "calibre": calibre,
            "calibre_unificado": calibre_unificado,
            "division": division,
            "producto_estadistico": _clean(r.get("Descripción.6")),
            "unidad_negocio": unidad,
            "ung_top": mm.get("ung_top", ""),
            "calibres_cpr": mm.get("calibres_cpr", ""),
            "hl": _num(r.get("Cantidades Totales")),
            "importe_neto": _num(r.get("Importes Netos")),
            "importe_final": _num(r.get("Importes Finales")),
            "facturas": _num(r.get("Cantidad de Facturas")),
            **focus,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("ventadiaria.txt no produjo ventas válidas")

    # La fuente del bot representa siempre el mes de la última fecha disponible.
    max_date = df["fecha"].max()
    df = df[(df["fecha"].dt.year == max_date.year) & (df["fecha"].dt.month == max_date.month)].copy()

    dims = [
        "fecha", "cliente_codigo", "cliente", "razon_social", "nombre_fantasia", "agrupacion", "localidad_base",
        "lista_precios", "subcanal", "vendedor_codigo", "vendedor", "supervisor", "ruta", "sku", "producto",
        "marca", "marca_unificada", "segmento", "segmento_2", "segmento_3", "calibre", "calibre_unificado",
        "division", "producto_estadistico", "unidad_negocio", "ung_top", "calibres_cpr", "foco_comercial",
        "es_cza", "es_core", "es_value", "es_above_core", "es_premium", "es_balanced", "es_nabs", "es_laton_710",
    ]
    nums = ["hl", "importe_neto", "importe_final", "facturas"]
    grouped = df.groupby(dims, dropna=False, as_index=False)[nums].sum()
    grouped = grouped.sort_values(["fecha", "cliente_codigo", "sku"]).reset_index(drop=True)

    min_date = grouped["fecha"].min()
    max_date = grouped["fecha"].max()
    return {
        "schema_version": 3,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "CHESS · ventadiaria.txt · Hectolitros",
        "drive_file_id": drive_file_id,
        "period_start": min_date.strftime("%Y-%m-%d"),
        "period_end": max_date.strftime("%Y-%m-%d"),
        "period_month": max_date.strftime("%Y-%m"),
        "raw_rows": int(len(venta)),
        "rows_count": int(len(grouped)),
        "clients_count": int(grouped["cliente_codigo"].nunique()),
        "skus_count": int(grouped["sku"].nunique()),
        "total_hl": float(grouped["hl"].sum()),
        "rows": _records(grouped),
        "customer_master": [
            {"cliente_codigo": cid, **data}
            for cid, data in sorted(client_map.items(), key=lambda item: item[0])
        ],
    }


def refresh(force=True):
    """Actualiza ventadiaria en segundo plano. Las preguntas sólo leen el snapshot local."""
    global _last_error
    with _lock:
        try:
            if RUNTIME_DIR.exists():
                shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            venta_path, drive_file_id = _download_current_venta(RUNTIME_DIR / "ventadiaria.txt")
            clientes_path = None
            aux_path = None
            try:
                clientes_path = _download_by_id(CLIENTES_ID, RUNTIME_DIR / "plantillaClientesAR.xlsx")
            except Exception:
                clientes_path = None
            try:
                aux_path = _download_by_id(AUXILIARES_ID, RUNTIME_DIR / "AUXILIARES.xlsx")
            except Exception:
                aux_path = None
            snap = _build_snapshot(venta_path, clientes_path, aux_path, drive_file_id=drive_file_id)
            _last_error = None
            return _save(snap)
        except Exception as exc:
            _last_error = exc
            raise


def status():
    snap = _load_disk()
    if snap:
        fresh = freshness()
        expected = fresh.get("expected_cutoff") or ""
        actual = fresh.get("period_end") or ""
        def ar(v):
            try:
                y, m, d = str(v)[:10].split("-")
                return f"{d}/{m}/{y}"
            except Exception:
                return str(v or "—")
        if fresh.get("is_current"):
            freshness_text = f"✅ al día hasta {ar(actual)}"
        else:
            freshness_text = f"⏳ corte {ar(actual)} · esperando carga {ar(expected)}"
        return {
            "ok": True,
            "name": "Venta mes actual",
            "detail": (
                f"{snap.get('period_start','—')} → {snap.get('period_end','—')} · "
                f"{snap.get('total_hl',0):.1f} HL · {snap.get('clients_count',0)} clientes · "
                f"{freshness_text}"
            ),
            "loaded_at": snap.get("updated_at", "—"),
        }
    if _last_error:
        return {"ok": False, "name": "Venta mes actual", "detail": str(_last_error), "loaded_at": "—"}
    return {"ok": None, "name": "Venta mes actual", "detail": "Sin snapshot. Se actualizará automáticamente.", "loaded_at": "—"}

def rows():
    snap = _load_disk() or {}
    return snap.get("rows") or []


def meta():
    snap = _load_disk() or {}
    out = {k: snap.get(k) for k in (
        "updated_at", "source", "drive_file_id", "period_start", "period_end", "period_month",
        "raw_rows", "rows_count", "clients_count", "skus_count", "total_hl",
    )}
    out.update(freshness())
    return out
