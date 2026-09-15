from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

import pandas as pd

from sources import frescura_source, grupos_source, repago_source

MAX_RESULT_ROWS = 40
DISPLAY_ROWS = 15


# -----------------------------
# Public status / routing
# -----------------------------

def _secret(name: str, default: str = "") -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if value:
        return value
    try:
        import streamlit as st
        return str(st.secrets.get(name, default) or default).strip()
    except Exception:
        return str(default or "").strip()


def analyst_status() -> dict[str, Any]:
    enabled = bool(_secret("OPENAI_API_KEY"))
    return {
        "enabled": enabled,
        "model": _secret("OPENAI_ANALYST_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        "detail": "Analista IA activo" if enabled else "Falta OPENAI_API_KEY en Secrets",
    }


def should_analyze(question: str) -> bool:
    """Detecta preguntas que piden análisis/ranking/comparación y no una búsqueda puntual.

    No intenta comprender toda la pregunta. Sólo decide si conviene saltar directamente
    al analista universal. Si devuelve False, las reglas rápidas existentes siguen teniendo
    prioridad y el analista queda igualmente como fallback final.
    """
    q = _norm(question)
    if not q:
        return False

    plural_or_set = any(term in q for term in (
        "que productos", "cuales productos", "que sku", "cuales sku",
        "que clientes", "cuales clientes", "de los clientes", "entre los clientes",
        "que edf", "cuales edf", "que heladeras", "cuales heladeras",
        "listame", "lista de", "ranking", "top ", "los que", "las que",
    ))
    comparative = any(term in q for term in (
        "mas que", "menos que", "mayor", "menor", "mucho", "poco", "sobrado",
        "sobrante", "faltante", "diferencia", "desbalance", "compar", "mejor", "peor",
        "alto", "bajo", "flojo", "fuerte", "riesgo de quiebre", "oportunidad",
    ))
    aggregate = any(term in q for term in (
        "promedio", "total", "cuantos clientes", "cuantas clientes", "cuantos sku",
        "cuantos productos", "suma", "acumulado",
        "porcentaje de", "proporcion", "distribucion",
    ))
    cross_source = sum(bool(x) for x in (
        any(k in q for k in ("repago", "heladera", "edf", "serie")),
        any(k in q for k in ("descuento", "core", "value")),
        any(k in q for k in ("tope", "bulto")),
        any(k in q for k in ("stock", "frescura", "venc", "sku", "producto")),
    )) >= 2

    return plural_or_set or comparative or aggregate or cross_source


# -----------------------------
# Snapshot -> local SQL tables
# -----------------------------

def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _safe_snap(module) -> dict:
    try:
        snap = module._load_disk()  # fuentes internas de la misma app
        return snap or {}
    except Exception:
        return {}


def _frescura_frames() -> dict[str, pd.DataFrame]:
    snap = _safe_snap(frescura_source)
    products = []
    for sku, row in (snap.get("products") or {}).items():
        products.append({
            "codigo": str(row.get("codigo") or sku),
            "descripcion": row.get("descripcion") or "",
            "stock_trelew": _num(row.get("stock_trelew")) or 0.0,
            "stock_madryn": _num(row.get("stock_madryn")) or 0.0,
            "stock_total_ddv": _num(row.get("stock_total_ddv")) or 0.0,
        })

    lots = []
    for row in snap.get("forecast_ddv") or []:
        lots.append(_lot_row(row, "TOTAL DDV"))
    for row in snap.get("forecast_base") or []:
        city = str(row.get("ciudad") or "").upper()
        scope = "TRELEW" if "TRELEW" in city else "MADRYN" if "MADRYN" in city else city
        lots.append(_lot_row(row, scope))

    profiles = []
    for row in snap.get("profiles_ddv") or []:
        profiles.append(_profile_row(row, "TOTAL DDV"))
    for row in snap.get("profiles_base") or []:
        loc = str(row.get("loc") or "").upper()
        scope = "TRELEW" if "TRELEW" in loc else "MADRYN" if "MADRYN" in loc else loc
        profiles.append(_profile_row(row, scope))

    return {
        "frescura_productos": pd.DataFrame(products),
        "frescura_lotes": pd.DataFrame(lots),
        "frescura_perfiles": pd.DataFrame(profiles),
    }


def _lot_row(row: dict, scope: str) -> dict:
    return {
        "codigo": str(row.get("codigo") or ""),
        "descripcion": row.get("descripcion") or "",
        "localidad": scope,
        "stock_lote": _num(row.get("stock_lote")) or 0.0,
        "vencimiento": str(row.get("fecha_vencimiento") or ""),
        "dias_para_vencer": _num(row.get("dias_para_vencer")),
        "salida_estimada_hasta_vencimiento": _num(row.get("venta_estimada_hasta_vto")) or 0.0,
        "riesgo_bultos": _num(row.get("bultos_riesgo")) or 0.0,
        "riesgo_pct": _num(row.get("riesgo_pct")) or 0.0,
        "estado": row.get("estado_predictivo") or "",
        "agotamiento_estimado": str(row.get("agotamiento_estimado") or ""),
        "venta_semanal_estimada": _num(row.get("venta_semanal_estimada")) or 0.0,
        "venta_mensual_estimada": _num(row.get("venta_mensual_estimada")) or 0.0,
        "incremento_necesario_pct": _num(row.get("incremento_necesario_pct")),
        "confianza": _num(row.get("confianza")),
    }


def _profile_row(row: dict, scope: str) -> dict:
    return {
        "codigo": str(row.get("sku") or ""),
        "descripcion": row.get("description") or "",
        "localidad": scope,
        "venta_semanal_bultos": _num(row.get("weekly_bultos")) or 0.0,
        "venta_mensual_bultos": _num(row.get("monthly_bultos")) or 0.0,
        "venta_normal_semanal_bultos": _num(row.get("normal_weekly_bultos")) or 0.0,
        "venta_super_semanal_bultos": _num(row.get("super_weekly_bultos")) or 0.0,
        "stock_total": _num(row.get("stock_total")) or 0.0,
        "dias_cobertura_dinamica": _num(row.get("dynamic_coverage_days")),
        "fecha_agotamiento_sku": str(row.get("sku_depletion_date") or ""),
        "confianza": _num(row.get("confidence")),
    }


def _repago_frames() -> dict[str, pd.DataFrame]:
    snap = _safe_snap(repago_source)
    customers = []
    for cid, c in (snap.get("customers") or {}).items():
        monthly = c.get("monthly_by_business") or {}
        latest = c.get("latest_by_business") or {}
        customers.append({
            "cliente_codigo": str(cid),
            "cliente": c.get("name") or "",
            "razon_social": c.get("legal_name") or "",
            "vendedor": c.get("seller") or "",
            "ruta": c.get("route") or "",
            "direccion": c.get("address") or "",
            "localidad": c.get("city") or "",
            "ultimo_mes_completo": c.get("monthly_period") or "",
            "hl_ultimo_mes_completo": _num(c.get("monthly_hl")) or 0.0,
            "hl_cza_ultimo_mes": _num(monthly.get("CZA")) or 0.0,
            "hl_ung_ultimo_mes": _num(monthly.get("UNG")) or 0.0,
            "hl_aguas_ultimo_mes": _num(monthly.get("AGUAS")) or 0.0,
            "hl_rb_ultimo_mes": _num(monthly.get("RB")) or 0.0,
            "ultimo_periodo_disponible": c.get("latest_period") or "",
            "hl_ultimo_periodo": _num(c.get("latest_hl")) or 0.0,
            "hl_cza_ultimo_periodo": _num(latest.get("CZA")) or 0.0,
            "hl_ung_ultimo_periodo": _num(latest.get("UNG")) or 0.0,
            "hl_aguas_ultimo_periodo": _num(latest.get("AGUAS")) or 0.0,
            "hl_rb_ultimo_periodo": _num(latest.get("RB")) or 0.0,
        })

    trim = _repago_rows_map(snap.get("rows_by_customer") or {})
    last = _repago_rows_map(snap.get("rows_by_customer_latest") or {})

    edfs = []
    for raw in snap.get("all_edfs") or []:
        cid = str(raw.get("customer_id") or "")
        key = _edf_key(cid, raw)
        tr = trim.get(key, {})
        lm = last.get(key, {})
        edfs.append({
            "cliente_codigo": cid,
            "cliente": raw.get("customer_name") or "",
            "razon_social": raw.get("legal_name") or "",
            "activo": raw.get("asset") or "",
            "serie": raw.get("serial") or "",
            "modelo": raw.get("model") or "",
            "negocio": raw.get("business") or "",
            "estado": raw.get("status") or "",
            "deposito": raw.get("deposit") or "",
            "direccion": raw.get("address") or "",
            "localidad": raw.get("city") or "",
            "objetivo_hl": _num(raw.get("target")) or 0.0,
            "repago_trimestre_pct": _num(tr.get("pct")) or 0.0,
            "hl_asignado_trimestre": _num(tr.get("hl")) or 0.0,
            "banda_trimestre": tr.get("band") or "",
            "repago_ultimo_mes_pct": _num(lm.get("pct")) or 0.0,
            "hl_asignado_ultimo_mes": _num(lm.get("hl")) or 0.0,
            "banda_ultimo_mes": lm.get("band") or "",
        })

    monthly_sales = []
    for row in snap.get("monthly_sales") or []:
        monthly_sales.append({
            "cliente_codigo": str(row.get("cliente_codigo") or ""),
            "cliente": row.get("cliente") or "",
            "periodo": str(row.get("periodo") or ""),
            "negocio": row.get("negocio") or "",
            "hl": _num(row.get("hl")) or 0.0,
        })

    # Compatibilidad con snapshot anterior a schema 4: al menos expone el último mes completo.
    if not monthly_sales:
        for cid, c in (snap.get("customers") or {}).items():
            period = str(c.get("monthly_period") or "")
            for business, value in (c.get("monthly_by_business") or {}).items():
                monthly_sales.append({
                    "cliente_codigo": str(cid),
                    "cliente": c.get("name") or "",
                    "periodo": period,
                    "negocio": business,
                    "hl": _num(value) or 0.0,
                })

    return {
        "clientes": pd.DataFrame(customers),
        "repago_edf": pd.DataFrame(edfs),
        "ventas_mensuales_cliente": pd.DataFrame(monthly_sales),
    }


def _edf_key(cid: str, row: dict) -> tuple[str, str, str, str]:
    return (
        str(cid or ""),
        str(row.get("asset") or ""),
        str(row.get("serial") or ""),
        str(row.get("business") or ""),
    )


def _repago_rows_map(rows_by_customer: dict) -> dict:
    out = {}
    for cid, rows in rows_by_customer.items():
        for row in rows or []:
            out[_edf_key(str(cid), row)] = row
    return out


def _grupos_frames() -> dict[str, pd.DataFrame]:
    snap = _safe_snap(grupos_source)
    customers = snap.get("customers") or {}
    discounts = []
    for cid, rows in (snap.get("rows_by_customer") or {}).items():
        name = (customers.get(str(cid)) or {}).get("name") or ""
        for row in rows or []:
            discounts.append({
                "cliente_codigo": str(cid),
                "cliente": name or row.get("fantasia") or "",
                "segmento": row.get("segmento") or "",
                "subsegmento": row.get("subsegmento") or "",
                "descuento_pct": (_num(row.get("porcentaje_total")) or 0.0) * 100.0,
                "grupo": row.get("grupos") or "",
                "promotor": row.get("promotor") or "",
                "ruta": row.get("ruta") or "",
            })

    topes = []
    topes_by_customer = snap.get("topes_by_customer") or {}
    if topes_by_customer:
        for cid, row in topes_by_customer.items():
            name = (customers.get(str(cid)) or {}).get("name") or row.get("name") or ""
            for seg, field in (("CORE", "tope_core"), ("VALUE", "tope_value")):
                value = _num(row.get(field))
                if value is None:
                    continue
                topes.append({
                    "cliente_codigo": str(cid),
                    "cliente": name,
                    "segmento": seg,
                    "canal": row.get("canal") or "",
                    "tope_bultos": value,
                })
    else:
        # Fallback para snapshots viejos basados en filas de Grupo de clientes.
        seen = set()
        for cid, rows in (snap.get("rows_by_customer") or {}).items():
            name = (customers.get(str(cid)) or {}).get("name") or ""
            for row in rows or []:
                seg = str(row.get("segmento") or "").upper()
                if seg not in {"CORE", "VALUE"}:
                    continue
                value = _num(row.get("tope_bultos"))
                if value is None:
                    continue
                key = (str(cid), seg, str(row.get("canal_tope") or ""), value)
                if key in seen:
                    continue
                seen.add(key)
                topes.append({
                    "cliente_codigo": str(cid),
                    "cliente": name,
                    "segmento": seg,
                    "canal": row.get("canal_tope") or "",
                    "tope_bultos": value,
                })

    return {
        "descuentos_cliente": pd.DataFrame(discounts),
        "topes_cliente": pd.DataFrame(topes),
    }


def _frames() -> dict[str, pd.DataFrame]:
    frames = {}
    frames.update(_frescura_frames())
    frames.update(_repago_frames())
    frames.update(_grupos_frames())
    return frames


EMPTY_SCHEMAS = {
    "frescura_productos": ["codigo", "descripcion", "stock_trelew", "stock_madryn", "stock_total_ddv"],
    "frescura_lotes": ["codigo", "descripcion", "localidad", "stock_lote", "vencimiento", "dias_para_vencer", "salida_estimada_hasta_vencimiento", "riesgo_bultos", "riesgo_pct", "estado", "agotamiento_estimado", "venta_semanal_estimada", "venta_mensual_estimada", "incremento_necesario_pct", "confianza"],
    "frescura_perfiles": ["codigo", "descripcion", "localidad", "venta_semanal_bultos", "venta_mensual_bultos", "venta_normal_semanal_bultos", "venta_super_semanal_bultos", "stock_total", "dias_cobertura_dinamica", "fecha_agotamiento_sku", "confianza"],
    "clientes": ["cliente_codigo", "cliente", "razon_social", "vendedor", "ruta", "direccion", "localidad", "ultimo_mes_completo", "hl_ultimo_mes_completo", "hl_cza_ultimo_mes", "hl_ung_ultimo_mes", "hl_aguas_ultimo_mes", "hl_rb_ultimo_mes", "ultimo_periodo_disponible", "hl_ultimo_periodo", "hl_cza_ultimo_periodo", "hl_ung_ultimo_periodo", "hl_aguas_ultimo_periodo", "hl_rb_ultimo_periodo"],
    "repago_edf": ["cliente_codigo", "cliente", "razon_social", "activo", "serie", "modelo", "negocio", "estado", "deposito", "direccion", "localidad", "objetivo_hl", "repago_trimestre_pct", "hl_asignado_trimestre", "banda_trimestre", "repago_ultimo_mes_pct", "hl_asignado_ultimo_mes", "banda_ultimo_mes"],
    "ventas_mensuales_cliente": ["cliente_codigo", "cliente", "periodo", "negocio", "hl"],
    "descuentos_cliente": ["cliente_codigo", "cliente", "segmento", "subsegmento", "descuento_pct", "grupo", "promotor", "ruta"],
    "topes_cliente": ["cliente_codigo", "cliente", "segmento", "canal", "tope_bultos"],
}


def _connection() -> tuple[sqlite3.Connection, dict[str, pd.DataFrame]]:
    frames = _frames()
    conn = sqlite3.connect(":memory:")
    conn.create_function("norm", 1, _norm)
    # Evita consultas accidentales demasiado costosas.
    steps = {"n": 0}

    def progress():
        steps["n"] += 1
        return 1 if steps["n"] > 4000 else 0

    conn.set_progress_handler(progress, 1000)
    for name, df in frames.items():
        if df is None:
            df = pd.DataFrame(columns=EMPTY_SCHEMAS.get(name, []))
        elif df.empty and len(df.columns) == 0:
            df = pd.DataFrame(columns=EMPTY_SCHEMAS.get(name, []))
        df.to_sql(name, conn, index=False, if_exists="replace")
    return conn, frames



# -----------------------------
# Analizador local GRATIS
# -----------------------------

def analyst_status() -> dict[str, Any]:
    return {
        "enabled": True,
        "model": "Motor local FREE",
        "detail": "Analista local activo · sin API · sin costo por consulta",
    }


def should_analyze(question: str) -> bool:
    """Deriva al analista local preguntas de conjuntos, comparaciones, rankings y cruces."""
    q = _norm(question)
    if not q:
        return False

    plural_or_set = any(term in q for term in (
        "que productos", "cuales productos", "qué productos", "que sku", "cuales sku",
        "que clientes", "cuales clientes", "qué clientes", "de los clientes", "entre los clientes",
        "que edf", "cuales edf", "que heladeras", "cuales heladeras",
        "listame", "lista de", "ranking", "top ", "los que", "las que",
    ))
    comparative = any(term in q for term in (
        "mas que", "menos que", "mayor", "menor", "mucho", "poco", "sobrado",
        "sobrante", "faltante", "diferencia", "desbalance", "compar", "mejor", "peor",
        "alto", "bajo", "flojo", "fuerte", "riesgo de quiebre", "oportunidad",
    ))
    aggregate = any(term in q for term in (
        "promedio", "total", "cuantos clientes", "cuantos sku", "cuantos productos",
        "suma", "acumulado", "porcentaje de", "proporcion", "distribucion",
    ))
    cross_source = sum(bool(x) for x in (
        any(k in q for k in ("repago", "heladera", "edf", "serie")),
        any(k in q for k in ("descuento", "core", "value")),
        any(k in q for k in ("tope", "bulto")),
        any(k in q for k in ("stock", "frescura", "venc", "sku", "producto")),
        any(k in q for k in ("venta", "compra", "hl", "hectolit")),
    )) >= 2
    return plural_or_set or comparative or aggregate or cross_source


def _extract_limit(q: str, default: int = 15) -> int:
    patterns = (
        r"\btop\s+(\d{1,2})\b",
        r"\blos\s+(\d{1,2})\s+(?:productos|sku|clientes|edf|heladeras)\b",
        r"\bprimer[oa]s?\s+(\d{1,2})\b",
    )
    for p in patterns:
        m = re.search(p, q)
        if m:
            return max(1, min(int(m.group(1)), 40))
    return default


def _location(q: str) -> str | None:
    if "madryn" in q:
        return "MADRYN"
    if "trelew" in q:
        return "TRELEW"
    if "total ddv" in q or re.search(r"\bddv\b", q):
        return "TOTAL DDV"
    return None


def _segment(q: str) -> str | None:
    if "core" in q:
        return "CORE"
    if "value" in q:
        return "VALUE"
    return None


def _repago_field(q: str) -> str:
    if any(x in q for x in ("ultimo mes", "último mes", "mes corriente", "mes actual")):
        return "repago_ultimo_mes_pct"
    return "repago_trimestre_pct"


def _threshold_after(words: tuple[str, ...], q: str) -> float | None:
    escaped = "|".join(re.escape(w) for w in words)
    m = re.search(rf"(?:{escaped})\s*(?:del|de|a|que)?\s*(\d+(?:[.,]\d+)?)\s*%?", q)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def _client_name_condition(q: str) -> str:
    # El analista libre trabaja principalmente con conjuntos. La búsqueda individual
    # queda en el motor rápido del bot. No fuerza nombres ambiguos dentro de SQL.
    return ""


def _stock_comparison_plan(q: str) -> dict | None:
    if "stock" not in q and not any(k in q for k in ("producto", "productos", "sku")):
        return None
    if not ("madryn" in q and "trelew" in q):
        return None

    limit = _extract_limit(q)
    # Dirección inferida por frases relativas.
    madryn_low = (
        ("poco" in q or "bajo" in q or "menos" in q or "falt" in q)
        and q.find("madryn") >= 0
        and (
            re.search(r"(poco|bajo|menos|falt\w*)[^.]{0,35}madryn", q)
            or re.search(r"madryn[^.]{0,35}(poco|bajo|menos|falt\w*)", q)
        )
    )
    trelew_low = (
        ("poco" in q or "bajo" in q or "menos" in q or "falt" in q)
        and q.find("trelew") >= 0
        and (
            re.search(r"(poco|bajo|menos|falt\w*)[^.]{0,35}trelew", q)
            or re.search(r"trelew[^.]{0,35}(poco|bajo|menos|falt\w*)", q)
        )
    )

    if madryn_low and not trelew_low:
        sql = f"""
        SELECT codigo, descripcion,
               ROUND(stock_madryn,1) AS stock_madryn,
               ROUND(stock_trelew,1) AS stock_trelew,
               ROUND(stock_trelew-stock_madryn,1) AS diferencia_bultos,
               ROUND(CASE WHEN stock_trelew>0 THEN stock_madryn/stock_trelew ELSE NULL END,3) AS relacion_madryn_trelew
        FROM frescura_productos
        WHERE stock_trelew > 0 AND stock_trelew > stock_madryn
        ORDER BY (stock_trelew-stock_madryn) DESC,
                 CASE WHEN stock_trelew>0 THEN stock_madryn/stock_trelew ELSE 999 END ASC
        LIMIT {limit}
        """
        return {
            "action": "query",
            "title": "Productos con poco stock en Madryn y más stock en Trelew",
            "sql": sql,
            "assumption": "Interpreté “poco/mucho” como los mayores desbalances relativos y absolutos entre ambas bases.",
            "clarifying_question": "",
            "reason": "",
        }

    if trelew_low and not madryn_low:
        sql = f"""
        SELECT codigo, descripcion,
               ROUND(stock_trelew,1) AS stock_trelew,
               ROUND(stock_madryn,1) AS stock_madryn,
               ROUND(stock_madryn-stock_trelew,1) AS diferencia_bultos,
               ROUND(CASE WHEN stock_madryn>0 THEN stock_trelew/stock_madryn ELSE NULL END,3) AS relacion_trelew_madryn
        FROM frescura_productos
        WHERE stock_madryn > 0 AND stock_madryn > stock_trelew
        ORDER BY (stock_madryn-stock_trelew) DESC,
                 CASE WHEN stock_madryn>0 THEN stock_trelew/stock_madryn ELSE 999 END ASC
        LIMIT {limit}
        """
        return {
            "action": "query",
            "title": "Productos con poco stock en Trelew y más stock en Madryn",
            "sql": sql,
            "assumption": "Interpreté “poco/mucho” como los mayores desbalances relativos y absolutos entre ambas bases.",
            "clarifying_question": "",
            "reason": "",
        }

    # Comparación genérica de stock entre bases.
    sql = f"""
    SELECT codigo, descripcion,
           ROUND(stock_trelew,1) AS stock_trelew,
           ROUND(stock_madryn,1) AS stock_madryn,
           ROUND(ABS(stock_trelew-stock_madryn),1) AS diferencia_bultos,
           CASE WHEN stock_trelew > stock_madryn THEN 'TRELEW'
                WHEN stock_madryn > stock_trelew THEN 'MADRYN'
                ELSE 'IGUAL' END AS base_con_mas_stock
    FROM frescura_productos
    ORDER BY ABS(stock_trelew-stock_madryn) DESC
    LIMIT {limit}
    """
    return {
        "action": "query",
        "title": "Mayor desbalance de stock entre Trelew y Madryn",
        "sql": sql,
        "assumption": "Ordené por diferencia absoluta de stock entre las dos bases.",
        "clarifying_question": "",
        "reason": "",
    }


def _stock_ranking_plan(q: str) -> dict | None:
    if "stock" not in q and not any(k in q for k in ("productos", "sku")):
        return None
    loc = _location(q)
    if loc is None:
        return None
    limit = _extract_limit(q)
    col = {"MADRYN":"stock_madryn", "TRELEW":"stock_trelew", "TOTAL DDV":"stock_total_ddv"}[loc]

    if any(k in q for k in ("sin stock", "stock cero", "stock 0")):
        where = f"{col} <= 0"
        order = "descripcion"
        title = f"Productos sin stock en {loc.title() if loc != 'TOTAL DDV' else 'Total DDV'}"
    elif any(k in q for k in ("poco", "menos stock", "menor stock", "bajo stock", "escaso")):
        where = f"{col} >= 0"
        order = f"{col} ASC"
        title = f"Productos con menor stock en {loc.title() if loc != 'TOTAL DDV' else 'Total DDV'}"
    elif any(k in q for k in ("mucho", "mas stock", "mayor stock", "alto stock", "sobrado")):
        where = f"{col} >= 0"
        order = f"{col} DESC"
        title = f"Productos con mayor stock en {loc.title() if loc != 'TOTAL DDV' else 'Total DDV'}"
    else:
        return None

    return {
        "action": "query",
        "title": title,
        "sql": f"""SELECT codigo, descripcion, ROUND({col},1) AS stock_bultos
                   FROM frescura_productos
                   WHERE {where}
                   ORDER BY {order}
                   LIMIT {limit}""",
        "assumption": "Usé el stock físico actual del snapshot de Frescura.",
        "clarifying_question": "",
        "reason": "",
    }


def _freshness_plan(q: str) -> dict | None:
    if not any(k in q for k in ("venc", "riesgo", "critico", "crítico", "accionar", "frescura")):
        return None
    limit = _extract_limit(q)
    loc = _location(q) or "TOTAL DDV"
    filters = [f"localidad='{loc}'"]
    if "crit" in q:
        filters.append("UPPER(estado)='CRITICO'")
    elif "accion" in q:
        filters.append("UPPER(estado)='ACCIONAR'")
    if any(k in q for k in ("riesgo", "venc", "frescura")):
        filters.append("(riesgo_bultos > 0 OR dias_para_vencer IS NOT NULL)")
    where = " AND ".join(filters)
    return {
        "action": "query",
        "title": f"Frescura y riesgo · {loc}",
        "sql": f"""
            SELECT codigo, descripcion, vencimiento, ROUND(stock_lote,1) AS stock_lote,
                   ROUND(riesgo_bultos,1) AS riesgo_bultos, estado, dias_para_vencer
            FROM frescura_lotes
            WHERE {where}
            ORDER BY riesgo_bultos DESC, dias_para_vencer ASC
            LIMIT {limit}
        """,
        "assumption": "Priorizo mayor riesgo en bultos y vencimientos más cercanos.",
        "clarifying_question": "",
        "reason": "",
    }


def _customer_cross_plan(q: str) -> dict | None:
    customer_domain = any(k in q for k in (
        "cliente", "clientes", "heladera", "heladeras", "edf", "repago",
        "descuento", "tope", "compra", "venta", "hl", "hectolit"
    ))
    if not customer_domain:
        return None

    limit = _extract_limit(q)
    rep_field = _repago_field(q)
    seg = _segment(q)

    # Cantidad de EDF + repago.
    edf_n = None
    m = re.search(r"(?:mas de|más de|mayor a|al menos)\s+(\d+)\s*(?:heladeras?|edf|equipos?)", q)
    if m:
        edf_n = int(m.group(1))
    else:
        m = re.search(r"(\d+)\s*(?:o mas|o más)\s*(?:heladeras?|edf|equipos?)", q)
        if m:
            edf_n = int(m.group(1))

    rep_low = _threshold_after(("menos del", "menos de", "menor a", "debajo de"), q)
    rep_high = _threshold_after(("mas del", "más del", "mas de", "más de", "mayor a", "arriba de"), q)

    if ("repago" in q or "repag" in q) and (edf_n is not None or rep_low is not None or rep_high is not None):
        having = []
        if edf_n is not None:
            having.append(f"COUNT(*) > {edf_n}")
        if rep_low is not None:
            having.append(f"AVG({rep_field}) < {rep_low}")
        if rep_high is not None and edf_n is None:
            having.append(f"AVG({rep_field}) > {rep_high}")
        if not having:
            having.append("1=1")
        return {
            "action": "query",
            "title": "Clientes según cantidad de EDF y repago",
            "sql": f"""
                SELECT cliente_codigo, MAX(cliente) AS cliente,
                       COUNT(*) AS cantidad_edf,
                       ROUND(AVG({rep_field}),1) AS repago_promedio_pct
                FROM repago_edf
                WHERE UPPER(estado)='PDV'
                GROUP BY cliente_codigo
                HAVING {' AND '.join(having)}
                ORDER BY repago_promedio_pct ASC, cantidad_edf DESC
                LIMIT {limit}
            """,
            "assumption": "El repago del cliente se resume como promedio de los EDF colocados.",
            "clarifying_question": "",
            "reason": "",
        }

    # Descuento + tope.
    if ("descuento" in q or "porcentaje" in q) and ("tope" in q or "bult" in q):
        seg_where = f"AND d.segmento='{seg}' AND t.segmento='{seg}'" if seg else "AND d.segmento=t.segmento"
        disc_eq = re.search(r"(?:descuento|porcentaje)[^0-9]{0,12}(\d+(?:[.,]\d+)?)\s*%?", q)
        tope_eq = re.search(r"(?:tope)[^0-9]{0,12}(\d+(?:[.,]\d+)?)", q)
        extras = []
        if disc_eq:
            extras.append(f"ABS(d.descuento_pct - {float(disc_eq.group(1).replace(',', '.'))}) < 0.01")
        if tope_eq:
            extras.append(f"ABS(t.tope_bultos - {float(tope_eq.group(1).replace(',', '.'))}) < 0.01")
        extra_sql = (" AND " + " AND ".join(extras)) if extras else ""
        return {
            "action": "query",
            "title": "Clientes por descuento y tope",
            "sql": f"""
                SELECT d.cliente_codigo, MAX(d.cliente) AS cliente,
                       d.segmento, ROUND(MAX(d.descuento_pct),2) AS descuento_pct,
                       ROUND(MAX(t.tope_bultos),0) AS tope_bultos,
                       MAX(t.canal) AS canal
                FROM descuentos_cliente d
                JOIN topes_cliente t ON t.cliente_codigo=d.cliente_codigo
                WHERE 1=1 {seg_where} {extra_sql}
                GROUP BY d.cliente_codigo, d.segmento
                ORDER BY descuento_pct DESC, tope_bultos DESC
                LIMIT {limit}
            """,
            "assumption": "Cruzo descuentos de Grupo de clientes con topes de Planificación por código de cliente.",
            "clarifying_question": "",
            "reason": "",
        }

    # Topes.
    if "tope" in q:
        where = f"WHERE segmento='{seg}'" if seg else ""
        order = "tope_bultos DESC" if any(x in q for x in ("mayor", "mas alto", "más alto", "500")) else "cliente"
        threshold = re.search(r"(?:tope|bultos)[^0-9]{0,15}(\d+(?:[.,]\d+)?)", q)
        if threshold:
            val = float(threshold.group(1).replace(",", "."))
            where += (" AND " if where else "WHERE ") + f"ABS(tope_bultos-{val}) < 0.01"
        return {
            "action": "query",
            "title": "Topes de bultos por cliente",
            "sql": f"""SELECT cliente_codigo, cliente, segmento, canal, ROUND(tope_bultos,0) AS tope_bultos
                       FROM topes_cliente {where}
                       ORDER BY {order}
                       LIMIT {limit}""",
            "assumption": "Uso el tope por cliente calculado desde Planificación.",
            "clarifying_question": "",
            "reason": "",
        }

    # Descuentos.
    if "descuento" in q or "porcentaje" in q:
        where = f"WHERE segmento='{seg}'" if seg else ""
        return {
            "action": "query",
            "title": "Descuentos por cliente",
            "sql": f"""SELECT cliente_codigo, cliente, segmento, subsegmento,
                              ROUND(descuento_pct,2) AS descuento_pct
                       FROM descuentos_cliente {where}
                       ORDER BY descuento_pct DESC, cliente
                       LIMIT {limit}""",
            "assumption": "Uso el porcentaje comercial vigente del snapshot de Grupo de clientes.",
            "clarifying_question": "",
            "reason": "",
        }

    # Ventas/HL + cantidad de EDF.
    if any(k in q for k in ("compra", "venta", "hl", "hectolit")):
        latest = any(k in q for k in ("ultimo mes", "último mes", "mes corriente", "mes actual"))
        hl_col = "hl_ultimo_periodo" if latest else "hl_ultimo_mes_completo"
        return {
            "action": "query",
            "title": "Clientes por compra y cantidad de EDF",
            "sql": f"""
                WITH e AS (
                    SELECT cliente_codigo, COUNT(*) AS cantidad_edf
                    FROM repago_edf
                    WHERE UPPER(estado)='PDV'
                    GROUP BY cliente_codigo
                )
                SELECT c.cliente_codigo, c.cliente,
                       ROUND(c.{hl_col},2) AS hl,
                       COALESCE(e.cantidad_edf,0) AS cantidad_edf
                FROM clientes c
                LEFT JOIN e ON e.cliente_codigo=c.cliente_codigo
                ORDER BY c.{hl_col} ASC, cantidad_edf DESC
                LIMIT {limit}
            """,
            "assumption": "Ordeno por menor compra en HL y muestro simultáneamente la cantidad de EDF colocados.",
            "clarifying_question": "",
            "reason": "",
        }

    # Ranking simple EDF.
    if any(k in q for k in ("heladera", "heladeras", "edf", "equipos")):
        return {
            "action": "query",
            "title": "Clientes por cantidad de EDF",
            "sql": f"""
                SELECT cliente_codigo, MAX(cliente) AS cliente, COUNT(*) AS cantidad_edf,
                       ROUND(AVG({rep_field}),1) AS repago_promedio_pct
                FROM repago_edf
                WHERE UPPER(estado)='PDV'
                GROUP BY cliente_codigo
                ORDER BY cantidad_edf DESC, repago_promedio_pct ASC
                LIMIT {limit}
            """,
            "assumption": "Cuento únicamente EDF colocados en PDV.",
            "clarifying_question": "",
            "reason": "",
        }

    return None


def _local_plan(question: str, context: dict[str, Any] | None = None) -> dict:
    q = _norm(question)
    if not q:
        return {
            "action":"clarify", "title":"", "sql":"", "assumption":"",
            "clarifying_question":"¿Qué querés consultar?", "reason":"pregunta vacía",
        }

    planners = (
        _stock_comparison_plan,
        _stock_ranking_plan,
        _freshness_plan,
        _customer_cross_plan,
    )
    for planner in planners:
        plan = planner(q)
        if plan:
            return plan

    # Repreguntas útiles por dominio detectado.
    if "stock" in q and _location(q) is None:
        return {
            "action":"clarify", "title":"", "sql":"", "assumption":"",
            "clarifying_question":"¿Querés analizar el stock de **Trelew**, **Madryn** o **Total DDV**?",
            "reason":"falta localidad",
        }

    return {
        "action":"unavailable", "title":"", "sql":"", "assumption":"",
        "clarifying_question":"",
        "reason":"Puedo analizar libremente stock/Frescura, EDF/repago, ventas en HL, descuentos y topes, pero no pude traducir esta frase a un cálculo seguro con las columnas disponibles.",
    }


def _context_text(context: dict[str, Any] | None) -> str:
    ctx = context or {}
    parts = []
    if ctx.get("active_client_id"):
        parts.append(f"cliente_activo={ctx.get('active_client_id')} {ctx.get('active_client_name') or ''}")
    if ctx.get("active_sku"):
        parts.append(f"sku_activo={ctx.get('active_sku')} {ctx.get('active_sku_name') or ''}")
    if ctx.get("active_scope"):
        parts.append(f"localidad_activa={ctx.get('active_scope')}")
    if ctx.get("active_topic"):
        parts.append(f"tema_activo={ctx.get('active_topic')}")
    return " | ".join(parts) if parts else "sin contexto activo"

# -----------------------------
# SQL validation / execution
# -----------------------------

FORBIDDEN_SQL = re.compile(
    r"\b(PRAGMA|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|ATTACH|DETACH|REPLACE|VACUUM|TRIGGER|REINDEX)\b",
    re.I,
)
ALLOWED_TABLES = {
    "frescura_productos", "frescura_lotes", "frescura_perfiles",
    "clientes", "repago_edf", "ventas_mensuales_cliente",
    "descuentos_cliente", "topes_cliente",
}


def _validate_sql(sql: str) -> str:
    clean = str(sql or "").strip().rstrip(";").strip()
    upper = clean.upper()
    if not clean or not (upper.startswith("SELECT ") or upper.startswith("WITH ")):
        raise ValueError("El plan no generó un SELECT de lectura.")
    if ";" in clean or "--" in clean or "/*" in clean or "*/" in clean:
        raise ValueError("SQL no permitido.")
    if FORBIDDEN_SQL.search(clean) or "SQLITE_" in upper:
        raise ValueError("SQL no permitido.")

    # Comprobación conservadora de tablas explícitas después de FROM/JOIN.
    refs = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", clean, flags=re.I)
    # CTEs pueden aparecer como FROM cte; reconocer nombres declarados en WITH.
    ctes = set(re.findall(r"(?:WITH|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", clean, flags=re.I))
    for table in refs:
        if table.lower() not in ALLOWED_TABLES and table not in ctes and table.lower() not in {x.lower() for x in ctes}:
            raise ValueError(f"Tabla no permitida: {table}")
    return clean


def _execute(sql: str) -> tuple[pd.DataFrame, list[str]]:
    clean = _validate_sql(sql)
    conn, frames = _connection()
    try:
        df = pd.read_sql_query(clean, conn)
    finally:
        conn.close()
    if len(df) > MAX_RESULT_ROWS:
        df = df.head(MAX_RESULT_ROWS).copy()
    used = [name for name in ALLOWED_TABLES if re.search(rf"\b{re.escape(name)}\b", clean, flags=re.I)]
    return df, sorted(used)


# -----------------------------
# Response formatting
# -----------------------------

FRIENDLY = {
    "codigo": "SKU",
    "descripcion": "Producto",
    "cliente_codigo": "Código cliente",
    "cliente": "Cliente",
    "razon_social": "Razón social",
    "stock_trelew": "Stock Trelew",
    "stock_madryn": "Stock Madryn",
    "stock_total_ddv": "Stock Total DDV",
    "diferencia": "Diferencia",
    "diferencia_bultos": "Diferencia bultos",
    "relacion": "Relación",
    "ratio": "Relación",
    "repago_trimestre_pct": "Repago trimestre %",
    "repago_ultimo_mes_pct": "Repago último mes %",
    "tope_bultos": "Tope bultos",
    "descuento_pct": "Descuento %",
    "cantidad": "Cantidad",
    "total": "Total",
}


def _format_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, (int,)):
        return f"{value:,}".replace(",", ".")
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return f"{int(round(value)):,}".replace(",", ".")
        txt = f"{value:,.2f}"
        return txt.replace(",", "X").replace(".", ",").replace("X", ".")
    return str(value)


