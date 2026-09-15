from __future__ import annotations

from datetime import datetime
from io import StringIO
from pathlib import Path
import ast
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

PLANIFICACION_TOPES_URL = (
    "https://raw.githubusercontent.com/tomasddv/planificacion/main/"
    "dashboard_bultos_accion.py"
)
DEFAULT_TOPES_CANAL = {"K+T": 200.0, "AUTOSERVICIO": 500.0, "AS": 500.0}

PLANIFICACION_DRIVE_URL = "https://drive.google.com/drive/folders/1cukgXLUaPsEDK_yD7tSwgaBFZAbiDUot"
TOPES_DRIVE_DIR = CACHE_DIR / "topes_clientes_drive"

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


def _download_text(url: str) -> str:
    response = requests.get(
        url,
        timeout=25,
        headers={"Cache-Control": "no-cache", "User-Agent": "DDV-fast-assistant/1.0"},
    )
    response.raise_for_status()
    return response.text


def _parse_topes_rules(source_text: str) -> dict[str, float]:
    """Lee TOPES_CANAL directamente del dashboard de planificacion."""
    rules = dict(DEFAULT_TOPES_CANAL)
    try:
        match = re.search(r"TOPES_CANAL\s*=\s*(\{.*?\})", source_text or "", flags=re.S)
        if match:
            parsed = ast.literal_eval(match.group(1))
            for key, value in (parsed or {}).items():
                rules[str(key).strip().upper()] = float(value)
    except Exception:
        pass
    if "AUTOSERVICIO" in rules and "AS" not in rules:
        rules["AS"] = rules["AUTOSERVICIO"]
    return rules


def _channel_from_groups(groups_text: object) -> str:
    text = str(groups_text or "").upper()
    if "K+T" in text or re.search(r"\bK\s*\+\s*T\b", text):
        return "K+T"
    if "AUTOSERVICIO" in text:
        return "AS"
    # Los nombres de grupo del ERP suelen verse como "SUR- CORE AS ..." o "SUR- VALUE AS ...".
    if re.search(r"\b(?:CORE|VALUE)\s+AS\b", text) or re.search(r"\bAS\s*\(", text):
        return "AS"
    return ""


def _tope_for_group(groups_text: object, rules: dict[str, float]) -> tuple[str, float | None]:
    channel = _channel_from_groups(groups_text)
    if not channel:
        return "", None
    if channel == "AS":
        value = rules.get("AS", rules.get("AUTOSERVICIO"))
    else:
        value = rules.get(channel)
    return channel, float(value) if value is not None else None



def _norm_col(value: object) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def _classify_customer_channel(value: object) -> str:
    """Replica la clasificación usada por planificacion/app.py."""
    text = unicodedata.normalize("NFD", str(value or "").upper())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    if "AUTOSERV" in text:
        return "AUTOSERVICIO"
    if "MAYOR" in text:
        return "MAYORISTA"
    if "REFRIG" in text:
        return "REF"
    if any(term in text for term in ("TRADICIONAL", "KIOSCO", "CADENITA", "LISTA UNICA")):
        return "K+T"
    return "NO"


def _drive_items():
    import gdown
    return gdown.download_folder(
        url=PLANIFICACION_DRIVE_URL,
        output=".",
        quiet=True,
        use_cookies=False,
        skip_download=True,
    ) or []


def _pick_customer_master(items):
    candidates = []
    for item in items:
        name = Path(str(item.path)).name
        suffix = Path(name).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".csv", ".txt"}:
            continue
        clean = _norm_text(Path(name).stem)
        if "cliente" not in clean:
            continue
        score = 0
        compact = re.sub(r"\s+", "", clean)
        if "plantillaclientesar" in compact:
            score += 100
        if "plantilla" in clean:
            score += 20
        candidates.append((score, name.lower(), item))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[-1][2]


