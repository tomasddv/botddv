"""Daily net bultos, aligned with Planificacion's Cantidades Totales report."""
from datetime import datetime
import pandas as pd
from . import planificacion_parsers as p
from .health import AR


def load_daily(sales_path, auxiliary_path):
    raw = p.read_tabular(sales_path)
    raw.columns = p.make_unique_columns(list(raw.columns))
    if 'cantidades_totales' not in raw.columns:
        raise ValueError("Falta Cantidades Totales; no se puede usar Bultos Promedio ni otra unidad.")
    dates = p.parse_period_date(p.first_present(raw, ['descripcion_periodo'], 'C'))
    if dates.isna().all():
        dates = p.parse_period_date(p.first_present(raw, ['periodos'], 'A'))
    customers = pd.to_numeric(p.first_present(raw, ['cod_cliente'], 'E'), errors='coerce')
    amounts = p.parse_argentine_number(raw['cantidades_totales'])
    if ((dates.isna() | amounts.isna()) & customers.notna()).any():
        raise ValueError("Hay ventas con cliente pero sin fecha o cantidad válida; no se publica un acumulado parcial.")
    brand = p.first_present(raw, ['descripcion_3'], 'U').map(p.key_text)
    business = p.first_present(raw, ['descripcion_8'], 'AJ').fillna('').astype(str)
    aux = pd.read_excel(auxiliary_path, sheet_name='PIVOT', dtype='string')
    if not {'MARCA', 'SEGMENTO'}.issubset(aux.columns):
        raise ValueError("AUXILIARES no contiene MARCA y SEGMENTO.")
    beer = aux[['MARCA','SEGMENTO']].dropna(subset=['MARCA']).copy()
    beer['key'] = beer['MARCA'].map(p.key_text)
    beer = beer.drop_duplicates('key')
    mapping = dict(zip(beer['key'], beer['SEGMENTO'].map(p.normalize_beer_segment)))
    action = brand.map(mapping).map({'CVZA CORE':'CORE','CVZA VALUE':'VALUE'})
    value = brand.str.contains('QUILMES 1890|1890', regex=True, na=False)
    core = brand.str.contains('QUILMES|BRAHMA|BUDWEISER', regex=True, na=False) & ~value
    action.loc[core] = 'CORE'
    action.loc[value] = 'VALUE'
    cza = business.str.contains('CZA|CERVEZ',case=False,na=False) & ~business.str.contains('UNG',case=False,na=False)
    data = pd.DataFrame({'date':dates.dt.strftime('%Y-%m-%d'),
                         'client':customers.astype('Int64').astype('string'),
                         'action':action, 'bultos':amounts})
    valid_dates = dates.dropna()
    if valid_dates.empty:
        raise ValueError("El archivo de ventas no tiene fechas válidas.")
    data = data.loc[cza & action.notna() & customers.notna()].copy()
    # Preserve signed quantities: returns/credit notes reduce net purchased bultos.
    daily = data.groupby(['client','action','date'],as_index=False)['bultos'].sum()
    return {'daily':daily.to_dict('records'), 'dates':sorted(valid_dates.dt.strftime('%Y-%m-%d').unique().tolist()),
            'source_file':sales_path.name, 'quantity_column':'Cantidades Totales', 'source_rows':len(raw)}


def purchases(sales, cid, action, base, extension=None, today=None):
    if not sales:
        return None
    today = today or datetime.now(AR).date().isoformat()
    month = today[:7]
    dates = [d for d in sales['dates'] if d[:7] == month and d <= today]
    if not dates:
        return None  # No current-month evidence is not zero sales.
    cutoff = max(dates)
    rows = [r for r in sales['daily'] if r['client'] == cid and r['action'] == action and r['date'][:7] == month and r['date'] <= today]
    bought = sum(r['bultos'] for r in rows)
    result = {'bought':bought, 'base_remaining':base-bought,
              'period_start':month+'-01', 'cutoff':cutoff, 'source_latest':max(sales['dates']),
              'future_excluded':any(d>today for d in sales['dates']), 'second_bought':None, 'second_remaining':None}
    if extension and extension.get('active') and extension.get('date'):
        second = sum(r['bultos'] for r in rows if r['date'] >= extension['date'])
        result.update(second_bought=second, second_remaining=base-second)
    return result
