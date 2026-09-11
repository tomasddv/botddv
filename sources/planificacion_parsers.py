"""Pure ERP parsing helpers from Planificacion app.py, revision ce5200e.

Kept local so questions and refreshes do not execute remote dashboard code.
"""
from __future__ import annotations
import io
import re
import unicodedata
from pathlib import Path
import pandas as pd

def excel_col_to_index(letter: str) -> int:
    value = 0
    for char in letter.upper():
        value = value * 26 + (ord(char) - ord('A') + 1)
    return value - 1

def strip_accents(value: str) -> str:
    return ''.join((char for char in unicodedata.normalize('NFKD', str(value)) if not unicodedata.combining(char)))

def clean_name(value: str) -> str:
    value = strip_accents(value).strip().lower()
    value = re.sub('[^a-z0-9]+', '_', value)
    return re.sub('_+', '_', value).strip('_')

def make_unique_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for column in columns:
        base = clean_name(column) or 'columna'
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f'{base}_{seen[base]}')
    return result

def parse_argentine_number(series: pd.Series) -> pd.Series:
    text = series.astype('string').str.strip().str.replace('%', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.replace('[^0-9.\\-]', '', regex=True)
    return pd.to_numeric(text, errors='coerce')

def parse_period_date(raw: pd.Series) -> pd.Series:
    month_map = {'ene': 'jan', 'feb': 'feb', 'mar': 'mar', 'abr': 'apr', 'may': 'may', 'jun': 'jun', 'jul': 'jul', 'ago': 'aug', 'sep': 'sep', 'set': 'sep', 'oct': 'oct', 'nov': 'nov', 'dic': 'dec'}
    text = raw.astype('string').str.strip().str.lower()
    text = text.str.replace('^\\(\\d+\\)\\s*', '', regex=True)
    for spanish, english in month_map.items():
        text = text.str.replace(f'\\b{spanish}\\b', english, regex=True)
    parsed = pd.Series(pd.NaT, index=raw.index, dtype='datetime64[ns]')
    for date_format in ('%d-%b-%y', '%d-%b-%Y', '%d %b %y', '%d %b %Y', '%d/%m/%y', '%d/%m/%Y'):
        missing = parsed.isna()
        if not missing.any():
            break
        parsed.loc[missing] = pd.to_datetime(text.loc[missing], format=date_format, errors='coerce')
    if parsed.isna().any():
        parsed.loc[parsed.isna()] = pd.to_datetime(text.loc[parsed.isna()], errors='coerce', dayfirst=True)
    return parsed.dt.normalize()

def col_by_position(df: pd.DataFrame, letter: str) -> pd.Series:
    index = excel_col_to_index(letter)
    if index >= len(df.columns):
        return pd.Series(pd.NA, index=df.index, dtype='object')
    return df.iloc[:, index]

def first_present(df: pd.DataFrame, names: list[str], fallback_letter: str | None=None) -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name]
    if fallback_letter:
        return col_by_position(df, fallback_letter)
    return pd.Series(pd.NA, index=df.index, dtype='object')

def read_tabular(source: str | Path | io.BytesIO) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ('utf-8-sig', 'cp1252', 'latin1'):
        try:
            if hasattr(source, 'seek'):
                source.seek(0)
            return pd.read_csv(source, sep='\t', dtype='string', encoding=encoding, engine='c')
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last_error = exc
    raise RuntimeError(f'No pude detectar la codificacion del archivo: {last_error}')

def key_text(value: str | None) -> str:
    return strip_accents('' if value is None or pd.isna(value) else str(value)).upper().strip()

def normalize_beer_segment(value: str | None) -> str:
    text = key_text(value)
    if text == 'CORE PLUS':
        return 'CVZA CORE +'
    if text == 'VALUE':
        return 'CVZA VALUE'
    if text == 'HE':
        return 'CVZA HE'
    if text == 'CORE':
        return 'CVZA CORE'
    return 'CVZA SIN SEGMENTO'