def _markdown_table(df: pd.DataFrame, limit: int = DISPLAY_ROWS) -> str:
    show = df.head(limit).copy()
    cols = list(show.columns)
    headers = [FRIENDLY.get(c, c.replace("_", " ").strip().capitalize()) for c in cols]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in show.iterrows():
        values = [_format_value(row[c]).replace("|", "/") for c in cols]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _format_answer(plan: dict, df: pd.DataFrame) -> str:
    title = str(plan.get("title") or "Análisis DDV").strip()
    assumption = str(plan.get("assumption") or "").strip()

    if df.empty:
        answer = f"**{title}**\n\nNo encontré resultados que cumplan ese criterio con los snapshots actuales."
        if assumption:
            answer += f"\n\n_Criterio usado: {assumption}_"
        return answer

    if len(df) == 1 and len(df.columns) <= 8:
        row = df.iloc[0]
        parts = []
        for col in df.columns:
            label = FRIENDLY.get(col, col.replace("_", " ").strip().capitalize())
            parts.append(f"**{label}:** {_format_value(row[col])}")
        answer = f"**{title}**\n\n" + "  \n".join(parts)
    else:
        answer = f"**{title}**\n\nEncontré **{len(df)} resultado(s)**. Los principales son:\n\n{_markdown_table(df)}"
        if len(df) > DISPLAY_ROWS:
            answer += f"\n\n_Muestro los primeros {DISPLAY_ROWS} del resultado calculado._"

    if assumption:
        answer += f"\n\n_Criterio usado: {assumption}_"
    return answer


