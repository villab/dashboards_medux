import streamlit as st
import pandas as pd
import plotly.express as px
import requests
from datetime import datetime, timedelta, time
import pytz
from streamlit_autorefresh import st_autorefresh
import time as tm

# ========== Config inicial ==========
st.set_page_config(page_title="Medux RAW Dashboard", layout="wide")
st.title("📊 Dashboard de Datos RAW – Medux API")

# ========== Auth & probes ==========
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

# guardamos token
st.session_state.api_token = token

st.sidebar.markdown("---")
probes_file = st.sidebar.file_uploader("📄 Subir CSV de probes", type=["csv"])
if probes_file is not None:
    df_probes = pd.read_csv(probes_file)
    probes = df_probes["probes_id"].dropna().astype(str).tolist()
else:
    st.warning("⚠️ Sube un archivo CSV con la columna `probes_id`.")
    st.stop()

# ========== Parámetros ==========
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
        "cloud-upload",
    ],
    default=["ping-test"]
)

# ========== Fecha/hora (zona Bogotá) ==========
zona_local = pytz.timezone("America/Bogota")
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

fecha_inicio_defecto = inicio_defecto_local.date()
hora_inicio_defecto = time(inicio_defecto_local.hour, inicio_defecto_local.minute)
fecha_fin_defecto = ahora_local.date()
hora_fin_defecto = time(ahora_local.hour, ahora_local.minute)

st.sidebar.markdown("---")
st.sidebar.header("📅 Rango de fechas y horas (hora local)")

fecha_inicio = st.sidebar.date_input("Fecha de inicio", fecha_inicio_defecto)
hora_inicio = st.sidebar.time_input("Hora de inicio", hora_inicio_defecto)
fecha_fin = st.sidebar.date_input("Fecha de fin", fecha_fin_defecto)
hora_fin = st.sidebar.time_input("Hora de fin", hora_fin_defecto)

# real-time
st_autorefresh(interval=30_000, key="real_time_refresh")
usar_real_time = st.sidebar.checkbox("⏱️ Modo real-time (últimos 30 min)", value=True)
if usar_real_time:
    ahora_local = datetime.now(zona_local)
    fecha_fin = ahora_local.date()
    hora_fin = ahora_local.time().replace(second=0, microsecond=0)
    inicio_rt = ahora_local - timedelta(minutes=30)
    fecha_inicio = inicio_rt.date()
    hora_inicio = inicio_rt.time().replace(second=0, microsecond=0)
    st.sidebar.caption(f"Real-time → Últimos 30 min ({ahora_local.strftime('%Y-%m-%d %H:%M:%S')})")

# calcular timestamps UTC (ms)
dt_inicio_local = zona_local.localize(datetime.combine(fecha_inicio, hora_inicio))
dt_fin_local = zona_local.localize(datetime.combine(fecha_fin, hora_fin))
if dt_inicio_local >= dt_fin_local:
    st.error("⚠️ La fecha/hora de inicio no puede ser posterior o igual a la de fin.")
    st.stop()

ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)

