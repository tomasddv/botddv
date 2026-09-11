from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

from sources import repago_source, frescura_source, grupos_source


def normalize(text):
    text = unicodedata.normalize("NFD", str(text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def number(v, dec=1):
    try:
        value = float(v)
    except Exception:
        return "—"
    txt = f"{value:,.{dec}f}"
    return txt.replace(",", "X").replace(".", ",").replace("X", ".")


def pct_points(v):
    try:
        return f"{float(v) * 100:.2f}%".replace(".", ",")
    except Exception:
        return "—"


def pct(v):
    try:
        return f"{float(v):.1f}%".replace(".", ",")
    except Exception:
        return "—"


def date_arg(v):
    if not v:
        return "—"
    text = str(v)[:10]
    try:
        y, m, d = text.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return str(v)


def extract_code(text):
    codes = re.findall(r"(?<![\d.,])(\d{1,7})(?![\d.,])", text)
    for code in codes:
        if len(code) >= 3 or str(text).strip().isdigit():
            return code.lstrip("0") or "0"
    return None



def scope_from(text):
    clean = normalize(text)
    if "trelew" in clean:
        return "TRELEW"
    if "madryn" in clean or "puerto madryn" in clean:
        return "MADRYN"
    if "total ddv" in clean or clean == "total" or " ddv" in f" {clean}":
        return "DDV"
    return None

def scope_label(scope):
    return "TOTAL DDV" if scope == "DDV" else str(scope or "DDV").title()


STOPWORDS = {
    "repago", "repaga", "repagan", "repagando", "cliente", "clientes", "del", "de", "la", "el", "los", "las",
    "como", "esta", "estado", "cuantas", "cuantos", "cuanta", "cuanto", "cantidad", "tiene", "tienen", "hay",
    "heladera", "heladeras", "edf", "equipo", "equipos", "que", "cual", "cuales", "descuento", "descuentos",
    "en", "core", "value", "litro", "lata", "porcentaje", "porcentajes", "mostrar", "muestra", "dame", "decime",
    "frescura", "sku", "riesgo", "vencimiento", "vencimientos", "stock", "lote", "lotes", "total", "ddv",
    "trelew", "madryn", "puerto", "un", "una", "para", "y", "o", "se", "me", "por", "favor", "razon", "social", "nombre", "compra", "compras", "vende", "venta", "ventas", "mes", "mensual", "hl", "hectolitro", "hectolitros",
    "mas", "menos", "mayor", "menor", "supera", "superan", "superior", "inferior", "arriba", "debajo",
    "al", "desde", "hasta", "ese", "esa", "esos", "esas", "mismo", "misma", "anterior", "anteriores",
    "serie", "serial", "numero", "nro", "ubicado", "ubicada", "ubicacion", "donde", "colocado", "colocada",
    "tope", "topes", "bulto", "bultos", "puede", "pueden", "comprar", "ultimo", "ultima", "corriente", "actual", "trimestre", "promedio",
    "este", "esta", "estos", "estas", "sobre", "son", "con", "sin", "lo", "le", "les", "quedan", "queda",
}


def entity_query(raw: str) -> str:
    text = normalize(raw)
    code = extract_code(text)
    if code:
        return code
    # Quitar porcentajes/umbrales para que un seguimiento como "más del 75%" no parezca un nombre de cliente.
    text = re.sub(r"\b\d{1,3}(?:[.,]\d+)?\s*%", " ", text)
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t not in STOPWORDS and len(t) > 1]
    # Números <=100 aislados en consultas de repago suelen ser umbrales, no códigos de cliente.
    tokens = [t for t in tokens if not (t.isdigit() and int(t) <= 100)]
    return " ".join(tokens).strip()


def _copy_context(context: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "active_client_id": None,
        "active_client_name": None,
        "active_sku": None,
        "active_sku_name": None,
        "active_serial": None,
        "active_scope": "DDV",
        "active_topic": None,
        "last_intent": None,
        "last_discount_segment": None,
        "last_repago_op": None,
        "last_repago_threshold": None,
        "last_repago_mode": "trimestre",
        "last_tope_segment": None,
        "pending_stock_scope": False,
        "pending_stock_sku": None,
    }
    if context:
        base.update(context)
    return base


def _set_client_context(ctx: dict[str, Any], customer: dict | None):
    if not customer:
        return
    ctx["active_client_id"] = str(customer.get("id") or "").strip() or ctx.get("active_client_id")
    ctx["active_client_name"] = (
        customer.get("name") or customer.get("legal_name") or customer.get("fantasia") or ctx.get("active_client_name")
    )


def _set_sku_context(ctx: dict[str, Any], product: dict | None, scope: str | None = None):
    if product:
        ctx["active_sku"] = str(product.get("codigo") or "").strip() or ctx.get("active_sku")
        ctx["active_sku_name"] = product.get("descripcion") or ctx.get("active_sku_name")
    if scope:
        ctx["active_scope"] = scope


def _candidate_lines(matches, source_label):
    if not matches:
        return ""
    lines = [f"Encontré varias coincidencias en **{source_label}**. Indicame el código:"]
    for c in matches[:8]:
        lines.append(f"- **{c.get('id')}** · {c.get('name') or c.get('legal_name') or 'Sin nombre'}")
    return "\n".join(lines)


def _resolve_repago_customer(raw, ctx):
    source_status = repago_source.status()
    if source_status.get("ok") is not True:
        return None, "La fuente **EDF/Repago** se está actualizando con el cálculo nuevo. Probá de nuevo en unos segundos."
    query = entity_query(raw)
    if not query and ctx.get("active_client_id"):
        c = repago_source.customer(ctx["active_client_id"])
        if c:
            return c, None
        return None, f"El cliente activo **{ctx['active_client_id']}** no figura en EDF/Repago."
    if not query:
        return None, "Necesito el código o nombre del cliente."
    if query.isdigit():
        c = repago_source.customer(query)
        if c:
            return c, None
    matches = repago_source.search_customers(query, limit=8)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, _candidate_lines(matches, "EDF")
    return None, f"No encontré un cliente que coincida con **{query}** en EDF."


def _resolve_group_customer(raw, ctx):
    query = entity_query(raw)
    if not query and ctx.get("active_client_id"):
        c = grupos_source.customer(ctx["active_client_id"])
        if c:
            return c, None
        return None, f"El cliente activo **{ctx['active_client_id']}** no figura en Grupo de clientes."
    if not query:
        return None, "Necesito el código o nombre del cliente."
    if query.isdigit():
        c = grupos_source.customer(query)
        if c:
            return c, None
    matches = grupos_source.search_customers(query, limit=8)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, _candidate_lines(matches, "Grupo de clientes")

    # Grupo de clientes sólo publica nombre de fantasía. Para consultas por
    # razón social usamos el padrón de EDF como índice de identidad y luego
    # volvemos al snapshot de descuentos por código.
    repago_matches = repago_source.search_customers(query, limit=8)
    linked = []
    for candidate in repago_matches:
        gc = grupos_source.customer(candidate.get("id"))
        if gc:
            linked.append((candidate, gc))
    if len(linked) == 1:
        repago_customer, group_customer = linked[0]
        # Conservamos la razón social para que el contexto sea más claro.
        group_customer = dict(group_customer)
        group_customer["legal_name"] = repago_customer.get("legal_name") or ""
        return group_customer, None
    if len(linked) > 1:
        lines = ["Encontré varias coincidencias por razón social. Indicame el código:"]
        for rc, _ in linked[:8]:
            lines.append(f"- **{rc.get('id')}** · {rc.get('name') or ''} · {rc.get('legal_name') or ''}")
        return None, "\n".join(lines)

    return None, f"No encontré un cliente que coincida con **{query}** en Grupo de clientes."


MONTHS_ES = {
    "01": "enero", "02": "febrero", "03": "marzo", "04": "abril",
    "05": "mayo", "06": "junio", "07": "julio", "08": "agosto",
    "09": "septiembre", "10": "octubre", "11": "noviembre", "12": "diciembre",
}


def _period_label(period):
    text = str(period or "")
    if re.match(r"^\d{4}-\d{2}$", text):
        y, m = text.split("-")
        return f"{MONTHS_ES.get(m, m)} {y}"
    return text or "último mes completo"


def _periods_label(periods):
    values = [_period_label(p) for p in (periods or [])]
    return ", ".join(values) if values else "sin períodos disponibles"


def _monthly_sales_answer(raw, ctx):
    cust, err = _resolve_repago_customer(raw, ctx)
    if err:
        return err, ["Ventas mensuales EDF · snapshot local"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or cust.get("legal_name") or ""
    period = cust.get("monthly_period") or ""
    total = float(cust.get("monthly_hl") or 0)
    by = cust.get("monthly_by_business") or {}
    parts = []
    for business in ("CZA", "UNG", "AGUAS", "RB"):
        value = float(by.get(business) or 0)
        if abs(value) > 1e-9:
            parts.append(f"{business} **{number(value, 2)} hl**")
    detail = " · ".join(parts) if parts else "sin ventas registradas"
    return (
        f"**Cliente {code} — {name}** compró **{number(total, 2)} hl** en **{_period_label(period)}**.\n\n"
        f"{detail}."
    ), ["Ventas mensuales EDF · snapshot local"]



def _repago_mode_from_text(raw: str, default: str = "trimestre") -> str:
    text = normalize(raw)
    if any(k in text for k in (
        "ultimo mes", "mes corriente", "mes actual", "este mes", "solo mes", "un mes"
    )):
        return "ultimo_mes"
    if any(k in text for k in ("trimestre", "promedio mensual", "promedio de")):
        return "trimestre"
    return default if default in {"trimestre", "ultimo_mes"} else "trimestre"



def _repago_answer(raw, ctx, forced_mode=None):
    cust, err = _resolve_repago_customer(raw, ctx)
    if err:
        return err, ["Repagos EDF · snapshot local"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or cust.get("legal_name") or ""

    default_mode = forced_mode or ctx.get("last_repago_mode") or "trimestre"
    mode = _repago_mode_from_text(raw, default_mode)
    rows = repago_source.customer_repayments(code, mode=mode)
    if not rows:
        return f"**Cliente {code} — {name}** no tiene EDF con repago calculado.", ["Repagos EDF · snapshot local"]

    periods = rows[0].get("repago_periods") or []
    placed = [r for r in rows if str(r.get("status") or "").upper() == "PDV"]

    if mode == "ultimo_mes":
        period_text = _period_label(periods[-1] if periods else cust.get("latest_period"))
        lines = [f"**Cliente {code} — {name}**", f"Repago de **{period_text}**:", ""]
        source = "Repagos EDF · último mes · secuencial"
    else:
        lines = [
            f"**Cliente {code} — {name}**",
            f"Repago · promedio mensual de **{_periods_label(periods)}**:",
            "",
        ]
        source = "Repagos EDF · trimestre promedio · secuencial"

    for r in placed:
        serial = f" · serie {r.get('serial')}" if r.get("serial") else ""
        lines.append(f"- **{r.get('asset','—')}** · {r.get('model','—')}{serial}: **{pct(r.get('pct'))}**")

    if not placed:
        lines.append("No tiene EDF colocados en PDV.")
    else:
        over_75 = sum(1 for r in placed if float(r.get("pct") or 0) >= 75)
        lines += ["", f"**{over_75} de {len(placed)}** están en **75% o más**."]

    ctx["last_repago_mode"] = mode
    return "\n".join(lines), [source]

def _edf_count_answer(raw, ctx):
    cust, err = _resolve_repago_customer(raw, ctx)
    if err:
        return err, ["EDF · snapshot local"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or cust.get("legal_name") or ""
    rows = repago_source.customer_edfs(code)
    count = len(rows)
    if count == 0:
        return f"**Cliente {code} — {name}** no tiene EDF/heladeras asignadas en el snapshot actual.", ["EDF · snapshot local"]

    models = Counter(str(r.get("model") or "Sin modelo") for r in rows)
    model_text = ", ".join(f"{qty} {model}" for model, qty in models.most_common())
    noun = "EDF/heladera" if count == 1 else "EDF/heladeras"
    return (
        f"**Cliente {code} — {name}** tiene **{count} {noun}**.\n\n"
        f"Distribución: {model_text}."
    ), ["EDF · snapshot local"]





def _serial_from_query(raw: str) -> str:
    """Extrae serie completa o parcial. También acepta 'EDF 565964'."""
    original = str(raw or "").strip()

    patterns = [
        r"(?:n[uú]mero\s+de\s+serie|nro\.?\s*serie|nro\.?\s*de\s*serie|serie|serial)\s*(?:nro\.?|n[uú]mero)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,})",
        r"(?:edf|equipo|heladera)\s*(?:nro\.?|n[uú]mero)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{3,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.I)
        if match:
            return match.group(1).strip(" .,:;?")

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}", original):
        return original.strip(" .,:;?")

    if any(k in normalize(original) for k in ("donde", "ubicacion", "ubicado", "ubicada", "colocado", "colocada")):
        candidates = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9._/-]{4,}\b", original)
        if candidates:
            return candidates[-1].strip(" .,:;?")
    return ""

def _edf_location_answer(raw, ctx):
    source_status = repago_source.status()
    if source_status.get("ok") is not True:
        return "La fuente **EDF/Repago** se está actualizando. Probá de nuevo en unos segundos.", ["EDF · actualización automática"]

    serial = _serial_from_query(raw)
    if not serial and ctx.get("active_serial"):
        serial = str(ctx.get("active_serial") or "")
    if not serial:
        return "Decime el **número de serie completo o una parte de la serie** del EDF.", ["EDF · snapshot local"]

    row = repago_source.edf_by_serial(serial)
    if not row:
        matches = repago_source.search_edf_serial(serial, limit=12)
        if len(matches) == 1:
            row = matches[0]
        elif len(matches) > 1:
            lines = [f"Encontré **{len(matches)} series** que se aproximan a **{serial}**. Indicame cuál:"]
            for r in matches:
                cid = str(r.get("customer_id") or "").strip()
                name = r.get("customer_name") or r.get("legal_name") or ""
                where = f" · cliente {cid} — {name}" if cid else f" · {r.get('status') or r.get('deposit') or 'sin ubicación'}"
                lines.append(f"- **{r.get('serial') or '—'}** · {r.get('model') or '—'}{where}")
            return "\n".join(lines), ["EDF · snapshot local"]
        else:
            return f"No encontré una serie que contenga **{serial}**.", ["EDF · snapshot local"]

    ctx["active_serial"] = row.get("serial") or serial
    cid = str(row.get("customer_id") or "").strip()
    if cid:
        ctx["active_client_id"] = cid
        ctx["active_client_name"] = row.get("customer_name") or row.get("legal_name") or ctx.get("active_client_name")

    serie = row.get("serial") or serial
    model = row.get("model") or "—"
    status = str(row.get("status") or "").upper()
    deposit = row.get("deposit") or ""

    if status == "PDV" and cid:
        name = row.get("customer_name") or row.get("legal_name") or ""
        location_bits = [x for x in (row.get("address"), row.get("city")) if x]
        location = " · ".join(location_bits)
        extra = f"\n{location}" if location else ""
        return f"La serie **{serie}** · {model} está colocada en el **cliente {cid} — {name}**.{extra}", ["EDF · snapshot local"]

    status_labels = {
        "DEPOSITO": "en depósito",
        "STOCK": "en stock",
        "REPARACION": "en reparación",
        "BAJA DEFINITIVA": "dada de baja",
    }
    where = status_labels.get(status, status.lower() if status else "sin ubicación definida")
    base = f" · {deposit}" if deposit else ""
    return f"La serie **{serie}** · {model} está **{where}**{base}.", ["EDF · snapshot local"]


def _valid_followup_entity(raw: str, ctx: dict[str, Any]) -> bool:
    """Evita convertir cualquier frase desconocida en una consulta del tema anterior."""
    text = normalize(raw)
    if text in {"y ese", "y esa", "ese", "esa", "el mismo", "la misma"}:
        return bool(ctx.get("active_client_id") or ctx.get("active_sku") or ctx.get("active_serial"))

    query = entity_query(raw)
    if not query:
        return False

    topic = ctx.get("active_topic")
    if topic in {"edf_count", "repago", "repago_count", "monthly_sales"}:
        if query.isdigit() and repago_source.customer(query):
            return True
        return len(repago_source.search_customers(query, limit=2)) > 0

    if topic in {"discount", "tope"}:
        if query.isdigit() and (grupos_source.customer(query) or repago_source.customer(query)):
            return True
        return (
            len(grupos_source.search_customers(query, limit=2)) > 0
            or len(repago_source.search_customers(query, limit=2)) > 0
        )

    if topic in {"frescura", "stock"}:
        if query.isdigit() and frescura_source.product(query):
            return True
        return len(frescura_source.search_products(query, limit=2)) > 0

    return False

def _repago_threshold(text: str):
    """Devuelve (operador, umbral). pct está expresado en puntos porcentuales (75 == 75%)."""
    norm = normalize(text)
    nums = []
    for raw_n in re.findall(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)(?:\s*%)?", norm):
        try:
            value = float(raw_n.replace(",", "."))
        except Exception:
            continue
        if 0 <= value <= 100:
            nums.append(value)
    threshold = nums[-1] if nums else None

    if "no repag" in norm:
        return "<", 75.0 if threshold is None else threshold
    if threshold is None:
        return ">=", 100.0
    if any(k in norm for k in ("mas del", "mas de", "mayor a", "mayor que", "supera", "superan", "arriba de")):
        return ">", threshold
    if any(k in norm for k in ("menos del", "menos de", "menor a", "menor que", "debajo de")):
        return "<", threshold
    if any(k in norm for k in ("al menos", "o mas", "desde")):
        return ">=", threshold
    if any(k in norm for k in ("como maximo", "o menos", "hasta")):
        return "<=", threshold
    return ">=", threshold


def _op_label(op: str, threshold: float) -> str:
    sign = {">": ">", ">=": "≥", "<": "<", "<=": "≤"}.get(op, op)
    return f"{sign}{number(threshold, 0)}%"



def _repago_count_answer(raw, ctx, forced_op=None, forced_threshold=None, forced_mode=None):
    cust, err = _resolve_repago_customer(raw, ctx)
    if err:
        return err, ["Repagos EDF · snapshot local"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or cust.get("legal_name") or ""

    default_mode = forced_mode or ctx.get("last_repago_mode") or "trimestre"
    mode = _repago_mode_from_text(raw, default_mode)
    rows = [
        r for r in repago_source.customer_repayments(code, mode=mode)
        if str(r.get("status") or "").upper() == "PDV"
    ]

    op, threshold = _repago_threshold(raw)
    if forced_op is not None:
        op = forced_op
    if forced_threshold is not None:
        threshold = float(forced_threshold)

    def ok(v):
        try:
            x = float(v or 0)
        except Exception:
            x = 0.0
        if op == ">":
            return x > threshold
        if op == "<":
            return x < threshold
        if op == "<=":
            return x <= threshold
        return x >= threshold

    matches = [r for r in rows if ok(r.get("pct"))]
    if not rows:
        return f"**Cliente {code} — {name}** no tiene EDF/heladeras colocadas.", ["Repagos EDF · snapshot local"]

    period = rows[0].get("repago_periods") or []
    period_label = _period_label(period[-1]) if mode == "ultimo_mes" and period else _periods_label(period)
    ctx["last_repago_mode"] = mode
    return (
        f"**Cliente {code} — {name}** · {period_label}: **{len(matches)} de {len(rows)}** EDF "
        f"tienen repago **{_op_label(op, threshold)}**."
    ), ["Repagos EDF · snapshot local"]

def _discount_segment(text):
    if "core" in text:
        return "CORE"
    if "value" in text and "litro" in text:
        return "VALUE LITRO"
    if "value" in text and "lata" in text:
        return "VALUE LATA"
    if "litro" in text:
        return "VALUE LITRO"
    if "lata" in text:
        return "VALUE LATA"
    if "value" in text:
        return "VALUE"
    return None


def _discount_label(row):
    seg = str(row.get("segmento") or "").upper()
    sub = str(row.get("subsegmento") or "").upper()
    if seg == "CORE":
        return "CORE"
    if seg == "VALUE" and sub:
        return f"VALUE {sub}"
    return seg or sub or "Segmento"


def _discount_answer(raw, ctx, requested_segment=None):
    if grupos_source.status().get("ok") is not True:
        return (
            "La fuente **Grupo de clientes** se está preparando automáticamente. Probá de nuevo en unos segundos.",
            ["Grupo de clientes"],
        )
    cust, err = _resolve_group_customer(raw, ctx)
    if err:
        return err, ["Grupo de clientes · snapshot local"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or ""
    rows = grupos_source.discounts(code, requested_segment)

    if requested_segment == "CORE":
        if not rows:
            return f"**Cliente {code} — {name}** no figura con descuento **CORE** en el snapshot actual.", ["Grupo de clientes · snapshot local"]
        row = rows[0]
        return f"**Cliente {code} — {name}**\n\nDescuento **CORE: {pct_points(row.get('porcentaje_total'))}**.", ["Grupo de clientes · snapshot local"]

    if requested_segment in {"VALUE LITRO", "VALUE LATA"}:
        if not rows:
            return f"**Cliente {code} — {name}** no figura con descuento **{requested_segment}** en el snapshot actual.", ["Grupo de clientes · snapshot local"]
        row = rows[0]
        return f"**Cliente {code} — {name}**\n\nDescuento **{requested_segment}: {pct_points(row.get('porcentaje_total'))}**.", ["Grupo de clientes · snapshot local"]

    if requested_segment == "VALUE":
        if not rows:
            return f"**Cliente {code} — {name}** no figura con descuentos **VALUE** en el snapshot actual.", ["Grupo de clientes · snapshot local"]
        lines = [f"**Cliente {code} — {name}**", ""]
        for row in sorted(rows, key=lambda r: _discount_label(r)):
            lines.append(f"- **{_discount_label(row)}: {pct_points(row.get('porcentaje_total'))}**")
        return "\n".join(lines), ["Grupo de clientes · snapshot local"]

    rows = grupos_source.discounts(code)
    if not rows:
        return f"**Cliente {code} — {name}** no figura con descuentos en el snapshot actual.", ["Grupo de clientes · snapshot local"]
    lines = [f"**Cliente {code} — {name}**", ""]
    for row in sorted(rows, key=lambda r: (_discount_label(r), r.get("porcentaje_total", 0))):
        lines.append(f"- **{_discount_label(row)}: {pct_points(row.get('porcentaje_total'))}**")
    return "\n".join(lines), ["Grupo de clientes · snapshot local"]



def _tope_segment(text: str):
    t = normalize(text)
    if "core" in t:
        return "CORE"
    if "value" in t:
        return "VALUE"
    return None


def _tope_answer(raw, ctx, requested_segment=None):
    if grupos_source.status().get("ok") is not True:
        return "La fuente de **topes Core/Value** se está actualizando. Probá de nuevo en unos segundos.", ["Planificacion · topes"]

    cust, err = _resolve_group_customer(raw, ctx)
    if err:
        return err, ["Planificacion · topes Core/Value"]
    _set_client_context(ctx, cust)
    code = cust["id"]
    name = cust.get("name") or cust.get("legal_name") or ""

    segment = requested_segment or _tope_segment(raw)
    rows = grupos_source.topes(code, segment)
    if not rows:
        target = f" **{segment}**" if segment else ""
        return f"No pude determinar el tope{target} del **cliente {code} — {name}** con la información actual.", ["Planificacion · topes Core/Value"]

    if segment and len(rows) == 1:
        r = rows[0]
        return (
            f"**Cliente {code} — {name}** · Tope **{segment}: {number(r.get('tope_bultos'), 0)} bultos** "
            f"(canal {r.get('canal') or '—'})."
        ), ["Planificacion · Control bultos Core/Value"]

    lines = [f"**Cliente {code} — {name}**", ""]
    for r in rows:
        lines.append(
            f"- Tope **{r.get('segmento')}: {number(r.get('tope_bultos'), 0)} bultos** "
            f"(canal {r.get('canal') or '—'})."
        )
    return "\n".join(lines), ["Planificacion · Control bultos Core/Value"]


def _stock_answer(product, scope):
    code = str(product.get("codigo") or "")
    title = product.get("descripcion") or ""
    if scope == "DDV":
        stock = product.get("stock_total_ddv")
    elif scope == "TRELEW":
        stock = product.get("stock_trelew")
    else:
        stock = product.get("stock_madryn")
    return (
        f"**SKU {code} — {title}** · **{scope_label(scope)}**\n\n"
        f"Stock actual: **{number(stock)} bultos**."
    ), ["Frescura · stock snapshot local"]


def _ask_stock_scope(product, ctx):
    _set_sku_context(ctx, product, None)
    ctx["pending_stock_scope"] = True
    ctx["pending_stock_sku"] = str(product.get("codigo") or "")
    ctx["active_topic"] = "stock"
    return (
        f"¿De qué localidad querés consultar el stock de **{product.get('codigo')} — {product.get('descripcion','')}**? "
        "Decime **Trelew**, **Madryn** o **Total DDV**.",
        [],
    )


def _resolve_frescura_product(raw: str, ctx: dict[str, Any]):
    text = normalize(raw)
    code = extract_code(text)
    if code and frescura_source.product(code):
        return frescura_source.product(code), None

    query = entity_query(raw)
    if query and not query.isdigit():
        matches = frescura_source.search_products(query, limit=8)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            lines = ["Encontré varios SKU. Indicame el código:"]
            for p in matches:
                lines.append(f"- **{p.get('codigo')}** · {p.get('descripcion','')}")
            return None, "\n".join(lines)

    active = ctx.get("active_sku")
    if active:
        product = frescura_source.product(active)
        if product:
            return product, None
    if code:
        return None, f"No encontré el SKU **{code}** en el snapshot de Frescura."
    return None, "Indicame el SKU o producto. Ejemplo: **frescura 30789**."


def _frescura_answer(product, scope):
    code = str(product.get("codigo") or "")
    rows = frescura_source.forecast(code, scope)
    prof = frescura_source.profile(code, scope)
    title = product.get("descripcion") or ""
    if not rows:
        stock = product.get("stock_total_ddv") if scope == "DDV" else product.get("stock_trelew" if scope == "TRELEW" else "stock_madryn")
        return (
            f"**SKU {code} — {title}** · **{scope_label(scope)}**\n\n"
            f"Stock actual: **{number(stock)} bultos**. No tiene vencimientos analizados dentro del horizonte de 120 días.",
            ["Frescura Predictiva v9 · snapshot local"],
        )

    rows = sorted(rows, key=lambda r: str(r.get("fecha_vencimiento") or ""))
    stock_scope = product.get("stock_total_ddv") if scope == "DDV" else product.get("stock_trelew" if scope == "TRELEW" else "stock_madryn")
    lines = [
        f"**SKU {code} — {title}** · **{scope_label(scope)}**",
        "",
        f"Stock actual: **{number(stock_scope)} bultos**",
    ]
    if prof:
        lines += [
            f"Ritmo normal: **{number(prof.get('normal_weekly_bultos'))} bultos/semana**",
            f"Supermercados: **{number(prof.get('super_weekly_bultos'))} bultos/semana**",
            f"Salida total de referencia: **{number(prof.get('weekly_bultos'))} bultos/semana**",
            "",
        ]
    for r in rows:
        lines.append(
            f"- Vence **{date_arg(r.get('fecha_vencimiento'))}** · stock lote **{number(r.get('stock_lote'))}** · "
            f"salida estimada **{number(r.get('venta_estimada_hasta_vto'))}** · riesgo **{number(r.get('bultos_riesgo'))}** · "
            f"**{r.get('estado_predictivo','—')}**"
        )
    cutoff = rows[0].get("super_hasta_fecha") if rows else None
    if cutoff:
        lines += ["", f"Supermercados se proyecta hasta **{date_arg(cutoff)}**; después, sólo venta normal."]
    return "\n".join(lines), ["Frescura Predictiva v9 · snapshot local"]


def _risk_answer(scope, state=None):
    rows = frescura_source.risk_list(scope, state=state, limit=15)
    if not rows:
        label = state or "riesgo"
        return f"No encontré SKU con **{label}** en **{scope_label(scope)}**.", ["Frescura Predictiva v9 · snapshot local"]
    heading = "CRÍTICOS" if state == "CRITICO" else "A ACCIONAR" if state == "ACCIONAR" else "CON RIESGO"
    lines = [f"**{heading} · {scope_label(scope)}**", ""]
    seen = set()
    for r in rows:
        key = (r.get("codigo"), r.get("fecha_vencimiento"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"- **{r.get('codigo')}** · {r.get('descripcion','')} · vence {date_arg(r.get('fecha_vencimiento'))} · "
            f"riesgo **{number(r.get('bultos_riesgo'))} bultos** · {r.get('estado_predictivo','—')}"
        )
    return "\n".join(lines), ["Frescura Predictiva v9 · snapshot local"]



def _topic_followup(raw: str, text: str, ctx: dict[str, Any], scope: str):
    """Continúa tema + cambia/retiene entidad cuando la frase no trae un intent nuevo."""
    topic = ctx.get("active_topic")
    if not topic:
        return None

    if topic == "discount":
        segment = ctx.get("last_discount_segment")
        answer, sources = _discount_answer(raw, ctx, segment)
        ctx["last_intent"] = f"discount:{segment or 'ALL'}"
        return answer, sources, ctx

    if topic == "tope":
        segment = ctx.get("last_tope_segment")
        answer, sources = _tope_answer(raw, ctx, segment)
        ctx["last_intent"] = f"tope:{segment or 'ALL'}"
        return answer, sources, ctx

    if topic == "edf_count":
        answer, sources = _edf_count_answer(raw, ctx)
        ctx["last_intent"] = "edf_count"
        return answer, sources, ctx

    if topic == "edf_location":
        answer, sources = _edf_location_answer(raw, ctx)
        ctx["last_intent"] = "edf_location"
        return answer, sources, ctx

    if topic == "repago":
        mode = ctx.get("last_repago_mode") or "trimestre"
        answer, sources = _repago_answer(raw, ctx, mode)
        ctx["last_intent"] = "repago"
        return answer, sources, ctx

    if topic == "repago_count":
        op = ctx.get("last_repago_op")
        threshold = ctx.get("last_repago_threshold")
        mode = ctx.get("last_repago_mode") or "trimestre"
        answer, sources = _repago_count_answer(raw, ctx, op, threshold, mode)
        ctx["last_intent"] = "repago_count"
        return answer, sources, ctx

    if topic == "monthly_sales":
        answer, sources = _monthly_sales_answer(raw, ctx)
        ctx["last_intent"] = "monthly_sales"
        return answer, sources, ctx

    if topic == "frescura":
        product, err = _resolve_frescura_product(raw, ctx)
        if err:
            return err, [], ctx
        _set_sku_context(ctx, product, scope)
        answer, sources = _frescura_answer(product, scope)
        ctx["last_intent"] = "frescura"
        return answer, sources, ctx

    if topic == "stock":
        product, err = _resolve_frescura_product(raw, ctx)
        if err:
            return err, [], ctx
        answer, sources = _ask_stock_scope(product, ctx)
        ctx["last_intent"] = "stock_wait_scope"
        return answer, sources, ctx

    return None


def respond(message: str, context: dict[str, Any] | None = None):
    """Responde manteniendo por separado entidad, tema y período."""
    ctx = _copy_context(context)
    raw = str(message or "").strip()
    text = normalize(raw)
    if not raw:
        return "Escribí una consulta.", [], ctx

    if any(k in text for k in ("ayuda", "que podes", "ejemplos")):
        return (
            "Puedo responder **EDF/heladeras**, **ubicación por número de serie**, **Repago por trimestre o último mes**, "
            "**ventas mensuales**, **stock/Frescura**, **descuentos** y **topes de bultos Core/Value**. "
            "Si una pregunta no es clara, te voy a pedir precisión en vez de adivinar.",
            [], ctx,
        )

    explicit_scope = scope_from(text)
    scope = explicit_scope or ctx.get("active_scope") or "DDV"
    code = extract_code(text)

    asset_words = any(k in text for k in ("heladera", "heladeras", "edf", "equipo", "equipos"))
    count_words = any(k in text for k in ("cuant", "cantidad", "tiene", "tienen", "hay"))
    repago_words = any(k in text for k in ("repago", "repaga", "repagan", "repag"))
    monthly_sales_words = (
        any(k in text for k in ("compra", "compras", "vende", "venta", "ventas", "hl", "hectolit"))
        and any(k in text for k in ("mes", "mensual", "compra", "vende", "hl", "hectolit"))
        and not repago_words
    )

    # 0) Respuesta a la repregunta de localidad para stock.
    if ctx.get("pending_stock_scope") and explicit_scope and not code:
        sku = str(ctx.get("pending_stock_sku") or ctx.get("active_sku") or "")
        product = frescura_source.product(sku) if sku else None
        if product:
            ctx["pending_stock_scope"] = False
            ctx["pending_stock_sku"] = None
            _set_sku_context(ctx, product, explicit_scope)
            ctx.update({"active_topic": "stock", "last_intent": "stock"})
            answer, sources = _stock_answer(product, explicit_scope)
            return answer, sources, ctx

    # Si el usuario abandona la repregunta y hace otra consulta, no dejamos un stock pendiente viejo.
    if ctx.get("pending_stock_scope"):
        ctx["pending_stock_scope"] = False
        ctx["pending_stock_sku"] = None

    # 1) Ubicación de EDF por serie completa o parcial.
    serial_candidate = _serial_from_query(raw)
    location_words = any(k in text for k in ("donde", "ubicacion", "ubicado", "ubicada", "colocado", "colocada"))
    serial_words = any(k in text for k in ("serie", "serial", "numero de serie", "nro serie", "nro de serie"))
    serial_followup = ctx.get("active_topic") == "edf_location" and bool(serial_candidate)
    serial_intent = serial_words or (location_words and asset_words and bool(serial_candidate)) or serial_followup
    if serial_intent:
        answer, sources = _edf_location_answer(raw, ctx)
        ctx.update({"active_topic": "edf_location", "last_intent": "edf_location"})
        return answer, sources, ctx

    # 2) Cambio de período dentro de un hilo de Repago.
    period_change = any(k in text for k in ("ultimo mes", "mes corriente", "mes actual", "este mes", "trimestre", "promedio"))
    if period_change and ctx.get("active_topic") in {"repago", "repago_count"} and ctx.get("active_client_id"):
        mode = _repago_mode_from_text(raw, ctx.get("last_repago_mode") or "trimestre")
        if ctx.get("active_topic") == "repago_count":
            answer, sources = _repago_count_answer(
                raw, ctx,
                ctx.get("last_repago_op"),
                ctx.get("last_repago_threshold"),
                mode,
            )
        else:
            answer, sources = _repago_answer(raw, ctx, mode)
        ctx["last_repago_mode"] = mode
        ctx["last_intent"] = ctx.get("active_topic")
        return answer, sources, ctx

    # 3) Repago cuantitativo explícito.
    if repago_words and count_words:
        op, threshold = _repago_threshold(raw)
        mode = _repago_mode_from_text(raw, ctx.get("last_repago_mode") or "trimestre")
        answer, sources = _repago_count_answer(raw, ctx, op, threshold, mode)
        ctx.update({
            "active_topic": "repago_count", "last_intent": "repago_count",
            "last_repago_op": op, "last_repago_threshold": threshold,
            "last_repago_mode": mode,
        })
        return answer, sources, ctx

    # 4) Conteo de EDF explícito.
    if asset_words and count_words and not repago_words:
        answer, sources = _edf_count_answer(raw, ctx)
        ctx.update({"active_topic": "edf_count", "last_intent": "edf_count"})
        return answer, sources, ctx

    # 5) Repago explícito.
    if repago_words:
        mode = _repago_mode_from_text(raw, ctx.get("last_repago_mode") or "trimestre")
        answer, sources = _repago_answer(raw, ctx, mode)
        ctx.update({"active_topic": "repago", "last_intent": "repago", "last_repago_mode": mode})
        return answer, sources, ctx

    # 6) Compra/venta mensual del cliente.
    if monthly_sales_words:
        answer, sources = _monthly_sales_answer(raw, ctx)
        ctx.update({"active_topic": "monthly_sales", "last_intent": "monthly_sales"})
        return answer, sources, ctx

    # 7) Topes de bultos Core/Value. Tiene prioridad sobre descuento.
    tope_intent = (
        "tope" in text
        or ("bulto" in text and any(k in text for k in ("core", "value")))
        or (ctx.get("active_topic") == "tope" and _tope_segment(text) is not None and "descuento" not in text)
    )
    if tope_intent:
        segment = _tope_segment(text)
        if segment is None and ctx.get("active_topic") == "tope":
            segment = ctx.get("last_tope_segment")
        answer, sources = _tope_answer(raw, ctx, segment)
        ctx.update({
            "active_topic": "tope",
            "last_tope_segment": segment,
            "last_intent": f"tope:{segment or 'ALL'}",
        })
        return answer, sources, ctx

    # 8) Descuentos explícitos.
    discount_intent = (
        "descuento" in text or "porcentaje" in text or "core" in text or "value" in text
        or ("litro" in text and ctx.get("active_client_id"))
        or ("lata" in text and ctx.get("active_client_id"))
    )
    if discount_intent:
        requested_segment = _discount_segment(text)
        if requested_segment is None and ctx.get("active_topic") == "discount":
            requested_segment = ctx.get("last_discount_segment")
        answer, sources = _discount_answer(raw, ctx, requested_segment)
        ctx.update({
            "active_topic": "discount",
            "last_discount_segment": requested_segment,
            "last_intent": f"discount:{requested_segment or 'ALL'}",
        })
        return answer, sources, ctx

    # 9) Stock: si no dice localidad, SIEMPRE se pregunta antes de responder.
    if "stock" in text:
        product, err = _resolve_frescura_product(raw, ctx)
        if err:
            return err, [], ctx
        if explicit_scope is None:
            answer, sources = _ask_stock_scope(product, ctx)
            ctx["last_intent"] = "stock_wait_scope"
            return answer, sources, ctx
        ctx["pending_stock_scope"] = False
        ctx["pending_stock_sku"] = None
        _set_sku_context(ctx, product, explicit_scope)
        answer, sources = _stock_answer(product, explicit_scope)
        ctx.update({"active_topic": "stock", "last_intent": "stock"})
        return answer, sources, ctx

    # Si veníamos hablando de stock y sólo cambia la localidad, mantenemos el SKU y respondemos stock.
    if ctx.get("active_topic") == "stock" and explicit_scope and ctx.get("active_sku"):
        product = frescura_source.product(str(ctx.get("active_sku")))
        if product:
            _set_sku_context(ctx, product, explicit_scope)
            answer, sources = _stock_answer(product, explicit_scope)
            ctx.update({"active_topic": "stock", "last_intent": "stock"})
            return answer, sources, ctx

    # 10) Frescura explícita.
    if any(k in text for k in ("critico", "criticos")) and not code:
        answer, sources = _risk_answer(scope, state="CRITICO")
        ctx.update({"active_scope": scope, "active_topic": "frescura", "last_intent": "frescura_criticos"})
        return answer, sources, ctx
    if any(k in text for k in ("accionar", "accion")) and not code:
        answer, sources = _risk_answer(scope, state="ACCIONAR")
        ctx.update({"active_scope": scope, "active_topic": "frescura", "last_intent": "frescura_accionar"})
        return answer, sources, ctx

    frescura_words = any(k in text for k in ("riesgo", "frescura", "venc", "lote"))
    frescura_followup = (
        ctx.get("active_topic") == "frescura"
        and explicit_scope is not None
        and ctx.get("active_sku") is not None
        and not ctx.get("pending_stock_scope")
    )
    if frescura_words or frescura_followup:
        product, err = _resolve_frescura_product(raw, ctx)
        if err:
            return err, [], ctx
        _set_sku_context(ctx, product, scope)
        answer, sources = _frescura_answer(product, scope)
        ctx.update({"active_topic": "frescura", "last_intent": "frescura"})
        return answer, sources, ctx

    # 11) Seguimiento corto de umbral.
    if ctx.get("active_client_id") and count_words and re.search(r"\b\d{1,3}\s*%", text):
        op, threshold = _repago_threshold(raw)
        mode = ctx.get("last_repago_mode") or "trimestre"
        answer, sources = _repago_count_answer(raw, ctx, op, threshold, mode)
        ctx.update({
            "active_topic": "repago_count", "last_intent": "repago_count",
            "last_repago_op": op, "last_repago_threshold": threshold,
        })
        return answer, sources, ctx

    # 12) Seguimiento de tema con una entidad nueva válida.
    if ctx.get("active_topic") and _valid_followup_entity(raw, ctx):
        result = _topic_followup(raw, text, ctx, scope)
        if result is not None:
            return result

    # 13) Código suelto sin tema previo: no adivina.
    if code:
        customer_match = repago_source.customer(code) or grupos_source.customer(code)
        product_match = frescura_source.product(code)
        serial_matches = repago_source.search_edf_serial(code, limit=3)
        found = bool(customer_match or product_match or serial_matches)
        if found:
            options = []
            if customer_match:
                cname = customer_match.get("name") or customer_match.get("legal_name") or ""
                options.append(f"cliente **{code} — {cname}**")
            if product_match:
                options.append(f"SKU **{code} — {product_match.get('descripcion','')}**")
            if serial_matches:
                options.append(f"{len(serial_matches)} coincidencia(s) de serie EDF")
            return (
                "Identifiqué " + ", ".join(options) + ", pero **no sé qué querés consultar**. "
                "Indicame si querés ver EDF, repago, topes, descuentos, ubicación por serie, stock o frescura.",
                [], ctx,
            )

    return (
        "**No entendí qué querés consultar.** No voy a asumir una respuesta. "
        "Podés preguntarme, por ejemplo: `cuántas heladeras tiene CLIENTE`, `repago CLIENTE`, "
        "`repago CLIENTE último mes`, `dónde está el EDF 565964`, `tope CORE de CLIENTE`, "
        "`descuento CORE de CLIENTE`, `stock SKU` o `frescura SKU`.",
        [], ctx,
    )

def refresh_all():
    result = {}
    for label, fn in (
        ("Repagos / EDF", repago_source.refresh),
        ("Frescura", frescura_source.refresh),
        ("Grupo de clientes", grupos_source.refresh),
    ):
        try:
            fn(force=True)
            result[label] = "OK"
        except Exception as exc:
            result[label] = f"{type(exc).__name__}: {exc}"
    return result


def statuses():
    return [repago_source.status(), frescura_source.status(), grupos_source.status()]
