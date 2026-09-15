from __future__ import annotations

import os
import time
import streamlit as st

from assistant_engine import respond, statuses
from analyst_engine import analyst_status
from auto_refresh import (
    ensure_initial_ready,
    force_refresh_async,
    interval_minutes,
    start_background_updater,
    state as auto_state,
)

st.set_page_config(page_title="Asistente DDV · Analista Comercial", page_icon="⚡", layout="wide")

st.markdown("""
<style>
.stApp { background:#07101f; color:#f8fafc; }
.block-container { max-width:1120px; padding-top:1.25rem; padding-bottom:5rem; }
.hero { padding:20px 24px; border:1px solid #22314a; border-radius:16px; background:linear-gradient(180deg,#111d30,#0a1423); margin-bottom:14px; }
.eyebrow { color:#22d3ee; font-size:.75rem; letter-spacing:.14em; font-weight:900; }
.hero h1 { margin:.28rem 0 .35rem; font-size:2rem; }
.hero p { color:#94a3b8; margin:0; }
.source-card { border:1px solid #22314a; border-radius:13px; padding:12px 14px; background:#0d1728; min-height:106px; }
.source-ok { color:#4ade80; font-weight:900; }
.source-idle { color:#facc15; font-weight:900; }
.source-bad { color:#fb7185; font-weight:900; }
.small-muted { color:#94a3b8; font-size:.80rem; }
.fast-note { border:1px solid #155eef; background:#0c1d3a; padding:10px 13px; border-radius:11px; color:#cbd5e1; margin:.4rem 0 1rem; }
.context-bar { border:1px solid #24334e; background:#0d1728; border-radius:11px; padding:9px 12px; margin:.4rem 0 1rem; color:#cbd5e1; }
div[data-testid="stChatMessage"] { border:1px solid #17243a; border-radius:15px; background:#0b1423; }
.stButton > button { border-radius:11px; font-weight:850; }
</style>
""", unsafe_allow_html=True)

password = os.getenv("BOT_ACCESS_PASSWORD", "").strip()
if password:
    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False
    if not st.session_state.auth_ok:
        st.title("Asistente DDV")
        entered = st.text_input("Clave", type="password")
        if st.button("Ingresar", type="primary"):
            if entered == password:
                st.session_state.auth_ok = True
                st.rerun()
            st.error("Clave incorrecta.")
        st.stop()

missing_before = [s for s in statuses() if s.get("ok") is not True]
if missing_before:
    with st.spinner("Preparando automáticamente las fuentes que faltan..."):
        initial_results = ensure_initial_ready()
    failed_initial = {k: v for k, v in initial_results.items() if v != "OK"}
    if failed_initial:
        st.warning("Alguna fuente no pudo actualizarse todavía. El asistente seguirá usando las fuentes disponibles y volverá a intentarlo automáticamente.")
else:
    ensure_initial_ready()

start_background_updater()

