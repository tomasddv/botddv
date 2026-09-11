from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re
import unicodedata

import numpy as np
import pandas as pd


DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]
DAY_COLS = ["lun", "mar", "mie", "jue", "vie", "sab"]
SPANISH_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

# Parámetros acordados para Frescura Predictiva v11 (misma lógica de cálculo v9).
HIST_MONTH_WEIGHTS = (0.20, 0.30, 0.50)  # mes -3, -2, -1
HIST_BLEND_WEIGHT = 0.70
CURRENT_BLEND_WEIGHT = 0.30
FORECAST_HORIZON_DAYS = 120
MAX_EXPIRY_BUCKETS = 2
SUPERMARKET_FORECAST_MONTHS = 2  # ventana operativa aproximada de 60 días
WEEKS_PER_MONTH = 52.0 / 12.0


def _clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _code(series: pd.Series) -> pd.Series:
    return (
        series.fillna("")
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
        .str.lstrip("0")
    )


def _num(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str).str.strip()
    comma_mask = text.str.contains(",", regex=False)
    out = pd.Series(np.nan, index=text.index, dtype=float)

    if comma_mask.any():
        out.loc[comma_mask] = pd.to_numeric(
            text.loc[comma_mask]
            .str.replace(".", "", regex=False)
            .str.replace(",", ".", regex=False),
            errors="coerce",
        )
    if (~comma_mask).any():
        out.loc[~comma_mask] = pd.to_numeric(
            text.loc[~comma_mask].str.replace(" ", "", regex=False),
            errors="coerce",
        )
    return out


def parse_spanish_dates(series: pd.Series) -> pd.Series:
    x = series.fillna("").astype(str).str.lower().str.strip()
    parts = x.str.extract(r"^\s*(\d{1,2})-([a-záéíóúñ]{3})-(\d{2,4})\s*$")
    day = pd.to_numeric(parts[0], errors="coerce")
    month = parts[1].map(SPANISH_MONTHS)
    year = pd.to_numeric(parts[2], errors="coerce")
    year = np.where(year < 100, 2000 + year, year)
    return pd.to_datetime(
        pd.DataFrame({"year": year, "month": month, "day": day}),
        errors="coerce",
    )