def _source_labels(tables: list[str]) -> list[str]:
    out = []
    if any(t.startswith("frescura_") for t in tables):
        out.append("Frescura · snapshot local")
    if any(t in {"clientes", "repago_edf", "ventas_mensuales_cliente"} for t in tables):
        out.append("EDF/Repago · snapshot local")
    if "descuentos_cliente" in tables:
        out.append("Grupo de clientes · snapshot local")
    if "topes_cliente" in tables:
        out.append("Planificación · topes local")
    out.append("Analista DDV · SQL local")
    return out


@dataclass
class AnalystResult:
    handled: bool
    answer: str
    sources: list[str]
    meta: dict[str, Any]


def analyze_question(question: str, context: dict[str, Any] | None = None) -> AnalystResult:
    """Analizador local sin API. Convierte preguntas generales a planes SQL seguros."""
    try:
        plan = _local_plan(question, context)
    except Exception as exc:
        return AnalystResult(False, "", [], {"reason": "local_planner_error", "error": str(exc)})

    action = str(plan.get("action") or "").lower()
    if action == "clarify":
        question_text = str(plan.get("clarifying_question") or "Necesito un dato más para responder.").strip()
        return AnalystResult(True, question_text, ["Analista DDV · local"], {"plan": plan})
    if action == "unavailable":
        reason = str(plan.get("reason") or "No tengo esa información en las fuentes actuales.").strip()
        return AnalystResult(True, f"**No puedo calcular esa consulta todavía con seguridad.** {reason}", ["Analista DDV · local"], {"plan": plan})
    if action != "query":
        return AnalystResult(False, "", [], {"reason": "unknown_action", "plan": plan})

    try:
        df, tables = _execute(plan.get("sql") or "")
        answer = _format_answer(plan, df)
        return AnalystResult(
            True,
            answer,
            _source_labels(tables),
            {"plan": plan, "tables": tables, "rows": len(df), "sql": plan.get("sql") or ""},
        )
    except Exception as exc:
        return AnalystResult(
            True,
            "Pude interpretar la pregunta, pero el cálculo local no se pudo ejecutar de forma segura. "
            f"Detalle técnico: **{type(exc).__name__}**.",
            ["Analista DDV · local"],
            {"reason": "query_error", "error": str(exc), "plan": plan},
        )
