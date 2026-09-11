"""Topes base y segundo tramo: reglas de Planificacion + maestro + extensiones.

No imports of dashboard UI. Rules are read as literals, never executed.
Channel classification and extension semantics match Planificacion ce5200e.
"""
from pathlib import Path
from io import BytesIO
import ast
import json
import os
import re
import threading
import unicodedata
import pandas as pd
import requests
from . import frescura_source
from .health import describe, stamp
from . import planificacion_sales

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "data/cache/topes_snapshot.json"
RULES_URL = "https://raw.githubusercontent.com/tomasddv/planificacion/main/dashboard_bultos_accion.py"
_snapshot = None
_last_error = None
_lock = threading.RLock()


def clean(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def norm(value):
    text = unicodedata.normalize("NFKD", clean(value))
    return re.sub(r"[^a-z0-9]+", "_", "".join(c for c in text if not unicodedata.combining(c)).lower()).strip("_")


def code(value):
    text = clean(value)
    if re.fullmatch(r"\d+(?:\.0)?", text):
        return str(int(float(text)))
    return ""


def read_rules(text):
    wanted = {"TOPES_CANAL", "DEFAULT_SHEET_URL", "EXTENSION_SHEET_NAME"}
    values = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    values[target.id] = ast.literal_eval(node.value)
    if wanted - values.keys():
        raise ValueError("Planificacion no contiene las reglas esperadas de topes.")
    if not isinstance(values["TOPES_CANAL"], dict) or not {"K+T", "AUTOSERVICIO", "AS"}.issubset(values["TOPES_CANAL"]):
        raise ValueError("Faltan los canales de Planificacion.")
    if not all(isinstance(v, (int, float)) and v > 0 for v in values["TOPES_CANAL"].values()):
        raise ValueError("Topes de canal inválidos.")
    return values


def classify(text):
    text = norm(text).upper()
    if "AUTOSERV" in text:
        return "AUTOSERVICIO"
    if "MAYOR" in text:
        return "MAYORISTA"
    if "REFRIG" in text:
        return "REF"
    if any(t in text for t in ("TRADICIONAL", "KIOSCO", "CADENITA", "LISTA_UNICA")):
        return "K+T"
    return "NO"


def read_customers(path):
    # Planificacion's ERP master uses row 2 as the header in all these sheets.
    with pd.ExcelFile(path) as book:
        if "Clientes" not in book.sheet_names:
            raise ValueError("Falta la hoja Clientes en el maestro.")
        clients = pd.read_excel(book, sheet_name="Clientes", header=1, dtype=str).fillna("")
        lists = pd.read_excel(book, sheet_name="Listas de precios", header=1, dtype=str).fillna("") if "Listas de precios" in book.sheet_names else pd.DataFrame()
        hierarchy_name = next((n for n in book.sheet_names if "jerarqu" in norm(n)), None)
        hierarchy = pd.read_excel(book, sheet_name=hierarchy_name, header=1, dtype=str).fillna("") if hierarchy_name else pd.DataFrame()
    for frame in (clients, lists, hierarchy):
        frame.columns = [norm(c) for c in frame.columns]
    if not {"cliente", "lista_de_precios", "subcanal_mkt"}.issubset(clients.columns):
        raise ValueError("El maestro no tiene cliente, lista de precios y subcanal MKT.")
    prices = {code(r.get("codigo")): clean(r.get("descripcion")) for r in lists.to_dict("records") if code(r.get("codigo"))}
    channels = {code(r.get("codigo")): " ".join(clean(r.get(k)) for k in ("segmento_mkt", "canal_mkt", "subcanal_mkt")) for r in hierarchy.to_dict("records") if code(r.get("codigo"))}
    result = {}
    for row in clients.to_dict("records"):
        cid = code(row.get("cliente"))
        if not cid or cid in result:
            continue
        text = prices.get(code(row.get("lista_de_precios")), "") + " " + channels.get(code(row.get("subcanal_mkt")), "")
        result[cid] = {"id": cid, "name": clean(row.get("nombre_de_fantasia")) or clean(row.get("razon_social")), "canal": classify(text)}
    if not result:
        raise ValueError("El maestro de clientes está vacío.")
    return result


def parse_extensions(frame):
    frame = frame.copy()
    frame.columns = [norm(c) for c in frame.columns]
    code_col = next((c for c in ("cliente_codigo", "cod_cliente", "codigo_cliente") if c in frame.columns), None)
    if not code_col or "accion" not in frame.columns:
        raise ValueError("La hoja de extensiones no contiene cliente y acción.")
    result = {}
    for row in frame.to_dict("records"):
        cid, action = code(row.get(code_col)), clean(row.get("accion")).upper()
        if not cid or action not in {"CORE", "VALUE"}:
            continue
        raw_date = clean(row.get("fecha_extension"))
        date = pd.to_datetime(raw_date, errors="coerce", dayfirst=not bool(re.match(r"^\d{4}-", raw_date)))
        active = clean(row.get("activa", "true")).lower() in {"1", "true", "si", "sí", "yes", "y", "activo", "activa"}
        # Same last-row-wins rule as Planificacion, including deactivation.
        result[f"{cid}:{action}"] = {"active": active and pd.notna(date), "date": date.strftime("%Y-%m-%d") if pd.notna(date) else None}
    return result


def download_extensions(url, sheet):
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError("URL de extensiones inválida.")
    response = requests.get(f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx", timeout=30)
    response.raise_for_status()
    with pd.ExcelFile(BytesIO(response.content)) as book:
        name = next((n for n in book.sheet_names if norm(n) == norm(sheet)), None)
        if name is None:
            raise ValueError("No se encontró la hoja de ampliaciones de Planificacion.")
        return parse_extensions(pd.read_excel(book, sheet_name=name, dtype=str))


def _load_disk():
    global _snapshot
    if _snapshot is None and SNAPSHOT_PATH.exists():
        try:
            _snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return _snapshot


def refresh(force=True):
    global _snapshot, _last_error
    with _lock:
        try:
            response = requests.get(RULES_URL, timeout=25)
            response.raise_for_status()
            rules = read_rules(response.text)
            items = frescura_source._drive_items()
            item = frescura_source._pick(items, ("plantillaclientesar",), (".xlsx",))
            if not item:
                raise ValueError("No se encontró el maestro de clientes de Planificacion.")
            path = frescura_source._download(item, ROOT / "data/topes-runtime")
            customers = read_customers(path)
            extensions, extension_error = {}, None
            try:
                extensions = download_extensions(os.getenv("PLANIFICACION_EXTENSIONS_SHEET_URL") or rules["DEFAULT_SHEET_URL"], rules["EXTENSION_SHEET_NAME"])
            except Exception as exc:
                extension_error = f"{type(exc).__name__}: {exc}"
            sales, sales_error = None, None
            try:
                sales_item = frescura_source._pick(items, ("venta", "bulto"), (".txt", ".csv"))
                aux_item = frescura_source._pick(items, ("auxiliares",), (".xlsx",))
                if not sales_item or not aux_item:
                    raise ValueError("Faltan ventas en bultos o AUXILIARES de Planificación.")
                sales_path = frescura_source._download(sales_item, ROOT / "data/topes-runtime")
                aux_path = frescura_source._download(aux_item, ROOT / "data/topes-runtime")
                sales = planificacion_sales.load_daily(sales_path, aux_path)
            except Exception as exc:
                sales_error = f"{type(exc).__name__}: {exc}"
            snapshot = {"schema_version": 2, "updated_at": stamp(), "customers": customers,
                        "rules": rules["TOPES_CANAL"], "extensions": extensions,
                        "sales": sales, "sales_error": sales_error,
                        "extensions_error": extension_error, "master_file": path.name,
                        "rules_url": RULES_URL}
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SNAPSHOT_PATH)
            _snapshot, _last_error = snapshot, None
            return snapshot
        except Exception as exc:
            _last_error = exc
            raise


def status():
    snap = _load_disk()
    result = describe("Topes Planificación", snap, _last_error, detail="Topes Core/Value desde maestro y ampliaciones de Planificación")
    if snap and snap.get("extensions_error"):
        result["warning"] = (result["warning"] + " No se pudieron verificar las ampliaciones; sólo se informa el tope base.").strip()
    if snap and not snap.get("sales"):
        result["warning"] = (result["warning"] + " Ventas en bultos sin verificar; no se informa comprado ni saldo.").strip()
    return result


def customer(cid):
    return (_load_disk() or {}).get("customers", {}).get(code(cid))


def search_customers(query):
    q = norm(query)
    return [c for c in (_load_disk() or {}).get("customers", {}).values() if q and q in norm(c.get("name"))][:8]


def topes(cid, segment=None):
    snap = _load_disk() or {}
    c = customer(cid)
    if not c:
        return []
    base = snap.get("rules", {}).get(c["canal"])
    if base is None:
        return []
    rows = []
    for action in ("CORE", "VALUE"):
        if segment and action != segment:
            continue
        extension = snap.get("extensions", {}).get(f"{c['id']}:{action}", {})
        second = base if extension.get("active") else 0
        rows.append({"segmento": action, "canal": c["canal"], "base": base,
                     "second": second, "total": base + second, "date": extension.get("date"),
                     "purchases": planificacion_sales.purchases(snap.get("sales"), c['id'], action, base, extension),
                     "extensions_verified": not bool(snap.get("extensions_error"))})
    return rows