def load_history_base(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    frame = pd.read_csv(path, compression="infer", dtype={"loc": str, "sku": str})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["sku"] = _code(frame["sku"])
    frame["loc"] = frame["loc"].fillna("").astype(str).str.upper().str.strip()
    frame["bultos"] = pd.to_numeric(frame["bultos"], errors="coerce").fillna(0.0)
    return (
        frame[frame["date"].notna() & frame["loc"].isin(["TRELEW", "MADRYN"])]
        .groupby(["date", "loc", "sku"], as_index=False)["bultos"]
        .sum()
    )


def load_customer_location_map(customer_xlsx: str | Path) -> dict[str, str]:
    df = pd.read_excel(customer_xlsx, header=1, dtype=str)
    if not df.empty:
        df = df.iloc[1:].copy()

    if "Cliente" not in df.columns or "Descripción Agrupación" not in df.columns:
        raise ValueError("Plantilla clientes sin columnas Cliente / Descripción Agrupación.")

    df["cliente_norm"] = _code(df["Cliente"])
    agr = df["Descripción Agrupación"].fillna("").astype(str).str.upper()
    df["loc"] = np.where(
        agr.str.contains("MADRYN", na=False),
        "MADRYN",
        np.where(agr.str.contains("TRELEW", na=False), "TRELEW", None),
    )
    return dict(zip(df["cliente_norm"], df["loc"]))


def load_current_bultos(
    sales_txt: str | Path,
    customer_xlsx: str | Path,
    wanted_skus: set[str] | None = None,
) -> pd.DataFrame:
    """Lee ventadiaria bultos.txt; Cantidades Totales ya está expresada en bultos."""
    customer_map = load_customer_location_map(customer_xlsx)
    pieces = []
    wanted = {str(x).lstrip("0") for x in wanted_skus} if wanted_skus else None
    cols = ["Descripción Período", "Cod. Cliente", "Código", "Cantidades Totales"]

    for chunk in pd.read_csv(
        sales_txt,
        sep="\t",
        encoding="latin1",
        usecols=cols,
        dtype=str,
        chunksize=120_000,
        low_memory=False,
    ):
        chunk["sku"] = _code(chunk["Código"])
        if wanted is not None:
            chunk = chunk[chunk["sku"].isin(wanted)].copy()
        if chunk.empty:
            continue

        chunk["date"] = parse_spanish_dates(chunk["Descripción Período"])
        chunk["client"] = _code(chunk["Cod. Cliente"])
        chunk["loc"] = chunk["client"].map(customer_map)
        chunk["bultos"] = _num(chunk["Cantidades Totales"]).fillna(0.0)
        chunk = chunk[
            chunk["date"].notna() & chunk["loc"].isin(["TRELEW", "MADRYN"])
        ].copy()
        if not chunk.empty:
            pieces.append(
                chunk.groupby(["date", "loc", "sku"], as_index=False)["bultos"].sum()
            )

    if not pieces:
        return pd.DataFrame(columns=["date", "loc", "sku", "bultos"])

    return (
        pd.concat(pieces, ignore_index=True)
        .groupby(["date", "loc", "sku"], as_index=False)["bultos"]
        .sum()
        .sort_values(["date", "loc", "sku"])
    )


def combine_history(history: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """El archivo más nuevo manda sobre claves fecha/base/SKU solapadas."""
    if history.empty:
        return current.copy()
    if current.empty:
        return history.copy()

    current_keys = current[["date", "loc", "sku"]].drop_duplicates()
    hist = history.merge(
        current_keys.assign(_current=1),
        on=["date", "loc", "sku"],
        how="left",
    )
    hist = hist[hist["_current"].isna()].drop(columns="_current")
    return (
        pd.concat([hist, current], ignore_index=True)
        .groupby(["date", "loc", "sku"], as_index=False)["bultos"]
        .sum()
        .sort_values(["date", "loc", "sku"])
    )


def _normalise_products(products: pd.DataFrame) -> pd.DataFrame:
    frame = products.copy()
    frame["loc"] = frame["ciudad"].fillna("").astype(str).str.upper().str.strip()
    frame["sku"] = _code(frame["codigo"])
    frame["venta_promedio"] = pd.to_numeric(
        frame.get("venta_promedio", 0), errors="coerce"
    ).fillna(0.0)
    frame["stock_total"] = pd.to_numeric(
        frame.get("stock_total", 0), errors="coerce"
    ).fillna(0.0)
    frame["politica_stock_dias"] = pd.to_numeric(
        frame.get("politica_stock_dias", 0), errors="coerce"
    ).fillna(0.0)
    return frame


def _products_ddv(products: pd.DataFrame) -> pd.DataFrame:
    """Un SKU se trata como un único stock DDV: Trelew + Madryn."""
    frame = _normalise_products(products)
    rows = []
    for sku, grp in frame.groupby("sku", sort=False):
        desc = grp["descripcion"].dropna().astype(str)
        rows.append({
            "loc": "DDV",
            "sku": sku,
            "description": desc.iloc[0] if not desc.empty else sku,
            "stock_total": float(grp["stock_total"].sum()),
            "venta_promedio": float(grp["venta_promedio"].sum()),
            "current_policy_days": float(grp["politica_stock_dias"].max()),
        })
    return pd.DataFrame(rows)


def _products_by_base(products: pd.DataFrame) -> pd.DataFrame:
    """Mantiene cada SKU separado por base física: Trelew / Madryn."""
    frame = _normalise_products(products)
    rows = []
    for (loc, sku), grp in frame.groupby(["loc", "sku"], sort=False):
        if loc not in {"TRELEW", "MADRYN"}:
            continue
        desc = grp["descripcion"].dropna().astype(str)
        rows.append({
            "loc": loc,
            "sku": sku,
            "description": desc.iloc[0] if not desc.empty else sku,
            "stock_total": float(grp["stock_total"].sum()),
            "venta_promedio": float(grp["venta_promedio"].sum()),
            "current_policy_days": float(grp["politica_stock_dias"].max()),
        })
    return pd.DataFrame(rows)


def _commercial_days(start: date, end: date) -> int:
    """Días operativos Lun-Sáb, inclusive."""
    if end < start:
        return 0
    return sum(1 for d in pd.date_range(start, end, freq="D") if d.weekday() < 6)


def _month_bounds(period: pd.Period) -> tuple[date, date]:
    return period.start_time.date(), period.end_time.date()


def _source_monthly_profile(
    daily: pd.DataFrame,
    sku: str,
    as_of: date,
    fallback_daily: float = 0.0,
    loc: str | None = None,
) -> dict:
    """
    Motor de ritmo v9 (vigente en v11):
    - 3 meses completos previos ponderados 20% / 30% / 50%.
    - Mes actual proyectado por ritmo transcurrido.
    - Blend final 70% histórico + 30% mes actual.
    - El volumen final se distribuye por el patrón histórico de día de semana.
    - Si `loc` es None suma Trelew + Madryn; si se informa, calcula sólo esa base.
    """
    if daily.empty:
        sku_daily = pd.DataFrame({
            "date": pd.Series(dtype="datetime64[ns]"),
            "bultos": pd.Series(dtype="float64"),
        })
    else:
        x = daily.copy()
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        x["sku"] = _code(x["sku"])
        x["bultos"] = pd.to_numeric(x["bultos"], errors="coerce").fillna(0.0)
        mask = (x["sku"] == str(sku).lstrip("0")) & (x["date"].dt.date <= as_of)
        if loc is not None:
            x["loc"] = x["loc"].fillna("").astype(str).str.upper().str.strip()
            mask &= x["loc"].eq(str(loc).upper().strip())
        sku_daily = x.loc[mask].groupby("date", as_index=False)["bultos"].sum()

    as_period = pd.Period(as_of, freq="M")
    periods = [as_period - 3, as_period - 2, as_period - 1]
    weights = np.array(HIST_MONTH_WEIGHTS, dtype=float)

    first_positive = None
    if not sku_daily.empty:
        pos = sku_daily[sku_daily["bultos"] > 0]
        if not pos.empty:
            first_positive = pos["date"].min().date()

    month_totals = []
    month_days = []
    eligible = []
    for period in periods:
        m_start, m_end = _month_bounds(period)
        m = sku_daily[
            (sku_daily["date"].dt.date >= m_start)
            & (sku_daily["date"].dt.date <= m_end)
        ].copy()
        total = float(m["bultos"].sum()) if not m.empty else 0.0
        by_day = np.zeros(6, dtype=float)
        if not m.empty:
            m["weekday"] = m["date"].dt.weekday
            for wd in range(6):
                by_day[wd] = float(m.loc[m["weekday"] == wd, "bultos"].sum())
        month_totals.append(total)
        month_days.append(by_day)
        # Para SKU nuevos no castigamos meses anteriores al alta.
        eligible.append(first_positive is None or m_end >= first_positive)

    eligible = np.array(eligible, dtype=bool)
    hist_available = bool(eligible.any()) and first_positive is not None
    if hist_available:
        w = weights * eligible.astype(float)
        if w.sum() > 0:
            w = w / w.sum()
        hist_monthly = float(np.dot(w, np.array(month_totals, dtype=float)))
        hist_day_amounts = np.sum(np.array(month_days) * w[:, None], axis=0)
    else:
        hist_monthly = 0.0
        hist_day_amounts = np.zeros(6, dtype=float)

    month_start = as_period.start_time.date()
    month_end = as_period.end_time.date()
    # Si el corte es hoy y hoy es Lun-Sáb, evitamos usar un día comercial incompleto
    # para calcular el ritmo del mes. El stock sí sigue siendo el snapshot actual.
    local_today = pd.Timestamp.now(tz="America/Argentina/Buenos_Aires").date()
    pace_as_of = (
        as_of - timedelta(days=1)
        if as_of == local_today and as_of.weekday() < 6
        else as_of
    )
    current = sku_daily[
        (sku_daily["date"].dt.date >= month_start)
        & (sku_daily["date"].dt.date <= pace_as_of)
    ].copy()
    current_total = float(current["bultos"].sum()) if not current.empty else 0.0
    elapsed_days = _commercial_days(month_start, pace_as_of)
    full_month_days = _commercial_days(month_start, month_end)
    current_monthly = (
        current_total / elapsed_days * full_month_days if elapsed_days > 0 else 0.0
    )

    if hist_available:
        final_monthly = (
            HIST_BLEND_WEIGHT * hist_monthly
            + CURRENT_BLEND_WEIGHT * current_monthly
        )
    elif elapsed_days > 0 and current_total > 0:
        final_monthly = current_monthly
    else:
        # Vta. prom. se usa sólo como red de seguridad para SKU sin historia.
        final_monthly = max(float(fallback_daily), 0.0) * full_month_days

    if hist_day_amounts.sum() > 1e-9:
        shares = hist_day_amounts / hist_day_amounts.sum()
    elif not current.empty and current_total > 1e-9:
        current = current.copy()
        current["weekday"] = current["date"].dt.weekday
        day_amounts = np.array([
            float(current.loc[current["weekday"] == wd, "bultos"].sum())
            for wd in range(6)
        ])
        shares = day_amounts / day_amounts.sum() if day_amounts.sum() > 0 else np.ones(6) / 6
    else:
        shares = np.ones(6) / 6

    weekly = max(float(final_monthly), 0.0) / WEEKS_PER_MONTH
    per_day = weekly * shares

    return {
        **{DAY_COLS[i]: float(max(per_day[i], 0.0)) for i in range(6)},
        "weekly_bultos": float(max(weekly, 0.0)),
        "monthly_bultos": float(max(final_monthly, 0.0)),
        "historical_monthly_bultos": float(max(hist_monthly, 0.0)),
        "current_month_projected_bultos": float(max(current_monthly, 0.0)),
        "current_month_actual_bultos": float(current_total),
        "elapsed_commercial_days": int(elapsed_days),
        "history_start": first_positive,
    }


def estimate_depletion_date(
    stock: float,
    profile: dict,
    as_of: date,
    max_days: int = 730,
) -> date | None:
    remaining = max(float(stock), 0.0)
    if remaining <= 1e-9:
        return as_of
    d = as_of + timedelta(days=1)
    for _ in range(max_days):
        if d.weekday() < 6:
            remaining -= max(float(profile.get(DAY_COLS[d.weekday()], 0.0)), 0.0)
            if remaining <= 1e-9:
                return d
        d += timedelta(days=1)
    return None


def _index_daily_groups(daily: pd.DataFrame, scope: str) -> dict:
    """
    Prepara una sola vez el histórico para evitar recorrer/copiar toda la tabla
    por cada SKU. No modifica ninguna fórmula; sólo acelera el cálculo.
    """
    if daily.empty:
        return {}
    x = daily[["date", "loc", "sku", "bultos"]].copy()
    x["date"] = pd.to_datetime(x["date"], errors="coerce")
    x["sku"] = _code(x["sku"])
    x["loc"] = x["loc"].fillna("").astype(str).str.upper().str.strip()
    x["bultos"] = pd.to_numeric(x["bultos"], errors="coerce").fillna(0.0)
    x = x[x["date"].notna() & x["loc"].isin(["TRELEW", "MADRYN"])].copy()
    if x.empty:
        return {}
    if scope == "DDV":
        return {str(k): g for k, g in x.groupby("sku", sort=False)}
    return {(str(loc), str(sku)): g for (loc, sku), g in x.groupby(["loc", "sku"], sort=False)}


def build_weekday_profiles(
    daily: pd.DataFrame,
    products: pd.DataFrame,
    as_of: date,
    recent_occurrences: int = 2,
    scope: str = "DDV",
) -> pd.DataFrame:
    """Perfil de venta normal con el cálculo v9, unificado o separado por base."""
    del recent_occurrences
    scope = str(scope).upper().strip()
    scoped_products = _products_ddv(products) if scope == "DDV" else _products_by_base(products)
    grouped_daily = _index_daily_groups(daily, scope)
    empty_daily = pd.DataFrame(columns=["date", "loc", "sku", "bultos"])
    rows = []
    for row in scoped_products.to_dict("records"):
        if scope == "DDV":
            daily_slice = grouped_daily.get(str(row["sku"]), empty_daily)
        else:
            daily_slice = grouped_daily.get((str(row["loc"]), str(row["sku"])), empty_daily)
        p = _source_monthly_profile(
            daily=daily_slice,
            sku=row["sku"],
            as_of=as_of,
            fallback_daily=row.get("venta_promedio", 0.0),
            loc=None,
        )
        depletion = estimate_depletion_date(row["stock_total"], p, as_of)
        age_weeks = (
            (as_of - p["history_start"]).days / 7.0 if p.get("history_start") else 0.0
        )
        data_confidence = "ALTA" if age_weeks >= 8 else "MEDIA" if age_weeks >= 4 else "BAJA"
        rows.append({
            "loc": row["loc"],
            "sku": row["sku"],
            "description": row["description"],
            "history_start": p.get("history_start"),
            "age_weeks": age_weeks,
            "confidence": data_confidence,
            "weekly_bultos": p["weekly_bultos"],
            "monthly_bultos": p["monthly_bultos"],
            "historical_monthly_bultos": p["historical_monthly_bultos"],
            "current_month_projected_bultos": p["current_month_projected_bultos"],
            "current_month_actual_bultos": p["current_month_actual_bultos"],
            "avg_sale_day": p["weekly_bultos"] / 6.0,
            "stock_total": row["stock_total"],
            "current_policy_days": row["current_policy_days"],
            "sku_depletion_date": depletion,
            "dynamic_coverage_days": (
                (depletion - as_of).days if depletion is not None else np.nan
            ),
            **{c: p[c] for c in DAY_COLS},
        })
    return pd.DataFrame(rows)


def load_supermarket_dispatches(
    report_xlsx: str | Path,
    wanted_skus: set[str] | None = None,
) -> pd.DataFrame:
    """
    Lee ReporteComprobantesDetallado de Cuenta y Orden.
    - Bultos Total positivos = salida.
    - Devoluciones negativas reducen la salida neta.
    - Anulados se excluyen.
    - Se excluye solamente el CD de La Anónima: cliente 999 / Ruta 25 Parque Industrial.
    """
    usecols = [
        "Fecha Comprobante", "Cliente", "Razon Social", "Domicilio",
        "Descripcion Agrupacion", "Codigo de Articulo", "Bultos Total", "Anulado",
    ]
    df = pd.read_excel(report_xlsx, sheet_name="Datos", usecols=usecols)

    def norm_code(value):
        if pd.isna(value):
            return ""
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text.lstrip("0") or ("0" if text else "")

    df["sku"] = df["Codigo de Articulo"].map(norm_code)
    df["client"] = df["Cliente"].map(norm_code)
    df["date"] = pd.to_datetime(df["Fecha Comprobante"], errors="coerce")
    df["bultos"] = pd.to_numeric(df["Bultos Total"], errors="coerce").fillna(0.0)

    agr = df["Descripcion Agrupacion"].fillna("").astype(str).str.upper()
    df["loc"] = np.where(
        agr.str.contains("MADRYN", na=False),
        "MADRYN",
        np.where(agr.str.contains("TRELEW", na=False), "TRELEW", None),
    )

    reason = df["Razon Social"].fillna("").astype(str).str.upper().str.strip()
    address = df["Domicilio"].fillna("").astype(str).str.upper()
    excluded_cd = (
        reason.eq("S.A. IMPORTADORA Y EXPORTADORA DE LA PATAGONIA")
        & (
            df["client"].eq("999")
            | (
                address.str.contains("RUTA 25", na=False)
                & address.str.contains("PQUE", na=False)
                & address.str.contains("INDUSTRIAL", na=False)
            )
        )
    )

    valid = (
        df["Anulado"].fillna("").astype(str).str.upper().ne("SI")
        & ~excluded_cd
        & df["loc"].isin(["TRELEW", "MADRYN"])
        & df["date"].notna()
    )
    if wanted_skus:
        wanted = {str(x).lstrip("0") for x in wanted_skus}
        valid &= df["sku"].isin(wanted)

    out = df.loc[valid, ["date", "loc", "sku", "bultos"]].copy()
    if out.empty:
        return pd.DataFrame(columns=["date", "loc", "sku", "bultos"])
    return (
        out.groupby(["date", "loc", "sku"], as_index=False)["bultos"]
        .sum()
        .sort_values(["date", "loc", "sku"])
    )


def build_supermarket_weekday_profiles(
    daily_super: pd.DataFrame,
    products: pd.DataFrame,
    as_of: date,
    recent_occurrences: int = 8,
    scope: str = "DDV",
) -> pd.DataFrame:
    """Perfil de supermercados con cálculo v9, unificado o separado por base."""
    del recent_occurrences
    scope = str(scope).upper().strip()
    scoped_products = _products_ddv(products) if scope == "DDV" else _products_by_base(products)
    grouped_daily = _index_daily_groups(daily_super, scope)
    empty_daily = pd.DataFrame(columns=["date", "loc", "sku", "bultos"])
    rows = []
    for row in scoped_products.to_dict("records"):
        if scope == "DDV":
            daily_slice = grouped_daily.get(str(row["sku"]), empty_daily)
        else:
            daily_slice = grouped_daily.get((str(row["loc"]), str(row["sku"])), empty_daily)
        p = _source_monthly_profile(
            daily=daily_slice,
            sku=row["sku"],
            as_of=as_of,
            fallback_daily=0.0,
            loc=None,
        )
        rows.append({
            "loc": row["loc"],
            "sku": row["sku"],
            "super_weekly_bultos": p["weekly_bultos"],
            "super_monthly_bultos": p["monthly_bultos"],
            "super_historical_monthly_bultos": p["historical_monthly_bultos"],
            "super_current_month_projected_bultos": p["current_month_projected_bultos"],
            "super_current_month_actual_bultos": p["current_month_actual_bultos"],
            **{f"super_{c}": p[c] for c in DAY_COLS},
        })
    return pd.DataFrame(rows)


def combine_normal_and_supermarket_profiles(
    normal_profiles: pd.DataFrame,
    supermarket_profiles: pd.DataFrame,
    as_of: date,
) -> pd.DataFrame:
    """Suma ambas fuentes para consumo físico manteniendo la clave base/SKU."""
    if normal_profiles.empty:
        return normal_profiles.copy()

    if supermarket_profiles.empty:
        merged = normal_profiles.copy()
        for c in DAY_COLS:
            merged[f"super_{c}"] = 0.0
        merged["super_weekly_bultos"] = 0.0
        merged["super_monthly_bultos"] = 0.0
    else:
        merged = normal_profiles.merge(
            supermarket_profiles,
            on=["loc", "sku"],
            how="left",
        )

    for col in DAY_COLS:
        merged[f"normal_{col}"] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)
        merged[f"super_{col}"] = pd.to_numeric(
            merged.get(f"super_{col}", 0.0), errors="coerce"
        ).fillna(0.0)
        merged[col] = merged[f"normal_{col}"] + merged[f"super_{col}"]

    merged["normal_weekly_bultos"] = merged[[f"normal_{c}" for c in DAY_COLS]].sum(axis=1)
    merged["super_weekly_bultos"] = merged[[f"super_{c}" for c in DAY_COLS]].sum(axis=1)
    merged["weekly_bultos"] = merged[DAY_COLS].sum(axis=1)
    merged["avg_sale_day"] = merged["weekly_bultos"] / 6.0

    merged["normal_monthly_bultos"] = pd.to_numeric(
        merged.get("monthly_bultos", 0.0), errors="coerce"
    ).fillna(0.0)
    merged["super_monthly_bultos"] = pd.to_numeric(
        merged.get("super_monthly_bultos", 0.0), errors="coerce"
    ).fillna(0.0)
    merged["total_monthly_bultos"] = (
        merged["normal_monthly_bultos"] + merged["super_monthly_bultos"]
    )

    depletion_dates = []
    coverage_days = []
    for rec in merged.to_dict("records"):
        depletion = estimate_depletion_date(
            stock=float(rec.get("stock_total", 0.0)),
            profile=rec,
            as_of=as_of,
        )
        depletion_dates.append(depletion)
        coverage_days.append((depletion - as_of).days if depletion is not None else np.nan)
    merged["sku_depletion_date"] = depletion_dates
    merged["dynamic_coverage_days"] = coverage_days
    return merged


def _horizon_confidence(days_to_expiry: int) -> str:
    if days_to_expiry <= 30:
        return "MUY ALTA"
    if days_to_expiry <= 60:
        return "ALTA"
    if days_to_expiry <= 90:
        return "MEDIA"
    return "BAJA"


def _consume_volume_date(volume: float, profile: dict, start: date, max_days: int = 730) -> date | None:
    """Fecha teórica para consumir un volumen, sin afectar la simulación FEFO real."""
    remaining = max(float(volume), 0.0)
    if remaining <= 1e-9:
        return start
    d = start
    for _ in range(max_days):
        if d.weekday() < 6:
            remaining -= max(float(profile.get(DAY_COLS[d.weekday()], 0.0)), 0.0)
            if remaining <= 1e-9:
                return d
        d += timedelta(days=1)
    return None


def simulate_fefo(
    lots: pd.DataFrame,
    profiles: pd.DataFrame,
    as_of: date,
    max_extra_days: int = 730,
    scope: str = "DDV",
) -> pd.DataFrame:
    """
    FEFO v10 con dos modos de visualización, conservando exactamente la lógica v9.

    Común a ambos modos:
    - 2 vencimientos más próximos dentro de 120 días.
    - Un lote vencido no absorbe demanda posterior.
    - Supermercados se proyecta sólo hasta +2 meses calendario desde `as_of`.
    - Después del corte, sólo venta normal.

    scope="DDV": Trelew + Madryn forman un único stock por SKU.
    scope="BASE": cada base se calcula y consume por separado.
    """
    if lots.empty or profiles.empty:
        return pd.DataFrame()

    scope = str(scope).upper().strip()
    if scope not in {"DDV", "BASE"}:
        raise ValueError("scope debe ser 'DDV' o 'BASE'.")

    lot_frame = lots.copy()
    lot_frame["loc"] = lot_frame["ciudad"].fillna("").astype(str).str.upper().str.strip()
    lot_frame["sku"] = _code(lot_frame["codigo"])
    lot_frame["stock_lote"] = pd.to_numeric(lot_frame["stock_lote"], errors="coerce").fillna(0.0)
    lot_frame["fecha_vencimiento"] = pd.to_datetime(lot_frame["fecha_vencimiento"], errors="coerce")
    lot_frame = lot_frame[
        (lot_frame["stock_lote"] > 0)
        & lot_frame["fecha_vencimiento"].notna()
        & lot_frame["loc"].isin(["TRELEW", "MADRYN"])
    ].copy()
    if lot_frame.empty:
        return pd.DataFrame()

    if scope == "DDV":
        pmap = {str(r["sku"]).lstrip("0"): r for r in profiles.to_dict("records")}
        group_iter = ((None, sku, grp) for sku, grp in lot_frame.groupby("sku", sort=False))
    else:
        pmap = {
            (str(r["loc"]).upper(), str(r["sku"]).lstrip("0")): r
            for r in profiles.to_dict("records")
        }
        group_iter = (
            (loc, sku, grp)
            for (loc, sku), grp in lot_frame.groupby(["loc", "sku"], sort=False)
        )

    horizon_end = as_of + timedelta(days=FORECAST_HORIZON_DAYS)
    result = []

    def _consume(items, demand, d, source):
        remaining_demand = max(float(demand), 0.0)
        if remaining_demand <= 1e-9:
            return 0.0
        for item in items:
            if item["remaining"] <= 1e-9 or d > item["expiry"]:
                continue
            taken = min(item["remaining"], remaining_demand)
            item["remaining"] -= taken
            item["sold_until_expiry"] += taken
            if source == "normal":
                item["sold_normal_until_expiry"] += taken
            else:
                item["sold_super_until_expiry"] += taken
            remaining_demand -= taken
            if item["remaining"] <= 1e-9 and item["depletion_date"] is None:
                item["depletion_date"] = d
            if remaining_demand <= 1e-9:
                break
        return remaining_demand

    for group_loc, sku, sku_lots in group_iter:
        if scope == "DDV":
            profile = pmap.get(str(sku).lstrip("0"))
        else:
            profile = pmap.get((str(group_loc).upper(), str(sku).lstrip("0")))
        if not profile:
            continue

        desc_series = sku_lots["descripcion"].dropna().astype(str)
        description = desc_series.iloc[0] if not desc_series.empty else str(sku)

        eligible = sku_lots[
            sku_lots["fecha_vencimiento"].dt.date <= horizon_end
        ].copy()
        if eligible.empty:
            continue

        expiry_dates = (
            eligible["fecha_vencimiento"].dt.normalize()
            .drop_duplicates()
            .sort_values()
            .head(MAX_EXPIRY_BUCKETS)
        )
        eligible = eligible[
            eligible["fecha_vencimiento"].dt.normalize().isin(expiry_dates)
        ].copy()

        items = []
        for idx, (expiry_ts, grp) in enumerate(
            eligible.groupby(eligible["fecha_vencimiento"].dt.normalize(), sort=True),
            start=1,
        ):
            expiry = pd.Timestamp(expiry_ts).date()
            stock = float(grp["stock_lote"].sum())
            location_parts = []
            for loc, lgrp in grp.groupby("loc"):
                location_parts.append(f"{loc.title()} {float(lgrp['stock_lote'].sum()):.1f}")
            location_text = " · ".join(location_parts)
            source_lots = ", ".join(
                sorted({str(x) for x in grp.get("lote_nro", pd.Series(dtype=object)).dropna().tolist()})
            )
            items.append({
                "expiry_order": idx,
                "expiry": expiry,
                "stock_lote": stock,
                "remaining": stock,
                "sold_until_expiry": 0.0,
                "sold_normal_until_expiry": 0.0,
                "sold_super_until_expiry": 0.0,
                "risk_at_expiry": None,
                "depletion_date": None,
                "ubicacion_stock": location_text,
                "lotes_origen": source_lots,
            })

        for item in items:
            if item["expiry"] < as_of:
                item["risk_at_expiry"] = item["remaining"]

        max_expiry = max(i["expiry"] for i in items)
        super_cutoff = (
            pd.Timestamp(as_of) + pd.DateOffset(months=SUPERMARKET_FORECAST_MONTHS)
        ).date()
        d = as_of + timedelta(days=1)
        while d <= max_expiry:
            if d.weekday() < 6:
                day_col = DAY_COLS[d.weekday()]
                normal_demand = max(
                    float(profile.get(f"normal_{day_col}", profile.get(day_col, 0.0))),
                    0.0,
                )
                _consume(items, normal_demand, d, "normal")

                if d <= super_cutoff:
                    super_demand = max(float(profile.get(f"super_{day_col}", 0.0)), 0.0)
                    _consume(items, super_demand, d, "super")

            for item in items:
                if item["risk_at_expiry"] is None and d == item["expiry"]:
                    item["risk_at_expiry"] = max(item["remaining"], 0.0)
            d += timedelta(days=1)

        for item in items:
            if item["risk_at_expiry"] is None:
                item["risk_at_expiry"] = max(item["remaining"], 0.0)

            stock = float(item["stock_lote"])
            sold = max(float(item["sold_until_expiry"]), 0.0)
            sold_normal = max(float(item["sold_normal_until_expiry"]), 0.0)
            sold_super = max(float(item["sold_super_until_expiry"]), 0.0)
            risk = max(float(item["risk_at_expiry"]), 0.0)
            risk_pct = risk / stock if stock > 0 else 0.0
            days_to_expiry = (item["expiry"] - as_of).days

            depletion = item["depletion_date"]
            if depletion is None and risk > 1e-9:
                normal_only_profile = {
                    c: float(profile.get(f"normal_{c}", profile.get(c, 0.0)))
                    for c in DAY_COLS
                }
                depletion = _consume_volume_date(
                    risk,
                    normal_only_profile,
                    item["expiry"] + timedelta(days=1),
                    max_days=max_extra_days,
                )
            margin = (
                (item["expiry"] - depletion).days if depletion is not None else np.nan
            )

            if risk <= 0.05:
                status = "OK"
            elif days_to_expiry <= 0:
                status = "CRITICO"
            elif risk_pct >= 0.50 or (days_to_expiry <= 30 and risk_pct >= 0.10):
                status = "CRITICO"
            else:
                status = "ACCIONAR"

            if risk <= 0.05:
                lift = 0.0
            elif sold_normal > 1e-9:
                lift = (risk / sold_normal) * 100.0
            else:
                lift = np.inf

            normal_weekly = max(
                float(profile.get("normal_weekly_bultos", profile.get("weekly_bultos", 0.0))),
                0.0,
            )
            normal_avg_day = normal_weekly / 6.0
            extra_days = risk / normal_avg_day if normal_avg_day > 1e-9 else np.inf

            city_value = "DDV" if scope == "DDV" else str(group_loc).title()
            result.append({
                "ciudad": city_value,
                "ubicacion_stock": item["ubicacion_stock"],
                "codigo": str(sku),
                "descripcion": description,
                "lote_nro": int(item["expiry_order"]),
                "lotes_origen": item["lotes_origen"],
                "stock_lote": stock,
                "fecha_vencimiento": pd.Timestamp(item["expiry"]),
                "dias_para_vencer": days_to_expiry,
                "venta_normal_estimada_hasta_vto": sold_normal,
                "super_estimado_hasta_vto": sold_super,
                "venta_estimada_hasta_vto": sold,
                "bultos_riesgo": risk,
                "riesgo_pct": risk_pct * 100.0,
                "agotamiento_estimado": pd.Timestamp(depletion) if depletion else pd.NaT,
                "margen_frescura_dias": margin,
                "incremento_necesario_pct": lift,
                "dias_venta_extra_equiv": extra_days,
                "confianza": _horizon_confidence(days_to_expiry),
                "confianza_datos": profile.get("confidence", "BAJA"),
                "venta_semanal_estimada": float(profile.get("weekly_bultos", 0.0)),
                "venta_normal_semanal_estimada": float(profile.get("normal_weekly_bultos", 0.0)),
                "super_semanal_estimada": float(profile.get("super_weekly_bultos", 0.0)),
                "venta_mensual_estimada": float(profile.get("total_monthly_bultos", 0.0)),
                "dias_stock_dinamicos": profile.get("dynamic_coverage_days", np.nan),
                "politica_actual_dias": profile.get("current_policy_days", np.nan),
                "estado_predictivo": status,
                "regla_super_60d": True,
                "super_hasta_fecha": pd.Timestamp(super_cutoff),
                "modo_calculo": scope,
                **{col: float(profile.get(col, 0.0)) for col in DAY_COLS},
            })

    return pd.DataFrame(result)


def forecast_summary(forecast: pd.DataFrame) -> dict:
    if forecast.empty:
        return {"risk_bultos": 0.0, "risk_lots": 0, "critical_lots": 0, "action_lots": 0}
    risk = pd.to_numeric(forecast["bultos_riesgo"], errors="coerce").fillna(0.0)
    states = forecast["estado_predictivo"].fillna("").astype(str)
    return {
        "risk_bultos": float(risk.sum()),
        "risk_lots": int((risk > 0.05).sum()),
        "critical_lots": int(states.eq("CRITICO").sum()),
        "action_lots": int(states.eq("ACCIONAR").sum()),
    }