def _download_drive_item(item, target_dir: Path) -> Path:
    import gdown
    target_dir.mkdir(parents=True, exist_ok=True)
    name = Path(str(item.path)).name
    target = target_dir / name
    tmp = target_dir / f"{name}.part"
    tmp.unlink(missing_ok=True)
    result = gdown.download(id=item.id, output=str(tmp), quiet=True, use_cookies=False)
    if not result or not tmp.exists() or tmp.stat().st_size <= 0:
        raise RuntimeError(f"No se pudo descargar {name}")
    target.unlink(missing_ok=True)
    tmp.replace(target)
    return target


def _read_customer_master(path: Path, rules: dict[str, float]) -> dict[str, dict]:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        source = pd.read_excel(path, dtype="string")
    else:
        try:
            source = pd.read_csv(path, sep=None, engine="python", dtype="string")
        except Exception:
            source = pd.read_csv(path, sep=";", dtype="string")

    source = source.copy()
    source.columns = [_norm_col(c) for c in source.columns]

    code_candidates = (
        "cliente", "cod_cliente", "codigo_cliente", "nro_cliente",
        "codigocliente", "codigo", "id_cliente",
    )
    code_col = next((c for c in code_candidates if c in source.columns), None)
    if code_col is None:
        return {}

    name_candidates = (
        "fantasia", "nombre_fantasia", "descripcion", "cliente_descripcion",
        "razon_social", "nombre",
    )
    name_col = next((c for c in name_candidates if c in source.columns and c != code_col), None)

    channel_cols = [
        c for c in source.columns
        if any(term in c for term in (
            "descripcion_lista", "lista_de_precios", "lista_precio",
            "descripcion_subcanal", "subcanal",
            "descripcion_ramo", "ramo",
        ))
    ]
    if not channel_cols:
        return {}

    result: dict[str, dict] = {}
    for _, row in source.iterrows():
        cid = _norm_code(row.get(code_col))
        if not cid:
            continue
        channel_text = " ".join(str(row.get(c) or "") for c in channel_cols)
        channel = _classify_customer_channel(channel_text)
        if channel not in {"K+T", "AUTOSERVICIO"}:
            continue

        lookup = "AS" if channel == "AUTOSERVICIO" else "K+T"
        tope = rules.get(lookup)
        if tope is None and channel == "AUTOSERVICIO":
            tope = rules.get("AUTOSERVICIO")
        if tope is None:
            continue

        name = str(row.get(name_col) or "").strip() if name_col else ""
        result[cid] = {
            "id": cid,
            "name": name,
            "canal": lookup,
            "tope_core": float(tope),
            "tope_value": float(tope),
            "source": "Planificacion · maestro clientes Drive",
        }
    return result


def _load_customer_topes_from_drive(rules: dict[str, float]) -> dict[str, dict]:
    items = _drive_items()
    item = _pick_customer_master(items)
    if item is None:
        return {}
    if TOPES_DRIVE_DIR.exists():
        for child in TOPES_DRIVE_DIR.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
    path = _download_drive_item(item, TOPES_DRIVE_DIR)
    return _read_customer_master(path, rules)



