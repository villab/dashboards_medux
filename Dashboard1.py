import streamlit as st
import pandas as pd
import plotly.express as px
import requests
from datetime import datetime, timedelta, time
import pytz
from streamlit_autorefresh import st_autorefresh

# ===========================================================
# 🧠 CONFIGURACIÓN INICIAL
# ===========================================================
st.set_page_config(page_title="Medux RAW Dashboard", layout="wide")
st.title("📊 Dashboard de Datos RAW – Medux API")

# ===========================================================
# 🔹 AUTENTICACIÓN
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

# ===========================================================
# 📄 CSV DE PROBES
# ===========================================================
st.sidebar.markdown("---")
probes_file = st.sidebar.file_uploader("📄 Subir CSV de probes", type=["csv"])
if probes_file is not None:
    df_probes = pd.read_csv(probes_file)
    if "probes_id" not in df_probes.columns:
        st.error("❌ El CSV debe tener una columna llamada `probes_id`.")
        st.stop()
    probes = df_probes["probes_id"].dropna().tolist()
else:
    st.warning("⚠️ Sube un archivo CSV con la columna `probes_id`.")
    st.stop()

# ===========================================================
# ⚙️ PARÁMETROS DE CONSULTA
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Parámetros de consulta")

programas = st.sidebar.multiselect(
    "Selecciona los programas",
    ["confess-chrome", "ping-test", "network", "voice-out", "cloud-download", "cloud-upload"],
    default=["ping-test"]
)

# ===========================================================
# ⏱️ ACTUALIZACIÓN EN TIEMPO REAL
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("⏱️ Actualización automática")

refresh_seconds = st.sidebar.slider("Frecuencia de refresco (segundos)", 10, 300, 30)
usar_real_time = st.sidebar.checkbox("Activar modo realtime (últimas 6 h)", value=False)

if usar_real_time:
    st_autorefresh(interval=refresh_seconds * 1000, key="real_time_refresh")

# ===========================================================
# 📅 RANGO MANUAL DE FECHAS
# ===========================================================
zona_local = pytz.timezone("America/Bogota")
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

st.sidebar.markdown("---")
st.sidebar.header("📅 Rango de fechas (modo manual)")

fecha_inicio = st.sidebar.date_input("Fecha de inicio", inicio_defecto_local.date())
hora_inicio = st.sidebar.time_input("Hora de inicio", inicio_defecto_local.time())
fecha_fin = st.sidebar.date_input("Fecha de fin", ahora_local.date())
hora_fin = st.sidebar.time_input("Hora de fin", ahora_local.time())

# ===========================================================
# 🧮 CALCULAR TIMESTAMPS
# ===========================================================
if usar_real_time:
    ts_end = int(datetime.now(pytz.utc).timestamp() * 1000)
    ts_start = int((datetime.now(pytz.utc) - timedelta(hours=6)).timestamp() * 1000)
    st.sidebar.caption(f"🔁 Modo realtime activo (últimas 6 h, refresca cada {refresh_seconds}s)")
else:
    dt_inicio_local = zona_local.localize(datetime.combine(fecha_inicio, hora_inicio))
    dt_fin_local = zona_local.localize(datetime.combine(fecha_fin, hora_fin))
    if dt_inicio_local >= dt_fin_local:
        st.error("⚠️ La fecha/hora de inicio no puede ser posterior o igual a la de fin.")
        st.stop()
    ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)
    st.sidebar.caption("📅 Rango de tiempo definido manualmente")

