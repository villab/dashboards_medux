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
    ["confess-chrome", "ping-test","network","voice-out","cloud-download","cloud-upload"],
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
st_autorefresh(interval=5 * 60 * 1000, key="real_time_refresh")

usar_real_time = st.sidebar.checkbox("⏱️ Modo real-time", value=True)

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
# 🟢 Tabla resumen de estado por sonda
# ===========================================================
st.markdown("## 🟩 Estado general de sondas")

if "df" in st.session_state and not st.session_state.df.empty:
    df_estado = st.session_state.df.copy()
    col_probe = next((c for c in ["probe", "probe_id", "probeId", "probes_id"] if c in df_estado.columns), None)
    col_isp = next((c for c in ["isp", "provider", "operator"] if c in df_estado.columns), None)
    col_time = next((c for c in ["dateStart", "ts", "datetime", "createdAt"] if c in df_estado.columns), None)

    if col_probe and col_time:
        # Convertir timestamp a datetime si es necesario
        df_estado[col_time] = pd.to_datetime(df_estado[col_time], errors="coerce", unit="s", utc=True).dt.tz_convert("America/Bogota")

        # Obtener el último registro por sonda
        resumen = (
            df_estado.sort_values(by=col_time, ascending=False)
            .groupby(col_probe)
            .agg({
                col_time: "first",
                col_isp: lambda x: ", ".join(sorted(set(x.dropna()))) if col_isp else "Desconocido"
            })
            .reset_index()
            .rename(columns={col_probe: "Sonda", col_time: "Último reporte", col_isp: "ISP"})
        )

        # Calcular diferencia de tiempo
        now = pd.Timestamp.now(tz="America/Bogota")
        resumen["Minutos desde último reporte"] = (now - resumen["Último reporte"]).dt.total_seconds() / 60

        # Determinar estado ON/OFF
        resumen["Estado"] = resumen["Minutos desde último reporte"].apply(lambda x: "🟢 ON" if x <= 20 else "🔴 OFF")

        # Reordenar columnas
        resumen = resumen[["Sonda", "ISP", "Último reporte", "Minutos desde último reporte", "Estado"]]

        # Mostrar la tabla
        st.dataframe(
            resumen.sort_values(by="Último reporte", ascending=False),
            use_container_width=True
        )

        # Pequeña nota
        st.caption("🕒 Estado calculado según la fecha del último registro recibido por sonda (ON = < 20 min).")

    else:
        st.warning("⚠️ No se encontraron columnas de sonda o tiempo en los datos.")
else:
    st.info("👈 Consulta primero la API para visualizar el resumen de sondas.")

# ===========================================================
# 🌍 Mapas de mediciones por ISP (3 por fila)
# ===========================================================
st.markdown("## 🗺️ Mapas por ISP")

if "df" in st.session_state and not st.session_state.df.empty:
    df_plot = st.session_state.df.copy()

    # Verificar columnas necesarias
    if all(col in df_plot.columns for col in ["latitude", "longitude", "isp"]):
        df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
        df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
        df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])

        if not df_plot.empty:
            # ============================
            # 🔍 Zoom global consistente
            # ============================
            lat_range_global = df_plot["latitude"].max() - df_plot["latitude"].min()
            lon_range_global = df_plot["longitude"].max() - df_plot["longitude"].min()

            if pd.isna(lat_range_global) or pd.isna(lon_range_global):
                zoom_default = 12
            elif lat_range_global == 0 and lon_range_global == 0:
                zoom_default = 15
            elif lat_range_global < 0.1 and lon_range_global < 0.1:
                zoom_default = 15
            elif lat_range_global < 1 and lon_range_global < 1:
                zoom_default = 14
            elif lat_range_global < 5 and lon_range_global < 5:
                zoom_default = 12
            else:
                zoom_default = 10

            zoom_global = st.sidebar.slider("🔍 Zoom general para mapas", 3, 15, int(zoom_default))

            # ============================
            # 🗺️ Mostrar en 3 columnas
            # ============================
            isps = df_plot["isp"].unique()
            cols_per_row = 3  # ahora son tres por fila

            for i in range(0, len(isps), cols_per_row):
                cols = st.columns(cols_per_row)
                for j, col in enumerate(cols):
                    if i + j >= len(isps):
                        break
                    isp = isps[i + j]
                    df_isp = df_plot[df_plot["isp"] == isp]
                    if df_isp.empty:
                        continue

                    ultimo_punto = df_isp.iloc[-1]
                    centro_lat = ultimo_punto["latitude"]
                    centro_lon = ultimo_punto["longitude"]
                    hover_cols = [c for c in ["latitude", "longitude", "city", "provider",
                                              "subtechnology", "avgLatency", "program"]
                                  if c in df_isp.columns]

                    fig = px.scatter_map(
                        df_isp,
                        lat="latitude",
                        lon="longitude",
                        color="isp",
                        hover_name="isp",
                        hover_data=hover_cols,
                        color_discrete_sequence=px.colors.qualitative.Bold,
                        height=320,  # más compacto
                        labels={"program": "Tipo de prueba"},
                    )

                    fig.update_layout(
                        map={
                            "style": "carto-positron",
                            "center": {"lat": centro_lat, "lon": centro_lon},
                            "zoom": zoom_global,
                        },
                        margin={"r": 0, "t": 0, "l": 0, "b": 0},
                        showlegend=False,  # ✅ oculta leyenda
                    )

                    with col:
                        st.markdown(f"**{isp}**", unsafe_allow_html=True)
                        st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("⚠️ No hay coordenadas válidas para mostrar.")
    else:
        st.warning("⚠️ El dataset no contiene 'latitude', 'longitude' o 'isp'.")