def _build_snapshot(clients: pd.DataFrame, groups: pd.DataFrame, topes_rules: dict[str, float], customer_topes: dict[str, dict] | None = None) -> dict:
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

        grupos_text = str(row.get("grupos") or "").strip()
        canal_tope, tope_bultos = _tope_for_group(grupos_text, topes_rules)
        item = {
            "segmento": segmento,
            "subsegmento": subsegmento,
            "porcentaje_total": porcentaje,
            "grupos": grupos_text,
            "canal_tope": canal_tope,
            "tope_bultos": tope_bultos,
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

    customer_topes = customer_topes or {}

    # Fallback: si un cliente todavía no llegó desde el maestro de Planificacion,
    # intentamos inferir el canal desde su grupo comercial.
    for cid, rows in rows_by_customer.items():
        if cid in customer_topes:
            continue
        for r in rows:
            channel, value = _tope_for_group(r.get("grupos"), topes_rules)
            if channel and value is not None:
                customer_topes[cid] = {
                    "id": cid,
                    "name": customers.get(cid, {}).get("name", ""),
                    "canal": channel,
                    "tope_core": float(value),
                    "tope_value": float(value),
                    "source": "Planificacion · fallback grupo cliente",
                }
                break

    # Los topes son una fuente por cliente independiente del descuento.
    for cid, top in customer_topes.items():
        if cid not in customers:
            customers[cid] = {
                "id": cid,
                "name": top.get("name") or "",
                "promotor": "",
                "ruta": "",
            }
        elif not customers[cid].get("name") and top.get("name"):
            customers[cid]["name"] = top.get("name")

    latest = ""
    if "fecha" in clients.columns:
        try:
            latest = str(clients["fecha"].dropna().astype(str).max())
        except Exception:
            latest = ""

    return {
        "schema_version": 3,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": latest,
        "source": "Grupo de clientes + topes Planificacion · GitHub snapshot local",
        "topes_rules": topes_rules,
        "topes_by_customer": customer_topes,
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
            try:
                topes_source = _download_text(PLANIFICACION_TOPES_URL)
                topes_rules = _parse_topes_rules(topes_source)
            except Exception:
                topes_rules = dict(DEFAULT_TOPES_CANAL)

            try:
                customer_topes = _load_customer_topes_from_drive(topes_rules)
            except Exception:
                customer_topes = {}

            snapshot = _build_snapshot(clients, groups, topes_rules, customer_topes)
            _last_error = None
            return _save(snapshot)
        except Exception as exc:
            _last_error = exc
            raise



def status():
    snap = _load_disk()
    if snap and int(snap.get("schema_version") or 0) >= 3:
        data_date = snap.get("data_date") or "—"
        return {
            "ok": True,
            "name": "Grupo de clientes",
            "detail": f"{len(snap.get('customers', {}))} clientes · descuentos + topes por cliente · datos {data_date}",
            "loaded_at": snap.get("updated_at", "—"),
        }
    if snap:
        return {
            "ok": None,
            "name": "Grupo de clientes",
            "detail": "Actualizando snapshot para sumar topes CORE/VALUE por cliente desde Planificacion...",
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



def topes(customer_id: str, segment: str | None = None):
    """Devuelve el tope real por cliente obtenido del maestro usado por Planificacion."""
    snap = _load_disk()
    if not snap:
        return []

    cid = _norm_code(customer_id)
    wanted = str(segment or "").strip().upper()
    top = (snap.get("topes_by_customer") or {}).get(cid)

    if top:
        rows = []
        if wanted in {"", "CORE"} and top.get("tope_core") is not None:
            rows.append({
                "segmento": "CORE",
                "canal": top.get("canal") or "",
                "tope_bultos": float(top["tope_core"]),
                "source": top.get("source") or "",
            })
        if wanted in {"", "VALUE"} and top.get("tope_value") is not None:
            rows.append({
                "segmento": "VALUE",
                "canal": top.get("canal") or "",
                "tope_bultos": float(top["tope_value"]),
                "source": top.get("source") or "",
            })
        return rows

    # Compatibilidad/fallback con snapshots que sólo tengan grupos.
    rows = list(snap.get("rows_by_customer", {}).get(cid, []))
    if wanted in {"CORE", "VALUE"}:
        rows = [r for r in rows if str(r.get("segmento") or "").upper() == wanted]
    else:
        rows = [r for r in rows if str(r.get("segmento") or "").upper() in {"CORE", "VALUE"}]

    out = []
    seen = set()
    for r in rows:
        seg = str(r.get("segmento") or "").upper()
        channel = str(r.get("canal_tope") or "")
        value = r.get("tope_bultos")
        if value is None:
            continue
        key = (seg, channel, float(value))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "segmento": seg,
            "canal": channel,
            "tope_bultos": float(value),
            "source": "Planificacion · fallback grupo cliente",
        })
    return sorted(out, key=lambda r: (r.get("segmento") or "", r.get("canal") or ""))


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
