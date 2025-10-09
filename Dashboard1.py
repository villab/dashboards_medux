import streamlit as st
import pandas as pd
import plotly.express as px
import requests
import json
from io import StringIO
from datetime import datetime, timedelta
import pytz

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
    ["http-upload-burst-test", "http-down-burst-test", "ping-test","network"],
    default=["ping-test"]
)
#------------------opciones de fecha------------------
from datetime import datetime, timedelta, time
import pytz
import streamlit as st

# ===============================
# 📅 Selector de fecha y hora local
# ===============================

zona_local = pytz.timezone("America/Bogota")

# Obtener hora local actual y rango por defecto (últimas 24h)
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

# Convertir defaults a tipos puros (sin tzinfo)
fecha_inicio_defecto = inicio_defecto_local.date()
hora_inicio_defecto = time(inicio_defecto_local.hour, inicio_defecto_local.minute)
fecha_fin_defecto = ahora_local.date()
hora_fin_defecto = time(ahora_local.hour, ahora_local.minute)

st.sidebar.markdown("---")
st.sidebar.header("📅 Rango de fechas y horas (hora local)")

# Inicializar valores si no existen en session_state
if "fecha_inicio" not in st.session_state:
    st.session_state["fecha_inicio"] = fecha_inicio_defecto
if "hora_inicio" not in st.session_state:
    st.session_state["hora_inicio"] = hora_inicio_defecto
if "fecha_fin" not in st.session_state:
    st.session_state["fecha_fin"] = fecha_fin_defecto
if "hora_fin" not in st.session_state:
    st.session_state["hora_fin"] = hora_fin_defecto

# Widgets (funcionan bien en Streamlit Cloud)
fecha_inicio = st.sidebar.date_input(
    "Fecha de inicio", st.session_state["fecha_inicio"], key="fecha_inicio_input"
)
hora_inicio = st.sidebar.time_input(
    "Hora de inicio", st.session_state["hora_inicio"], key="hora_inicio_input"
)
fecha_fin = st.sidebar.date_input(
    "Fecha de fin", st.session_state["fecha_fin"], key="fecha_fin_input"
)
hora_fin = st.sidebar.time_input(
    "Hora de fin", st.session_state["hora_fin"], key="hora_fin_input"
)

# Guardar cambios en session_state
st.session_state["fecha_inicio"] = fecha_inicio
st.session_state["hora_inicio"] = hora_inicio
st.session_state["fecha_fin"] = fecha_fin
st.session_state["hora_fin"] = hora_fin

# Combinar fecha + hora y convertir a UTC
dt_inicio_local = zona_local.localize(datetime.combine(fecha_inicio, hora_inicio))
dt_fin_local = zona_local.localize(datetime.combine(fecha_fin, hora_fin))

if dt_inicio_local >= dt_fin_local:
    st.error("⚠️ La fecha/hora de inicio no puede ser posterior o igual a la de fin.")
    st.stop()

# Convertir a UTC → timestamps en milisegundos
dt_inicio_utc = dt_inicio_local.astimezone(pytz.utc)
dt_fin_utc = dt_fin_local.astimezone(pytz.utc)
ts_start = int(dt_inicio_utc.timestamp() * 1000)
ts_end = int(dt_fin_utc.timestamp() * 1000)

