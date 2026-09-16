BOT DDV v14.3 — Fix Latones 710 + parser comercial

Cambios:
- "Latones 710", "Latón 710", "calibre 710", "710 cc" y "latones" se interpretan como el foco/KPI Latones 710.
- Evita que "latones" se confunda con productos llamados "COMBO LATONES...".
- Permite combinar Latones 710 con otros filtros (Core, marca, promotor, etc.).
- El validador SQL ahora ignora palabras reservadas dentro de literales de datos. Un producto real que contenga la palabra DROP ya no bloquea una consulta segura.
- Versión visible: Analista DDV v14.3.

Actualizar en GitHub:
- Reemplazar únicamente analyst_engine.py