st.markdown(f"""
<div class="hero">
  <div class="eyebrow">ASISTENTE DDV · RÁPIDO + ANALISTA</div>
  <h1>Asistente Comercial DDV</h1>
  <p>Consultas rápidas desde snapshots locales y análisis libre para comparar, cruzar y calcular sobre tus datos.</p>
</div>
<div class="fast-note"><b>⚡ Respuesta rápida:</b> una pregunta nunca espera Drive/GitHub ni recalcula Frescura. La actualización corre sola en segundo plano.</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.subheader("Actualización automática")
    auto = auto_state()
    if auto.get("running"):
        st.success("🔄 Sincronizando en segundo plano")
    else:
        st.success("✅ Automática activa")
    st.caption(f"Cada {auto.get('interval_minutes', interval_minutes())} minutos")
    if auto.get("last_finished"):
        st.caption(f"Última sincronización: {auto.get('last_finished')}")
    if auto.get("next_run"):
        st.caption(f"Próxima: {auto.get('next_run')}")

    st.markdown("---")
    st.subheader("Analista DDV")
    astatus = analyst_status()
    st.success(f"🧠 Activo · {astatus.get('model')}")
    st.caption("Analiza comparaciones, rankings y cruces localmente. No usa API y no tiene costo por consulta.")

    st.markdown("---")
    st.subheader("Contexto del chat")
    ctx = st.session_state.get("assistant_context", {})
    if ctx.get("active_client_id"):
        st.write(f"Cliente activo: **{ctx.get('active_client_id')} · {ctx.get('active_client_name') or ''}**")
    else:
        st.caption("Sin cliente activo")
    if ctx.get("active_sku"):
        st.write(f"SKU activo: **{ctx.get('active_sku')} · {ctx.get('active_sku_name') or ''}**")
    else:
        st.caption("Sin SKU activo")

    topic = ctx.get("active_topic")
    topic_labels = {
        "discount": "Descuentos",
        "edf_count": "Cantidad de EDF",
        "repago": "Repago mensual",
        "repago_count": "Conteo por repago",
        "monthly_sales": "Compra mensual",
        "frescura": "Frescura",
        "analyst": "Análisis libre",
        "tope": "Tope de bultos",
        "edf_location": "Ubicación EDF",
    }
    if topic:
        extra = ""
        if topic == "discount" and ctx.get("last_discount_segment"):
            extra = f" · {ctx.get('last_discount_segment')}"
        elif topic == "repago_count" and ctx.get("last_repago_threshold") is not None:
            extra = f" · {ctx.get('last_repago_op') or ''}{ctx.get('last_repago_threshold'):g}%"
        st.write(f"Tema activo: **{topic_labels.get(topic, topic)}{extra}**")
    else:
        st.caption("Sin tema activo")

    if st.button("🧹 Nueva conversación", use_container_width=True):
        st.session_state.messages = []
        st.session_state.assistant_context = {}
        st.rerun()

    with st.expander("Mantenimiento (opcional)"):
        st.caption("No hace falta tocar nada para usar el bot. Esto queda sólo como respaldo.")
        if st.button("Forzar actualización ahora", use_container_width=True):
            started = force_refresh_async()
            if started:
                st.success("Actualización iniciada en segundo plano.")
            else:
                st.info("Ya hay una actualización en curso.")

source_status = statuses()
cols = st.columns(3)
for col, item in zip(cols, source_status):
    with col:
        state = item.get("ok")
        if state is True:
            css, label = "source-ok", "● LISTO"
        elif state is None:
            css, label = "source-idle", "● PREPARANDO"
        else:
            css, label = "source-bad", "● ERROR"
        st.markdown(
            f'<div class="source-card"><div class="{css}">{label}</div><b>{item.get("name")}</b><br>'
            f'<span class="small-muted">{item.get("detail","")}</span><br>'
            f'<span class="small-muted">Snapshot: {item.get("loaded_at","—")}</span></div>',
            unsafe_allow_html=True,
        )

if "assistant_context" not in st.session_state:
    st.session_state.assistant_context = {}

ctx = st.session_state.assistant_context
context_parts = []
if ctx.get("active_client_id"):
    context_parts.append(f"Cliente: <b>{ctx.get('active_client_id')} · {ctx.get('active_client_name') or ''}</b>")
if ctx.get("active_sku"):
    context_parts.append(f"SKU: <b>{ctx.get('active_sku')} · {ctx.get('active_sku_name') or ''}</b>")
if ctx.get("active_topic"):
    label = {
        "discount":"Descuentos", "edf_count":"Cantidad EDF", "repago":"Repago mensual",
        "repago_count":"Conteo repago", "monthly_sales":"Compra mensual", "frescura":"Frescura", "tope":"Tope de bultos", "edf_location":"Ubicación EDF", "analyst":"Análisis libre",
    }.get(ctx.get("active_topic"), ctx.get("active_topic"))
    if ctx.get("active_topic") == "discount" and ctx.get("last_discount_segment"):
        label += f" · {ctx.get('last_discount_segment')}"
    context_parts.append(f"Tema: <b>{label}</b>")
if context_parts:
    st.markdown('<div class="context-bar">🧠 Contexto activo · ' + ' &nbsp; | &nbsp; '.join(context_parts) + '</div>', unsafe_allow_html=True)

if "messages" not in st.session_state or not st.session_state.messages:
    st.session_state.messages = [{
        "role":"assistant",
        "content":(
            "👋 **¡Hola! Bienvenido al Asistente Comercial DDV.**  \n\n"
            "Podés preguntarme directamente lo que necesites sobre **stock, frescura, EDF, repago, ventas mensuales, descuentos y topes**. "
            "También puedo **comparar bases, hacer rankings, cruzar fuentes y calcular relaciones** aunque la pregunta no esté programada de antemano.  \n\n"
            "Voy a mantener el contexto de la conversación. **¿Qué querés analizar?**"
        ),
    }]

st.markdown("### Chat")
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("elapsed") is not None:
            st.caption(f"Respuesta local: {msg['elapsed']:.3f} s")
        if msg.get("sources"):
            st.caption("Fuente: " + " · ".join(msg["sources"]))

prompt = st.chat_input("Escribí tu consulta...")

if prompt:
    st.session_state.messages.append({"role":"user","content":prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    t0 = time.perf_counter()
    try:
        answer, source_names, new_context = respond(prompt, st.session_state.assistant_context)
        st.session_state.assistant_context = new_context
    except Exception as exc:
        answer, source_names = f"Error de consulta local: **{type(exc).__name__}** — {exc}", []
    elapsed = time.perf_counter() - t0
    with st.chat_message("assistant"):
        st.markdown(answer)
        st.caption(f"Respuesta local: {elapsed:.3f} s")
        if source_names:
            st.caption("Fuente: " + " · ".join(source_names))
    st.session_state.messages.append({"role":"assistant","content":answer,"sources":source_names,"elapsed":elapsed})
    st.rerun()