# Mostrar resumen en el sidebar
st.sidebar.markdown("### 🕒 Rango seleccionado")
st.sidebar.write(f"Inicio local: {dt_inicio_local.strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.write(f"Fin local: {dt_fin_local.strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.caption(
    f"Convertido a UTC: {dt_inicio_utc.strftime('%Y-%m-%d %H:%M:%S')} → {dt_fin_utc.strftime('%Y-%m-%d %H:%M:%S')}"
)


url = "https://medux-ids.caseonit.com/api/results"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

body = {
    "tsStart": ts_start,
    "tsEnd": ts_end,
    "format": "raw",
    "programs": programas,
    "probes": [str(p) for p in probes if pd.notna(p)],
}

# ===========================================================
# 🔹 Llamada a la API
# ===========================================================
@st.cache_data(ttl=1800)
def obtener_datos(url, headers, body):
    """Llama la API y devuelve el JSON (se cachea por parámetros únicos)."""
    response = requests.post(url, headers=headers, json=body)

    if response.status_code == 200:

        return response.json()
    else:
        st.error(f"❌ Error API: {response.status_code}")
        return None
# ===========================================================
# 🔹 Lógica de ejecución principal
# ===========================================================
if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()

if st.sidebar.button("🚀 Consultar API"):

    data = obtener_datos(url, headers, body)
    if not data:
        st.stop()

    def flatten_results(raw_json):
        rows = []
        for program, results in raw_json.items():
            # Verifica que sea lista y tenga datos
            if not isinstance(results, list) or len(results) == 0:
                continue
            for item in results:  # 👈 sin [:10], trae todo
                flat = {"program": program}
                if isinstance(item, dict):
                    flat.update(item)
                rows.append(flat)
        return pd.DataFrame(rows)

    df = flatten_results(data)

    if df.empty:
        st.warning("No se recibieron datos de la API.")
        st.stop()

    # Guardar en la sesión para no perderlo
    st.session_state.df = df
    st.success("✅ Datos cargados correctamente. Ya puedes explorar los ejes y programas.")
else:
    df = st.session_state.df

# ===========================================================
# 🔹 Interfaz de gráfico dinámico
# ===========================================================
if not df.empty:
    st.sidebar.header("📈 Visualización")

    programa = st.sidebar.selectbox("Programa", sorted(df["program"].unique()))
    subset = df[df["program"] == programa]

    columnas_numericas = subset.select_dtypes(include="number").columns.tolist()
    columnas_todas = subset.columns.tolist()

    eje_x = st.sidebar.selectbox("Eje X", columnas_todas, index=0)
    eje_y = st.sidebar.selectbox("Eje Y", columnas_numericas, index=1 if len(columnas_numericas) > 1 else 0)
    tipo = st.sidebar.selectbox("Tipo de gráfico", ["scatter", "line", "bar"])

    # =======================================================
    # 🔹 Render gráfico
    # =======================================================
    if tipo == "scatter":
        fig = px.scatter(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")
    elif tipo == "line":
        fig = px.line(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")
    else:
        fig = px.bar(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")

    st.plotly_chart(fig, use_container_width=True)

    with st.expander("📄 Ver datos"):
        st.dataframe(subset)
else:
    st.info("👈 Configura y presiona **Consultar API** para ver los resultados.")

import plotly.express as px

# ===========================================================
# 🌍 Mapa de mediciones centrado en la última coordenada
# ===========================================================
st.markdown("## 🗺️ Mapa de mediciones")

if "df" in st.session_state and not st.session_state.df.empty:
    df_plot = st.session_state.df.copy()

    # Asegurarse de que existan columnas de coordenadas
    if "latitude" in df_plot.columns and "longitude" in df_plot.columns:
        df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
        df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
        df_plot = df_plot.dropna(subset=["latitude", "longitude"])

        if not df_plot.empty:
            # 📌 Centrar en la última coordenada
            ultimo_punto = df_plot.iloc[-1]
            centro_lat = ultimo_punto["latitude"]
            centro_lon = ultimo_punto["longitude"]

            # Calcular dispersión para zoom automático
            lat_range = df_plot["latitude"].max() - df_plot["latitude"].min()
            lon_range = df_plot["longitude"].max() - df_plot["longitude"].min()

            if lat_range < 0.1 and lon_range < 0.1:
                zoom_auto = 15
            elif lat_range < 1 and lon_range < 1:
                zoom_auto = 14
            elif lat_range < 5 and lon_range < 5:
                zoom_auto = 12
            else:
                zoom_auto = 10

            # Slider de zoom manual
            zoom_user = st.sidebar.slider("🔍 Nivel de zoom del mapa", 3, 15, int(zoom_auto))

            # Determinar columna de color disponible
            if "program" in df_plot.columns:
                color_col = "program"
            elif "isp" in df_plot.columns:
                color_col = "isp"
            elif "provider" in df_plot.columns:
                color_col = "provider"
            else:
                color_col = None

            # Columnas existentes para hover
            hover_cols = [c for c in ["latitude", "longitude", "city", "isp", "provider", "subtechnology", "avgLatency"] if c in df_plot.columns]
            hover_name_col = "program" if "program" in df_plot.columns else None

            # Crear mapa
            fig = px.scatter_mapbox(
                df_plot,
                lat="latitude",
                lon="longitude",
                color=color_col,
                hover_name=hover_name_col,
                hover_data=hover_cols,
                color_discrete_sequence=px.colors.qualitative.Bold,
                height=600,
            )

            fig.update_layout(
                mapbox_style="open-street-map",
                mapbox_center={"lat": centro_lat, "lon": centro_lon},
                mapbox_zoom=zoom_user,
                margin={"r": 0, "t": 0, "l": 0, "b": 0},
            )

            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"🗺️ Última medición: ({centro_lat:.4f}, {centro_lon:.4f}) | Zoom: {zoom_user}")

        else:
            st.warning("⚠️ No hay coordenadas válidas para mostrar en el mapa.")
    else:
        st.warning("⚠️ El dataset no contiene columnas 'latitude' y 'longitude'.")
else:
    st.info("👈 Consulta primero la API para visualizar el mapa.")