else:
    st.info("👈 Consulta primero la API para visualizar los mapas.")


st.markdown("<br><br>", unsafe_allow_html=True)
# ===========================================================
# 📊 Gráfica de dispersión interactiva
# ===========================================================
st.markdown("## 📈 Comparativa de métricas")

if "df" in st.session_state and not st.session_state.df.empty:
    df_plot = st.session_state.df.copy()

    # --- Intentar convertir strings numéricos a float ---
    for col in df_plot.columns:
        if df_plot[col].dtype == "object":
            try:
                df_plot[col] = pd.to_numeric(df_plot[col])
            except Exception:
                pass  # Si no se puede convertir, se deja igual

    # --- Detectar columnas numéricas y de texto ---
    columnas_todas = df_plot.columns.tolist()
    columnas_numericas = [
        c for c in columnas_todas if pd.api.types.is_numeric_dtype(df_plot[c])
    ]

    # --- Verificar si hay columnas suficientes ---
    if len(columnas_todas) >= 2:
        col1, col2, col3 = st.columns([1, 1, 2])

        with col1:
            eje_x = st.selectbox("📏 Eje X", options=columnas_todas, index=0)

        with col2:
            eje_y = st.selectbox("📐 Eje Y", options=columnas_numericas, index=min(1, len(columnas_numericas) - 1))

        with col3:
            color_var = st.selectbox(
                "🎨 Agrupar por",
                options=[c for c in columnas_todas if c not in [eje_x, eje_y]],
                index=df_plot.columns.get_loc("isp") if "isp" in df_plot.columns else 0
            )

        # --- Crear la gráfica ---
        fig_disp = px.scatter(
            df_plot,
            x=eje_x,
            y=eje_y,
            color=color_var,
            hover_data=[c for c in ["city", "provider", "subtechnology"] if c in df_plot.columns],
            color_discrete_sequence=px.colors.qualitative.Bold,
            labels={eje_x: eje_x.replace("_", " "), eje_y: eje_y.replace("_", " ")},
            title=f"Relación entre **{eje_x}** y **{eje_y}**",
            height=500
        )

        # --- Ajustes visuales ---
        fig_disp.update_traces(marker=dict(size=8, opacity=0.8, line=dict(width=0.5, color="white")))
        fig_disp.update_layout(
            legend_title_text=color_var,
            margin=dict(l=0, r=0, t=50, b=0),
            hovermode="closest",
            template="plotly_white",
        )

        st.plotly_chart(fig_disp, use_container_width=True)

    else:
        st.warning("⚠️ No hay suficientes columnas para generar la gráfica.")
else:
    st.info("👈 Consulta primero la API para visualizar la gráfica.")




# ===========================================================
# 📋 Tablas por Sonda (una debajo de otra)
# ===========================================================
st.markdown("<br><br>", unsafe_allow_html=True)
st.markdown("## 📋 Resultados por Sonda")

if "df" in st.session_state and not st.session_state.df.empty:
    df_tablas = st.session_state.df.copy()
    col_probe = next((c for c in ["probe", "probe_id", "probeId", "probes_id"] if c in df_tablas.columns), None)
    col_isp = next((c for c in ["isp", "provider", "operator"] if c in df_tablas.columns), None)
    col_time = next((c for c in ["timestamp", "ts", "datetime", "createdAt"] if c in df_tablas.columns), None)

    if col_probe:
        for s in sorted(df_tablas[col_probe].dropna().unique()):
            df_sonda = df_tablas[df_tablas[col_probe] == s].copy()

            # Determinar el ISP asociado (si hay más de uno, mostrar "Varios")
            isp_values = df_sonda[col_isp].dropna().unique().tolist() if col_isp else []
            isp_label = ", ".join(sorted(isp_values)) if len(isp_values) == 1 else (
                "Varios" if len(isp_values) > 1 else "Desconocido"
            )

            # Título con ISP y sonda
            st.subheader(f"🔹 Sonda {s} — ISP: {isp_label}")

            # Ordenar por tiempo descendente (más reciente arriba)
            if col_time:
                df_sonda[col_time] = pd.to_numeric(df_sonda[col_time], errors="coerce")
                df_sonda = df_sonda.sort_values(by=col_time, ascending=False, na_position="last")

            # Mostrar tabla
            st.dataframe(df_sonda.head(20), use_container_width=True)
            st.caption(f"{len(df_sonda)} registros — del más reciente al más antiguo.")
            st.markdown("<br>", unsafe_allow_html=True)
    else:
        st.warning("⚠️ No se encontró ninguna columna de sonda ('probe', 'probe_id', 'probeId' o 'probes_id').")
else:
    st.info("👈 Consulta primero la API para mostrar las tablas por sonda.")













