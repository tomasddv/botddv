from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

import pandas as pd

from sources import frescura_source, grupos_source, repago_source, ventas_actual_source

MAX_RESULT_ROWS = 500
DISPLAY_ROWS = 15

# Conserva la última consulta analítica para interpretar repreguntas/correcciones
# breves como "esos no tienen venta 0". El contexto de sesión, cuando existe,
# tiene prioridad sobre este fallback de proceso.
_LAST_ANALYST_TURN: dict[str, Any] = {}


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



def _ventas_actual_frames() -> dict[str, pd.DataFrame]:
    snap = _safe_snap(ventas_actual_source)
    rows = snap.get("rows") or []
    meta = [{
        "period_start": snap.get("period_start") or "",
        "period_end": snap.get("period_end") or "",
        "period_month": snap.get("period_month") or "",
        "updated_at": snap.get("updated_at") or "",
        "total_hl": _num(snap.get("total_hl")) or 0.0,
        "clientes": int(snap.get("clients_count") or 0),
        "skus": int(snap.get("skus_count") or 0),
    }] if snap else []
    return {
        "ventas_mes_actual": pd.DataFrame(rows),
        "ventas_mes_actual_meta": pd.DataFrame(meta),
        "maestro_clientes_actual": pd.DataFrame(snap.get("customer_master") or []),
    }

def _frames() -> dict[str, pd.DataFrame]:
    frames = {}
    frames.update(_frescura_frames())
    frames.update(_repago_frames())
    frames.update(_grupos_frames())
    frames.update(_ventas_actual_frames())
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
    "ventas_mes_actual": ["fecha", "cliente_codigo", "cliente", "razon_social", "nombre_fantasia", "agrupacion", "localidad_base", "lista_precios", "subcanal", "vendedor_codigo", "vendedor", "supervisor", "ruta", "sku", "producto", "marca", "marca_unificada", "segmento", "segmento_2", "segmento_3", "calibre", "calibre_unificado", "division", "producto_estadistico", "unidad_negocio", "ung_top", "calibres_cpr", "foco_comercial", "es_cza", "es_core", "es_value", "es_above_core", "es_premium", "es_balanced", "es_nabs", "es_laton_710", "hl", "importe_neto", "importe_final", "facturas"],
    "ventas_mes_actual_meta": ["period_start", "period_end", "period_month", "updated_at", "total_hl", "clientes", "skus"],
    "maestro_clientes_actual": ["cliente_codigo", "razon_social", "nombre_fantasia", "agrupacion", "localidad_base", "lista_precios", "subcanal", "ramo_cliente"],
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


def _is_purchase_date_question(q: str) -> bool:
    """Detecta consultas que preguntan por la fecha/día de compra.

    Ejemplos: "cuándo compró el cliente 891", "qué días compró el SKU 30645",
    "cuándo fue la última compra". Se resuelve contra Venta mes actual (CHESS).
    """
    qn = _norm(q)
    phrases = (
        "cuando compro", "cuando fue la compra", "cuando fue la ultima compra",
        "ultima compra", "fecha de compra", "fechas de compra",
        "que dia compro", "que dias compro", "dia que compro", "dias que compro",
        "ultima vez que compro", "cuando hizo la compra", "cuando hicieron la compra",
    )
    return any(phrase in qn for phrase in phrases)


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
    current_sales = (
        any(k in q for k in ("este mes", "mes actual", "mes corriente", "acumulado del mes", "acumulado mes", "venta diaria", "ventas diarias", "vendimos", "vendido", "vendida", "ultimos", "últimos", "hoy", "ayer"))
        and any(k in q for k in ("venta", "ventas", "vende", "vend", "compra", "compr", "hl", "hectolit", "factur", "producto", "sku", "cliente", "marca", "division", "negocio"))
    )
    cross_source = sum(bool(x) for x in (
        any(k in q for k in ("repago", "heladera", "edf", "serie")),
        any(k in q for k in ("descuento", "core", "value")),
        any(k in q for k in ("tope", "bulto")),
        any(k in q for k in ("stock", "frescura", "venc", "sku", "producto")),
        any(k in q for k in ("venta", "compra", "hl", "hectolit", "este mes", "mes actual", "mes corriente")),
    )) >= 2
    correction_followup = (
        "venta" in q
        and any(k in q for k in (
            "esos no", "esas no", "no son esos", "no son esas", "esos tienen",
            "esas tienen", "me trajiste", "me mostraste", "esta mal", "no esta bien"
        ))
    )
    zero_repago = "repago" in q and any(k in q for k in (
        "venta 0", "venta cero", "sin venta", "sin ventas", "0 hl", "cero hl",
        "no vendio", "no vendieron", "no compro", "no compraron"
    ))
    purchase_date = _is_purchase_date_question(q)
    freshness_month_list = (
        "frescura" in q
        and ("mes" in q or bool(_extract_freshness_months(q)))
        and any(k in q for k in ("producto", "productos", "sku", "venc", "frescura"))
    )
    return current_sales or plural_or_set or comparative or aggregate or cross_source or correction_followup or zero_repago or purchase_date or freshness_month_list


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
    # Dirección inferida por la cláusula cercana a cada ciudad. Evita que
    # "poco stock en Madryn y mucho en Trelew" marque a ambas como bajas.
    def low_for(city: str) -> bool:
        low = r"(?:poco|bajo|escaso|menos|faltante|falt\w*)"
        city_re = re.escape(city)
        return bool(
            re.search(rf"{low}\s+(?:stock\s+)?(?:en|de|para)?\s*{city_re}\b", q)
            or re.search(rf"\b{city_re}\b\s+(?:con\s+)?(?:stock\s+)?{low}", q)
        )

    madryn_low = low_for("madryn")
    trelew_low = low_for("trelew")

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




def _extract_freshness_months(q: str) -> list[str]:
    """Extrae meses pedidos para consultas de Frescura.

    Soporta formas como "mes 09 y 10", "meses 9, 10", "septiembre y octubre".
    Devuelve meses con dos dígitos, sin duplicados y en el orden pedido.
    """
    qn = _norm(q)
    month_names = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "setiembre": "09", "octubre": "10",
        "noviembre": "11", "diciembre": "12",
    }
    found: list[tuple[int, str]] = []
    for name, num in month_names.items():
        for m in re.finditer(rf"\b{re.escape(name)}\b", qn):
            found.append((m.start(), num))

    # Si aparece "mes/meses", tomar los números de 1 a 12 que siguen en esa frase.
    # También cubre la forma habitual "mes 09 y 10".
    for m in re.finditer(r"\b(?:mes|meses)\b([^.;]*)", qn):
        tail = m.group(1)
        base = m.start(1)
        for n in re.finditer(r"(?<!\d)(0?[1-9]|1[0-2])(?!\d)", tail):
            found.append((base + n.start(), f"{int(n.group(1)):02d}"))

    found.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, month in found:
        if month not in out:
            out.append(month)
    return out


