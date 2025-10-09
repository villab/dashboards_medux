import streamlit as st
import pandas as pd
import plotly.express as px
import requests
import json
from io import StringIO
from datetime import datetime, timedelta, time
import pytz
from streamlit_autorefresh import st_autorefresh
import time

# ===========================================================
# 🧠 CONFIGURACIÓN INICIAL
# ===========================================================
st.set_page_config(page_title="Medux RAW Dashboard", layout="wide")
st.title("📊 Dashboard de Datos RAW – Medux API")

# ===========================================================
# 🔹 Sección de autenticación y configuración
# ===========================================================
st.sidebar.header("🔐 Configuración API")

token_input = st.sidebar.text_input("Token Bearer", type="password")
token_file = st.sidebar.file_uploader("O subir archivo de token (.txt)", type=["txt"])
if token_file is not None:
    token = token_file.read().decode().strip()
elif token_input:
    token = token_input.strip()
else:
    st.warning("⚠️ Ingresa o sube un token válido para continuar.")
    st.stop()

# CSV de probes
st.sidebar.markdown("---")
probes_file = st.sidebar.file_uploader("📄 Subir CSV de probes", type=["csv"])
if probes_file is not None:
    df_probes = pd.read_csv(probes_file)
    probes = df_probes["probes_id"].dropna().astype(str).tolist()
else:
    st.warning("⚠️ Sube un archivo CSV con la columna `probes_id`.")
    st.stop()

# ===========================================================
# 🔹 Parámetros de consulta
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Parámetros de consulta")

programas = st.sidebar.multiselect(
    "Selecciona los programas",
    [
        "http-upload-burst-test",
        "http-down-burst-test",
        "ping-test",
        "network",
        "voice-out",
        "cloud-download",
        "cloud-upload"
    ],
    default=["ping-test"]
)

# ===============================
# 📅 Selector de fecha y hora local
# ===============================
zona_local = pytz.timezone("America/Bogota")
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

fecha_inicio_defecto = inicio_defecto_local.date()
hora_inicio_defecto = time(inicio_defecto_local.hour, inicio_defecto_local.minute)
fecha_fin_defecto = ahora_local.date()
hora_fin_defecto = time(ahora_local.hour, ahora_local.minute)

st.sidebar.markdown("---")
st.sidebar.header("📅 Rango de fechas y horas (hora local)")

for key, default in [("fecha_inicio", fecha_inicio_defecto), ("hora_inicio", hora_inicio_defecto),
                     ("fecha_fin", fecha_fin_defecto), ("hora_fin", hora_fin_defecto)]:
    if key not in st.session_state:
        st.session_state[key] = default

fecha_inicio = st.sidebar.date_input("Fecha de inicio", st.session_state["fecha_inicio"])
hora_inicio = st.sidebar.time_input("Hora de inicio", st.session_state["hora_inicio"])
fecha_fin = st.sidebar.date_input("Fecha de fin", st.session_state["fecha_fin"])
hora_fin = st.sidebar.time_input("Hora de fin", st.session_state["hora_fin"])

st.session_state["fecha_inicio"] = fecha_inicio
st.session_state["hora_inicio"] = hora_inicio
st.session_state["fecha_fin"] = fecha_fin
st.session_state["hora_fin"] = hora_fin

# ===========================================================
# 🔄 Real-time y refresco automático
# ===========================================================
st_autorefresh(interval=30_000, key="real_time_refresh")
usar_real_time = st.sidebar.checkbox("⏱️ Modo real-time (últimos 30 min)", value=True)

if usar_real_time:
    ahora_local = datetime.now(zona_local)
    ts_end = int(ahora_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_start = int((ahora_local - timedelta(minutes=30)).astimezone(pytz.utc).timestamp() * 1000)
    st.sidebar.caption(f"Real-time activado → Últimos 30 min ({ahora_local.strftime('%H:%M:%S')})")
else:
    dt_inicio_local = zona_local.localize(datetime.combin_
