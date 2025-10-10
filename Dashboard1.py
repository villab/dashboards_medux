import streamlit as st
import pandas as pd
import plotly.express as px
import requests
import json
from io import StringIO
from datetime import datetime, timedelta, time
import pytz
from streamlit_autorefresh import st_autorefresh

# ===========================================================
# 🧠 CONFIGURACIÓN INICIAL
# ===========================================================
st.set_page_config(page_title="Medux RAW Dashboard", layout="wide")
st.title("📊 Dashboard de Datos RAW – Medux API")

# ===========================================================
# 🔹 Sección de autenticación y configuración
# ===========================================================
st.sidebar.header("🔐 Configuración API")

# Token: puedes pegarlo o cargarlo desde archivo
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
    probes = df_probes["probes_id"].dropna().tolist()
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
    ["http-upload-burst-test", "http-down-burst-test", "ping-test","network","voice-out","cloud-download","cloud-upload"],
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

# Inicializar session_state
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
    dt_inicio_local = zona_local.localize(datetime.combine(fecha_inicio, hora_inicio))
    dt_fin_local = zona_local.localize(datetime.combine(fecha_fin, hora_fin))
    if dt_inicio_local >= dt_fin_local:
        st.error("⚠️ La fecha/hora de inicio no puede ser posterior o igual a la de fin.")
        st.stop()
    ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)

# Mostrar resumen en el sidebar
st.sidebar.markdown("### 🕒 Rango seleccionado")
st.sidebar.write(f"Inicio local: {datetime.fromtimestamp(ts_start/1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.write(f"Fin local: {datetime.fromtimestamp(ts_end/1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')}")

# ===========================================================
# 🔹 Llamada a la API (con paginación automática)
# ===========================================================
url = "https://medux-ids.caseonit.com/api/results"
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
body = {
    "tsStart": ts_start,
    "tsEnd": ts_end,
    "format": "raw",
    "programs": programas,
    "probes": [str(p) for p in probes if pd.notna(p)],
}


# ===========================================================
# 🔹 Función para obtener datos con paginación
# ===========================================================
@st.cache_data(ttl=1800)
def obtener_datos_pag(url, headers, body):
    """
    Descarga datos paginados desde la API de Medux con manejo flexible de formatos.
    Compatible con 'results' tipo dict o list.
    """
    todos_los_resultados = {}
    pagina = 1
    total_registros = 0

    while True:
        st.info(f"📡 Descargando página {pagina}...")

        response = requests.post(url, headers=headers, json=body)
        if response.status_code != 200:
            st.error(f"❌ Error API (página {pagina}): {response.status_code}")
            break

        data = response.json()
        resultados = data.get("results", {})

        # 🔹 Caso 1: results es un diccionario {programa: [registros]}
        if isinstance(resultados, dict):
            for program, results in resultados.items():
                if program not in todos_los_resultados:
                    todos_los_resultados[program] = []
                todos_los_resultados[program].extend(results)
                total_registros += len(results)

        # 🔹 Caso 2: results es una lista directa
        elif isinstance(resultados, list):
            if "general" not in todos_los_resultados:
                todos_los_resultados["general"] = []
            todos_los_resultados["general"].extend(resultados)
            total_registros += len(resultados)

        else:
            st.warning(f"⚠️ Formato de 'results' desconocido en página {pagina}")
            break

        st.success(f"📄 Página {pagina} descargada... ({total_registros:,} registros acumulados)")

        # 🔹 Revisar si hay más páginas
        next_data = data.get("next_pagination_data")

        # 🚫 Si no hay siguiente página, terminamos
        if not next_data or not any(next_data.values()):
            st.info(f"✅ Descarga completada. Total: {total_registros:,} registros en {pagina} página(s).")
            break

        # 🔹 Preparar la siguiente llamada
        body["pagination_data"] = next_data
        pagina += 1

    return todos_los_resultados


# ===========================================================
# 🔹 Función para aplanar resultados en DataFrame
# ===========================================================
def flatten_results(raw_json):
    rows = []
    for program, results in raw_json.items():
        if not isinstance(results, list) or len(results) == 0:
            continue
        for item in results:
            flat = {"program": program}
            if isinstance(item, dict):
                flat.update(item)
            rows.append(flat)
    return pd.DataFrame(rows)


# ===========================================================
# 🔹 Lógica principal de ejecución
# ===========================================================
if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()

if st.sidebar.button("🚀 Consultar API") or usar_real_time:
    data = obtener_datos_pag(url, headers, body)
    if not data:
        st.stop()

    df = flatten_results(data)
    if df.empty:
        st.warning("⚠️ No se recibieron datos de la API.")
        st.stop()

    st.session_state.df = df
    st.success(f"✅ Datos cargados correctamente ({len(df)} filas en total).")
else:
    df = st.session_state.df

# ===========================================================
# 🌍 Mapas de mediciones por ISP
# ===========================================================
st.markdown("## 🗺️ Mapas por ISP")

if "df" in st.session_state and not st.session_state.df.empty:
    df_plot = st.session_state.df.copy()

    # Verificar columnas de coordenadas y 'isp'
    if all(col in df_plot.columns for col in ["latitude", "longitude", "isp"]):
        df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
        df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
        df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])

        if not df_plot.empty:
            for isp in df_plot["isp"].unique():
                df_isp = df_plot[df_plot["isp"] == isp]

                if df_isp.empty:
                    continue

                # Centrar en la última medición del ISP
                ultimo_punto = df_isp.iloc[-1]
                centro_lat = ultimo_punto["latitude"]
                centro_lon = ultimo_punto["longitude"]

                # Zoom automático basado en dispersión
                lat_range = df_isp["latitude"].max() - df_isp["latitude"].min()
                lon_range = df_isp["longitude"].max() - df_isp["longitude"].min()

                if lat_range < 0.1 and lon_range < 0.1:
                    zoom_auto = 15
                elif lat_range < 1 and lon_range < 1:
                    zoom_auto = 14
                elif lat_range < 5 and lon_range < 5:
                    zoom_auto = 12
                else:
                    zoom_auto = 10

                zoom_user = st.sidebar.slider(f"Zoom para {isp}", 3, 15, int(zoom_auto))

                hover_cols = [c for c in ["latitude", "longitude", "city", "provider", "subtechnology", "avgLatency", "program"] if c in df_isp.columns]

                fig = px.scatter_map(
                    df_isp,
                    lat="latitude",
                    lon="longitude",
                    color="isp",  # puedes cambiar por 'subtechnology' si quieres
                    hover_name="isp",
                    hover_data=hover_cols,
                    color_discrete_sequence=px.colors.qualitative.Bold,
                    height=500,
                    labels={"program": "Tipo de prueba"},
                )
                
                fig.update_layout(
                    map={
                        "style": "carto-positron",  # equivalente a mapbox_style
                        "center": {"lat": centro_lat, "lon": centro_lon},
                        "zoom": zoom_user,
                    },
                    margin={"r": 0, "t": 0, "l": 0, "b": 0},
                    legend_title_text="Programas Medux",
                )
                
                st.subheader(f"ISP: {isp}")
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"Última medición ISP {isp}: ({centro_lat:.4f}, {centro_lon:.4f}) | Zoom: {zoom_user}")

                
    else:
        st.warning("⚠️ El dataset no contiene 'latitude', 'longitude' o 'isp'.")
else:
    st.info("👈 Consulta primero la API para visualizar los mapas.")