st.sidebar.markdown("### 🕒 Rango seleccionado")
st.sidebar.write(f"Inicio local: {dt_inicio_local.strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.write(f"Fin local: {dt_fin_local.strftime('%Y-%m-%d %H:%M:%S')}")

# ========== Helpers para API (NO cacheado: real-time) ==========
API_URL = "https://medux-ids.caseonit.com/api/results"
HEADERS = {"Authorization": f"Bearer {st.session_state.api_token}", "Content-Type": "application/json"}

def build_body_for_program(programa, next_pagination_data=None):
    body = {
        "format": "raw",
        "paginate": True,
        "size": 10000,
        "programs": [programa],
        "probes": probes,
        "tsStart": ts_start,
        "tsEnd": ts_end,
    }
    if next_pagination_data:
        # la API devuelve 'next_pagination_data' en la respuesta; hay que reenviarlo igual
        body["next_pagination_data"] = next_pagination_data
    return body

def fetch_all_pages_for_program(programa):
    """Trae todas las páginas para *un* programa usando next_pagination_data."""
    all_results = []
    next_page = None
    page = 1

    while True:
        body = build_body_for_program(programa, next_page)
        # debug: muestra el body la primera vez (evitar datos sensibles en producción)
        if page == 1:
            st.write("📤 Enviando body (resumen):", {k: v for k, v in body.items() if k != "probes"})
        resp = requests.post(API_URL, headers=HEADERS, json=body, timeout=90)
        st.write(f"🔁 Respuesta página {page}: status {resp.status_code}")
        # mostrar claves de respuesta para depuración
        try:
            data = resp.json()
            st.write("🔎 Claves recibidas:", list(data.keys()))
        except Exception:
            st.error("❌ No se pudo parsear JSON de la respuesta.")
            st.text(resp.text)
            return []

        if resp.status_code != 200:
            # mostrar mensaje de error con detalle
            st.warning(f"⚠️ Status {resp.status_code} - detalle: {data}")
            return []

        results = data.get("results", [])
        st.info(f"📥 Página {page} → {len(results):,} registros")
        all_results.extend(results)

        # determinamos siguiente page
        next_page = data.get("next_pagination_data")
        if not next_page:
            break
        page += 1
        tm.sleep(0.4)  # pequeña pausa para no saturar

    return all_results

# ========== Ejecución de consulta por programas seleccionados ==========
if st.sidebar.button("🚀 Consultar API") or usar_real_time:
    if not programas:
        st.warning("Selecciona al menos un programa.")
        st.stop()

    lista_df = []
    for programa in programas:
        st.subheader(f"📡 Consultando programa: {programa}")
        results = fetch_all_pages_for_program(programa)
        if not results:
            st.warning(f"⚠️ No se obtuvieron registros para {programa}. Revisa el body y el rango de fechas.")
            continue

        df_prog = pd.DataFrame(results)
        # asegurar columna program (si falta, ponemos el programa solicitado)
        if "test" in df_prog.columns:
            df_prog["program"] = df_prog["test"].fillna(programa)
        elif "program" in df_prog.columns:
            df_prog["program"] = df_prog["program"].fillna(programa)
        else:
            df_prog["program"] = programa

        lista_df.append(df_prog)

    if not lista_df:
        st.error("❌ No se obtuvieron datos de ningún programa. Revisa los mensajes anteriores.")
        st.stop()

    df = pd.concat(lista_df, ignore_index=True)
    st.success(f"✅ Datos combinados: {len(df):,} filas.")
    st.write("📊 Conteo por programa:")
    st.write(df["program"].value_counts())

    st.session_state.df = df
else:
    df = st.session_state.df if "df" in st.session_state else pd.DataFrame()

# ========== Visualización (gráficas) ==========
if not df.empty:
    st.sidebar.header("📈 Visualización")
    programa_sel = st.sidebar.selectbox("Programa", sorted(df["program"].unique()))
    subset = df[df["program"] == programa_sel]

    columnas_numericas = subset.select_dtypes(include="number").columns.tolist()
    columnas_todas = subset.columns.tolist()
    eje_x = st.sidebar.selectbox("Eje X", columnas_todas, index=0)
    eje_y = st.sidebar.selectbox("Eje Y", columnas_numericas, index=0 if columnas_numericas else None)
    tipo = st.sidebar.selectbox("Tipo de gráfico", ["scatter", "line", "bar"])

    if eje_y is None:
        st.warning("No hay columnas numéricas para graficar.")
    else:
        if tipo == "scatter":
            fig = px.scatter(subset, x=eje_x, y=eje_y, title=f"{programa_sel}: {eje_y} vs {eje_x}")
        elif tipo == "line":
            fig = px.line(subset, x=eje_x, y=eje_y, title=f"{programa_sel}: {eje_y} vs {eje_x}")
        else:
            fig = px.bar(subset, x=eje_x, y=eje_y, title=f"{programa_sel}: {eje_y} vs {eje_x}")

        st.plotly_chart(fig, use_container_width=True)

    with st.expander("📄 Ver datos del programa"):
        st.dataframe(subset)

else:
    st.info("👈 Configura y presiona **Consultar API** o activa real-time para ver los resultados.")

# ========== Mapas por ISP ==========
st.markdown("## 🗺️ Mapas por ISP")
if "df" in st.session_state and not st.session_state.df.empty:
    df_plot = st.session_state.df.copy()
    if all(col in df_plot.columns for col in ["latitude", "longitude", "isp"]):
        df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
        df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
        df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])
        if not df_plot.empty:
            isps = df_plot["isp"].unique().tolist()
            colores = px.colors.qualitative.Bold
            for i, isp in enumerate(isps):
                df_isp = df_plot[df_plot["isp"] == isp]
                if df_isp.empty:
                    continue
                ultimo = df_isp.iloc[-1]
                centro_lat, centro_lon = ultimo["latitude"], ultimo["longitude"]
                lat_range = df_isp["latitude"].max() - df_isp["latitude"].min()
                zoom_auto = 15 if lat_range < 0.1 else 13 if lat_range < 1 else 11 if lat_range < 5 else 9
                zoom_user = st.sidebar.slider(f"🔍 Zoom para {isp}", 3, 15, int(zoom_auto))
                hover_cols = [c for c in ["latitude", "longitude", "city", "program", "subtechnology", "avgLatency"] if c in df_isp.columns]
                fig = px.scatter_mapbox(
                    df_isp,
                    lat="latitude",
                    lon="longitude",
                    color="program",
                    hover_name="program",
                    hover_data=hover_cols,
                    color_discrete_sequence=[colores[i % len(colores)]],
                    height=450,
                )
                fig.update_layout(mapbox_style="carto-positron", mapbox_center={"lat": centro_lat, "lon": centro_lon}, mapbox_zoom=zoom_user, margin={"r":0,"t":0,"l":0,"b":0})
                st.subheader(f"🗺️ ISP: {isp}")
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"Última medición para {isp}: ({centro_lat:.4f}, {centro_lon:.4f}) | Zoom: {zoom_user}")
        else:
            st.warning("⚠️ No hay coordenadas válidas para mostrar en los mapas.")
    else:
        st.warning("⚠️ El dataset no contiene 'latitude', 'longitude' o 'isp'.")
else:
    st.info("👈 Consulta primero la API para visualizar los mapas por ISP.")