# Mostrar rango activo
st.sidebar.markdown("### 🕒 Rango activo")
inicio_local_str = datetime.fromtimestamp(ts_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
fin_local_str = datetime.fromtimestamp(ts_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
st.sidebar.write(f"Inicio local: {inicio_local_str}")
st.sidebar.write(f"Fin local: {fin_local_str}")

# ===========================================================
# 📡 CONFIGURACIÓN API
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
# 🔹 FUNCIONES API
# ===========================================================
@st.cache_data(ttl=1800)
def obtener_datos_pag(url, headers, body):
    todos_los_resultados = {}
    pagina = 1
    total = 0

    while True:
        r = requests.post(url, headers=headers, json=body)
        if r.status_code != 200:
            st.error(f"❌ Error API: {r.status_code}")
            break

        data = r.json()
        results = data.get("results", {})

        if isinstance(results, dict):
            for prog, res in results.items():
                todos_los_resultados.setdefault(prog, []).extend(res)
                total += len(res)
        elif isinstance(results, list):
            todos_los_resultados.setdefault("general", []).extend(results)
            total += len(results)

        next_data = data.get("next_pagination_data")
        if not next_data:
            break
        body["pagination_data"] = next_data
        pagina += 1

    return todos_los_resultados


def obtener_datos_pag_no_cache(url, headers, body):
    try:
        r = requests.post(url, headers=headers, json=body)
        if r.status_code == 200:
            return r.json()
        st.warning(f"⚠️ Error API: {r.status_code}")
        return None
    except Exception as e:
        st.error(f"❌ Error al consultar API: {e}")
        return None


def flatten_results(raw_json):
    filas = []
    if isinstance(raw_json, dict):
        for prog, results in raw_json.items():
            if isinstance(results, list):
                for item in results:
                    row = {"program": prog}
                    if isinstance(item, dict):
                        row.update(item)
                    filas.append(row)
    elif isinstance(raw_json, list):
        for item in raw_json:
            if isinstance(item, dict):
                filas.append(item)
    return pd.DataFrame(filas)

# ===========================================================
# 🚀 CONSULTAR API
# ===========================================================
if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()

if st.sidebar.button("🚀 Consultar API") or usar_real_time:
    raw = obtener_datos_pag_no_cache(url, headers, body) if usar_real_time else obtener_datos_pag(url, headers, body)
    if not raw:
        st.warning("⚠️ No se recibieron datos de la API.")
        st.stop()

    if isinstance(raw, dict):
        data = raw.get("results", raw)
    elif isinstance(raw, list):
        data = {"resultados": raw}
    else:
        st.error("❌ Formato inesperado de respuesta de la API.")
        st.stop()

    df = flatten_results(data)
    if df.empty:
        st.warning("⚠️ No se recibieron datos válidos.")
        st.stop()

    st.session_state.df = df
    st.success(f"✅ Datos cargados correctamente ({len(df)} filas).")
else:
    df = st.session_state.df

# ===========================================================
# 📋 TABLAS POR SONDA
# ===========================================================
st.markdown("## 📋 Resultados por Sonda")

if not df.empty:
    df_tablas = df.copy()
    col_probe = next((c for c in ["probe", "probe_id", "probeId", "probes_id"] if c in df_tablas.columns), None)
    col_time = next((c for c in ["dateStart", "timestamp", "ts", "datetime", "createdAt"] if c in df_tablas.columns), None)
    col_isp = next((c for c in ["isp", "ISP", "provider"] if c in df_tablas.columns), None)

    if col_probe and col_time:
        df_tablas["_parsed_time"] = pd.to_datetime(df_tablas[col_time], errors="coerce", utc=True)
        for s in sorted(df_tablas[col_probe].dropna().unique()):
            df_sonda = df_tablas[df_tablas[col_probe] == s].sort_values(by="_parsed_time", ascending=False)
            isp = df_sonda[col_isp].iloc[0] if col_isp else "Desconocido"
            st.subheader(f"Sonda {s} – ISP: {isp}")
            st.dataframe(df_sonda.drop(columns=["_parsed_time"]), use_container_width=True, height=350)
            st.markdown("<br>", unsafe_allow_html=True)
    else:
        st.warning("⚠️ No se encontró columna de sonda o tiempo.")
else:
    st.info("👈 Consulta primero la API para visualizar resultados.")

# ===========================================================
# 🗺️ MAPAS POR ISP
# ===========================================================
st.markdown("## 🗺️ Mapas por ISP")

if not df.empty and all(c in df.columns for c in ["latitude", "longitude", "isp"]):
    df_plot = df.copy()
    df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
    df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
    df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])

    if not df_plot.empty:
        lat_range = df_plot["latitude"].max() - df_plot["latitude"].min()
        lon_range = df_plot["longitude"].max() - df_plot["longitude"].min()

        if lat_range < 0.1 and lon_range < 0.1:
            zoom_default = 15
        elif lat_range < 1 and lon_range < 1:
            zoom_default = 14
        elif lat_range < 5 and lon_range < 5:
            zoom_default = 12
        else:
            zoom_default = 10

        zoom_global = st.sidebar.slider("🔍 Zoom general mapas", 3, 15, int(zoom_default))

        isps = df_plot["isp"].unique()
        cols_per_row = 3
        for i in range(0, len(isps), cols_per_row):
            cols = st.columns(cols_per_row)
            for j, col in enumerate(cols):
                if i + j >= len(isps):
                    break
                isp = isps[i + j]
                df_isp = df_plot[df_plot["isp"] == isp]
                if df_isp.empty:
                    continue
                centro_lat = df_isp["latitude"].iloc[-1]
                centro_lon = df_isp["longitude"].iloc[-1]
                fig = px.scatter_mapbox(
                    df_isp,
                    lat="latitude",
                    lon="longitude",
                    color="isp",
                    hover_name="isp",
                    hover_data=[c for c in ["city", "provider", "subtechnology", "program"] if c in df_isp.columns],
                    color_discrete_sequence=px.colors.qualitative.Bold,
                    height=320,
                )
                fig.update_layout(
                    mapbox=dict(
                        style="carto-positron",
                        center={"lat": centro_lat, "lon": centro_lon},
                        zoom=zoom_global,
                    ),
                    margin={"r": 0, "t": 0, "l": 0, "b": 0},
                    showlegend=False,
                )
                with col:
                    st.markdown(f"**{isp}**", unsafe_allow_html=True)
                    st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ No hay coordenadas válidas.")
