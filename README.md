# Asistente DDV

Consultas locales de EDF, repago, descuentos, stock, Frescura y topes Core/Value.

## Ejecutar

```sh
pip install -r requirements.txt
streamlit run app.py
```

`app_botddv_bienvenida.py` conserva la entrada alternativa ya existente. Ambas usan el mismo motor y conectores. En Streamlit Cloud mantener el archivo de entrada configurado.

## Consultas

- `tope CORE y VALUE de 3992`
- `y en VALUE?`
- `y el 275?`
- `stock 10810` → `Trelew`
- `frescura 10810 Madryn`
- `repago 3992 último mes`

Un código explícito inexistente nunca se sustituye por la entidad anterior. Una nueva intención explícita tiene prioridad sobre el seguimiento de localidad de stock.

## Fuentes y actualización

Las preguntas no acceden a red. Los JSON de `data/cache/` permiten arrancar sin esperar descargas. Un hilo coordina la actualización de cada fuente en paralelo cada 30 minutos; un fallo de una fuente no bloquea las demás. Las escrituras de snapshots se reemplazan atómicamente.

Se muestran por separado disponibilidad, fecha de copia/datos y fallos de actualización. Una copia antigua puede seguir respondiendo con aviso. Repagos exige esquema 3 para no presentar el cálculo anterior como trimestral.

### Topes de Planificación

`sources/planificacion_source.py` lee:

1. `TOPES_CANAL` y configuración de ampliaciones del archivo `tomasddv/planificacion/dashboard_bultos_accion.py` mediante AST y literales, sin ejecutar el dashboard.
2. El maestro `PlantillaClientesAR` de la carpeta Drive que usa Planificación. Cruza lista de precios y jerarquía comercial para clasificar el canal, con la misma prioridad del dashboard.
3. La hoja `BD_EXTENSION_TOPES` del libro de ampliaciones configurado en Planificación.

Core y Value son independientes. Con ampliación activa y fecha válida, Planificación habilita un segundo tramo igual al tope base. La última fila de cada cliente/acción prevalece, incluso para desactivar una ampliación. No se reinterpretan los campos primer/segundo_tope de la hoja: el dashboard calcula el segundo tramo desde el tope del canal.

La respuesta detalla base, segundo tramo, fecha, compras y saldo. Lee `ventadiaria bultos.txt` y `AUXILIARES.xlsx` de la misma carpeta. Usa exclusivamente `Cantidades Totales`, con signo para devoluciones, clasificación de CZA y las excepciones de marcas Core/Value del dashboard. Los parsers locales proceden de Planificación `ce5200e`.

Por defecto se muestra el acumulado del mes calendario actual hasta hoy (zona Argentina), con el último día de ventas disponible. No se cuentan fechas futuras ni se confunde fecha de descarga con fecha de ventas. Si el archivo no tiene datos del mes actual, se indica información no disponible, no cero.

Sin ampliación, el saldo es tope base menos compras netas. Con ampliación se muestra además comprado desde la fecha de ampliación (inclusive) y saldo del segundo tramo, igual que Planificación; no se mezclan los dos tramos. Los excedentes se muestran expresamente.

Si falla la hoja de ampliaciones se informa sólo el tope y saldo base, indicando que las ampliaciones no se pudieron verificar. Si fallan ventas o auxiliares no se informa comprado ni saldo. Si no hay reglas o canal verificable, no se inventa un límite a partir del grupo de descuentos.

La paridad se contrastó con Planificación `ce5200e562a371d320669e094c88a653c1669655`: 2.126 clientes y 3.668 combinaciones cliente/segmento sin discrepancias, usando los mismos archivos fuente. Revisar la integración cuando cambien clasificación o reglas del dashboard.

## Configuración opcional

- `AUTO_REFRESH_MINUTES`: intervalo de actualización; mínimo 5, predeterminado 30.
- `BOT_ACCESS_PASSWORD`: clave de acceso a la interfaz.
- `GITHUB_RAW_TOKEN`: acceso del importador remoto de Repagos.
- `PLANIFICACION_EXTENSIONS_SHEET_URL`: reemplaza la URL de la hoja de ampliaciones sólo si el despliegue de Planificación usa otra configuración.

No guardar credenciales en el repositorio. Archivos descargados e importadores temporales quedan excluidos por `.gitignore`.

## Validación

```sh
python -m unittest discover -s tests -v
```

Pruebas de conversaciones, selección de SKU, cambio de tema, topes separados, ampliaciones, fuentes ausentes, errores y actualización independiente. Los casos de conversación bloquean HTTP para asegurar que la respuesta sea local. Ambas entradas Streamlit también se verificaron con AppTest.