def _freshness_month_products_plan(q: str) -> dict | None:
    """Lista todos los productos de Frescura que vencen en los meses pedidos."""
    if "frescura" not in q:
        return None
    months = _extract_freshness_months(q)
    if not months:
        return None
    if not any(k in q for k in ("producto", "productos", "sku", "venc", "mes", "meses")):
        return None

    loc = _location(q) or "TOTAL DDV"
    month_sql = ", ".join(_sql_text(m) for m in months)
    month_label = " y ".join(months)
    loc_label = loc.title() if loc != "TOTAL DDV" else "Total DDV"

    return {
        "action": "query",
        "title": f"Productos de Frescura · meses {month_label} · {loc_label}",
        "sql": f"""
            SELECT codigo,
                   descripcion,
                   substr(vencimiento,6,2) AS mes,
                   MIN(vencimiento) AS proximo_vencimiento,
                   ROUND(SUM(stock_lote),1) AS stock_bultos,
                   ROUND(SUM(riesgo_bultos),1) AS riesgo_bultos,
                   CASE
                       WHEN MAX(CASE WHEN UPPER(estado)='CRITICO' THEN 3
                                     WHEN UPPER(estado)='ACCIONAR' THEN 2
                                     WHEN UPPER(estado)='OK' THEN 1 ELSE 0 END)=3 THEN 'CRITICO'
                       WHEN MAX(CASE WHEN UPPER(estado)='CRITICO' THEN 3
                                     WHEN UPPER(estado)='ACCIONAR' THEN 2
                                     WHEN UPPER(estado)='OK' THEN 1 ELSE 0 END)=2 THEN 'ACCIONAR'
                       WHEN MAX(CASE WHEN UPPER(estado)='CRITICO' THEN 3
                                     WHEN UPPER(estado)='ACCIONAR' THEN 2
                                     WHEN UPPER(estado)='OK' THEN 1 ELSE 0 END)=1 THEN 'OK'
                       ELSE MAX(estado)
                   END AS estado
            FROM frescura_lotes
            WHERE localidad={_sql_text(loc)}
              AND vencimiento IS NOT NULL
              AND length(vencimiento) >= 7
              AND substr(vencimiento,6,2) IN ({month_sql})
            GROUP BY codigo, descripcion, substr(vencimiento,6,2)
            ORDER BY substr(vencimiento,6,2), MIN(vencimiento), descripcion
        """,
        "assumption": (
            f"Traigo todos los SKU del snapshot de Frescura con vencimiento en los meses {month_label}. "
            f"Ámbito: {loc_label}. Si un SKU tiene más de un lote en el mismo mes, acumulo su stock y riesgo y muestro el vencimiento más próximo."
        ),
        "clarifying_question": "",
        "reason": "",
        "display_all": True,
        "intent": "frescura_meses",
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



def _sql_text(value: Any) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _sales_snap() -> dict:
    return _safe_snap(ventas_actual_source)


def _sales_period_note() -> str:
    snap = _sales_snap()
    start = str(snap.get("period_start") or "")
    end = str(snap.get("period_end") or "")
    if start and end:
        def ar(v):
            try:
                y, m, d = str(v)[:10].split("-")
                return f"{d}/{m}/{y}"
            except Exception:
                return str(v or "")
        try:
            fresh = ventas_actual_source.freshness()
        except Exception:
            fresh = {}
        note = f"Venta CHESS del mes corriente, corte **{ar(start)} al {ar(end)}**."
        expected = str(fresh.get("expected_cutoff") or "")
        if expected and not fresh.get("is_current", True):
            note += (
                f" La carga esperada del **{ar(expected)}** todavía no está disponible; "
                "el bot la vuelve a buscar automáticamente cada pocos minutos."
            )
        return note
    return "Venta CHESS del mes corriente según el último snapshot disponible."


def _is_current_sales_question(q: str) -> bool:
    """Por defecto, una consulta de venta/compra sin período explícito usa el mes corriente.

    Sólo se deriva al histórico cuando el usuario lo pide expresamente. Esto evita que
    preguntas naturales como "cuántos clientes con compra hay de CZA" terminen en Repago/EDF.
    """
    explicit_history = any(k in q for k in (
        "ultimo mes completo", "último mes completo", "mes pasado", "mes anterior",
        "trimestre", "trimestral", "promedio mensual", "promedio de los ultimos",
        "promedio de los últimos", "historico", "histórico", "periodo anterior",
        "período anterior", "año pasado", "ano pasado",
    ))
    explicit_current = any(k in q for k in (
        "este mes", "mes actual", "mes corriente", "acumulado del mes", "acumulado mes",
        "venta diaria", "ventas diarias", "vendimos", "vendido", "vendida", "hoy", "ayer",
        "ultimos dias", "ultimos ", "últimos ",
    ))
    sales = any(k in q for k in (
        "venta", "ventas", "vende", "vend", "compra", "compr", "hl", "hectolit",
        "factur", "importe", "monto", "dinero", "pesos", "$", "cliente", "clientes",
        "producto", "productos", "sku", "marca", "division", "negocio",
    ))
    if explicit_history:
        return False
    return explicit_current or sales


def _sales_rows() -> list[dict]:
    return (_sales_snap().get("rows") or [])


def _sales_codes(field: str) -> set[str]:
    return {str(r.get(field) or "") for r in _sales_rows() if str(r.get(field) or "")}


def _match_sales_customer(q: str, context: dict[str, Any] | None = None) -> tuple[str, str] | None:
    rows = _sales_rows()
    if not rows:
        return None
    codes = {str(r.get("cliente_codigo") or "") for r in rows}

    # Si la pregunta identifica explícitamente "cliente 891", ese número tiene
    # prioridad aunque también exista como código de SKU.
    explicit = re.search(r"\bcliente(?:\s+(?:nro|numero|num))?\s*[:#-]?\s*(\d{1,7})\b", q)
    if explicit:
        clean = explicit.group(1).lstrip("0") or "0"
        if clean in codes:
            name = next((str(r.get("nombre_fantasia") or r.get("cliente") or "") for r in rows if str(r.get("cliente_codigo") or "") == clean), "")
            return clean, name

    for code in re.findall(r"(?<!\d)(\d{3,7})(?!\d)", q):
        clean = code.lstrip("0") or "0"
        if clean in codes:
            name = next((str(r.get("nombre_fantasia") or r.get("cliente") or "") for r in rows if str(r.get("cliente_codigo") or "") == clean), "")
            return clean, name

    ctx = context or {}
    active = str(ctx.get("active_client_id") or "")
    followup = (
        q.startswith("y ")
        or q in {"este mes", "mes actual", "mes corriente", "ahora", "y este mes", "y ahora"}
        or any(k in q for k in ("ese cliente", "este cliente", "el mismo cliente", "del mismo"))
    )
    if active and active in codes and followup:
        name = str(ctx.get("active_client_name") or "")
        return active, name

    candidates = {}
    for r in rows:
        cid = str(r.get("cliente_codigo") or "")
        for field in ("nombre_fantasia", "cliente", "razon_social"):
            label = str(r.get(field) or "").strip()
            n = _norm(label)
            if cid and len(n) >= 4:
                candidates[(cid, label)] = n
    matches = [(len(n), cid, label) for (cid, label), n in candidates.items() if n and re.search(rf"(^|\b){re.escape(n)}(\b|$)", q)]
    if matches:
        _, cid, label = max(matches)
        return cid, label
    return None


def _match_sales_sku(q: str, context: dict[str, Any] | None = None) -> tuple[str, str] | None:
    rows = _sales_rows()
    if not rows:
        return None
    codes = {str(r.get("sku") or "") for r in rows}

    explicit = re.search(r"\b(?:sku|producto|codigo|código)(?:\s+(?:nro|numero|num))?\s*[:#-]?\s*(\d{1,7})\b", q)
    if explicit:
        clean = explicit.group(1).lstrip("0") or "0"
        if clean in codes:
            name = next((str(r.get("producto") or "") for r in rows if str(r.get("sku") or "") == clean), "")
            return clean, name

    client_tag = re.search(r"\bcliente(?:\s+(?:nro|numero|num))?\s*[:#-]?\s*(\d{1,7})\b", q)
    explicit_client_code = (client_tag.group(1).lstrip("0") or "0") if client_tag else ""
    for code in re.findall(r"(?<!\d)(\d{3,7})(?!\d)", q):
        clean = code.lstrip("0") or "0"
        if clean == explicit_client_code:
            continue
        if clean in codes:
            name = next((str(r.get("producto") or "") for r in rows if str(r.get("sku") or "") == clean), "")
            return clean, name
    ctx = context or {}
    active = str(ctx.get("active_sku") or "")
    followup = (
        q.startswith("y ")
        or q in {"este mes", "mes actual", "mes corriente", "ahora", "y este mes", "y ahora"}
        or any(k in q for k in ("ese producto", "este producto", "el mismo sku", "del mismo"))
    )
    if active and active in codes and followup:
        return active, str(ctx.get("active_sku_name") or "")
    # Sólo coincidencias fuertes por descripción completa; evita adivinar con una palabra de marca.
    candidates = {}
    for r in rows:
        sku = str(r.get("sku") or "")
        label = str(r.get("producto") or "").strip()
        n = _norm(label)
        if sku and len(n) >= 8:
            candidates[(sku, label)] = n
    matches = [(len(n), sku, label) for (sku, label), n in candidates.items() if n and n in q]
    if matches:
        _, sku, label = max(matches)
        return sku, label
    return None


def _match_sales_seller(q: str) -> tuple[str, str] | None:
    rows = _sales_rows()
    candidates = {}
    for r in rows:
        code = str(r.get("vendedor_codigo") or "")
        label = str(r.get("vendedor") or "").strip()
        n = _norm(label)
        if code and len(n) >= 5:
            candidates[(code, label)] = n
    matches = [(len(n), code, label) for (code, label), n in candidates.items() if n and n in q]
    if matches:
        _, code, label = max(matches)
        return code, label
    return None


def _sales_focus_condition(q: str, alias: str = "v") -> tuple[str, str]:
    p = alias + "." if alias else ""
    if "above core" in q or "abovecore" in q:
        return f"{p}es_above_core=1", "Above Core"
    if "balanced" in q:
        return f"{p}es_balanced=1", "Balanced Choices"
    if "premium" in q:
        return f"{p}es_premium=1", "Premium"
    if "value" in q:
        return f"{p}es_value=1", "Value"
    if re.search(r"\bcore\b", q):
        return f"{p}es_core=1", "Core"
    if "laton 710" in q or "latones 710" in q or "710" in q and "laton" in q:
        return f"{p}es_laton_710=1", "Latones 710"
    if "nabs" in q:
        return f"{p}es_nabs=1", "Nabs"
    if "cerveza" in q or re.search(r"\bcza\b", q):
        return f"{p}es_cza=1", "Total CZA"
    if re.search(r"\bung\b", q):
        return f"UPPER({p}unidad_negocio) LIKE '%UNG%'", "UNG"
    if "gaseosa" in q:
        return f"UPPER({p}division)='GASEOSAS'", "Gaseosas"
    if "isoton" in q:
        return f"UPPER({p}division)='ISOTONICAS'", "Isotónicas"
    if re.search(r"\bagua(s)?\b", q):
        return f"UPPER({p}division)='AGUAS'", "Aguas"
    return "", ""


def _sales_time_condition(q: str, alias: str = "v") -> tuple[str, str]:
    p = alias + "." if alias else ""
    snap = _sales_snap()
    end = str(snap.get("period_end") or "")[:10]
    if not end:
        return "", ""
    end_ts = pd.to_datetime(end, errors="coerce")
    if pd.isna(end_ts):
        return "", ""
    m = re.search(r"ultimos\s+(\d{1,2})\s+dias", q)
    if m:
        days = max(1, min(int(m.group(1)), 31))
        start = (end_ts - pd.Timedelta(days=days-1)).strftime("%Y-%m-%d")
        return f"{p}fecha >= '{start}' AND {p}fecha <= '{end}'", f"últimos {days} días disponibles ({start} a {end})"
    if "ayer" in q:
        d = (end_ts - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return f"{p}fecha='{d}'", f"día {d}"
    if "hoy" in q:
        # Para el bot, 'hoy' significa la última fecha efectivamente cargada; siempre se aclara el corte.
        return f"{p}fecha='{end}'", f"última fecha cargada {end}"
    return "", ""



_GENERIC_SALES_STOPWORDS = {
    "cuanta", "cuanto", "cuantas", "cuantos", "que", "cual", "cuales", "hay", "tiene", "tienen",
    "venta", "ventas", "vende", "venden", "vendido", "vendida", "vendimos", "compra", "compras",
    "compraron", "compradores", "cliente", "clientes", "producto", "productos", "sku", "marca",
    "division", "divisiones", "negocio", "unidad", "importe", "monto", "dinero", "pesos",
    "facturacion", "facturas", "hl", "hectolitros", "este", "mes", "actual", "corriente",
    "de", "del", "la", "las", "el", "los", "en", "por", "para", "con", "sin", "un", "una",
    "total", "acumulado", "mas", "menos", "mayor", "menor", "mucho", "poco", "top",
    "trelew", "madryn", "puerto", "cza", "core", "value", "premium", "balanced", "nabs",
}

_GENERIC_DIMENSION_PRIORITY = (
    ("unidad_negocio", "unidad de negocio"),
    ("foco_comercial", "foco comercial"),
    ("division", "división"),
    ("marca_unificada", "marca"),
    ("segmento", "segmento"),
    ("segmento_2", "segmento 2"),
    ("segmento_3", "segmento 3"),
    ("subcanal", "subcanal"),
    ("agrupacion", "agrupación"),
    ("lista_precios", "lista de precios"),
    ("supervisor", "supervisor"),
)


def _generic_sales_dimension_condition(q: str, alias: str = "v") -> tuple[str, str]:
    """Reconoce valores comerciales aunque el usuario no nombre la dimensión.

    Ej.: "cuánta venta en $ tiene marketplace" -> unidad_negocio contiene MARKETPLACE.
    """
    rows = _sales_rows()
    if not rows:
        return "", ""

    tokens = [
        t for t in re.findall(r"[a-z0-9]+", _norm(q))
        if len(t) >= 4 and t not in _GENERIC_SALES_STOPWORDS and not t.isdigit()
    ]
    tokens = sorted(dict.fromkeys(tokens), key=lambda x: (-len(x), x))
    if not tokens:
        return "", ""

    p = alias + "." if alias else ""
    for field, label in _GENERIC_DIMENSION_PRIORITY:
        values = {}
        for row in rows:
            raw = str(row.get(field) or "").strip()
            if not raw:
                continue
            n = _norm(raw)
            if n:
                values[n] = raw
        if not values:
            continue

        for token in tokens:
            matched = [raw for n, raw in values.items() if re.search(rf"(^|\b){re.escape(token)}", n)]
            if not matched:
                continue
            escaped = token.replace("'", "''").upper()
            condition = f"UPPER({p}{field}) LIKE '%{escaped}%'"
            sample = sorted(set(matched))[:3]
            detail = ", ".join(sample)
            if len(set(matched)) > 3:
                detail += ", …"
            return condition, f"{label}: {detail}"
    return "", ""


def _requested_business_units(q: str, alias: str = "v") -> list[tuple[str, str]]:
    """Devuelve negocios explícitamente mencionados en la pregunta.

    Agrupa alias comerciales que pueden corresponder a más de un valor real de
    `unidad_negocio` (por ejemplo MARKETPLACE) y evita que una consulta con
    varios negocios termine combinándolos como filtros AND.
    """
    qn = _norm(q)
    p = alias + "." if alias else ""
    rows = _sales_rows()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, condition: str) -> None:
        key = _norm(label)
        if key and key not in seen:
            found.append((label, condition))
            seen.add(key)

    def distinct_values(field: str) -> list[str]:
        vals = []
        seen_vals = set()
        for row in rows:
            raw = str(row.get(field) or "").strip()
            if not raw:
                continue
            key = _norm(raw)
            if key and key not in seen_vals:
                vals.append(raw)
                seen_vals.add(key)
        return vals

    def in_condition(field: str, values: list[str]) -> str:
        quoted = ",".join(_sql_text(v.upper()) for v in values)
        return f"UPPER({p}{field}) IN ({quoted})"

    unit_values = distinct_values("unidad_negocio")

    # 1) Valores reales completos mencionados literalmente.
    for raw in sorted(unit_values, key=lambda x: (-len(_norm(x)), _norm(x))):
        nv = _norm(raw)
        if nv and re.search(rf"(^|\b){re.escape(nv)}(\b|$)", qn):
            add(raw, f"UPPER({p}unidad_negocio)={_sql_text(raw.upper())}")

    # 2) Alias/grupos comerciales habituales. MARKETPLACE puede tener varias
    # variantes reales (p.ej. Marketplace Bees y Marketplace Alimentos).
    if re.search(r"\bmarket\s*place\b|\bmarketplace\b", qn):
        matches = [v for v in unit_values if "marketplace" in _norm(v).replace(" ", "")]
        if matches:
            add("MARKETPLACE", in_condition("unidad_negocio", matches))
        else:
            add("MARKETPLACE", f"UPPER({p}unidad_negocio) LIKE '%MARKETPLACE%'")

    if re.search(r"\bred\s*bull\b|\bredbull\b", qn):
        # Preferimos Unidad de Negocio si existe como tal en el snapshot.
        rb_units = [v for v in unit_values if "redbull" in _norm(v).replace(" ", "")]
        if rb_units:
            add("RED BULL", in_condition("unidad_negocio", rb_units))
        else:
            # Fallback seguro cuando el archivo lo identifica por marca.
            brand_values = distinct_values("marca_unificada") + distinct_values("marca")
            rb_brands = []
            used = set()
            for v in brand_values:
                if "redbull" in _norm(v).replace(" ", "") and _norm(v) not in used:
                    rb_brands.append(v)
                    used.add(_norm(v))
            if rb_brands:
                conds = []
                mu = [v for v in distinct_values("marca_unificada") if "redbull" in _norm(v).replace(" ", "")]
                ma = [v for v in distinct_values("marca") if "redbull" in _norm(v).replace(" ", "")]
                if mu:
                    conds.append(in_condition("marca_unificada", mu))
                if ma:
                    conds.append(in_condition("marca", ma))
                add("RED BULL", "(" + " OR ".join(conds) + ")")
            else:
                add("RED BULL", f"REPLACE(UPPER({p}unidad_negocio),' ','') LIKE '%REDBULL%'")

    # Aguas se maneja como negocio comercial aunque la fuente la identifique
    # por División. Si hubiera una unidad real llamada Aguas, se prioriza.
    if re.search(r"\bagua(?:s)?\b", qn):
        agua_units = [v for v in unit_values if re.search(r"\bagua(?:s)?\b", _norm(v))]
        if agua_units:
            add("AGUAS", in_condition("unidad_negocio", agua_units))
        else:
            add("AGUAS", f"UPPER({p}division)='AGUAS'")

    if re.search(r"\bcza\b", qn) or "cerveza" in qn:
        add("CZA", f"{p}es_cza=1")

    if re.search(r"\bung\b", qn):
        add("UNG", f"UPPER({p}unidad_negocio) LIKE '%UNG%'")

    return found


def _requested_sales_metrics(q: str) -> tuple[bool, bool, bool]:
    """Indica si la pregunta pide HL, pesos y/o facturas de forma explícita."""
    qn = _norm(q)
    wants_money = any(k in qn for k in ("importe", "pesos", "facturacion", "monto", "dinero")) or "$" in q
    wants_hl = bool(re.search(r"\bhl\b|hectolitro", qn))
    wants_invoices = "factura" in qn and any(k in qn for k in ("cuantas", "cantidad", "facturas"))
    return wants_hl, wants_money, wants_invoices

def _sales_filters(q: str, context: dict[str, Any] | None = None, alias: str = "v", include_business_focus: bool = True) -> tuple[list[str], list[str]]:
    p = alias + "." if alias else ""
    filters, notes = [], []
    loc = _location(q)
    if loc in {"TRELEW", "MADRYN"}:
        filters.append(f"UPPER({p}localidad_base)={_sql_text(loc)}")
        notes.append(loc.title())
    cust = _match_sales_customer(q, context)
    if cust:
        filters.append(f"{p}cliente_codigo={_sql_text(cust[0])}")
        notes.append(f"cliente {cust[0]} · {cust[1]}")
    sku = _match_sales_sku(q, context)
    if sku:
        filters.append(f"{p}sku={_sql_text(sku[0])}")
        notes.append(f"SKU {sku[0]} · {sku[1]}")
    seller = _match_sales_seller(q)
    if seller:
        filters.append(f"{p}vendedor_codigo={_sql_text(seller[0])}")
        notes.append(f"vendedor {seller[1]}")
    if include_business_focus:
        focus_cond, focus_label = _sales_focus_condition(q, alias)
        if focus_cond:
            filters.append(focus_cond)
            notes.append(focus_label)
        generic_cond, generic_label = _generic_sales_dimension_condition(q, alias)
        if generic_cond:
            filters.append(generic_cond)
            notes.append(generic_label)
    time_cond, time_label = _sales_time_condition(q, alias)
    if time_cond:
        filters.append(time_cond)
        notes.append(time_label)
    return filters, notes


def _metric(q: str, alias: str = "v") -> tuple[str, str, str]:
    p = alias + "." if alias else ""
    if any(k in q for k in ("importe", "pesos", "facturacion", "facturación", "monto", "dinero", "$")):
        return f"SUM({p}importe_neto)", "importe_neto", "importe neto"
    if "factura" in q and any(k in q for k in ("cuantas", "cuántas", "cantidad", "facturas")):
        return f"SUM({p}facturas)", "facturas", "facturas"
    return f"SUM({p}hl)", "hl", "HL"


def _group_entity(q: str) -> str | None:
    if any(k in q for k in ("vendedor", "vendedores", "promotor", "promotores")):
        return "vendedor"
    if "supervisor" in q or "supervisores" in q:
        return "supervisor"
    if "marca" in q or "marcas" in q:
        return "marca"
    if "division" in q or "división" in q or "divisiones" in q:
        return "division"
    if "unidad de negocio" in q or "por negocio" in q or "negocios" in q:
        return "unidad_negocio"
    if any(k in q for k in ("producto", "productos", "sku")):
        return "producto"
    if "cliente" in q or "clientes" in q:
        return "cliente"
    if any(k in q for k in ("por dia", "por día", "dia vendimos", "día vendimos", "que dia", "qué día")):
        return "fecha"
    if "por base" in q or "entre trelew y madryn" in q or "trelew vs madryn" in q:
        return "localidad_base"
    return None



def _sales_no_activity_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    if not _is_current_sales_question(q):
        return None
    no_sale = any(k in q for k in ("no compraron", "no compro", "no compró", "sin compra", "sin ventas", "sin venta", "no vendimos", "no se vendieron", "no vendieron"))
    if not no_sale:
        return None
    limit = _extract_limit(q)
    period_note = _sales_period_note()
    focus_cond, focus_label = _sales_focus_condition(q, "v")
    focus_where = f"WHERE {focus_cond}" if focus_cond else ""
    loc = _location(q)

    if "cliente" in q or "clientes" in q:
        master_filters = []
        if loc in {"TRELEW", "MADRYN"}:
            master_filters.append(f"UPPER(m.localidad_base)={_sql_text(loc)}")
        master_where = ("WHERE " + " AND ".join(master_filters)) if master_filters else ""
        sql=f"""
            WITH s AS (
                SELECT cliente_codigo, SUM(hl) AS hl_mes
                FROM ventas_mes_actual v
                {focus_where}
                GROUP BY cliente_codigo
            )
            SELECT m.cliente_codigo,
                   COALESCE(NULLIF(m.nombre_fantasia,''),m.razon_social) AS cliente,
                   m.localidad_base,
                   ROUND(COALESCE(s.hl_mes,0),2) AS hl_mes
            FROM maestro_clientes_actual m
            LEFT JOIN s ON s.cliente_codigo=m.cliente_codigo
            {master_where + (' AND ' if master_where else 'WHERE ') + 'COALESCE(s.hl_mes,0) <= 0'}
            ORDER BY m.localidad_base, cliente
            LIMIT {limit}
        """
        return {
            "action":"query", "title":"Clientes sin compra en el mes", "sql":sql,
            "assumption": period_note + (f" Controlado sobre el foco {focus_label}." if focus_label else " Universo: maestro de clientes vigente."),
            "clarifying_question":"", "reason":"",
        }

    if any(k in q for k in ("producto", "productos", "sku")):
        sales_filters=[]
        if focus_cond:
            sales_filters.append(focus_cond)
        if loc in {"TRELEW", "MADRYN"}:
            sales_filters.append(f"UPPER(v.localidad_base)={_sql_text(loc)}")
        where_sales = ("WHERE " + " AND ".join(sales_filters)) if sales_filters else ""
        stock_col = {"TRELEW":"stock_trelew", "MADRYN":"stock_madryn"}.get(loc, "stock_total_ddv")
        sql=f"""
            WITH s AS (
                SELECT sku, SUM(hl) AS hl_mes
                FROM ventas_mes_actual v
                {where_sales}
                GROUP BY sku
            )
            SELECT f.codigo, f.descripcion, ROUND(f.{stock_col},1) AS stock_bultos,
                   ROUND(COALESCE(s.hl_mes,0),2) AS hl_mes
            FROM frescura_productos f
            LEFT JOIN s ON s.sku=f.codigo
            WHERE COALESCE(s.hl_mes,0) <= 0
            ORDER BY f.{stock_col} DESC, f.descripcion
            LIMIT {limit}
        """
        return {
            "action":"query", "title":"Productos sin venta en el mes", "sql":sql,
            "assumption": period_note + " Universo de productos: SKU presentes en Frescura.",
            "clarifying_question":"", "reason":"",
        }
    return None

def _sales_stock_cross_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    if not _is_current_sales_question(q) or "stock" not in q:
        return None
    loc = _location(q) or "TOTAL DDV"
    stock_col = {"TRELEW":"stock_trelew", "MADRYN":"stock_madryn", "TOTAL DDV":"stock_total_ddv"}[loc]
    sales_filters, notes = _sales_filters(q, context, "v")
    # La localidad elegida aplica a ambas magnitudes para comparar la misma base.
    where_sales = ("WHERE " + " AND ".join(sales_filters)) if sales_filters else ""
    limit = _extract_limit(q)
    low_sales = any(k in q for k in ("poca venta", "poco vende", "poco vendido", "menos venta", "casi no", "baja venta"))
    high_sales = any(k in q for k in ("mucha venta", "mucho vende", "mas venta", "más venta", "alta venta"))
    low_stock = any(k in q for k in ("poco stock", "bajo stock", "menos stock", "faltante", "quiebre"))
    if low_stock and high_sales:
        order = f"f.{stock_col} ASC, hl_mes DESC"
        title = f"Poco stock y mucha venta · {loc}"
    else:
        order = f"f.{stock_col} DESC, hl_mes ASC"
        title = f"Mucho stock y poca venta · {loc}"
    sql = f"""
        WITH s AS (
            SELECT sku, SUM(hl) AS hl_mes
            FROM ventas_mes_actual v
            {where_sales}
            GROUP BY sku
        )
        SELECT f.codigo, f.descripcion,
               ROUND(f.{stock_col},1) AS stock_bultos,
               ROUND(COALESCE(s.hl_mes,0),2) AS hl_mes
        FROM frescura_productos f
        LEFT JOIN s ON s.sku=f.codigo
        WHERE f.{stock_col} > 0
        ORDER BY {order}
        LIMIT {limit}
    """
    return {
        "action":"query", "title":title, "sql":sql,
        "assumption": _sales_period_note() + " Stock en bultos y venta en HL son magnitudes distintas; las muestro juntas para detectar extremos, no como un ratio directo.",
        "clarifying_question":"", "reason":"",
    }



def _zero_sales_requested(q: str) -> bool:
    qn = _norm(q)
    return bool(
        re.search(r"\bventa\s*(?:=|de)?\s*0\b", qn)
        or "venta cero" in qn
        or "sin venta" in qn
        or "sin ventas" in qn
        or re.search(r"\b0\s*hl\b", qn)
        or "cero hl" in qn
        or any(k in qn for k in ("no vendio", "no vendieron", "no compro", "no compraron"))
    )


def _repago_business(q: str) -> tuple[str, str, str, str]:
    """Negocio pedido en Repago.

    Retorna: etiqueta, columna último período, columna último mes completo,
    condición SQL sobre repago_edf.negocio.
    """
    qn = _norm(q)
    norm_business = "UPPER(COALESCE(negocio,''))"
    if "cerveza" in qn or re.search(r"\bcza\b", qn):
        return (
            "CERVEZA / CZA",
            "hl_cza_ultimo_periodo",
            "hl_cza_ultimo_mes",
            f"({norm_business} LIKE '%CZA%' OR {norm_business} LIKE '%CERVEZA%')",
        )
    if re.search(r"\bung\b", qn):
        return (
            "UNG",
            "hl_ung_ultimo_periodo",
            "hl_ung_ultimo_mes",
            f"{norm_business} LIKE '%UNG%'",
        )
    if re.search(r"\bagua(?:s)?\b", qn):
        return (
            "AGUAS",
            "hl_aguas_ultimo_periodo",
            "hl_aguas_ultimo_mes",
            f"{norm_business} LIKE '%AGUA%'",
        )
    if "redbull" in qn or "red bull" in qn or re.search(r"\brb\b", qn):
        return (
            "RED BULL",
            "hl_rb_ultimo_periodo",
            "hl_rb_ultimo_mes",
            f"(TRIM({norm_business})='RB' OR {norm_business} LIKE '%RED%BULL%')",
        )
    return ("TOTAL", "hl_ultimo_periodo", "hl_ultimo_mes_completo", "1=1")


def _repago_zero_sales_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    """Clientes de Repago con venta exactamente 0 para el negocio solicitado.

    Importante: usa las ventas del propio snapshot de Repago, no la venta CHESS
    corriente. Además limita el universo a clientes con EDF en estado PDV del
    negocio pedido, para que "Repago solo de cerveza" no mezcle otros negocios.
    """
    qn = _norm(q)
    if "repago" not in qn or not _zero_sales_requested(qn):
        return None
    if not any(k in qn for k in ("cliente", "clientes", "quienes", "cuales", "que ")):
        return None

    business_label, latest_col, full_col, edf_business_cond = _repago_business(qn)
    use_full_month = any(k in qn for k in ("ultimo mes completo", "mes completo", "mes pasado", "mes anterior"))
    sales_col = full_col if use_full_month else latest_col
    period_col = "ultimo_mes_completo" if use_full_month else "ultimo_periodo_disponible"
    hl_alias = {
        "CERVEZA / CZA": "hl_cza",
        "UNG": "hl_ung",
        "AGUAS": "hl_aguas",
        "RED BULL": "hl_rb",
        "TOTAL": "hl",
    }[business_label]

    sql = f"""
        WITH e AS (
            SELECT cliente_codigo, MAX(cliente) AS cliente, COUNT(*) AS cantidad_edf
            FROM repago_edf
            WHERE UPPER(estado)='PDV' AND {edf_business_cond}
            GROUP BY cliente_codigo
        )
        SELECT e.cliente_codigo,
               COALESCE(NULLIF(c.cliente,''), e.cliente) AS cliente,
               e.cantidad_edf,
               ROUND(COALESCE(c.{sales_col},0),2) AS {hl_alias},
               c.{period_col} AS periodo
        FROM e
        JOIN clientes c ON c.cliente_codigo=e.cliente_codigo
        WHERE ABS(COALESCE(c.{sales_col},0)) < 0.000001
        ORDER BY e.cantidad_edf DESC, cliente ASC
    """
    period_label = "último mes completo" if use_full_month else "último período disponible en Repago"
    return {
        "action": "query",
        "title": f"Clientes de Repago con venta 0 · {business_label}",
        "sql": sql,
        "assumption": (
            f"Universo: clientes con EDF en estado PDV de {business_label}. "
            f"Venta considerada: {business_label} en el {period_label}. "
            "Venta 0 significa exactamente 0,00 HL en el snapshot de Repago."
        ),
        "clarifying_question": "",
        "reason": "",
        "intent": "repago_zero_sales",
        "repago_business": business_label,
    }


def _previous_analyst_question(context: dict[str, Any] | None = None) -> str:
    ctx = context or {}
    for key in ("_analyst_previous_question", "previous_question", "last_question", "last_user_question"):
        value = str(ctx.get(key) or "").strip()
        if value:
            return value
    return str(_LAST_ANALYST_TURN.get("question") or "").strip()


def _repago_zero_sales_followup_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    """Repara repreguntas de corrección como "esos no tienen venta 0"."""
    qn = _norm(q)
    correction = (
        "venta" in qn
        and any(k in qn for k in (
            "esos no", "esas no", "no son esos", "no son esas", "esos tienen",
            "esas tienen", "me trajiste", "me mostraste", "esta mal", "no esta bien"
        ))
    )
    if not correction:
        return None

    previous = _norm(_previous_analyst_question(context))
    if previous and "repago" in previous and _zero_sales_requested(previous):
        plan = _repago_zero_sales_plan(previous, context)
        if plan:
            plan = dict(plan)
            plan["title"] = "Corrección · " + str(plan.get("title") or "Clientes con venta 0")
            return plan

    # Si la aplicación conserva sólo el tema activo, al menos evita caer en la
    # venta total y pide la precisión que falta.
    active_topic = _norm((context or {}).get("active_topic") or "")
    if "repago" in active_topic:
        return {
            "action": "clarify",
            "title": "",
            "sql": "",
            "assumption": "",
            "clarifying_question": "¿Te referís a los clientes de **Repago con venta 0 de Cerveza/CZA** de la consulta anterior?",
            "reason": "repregunta de corrección sin detalle de negocio",
        }
    return None

def _sales_edf_cross_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    if not _is_current_sales_question(q) or not any(k in q for k in ("edf", "heladera", "heladeras", "equipos")):
        return None
    filters, notes = _sales_filters(q, context, "v")
    where_sales = ("WHERE " + " AND ".join(filters)) if filters else ""
    limit = _extract_limit(q)
    having = ""
    m = re.search(r"menos de\s+(\d+(?:[.,]\d+)?)\s*(?:hl|hectolit)", q)
    if m:
        having = f"WHERE COALESCE(s.hl_mes,0) < {float(m.group(1).replace(',', '.'))}"
    elif any(k in q for k in ("no compraron", "sin compra", "sin ventas", "no compran", "no compraron")):
        having = "WHERE COALESCE(s.hl_mes,0) <= 0"
    elif re.search(r"mas de\s+(\d+(?:[.,]\d+)?)\s*(?:hl|hectolit)", q):
        m = re.search(r"mas de\s+(\d+(?:[.,]\d+)?)\s*(?:hl|hectolit)", q)
        having = f"WHERE COALESCE(s.hl_mes,0) > {float(m.group(1).replace(',', '.'))}"
    sql = f"""
        WITH e AS (
            SELECT cliente_codigo, MAX(cliente) AS cliente, COUNT(*) AS cantidad_edf,
                   ROUND(AVG(repago_trimestre_pct),1) AS repago_promedio_pct
            FROM repago_edf
            WHERE UPPER(estado)='PDV'
            GROUP BY cliente_codigo
        ), s AS (
            SELECT cliente_codigo, MAX(COALESCE(NULLIF(nombre_fantasia,''), cliente)) AS cliente_venta,
                   SUM(hl) AS hl_mes
            FROM ventas_mes_actual v
            {where_sales}
            GROUP BY cliente_codigo
        )
        SELECT e.cliente_codigo, COALESCE(NULLIF(s.cliente_venta,''), e.cliente) AS cliente,
               e.cantidad_edf, e.repago_promedio_pct,
               ROUND(COALESCE(s.hl_mes,0),2) AS hl_mes
        FROM e LEFT JOIN s ON s.cliente_codigo=e.cliente_codigo
        {having}
        ORDER BY hl_mes ASC, cantidad_edf DESC
        LIMIT {limit}
    """
    return {
        "action":"query", "title":"Clientes con EDF vs venta del mes", "sql":sql,
        "assumption": _sales_period_note() + " EDF: sólo equipos en estado PDV.",
        "clarifying_question":"", "reason":"",
    }


def _sales_discount_cross_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    if not _is_current_sales_question(q) or "descuento" not in q:
        return None
    seg = _segment(q)
    filters, notes = _sales_filters(q, context, "v")
    where_sales = ("WHERE " + " AND ".join(filters)) if filters else ""
    disc_where = f"WHERE d.segmento={_sql_text(seg)}" if seg else ""
    no_sale = any(k in q for k in ("no compraron", "sin compra", "sin ventas", "no compran"))
    post = "WHERE COALESCE(s.hl_mes,0) <= 0" if no_sale else ""
    limit = _extract_limit(q)
    sql=f"""
        WITH d AS (
            SELECT cliente_codigo, MAX(cliente) AS cliente, segmento, MAX(descuento_pct) AS descuento_pct
            FROM descuentos_cliente d
            {disc_where}
            GROUP BY cliente_codigo, segmento
        ), s AS (
            SELECT cliente_codigo, SUM(hl) AS hl_mes
            FROM ventas_mes_actual v
            {where_sales}
            GROUP BY cliente_codigo
        )
        SELECT d.cliente_codigo, d.cliente, d.segmento, ROUND(d.descuento_pct,2) AS descuento_pct,
               ROUND(COALESCE(s.hl_mes,0),2) AS hl_mes
        FROM d LEFT JOIN s ON s.cliente_codigo=d.cliente_codigo
        {post}
        ORDER BY hl_mes ASC, descuento_pct DESC
        LIMIT {limit}
    """
    return {
        "action":"query", "title":"Descuento comercial vs venta del mes", "sql":sql,
        "assumption": _sales_period_note() + (f" Venta filtrada al foco {seg}." if seg else ""),
        "clarifying_question":"", "reason":"",
    }


def _current_sales_plan(q: str, context: dict[str, Any] | None = None) -> dict | None:
    if not _is_current_sales_question(q):
        return None
    if not _sales_rows():
        return {
            "action":"unavailable", "title":"", "sql":"", "assumption":"", "clarifying_question":"",
            "reason":"La fuente **Venta mes actual** todavía no tiene snapshot. Se actualizará automáticamente desde ventadiaria.txt.",
        }

    filters, notes = _sales_filters(q, context, "v")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    metric_expr, metric_alias, metric_label = _metric(q, "v")
    entity = _group_entity(q)
    requested_units = _requested_business_units(q, "v")
    limit = _extract_limit(q)
    low = any(k in q for k in ("menos", "menor", "poco", "bajo", "peor")) and not any(k in q for k in ("mas", "más", "mayor"))
    direction = "ASC" if low else "DESC"
    period_note = _sales_period_note()

    # Consulta temporal de compra: "cuándo compró el cliente 891" o
    # "cuándo compró 30645 el cliente 891". Devuelve días reales de compra
    # dentro del mes corriente, no un resumen de identidad del cliente/SKU.
    if _is_purchase_date_question(q):
        cust = _match_sales_customer(q, context)
        sku = _match_sales_sku(q, context)
        if not cust and not sku:
            return {
                "action":"clarify", "title":"", "sql":"", "assumption":"",
                "clarifying_question":"¿De qué **cliente** o **SKU** querés ver las fechas de compra?",
                "reason":"falta cliente o SKU para consulta de fecha de compra",
            }

        title_parts = []
        if cust:
            title_parts.append(f"cliente {cust[0]}" + (f" · {cust[1]}" if cust[1] else ""))
        if sku:
            title_parts.append(f"SKU {sku[0]}" + (f" · {sku[1]}" if sku[1] else ""))

        last_only = any(k in q for k in (
            "ultima compra", "ultima vez que compro", "compra mas reciente",
            "compra más reciente", "cuando fue la ultima",
        ))
        limit_sql = " LIMIT 1" if last_only else ""
        title_prefix = "Última compra" if last_only else "Fechas de compra del mes"
        sql = f"""
            SELECT DATE(v.fecha) AS fecha,
                   ROUND(SUM(v.hl),2) AS hl,
                   ROUND(SUM(v.importe_neto),2) AS importe_neto,
                   ROUND(SUM(v.facturas),0) AS facturas
            FROM ventas_mes_actual v
            {where}
            GROUP BY DATE(v.fecha)
            HAVING SUM(v.hl) > 0 OR SUM(v.importe_neto) > 0
            ORDER BY DATE(v.fecha) DESC
            {limit_sql}
        """
        return {
            "action":"query",
            "title": title_prefix + " · " + " · ".join(title_parts),
            "sql": sql,
            "assumption": period_note + " Se consideran días con venta neta positiva para el filtro solicitado.",
            "clarifying_question":"", "reason":"",
        }

    # Varias unidades de negocio pedidas explícitamente: detalle + TOTAL de las elegidas.
    # Ej.: "cuánto llevamos Marketplace, Aguas y Red Bull en pesos/HL".
    if len(requested_units) >= 2:
        base_filters, base_notes = _sales_filters(q, context, "v", include_business_focus=False)
        base_where = (" AND ".join(base_filters)) if base_filters else "1=1"
        wants_hl, wants_money, wants_invoices = _requested_sales_metrics(q)

        # Si no especifica métrica, mostramos las dos métricas comerciales principales.
        if not wants_hl and not wants_money and not wants_invoices:
            wants_hl = True
            wants_money = True

        metric_specs = []
        if wants_hl:
            metric_specs.append(("hl", "v.hl", "HL"))
        if wants_money:
            metric_specs.append(("importe_neto", "v.importe_neto", "importe neto"))
        if wants_invoices:
            metric_specs.append(("facturas", "v.facturas", "facturas"))

        detail_parts = []
        for label, condition in requested_units:
            safe_label = label.replace("'", "''")
            sums = ", ".join(f"SUM({expr}) AS {alias_name}" for alias_name, expr, _ in metric_specs)
            detail_parts.append(
                f"SELECT '{safe_label}' AS unidad_negocio, {sums}, 0 AS orden "
                f"FROM ventas_mes_actual v WHERE ({base_where}) AND ({condition})"
            )

        detail_sql = " UNION ALL ".join(detail_parts)
        total_sums = ", ".join(f"SUM({alias_name}) AS {alias_name}" for alias_name, _, _ in metric_specs)
        out_cols = ", ".join(f"ROUND({alias_name},2) AS {alias_name}" for alias_name, _, _ in metric_specs)
        sql = f"""
            WITH detalle AS (
                {detail_sql}
            ), salida AS (
                SELECT * FROM detalle
                UNION ALL
                SELECT 'TOTAL' AS unidad_negocio, {total_sums}, 1 AS orden FROM detalle
            )
            SELECT unidad_negocio, {out_cols}
            FROM salida
            ORDER BY orden ASC, unidad_negocio ASC
        """
        metric_title = " + ".join(label for _, _, label in metric_specs)
        title = f"Venta del mes por unidades de negocio · {metric_title} + TOTAL"
        if base_notes:
            title += " · " + " · ".join(base_notes)
        return {"action":"query", "title":title, "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}

    # Conteos simples. "Clientes con compra" = clientes cuyo neto acumulado en el filtro es > 0.
    if any(k in q for k in ("cuantos clientes", "cuántos clientes", "cantidad de clientes")):
        sql = f"""
            SELECT COUNT(*) AS clientes
            FROM (
                SELECT v.cliente_codigo
                FROM ventas_mes_actual v
                {where}
                GROUP BY v.cliente_codigo
                HAVING SUM(v.hl) > 0
            ) compradores
        """
        return {"action":"query", "title":"Clientes compradores del mes", "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}
    if any(k in q for k in ("cuantos productos", "cuántos productos", "cuantos sku", "cuántos sku", "cantidad de sku")):
        return {"action":"query", "title":"SKU vendidos en el mes", "sql":f"SELECT COUNT(DISTINCT v.sku) AS productos FROM ventas_mes_actual v {where} AND v.hl<>0" if where else "SELECT COUNT(DISTINCT v.sku) AS productos FROM ventas_mes_actual v WHERE v.hl<>0", "assumption":period_note, "clarifying_question":"", "reason":""}

    # Identidad específica: cliente o SKU sin pedir ranking.
    cust = _match_sales_customer(q, context)
    sku = _match_sales_sku(q, context)
    if cust and entity not in {"producto", "vendedor", "marca", "division", "unidad_negocio", "fecha", "localidad_base"}:
        sql=f"""
            SELECT v.cliente_codigo, MAX(COALESCE(NULLIF(v.nombre_fantasia,''),v.cliente)) AS cliente,
                   ROUND(SUM(v.hl),2) AS hl,
                   ROUND(SUM(CASE WHEN v.es_cza=1 THEN v.hl ELSE 0 END),2) AS hl_cza,
                   ROUND(SUM(CASE WHEN UPPER(v.unidad_negocio) LIKE '%UNG%' THEN v.hl ELSE 0 END),2) AS hl_ung,
                   ROUND(SUM(v.importe_neto),2) AS importe_neto
            FROM ventas_mes_actual v
            {where}
            GROUP BY v.cliente_codigo
        """
        return {"action":"query", "title":f"Venta mes actual · cliente {cust[0]}", "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}
    if sku and entity not in {"cliente", "vendedor", "marca", "division", "unidad_negocio", "fecha", "localidad_base"}:
        sql=f"""
            SELECT v.sku, MAX(v.producto) AS producto, ROUND(SUM(v.hl),2) AS hl,
                   COUNT(DISTINCT v.cliente_codigo) AS clientes,
                   ROUND(SUM(v.importe_neto),2) AS importe_neto
            FROM ventas_mes_actual v
            {where}
            GROUP BY v.sku
        """
        return {"action":"query", "title":f"Venta mes actual · SKU {sku[0]}", "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}

    # Rankings/agrupaciones.
    if entity == "cliente":
        select="v.cliente_codigo, MAX(COALESCE(NULLIF(v.nombre_fantasia,''),v.cliente)) AS cliente"
        group="v.cliente_codigo"
    elif entity == "producto":
        select="v.sku, MAX(v.producto) AS producto"
        group="v.sku"
    elif entity == "vendedor":
        select="v.vendedor_codigo, MAX(v.vendedor) AS vendedor, MAX(v.supervisor) AS supervisor"
        group="v.vendedor_codigo"
    elif entity == "supervisor":
        select="v.supervisor"
        group="v.supervisor"
    elif entity == "marca":
        select="v.marca_unificada AS marca"
        group="v.marca_unificada"
    elif entity == "division":
        select="v.division"
        group="v.division"
    elif entity == "unidad_negocio":
        # Para un desglose por Unidad de Negocio no aplicamos un filtro de negocio
        # inferido desde la misma pregunta: mostramos el detalle completo y el TOTAL.
        unit_filters, unit_notes = _sales_filters(q, context, "v", include_business_focus=False)
        unit_where = ("WHERE " + " AND ".join(unit_filters)) if unit_filters else ""
        wants_hl, wants_money, wants_invoices = _requested_sales_metrics(q)
        if not wants_hl and not wants_money and not wants_invoices:
            wants_hl = True
            wants_money = True

        metric_specs = []
        if wants_hl:
            metric_specs.append(("hl", "v.hl", "HL"))
        if wants_money:
            metric_specs.append(("importe_neto", "v.importe_neto", "importe neto"))
        if wants_invoices:
            metric_specs.append(("facturas", "v.facturas", "facturas"))

        detail_sums = ", ".join(f"SUM({expr}) AS {alias_name}" for alias_name, expr, _ in metric_specs)
        total_sums = ", ".join(f"SUM({alias_name}) AS {alias_name}" for alias_name, _, _ in metric_specs)
        out_cols = ", ".join(f"ROUND({alias_name},2) AS {alias_name}" for alias_name, _, _ in metric_specs)
        sql=f"""
            WITH detalle AS (
                SELECT COALESCE(NULLIF(v.unidad_negocio,''),'SIN UNIDAD') AS unidad_negocio,
                       {detail_sums}
                FROM ventas_mes_actual v
                {unit_where}
                GROUP BY COALESCE(NULLIF(v.unidad_negocio,''),'SIN UNIDAD')
            ), salida AS (
                SELECT *, 0 AS orden FROM detalle
                UNION ALL
                SELECT 'TOTAL' AS unidad_negocio, {total_sums}, 1 AS orden FROM detalle
            )
            SELECT unidad_negocio, {out_cols}
            FROM salida
            ORDER BY orden ASC, unidad_negocio ASC
        """
        metric_title = " + ".join(label for _, _, label in metric_specs)
        title=f"Venta del mes por unidad de negocio · {metric_title} + TOTAL"
        if unit_notes:
            title += " · " + " · ".join(unit_notes)
        return {"action":"query", "title":title, "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}
    elif entity == "fecha":
        select="v.fecha"
        group="v.fecha"
        if any(k in q for k in ("que dia", "qué día", "dia vendimos mas", "día vendimos más", "dia vendimos menos", "día vendimos menos")):
            limit=1
    elif entity == "localidad_base":
        select="COALESCE(NULLIF(v.localidad_base,''),'SIN BASE') AS localidad_base"
        group="COALESCE(NULLIF(v.localidad_base,''),'SIN BASE')"
    else:
        # Total del mes/filtro pedido. Si el usuario pide una métrica concreta ($/facturas),
        # respondemos esa métrica en vez de mezclarla con información no solicitada.
        if metric_alias == "importe_neto":
            sql=f"""
                SELECT ROUND(SUM(v.importe_neto),2) AS importe_neto
                FROM ventas_mes_actual v
                {where}
            """
            title="Venta en $ del mes"
        elif metric_alias == "facturas":
            sql=f"""
                SELECT ROUND(SUM(v.facturas),0) AS facturas
                FROM ventas_mes_actual v
                {where}
            """
            title="Facturas del mes"
        else:
            sql=f"""
                SELECT ROUND(SUM(v.hl),2) AS hl,
                       ROUND(SUM(v.importe_neto),2) AS importe_neto,
                       COUNT(DISTINCT v.cliente_codigo) AS clientes,
                       COUNT(DISTINCT v.sku) AS productos
                FROM ventas_mes_actual v
                {where}
            """
            title="Venta acumulada del mes"
        if notes:
            title += " · " + " · ".join(notes)
        return {"action":"query", "title":title, "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}

    having=""
    m=re.search(r"menos de\s+(\d+(?:[.,]\d+)?)\s*(?:hl|hectolit)", q)
    if m and metric_alias == "hl":
        having=f"HAVING SUM(v.hl) < {float(m.group(1).replace(',', '.'))}"
    m2=re.search(r"mas de\s+(\d+(?:[.,]\d+)?)\s*(?:hl|hectolit)", q)
    if m2 and metric_alias == "hl":
        having=f"HAVING SUM(v.hl) > {float(m2.group(1).replace(',', '.'))}"

    sql=f"""
        SELECT {select}, ROUND({metric_expr},2) AS {metric_alias}
        FROM ventas_mes_actual v
        {where}
        GROUP BY {group}
        {having}
        ORDER BY {metric_alias} {direction}
        LIMIT {limit}
    """
    title=f"Venta del mes por {entity.replace('_',' ')}"
    return {"action":"query", "title":title, "sql":sql, "assumption":period_note, "clarifying_question":"", "reason":""}

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

    # Listados de Frescura por mes deben resolverse como conjuntos completos,
    # sin exigir un SKU individual. Ej.: "productos frescura mes 09 y 10".
    plan = _freshness_month_products_plan(q)
    if plan:
        return plan

    # Repago + venta 0 es un cruce específico del snapshot de Repago y debe
    # resolverse antes que la venta CHESS del mes corriente. También se atienden
    # primero las repreguntas de corrección ("esos no tienen venta 0").
    for planner in (_repago_zero_sales_followup_plan, _repago_zero_sales_plan):
        plan = planner(q, context)
        if plan:
            return plan

    # Cruces con venta mes actual tienen prioridad para que "este mes" nunca caiga
    # en el histórico mensual de Repago.
    for planner in (_sales_stock_cross_plan, _sales_edf_cross_plan, _sales_discount_cross_plan, _sales_no_activity_plan, _current_sales_plan):
        plan = planner(q, context)
        if plan:
            return plan

    for planner in (_stock_comparison_plan, _stock_ranking_plan, _freshness_plan, _customer_cross_plan):
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
        "reason":"Puedo analizar libremente venta del mes corriente, stock/Frescura, EDF/repago, ventas históricas en HL, descuentos y topes, pero no pude traducir esta frase a un cálculo seguro con las columnas disponibles.",
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
    "ventas_mes_actual", "ventas_mes_actual_meta", "maestro_clientes_actual",
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
    refs = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", clean, flags=re.I)
    used = sorted({ref.lower() for ref in refs if ref.lower() in ALLOWED_TABLES})
    return df, used


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
    "hl": "HL",
    "hl_mes": "HL mes",
    "hl_cza": "HL CZA",
    "hl_ung": "HL UNG",
    "hl_aguas": "HL Aguas",
    "hl_rb": "HL Red Bull",
    "periodo": "Período",
    "cantidad_edf": "Cantidad EDF",
    "importe_neto": "Importe neto",
    "importe_final": "Importe final",
    "facturas": "Facturas",
    "fecha": "Fecha",
    "sku": "SKU",
    "producto": "Producto",
    "vendedor": "Vendedor",
    "vendedor_codigo": "Código vendedor",
    "supervisor": "Supervisor",
    "marca": "Marca",
    "division": "División",
    "unidad_negocio": "Unidad de negocio",
    "localidad_base": "Base",
    "stock_bultos": "Stock bultos",
    "mes": "Mes",
    "proximo_vencimiento": "Próximo vencimiento",
    "riesgo_bultos": "Riesgo bultos",
    "estado": "Estado",
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
    text = str(value)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?", text)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return text


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
        show_all = bool(plan.get("display_all"))
        if show_all:
            answer = f"**{title}**\n\nEncontré **{len(df)} resultado(s)**:\n\n{_markdown_table(df, limit=len(df))}"
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
    if any(t.startswith("ventas_mes_actual") or t == "maestro_clientes_actual" for t in tables):
        out.append("CHESS · venta mes actual · snapshot local")
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

    # Guardar el turno recién después de planificar: así una repregunta puede leer
    # la consulta anterior. Si el contexto es persistente por sesión, queda aislado
    # allí; el global funciona como fallback para el despliegue actual de un solo bot.
    global _LAST_ANALYST_TURN
    _LAST_ANALYST_TURN = {"question": str(question or ""), "plan": dict(plan or {})}
    if isinstance(context, dict):
        context["_analyst_previous_question"] = str(question or "")
        if plan.get("intent"):
            context["active_topic"] = str(plan.get("intent"))

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