else:
    st.info("👈 Consulta primero la API para mostrar mapas.")

# ===========================================================
# 📈 GRÁFICA DE DISPERSIÓN
# ===========================================================
st.markdown("## 📈 Comparativa de métricas")

if not df.empty:
    df_plot = df.copy()
    for col in df_plot.columns:
        if df_plot[col].dtype == "object":
            try:
                df_plot[col] = pd.to_numeric(df_plot[col])
            except Exception:
                pass

    columnas_num = [c for c in df_plot.columns if pd.api.types.is_numeric_dtype(df_plot[c])]

    if len(columnas_num) >= 1:
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            eje_x = st.selectbox("📏 Eje X", options=df_plot.columns)
        with col2:
            eje_y = st.selectbox("📐 Eje Y", options=columnas_num)
        with col3:
            color_var = st.selectbox("🎨 Agrupar por", options=[c for c in df_plot.columns if c not in [eje_x, eje_y]], index=0)

        fig = px.scatter(
            df_plot,
            x=eje_x,
            y=eje_y,
            color=color_var,
            hover_data=[c for c in ["city", "provider", "subtechnology"] if c in df_plot.columns],
            color_discrete_sequence=px.colors.qualitative.Bold,
            title=f"Relación entre **{eje_x}** y **{eje_y}**",
            height=500,
        )
        fig.update_traces(marker=dict(size=8, opacity=0.8, line=dict(width=0.5, color="white")))
        fig.update_layout(margin=dict(l=0, r=0, t=50, b=0), template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ No hay suficientes columnas numéricas.")
else:
    st.info("👈 Consulta primero la API para visualizar la gráfica.")
