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
st.set_page_config(page_title="Medux Verveba Dashboard", layout="wide")
st.markdown("### 📊 Dashboard Verveba Mobile")

# ===========================================================
# 🔐 TOKEN Y PROBES DESDE SECRETS
# ===========================================================
st.sidebar.caption("🔐 Configuración API (modo automático)")
try:
    token = st.secrets["token"]
    probes = st.secrets["ids"]
    st.sidebar.caption(f"✅ Token y {len(probes)} sondas cargadas desde secrets (seguro)")
except Exception as e:
    st.caption("❌ No se pudo cargar token o sondas desde secrets.")
    st.exception(e)
    st.stop()

# ===========================================================
# ⚙️ PARÁMETROS DE CONSULTA
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Parámetros de consulta")

programas = st.sidebar.multiselect(
    "Selecciona los programas",
    ["confess-chrome", "youtube-test", "ping-test", "network", "voice-out", "cloud-download", "cloud-upload"],
    default=["confess-chrome", "youtube-test", "ping-test", "voice-out", "cloud-download", "cloud-upload"]
)

# ===========================================================
# 🕒 ZONA HORARIA DE LAS VEGAS
# ===========================================================
zona_local = pytz.timezone("America/Los_Angeles")

# ===========================================================
# ⏱️ ACTUALIZACIÓN EN TIEMPO REAL
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("⏱️ Actualización automática")

refresh_seconds = st.sidebar.slider("Frecuencia de refresco (segundos)", 10, 300, 30)
usar_real_time = st.sidebar.checkbox("Activar modo realtime (últimas 8 h)", value=True)

if usar_real_time:
    st_autorefresh(interval=refresh_seconds * 1000, key="real_time_refresh")

# ===========================================================
# 📅 RANGO MANUAL DE FECHAS
# ===========================================================
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

st.sidebar.markdown("---")
st.sidebar.header("📅 Rango de fechas")

fecha_inicio = st.sidebar.date_input("Fecha de inicio", inicio_defecto_local.date())
hora_inicio = st.sidebar.time_input("Hora de inicio", inicio_defecto_local.time())
fecha_fin = st.sidebar.date_input("Fecha de fin", ahora_local.date())
hora_fin = st.sidebar.time_input("Hora de fin", ahora_local.time())

# ===========================================================
# 🧮 CALCULAR TIMESTAMPS
# ===========================================================
if usar_real_time:
    ts_end = int(datetime.now(pytz.utc).timestamp() * 1000)
    ts_start = int((datetime.now(pytz.utc) - timedelta(hours=8)).timestamp() * 1000)
    st.sidebar.caption(f"🔁 Modo realtime activo (últimas 8 h, refresca cada {refresh_seconds}s)")
else:
    dt_inicio_local = zona_local.localize(datetime.combine(fecha_inicio, hora_inicio))
    dt_fin_local = zona_local.localize(datetime.combine(fecha_fin, hora_fin))
    if dt_inicio_local >= dt_fin_local:
        st.error("⚠️ La fecha/hora de inicio no puede ser posterior o igual a la de fin.")
        st.stop()
    ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)
    st.sidebar.caption("📅 Rango de tiempo definido manualmente")

# Mostrar rango activo (formato Las Vegas)
st.sidebar.markdown("### 🕒 Rango activo")
inicio_local_str = datetime.fromtimestamp(ts_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
fin_local_str = datetime.fromtimestamp(ts_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
st.sidebar.write(f"Inicio (Las Vegas): {inicio_local_str}")
st.sidebar.write(f"Fin (Las Vegas): {fin_local_str}")

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
# 🔹 FUNCIONES DE CONSULTA Y NORMALIZACIÓN API
# ===========================================================
@st.cache_data(ttl=1800)
def obtener_datos_pag(url, headers, body):
    todos_los_resultados = {}
    pagina = 1
    total = 0
    payload = body.copy()  # ✅ evita modificar el original

    while True:
        st.info(f"📡 Descargando página {pagina}...")
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            st.error(f"❌ Error API: {r.status_code}")
            break

        data = r.json()
        results = data.get("results")
        if isinstance(results, list):
            todos_los_resultados.setdefault("network", []).extend(results)
            total += len(results)
        elif isinstance(results, dict):
            for prog, res in results.items():
                if isinstance(res, list):
                    todos_los_resultados.setdefault(prog, []).extend(res)
                    total += len(res)

        next_data = data.get("next_pagination_data")
        if not next_data:
            break

        payload["pagination_data"] = next_data
        pagina += 1
        if pagina > 100:  # ✅ evita loops infinitos
            st.warning("⚠️ Se alcanzó el límite máximo de 100 páginas de paginación.")
            break

    st.success(f"✅ {total:,} registros descargados en {pagina} página(s).")
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
    def extraer_filas(obj, program=None):
        if isinstance(obj, dict):
            if "results" in obj:
                extraer_filas(obj["results"], program)
            else:
                tiene_lista = False
                for k, v in obj.items():
                    if isinstance(v, list):
                        tiene_lista = True
                        extraer_filas(v, k)
                if not tiene_lista:
                    fila = obj.copy()
                    if program:
                        fila["program"] = fila.get("program", program)
                    filas.append(fila)
        elif isinstance(obj, list):
            for item in obj:
                extraer_filas(item, program)
    extraer_filas(raw_json)
    if not filas:
        return pd.DataFrame()
    df = pd.DataFrame(filas)
    if "program" not in df.columns:
        df["program"] = "network"
    # 🔹 Convertir campos de fecha detectados a zona Las Vegas
    for col in df.columns:
        if any(x in col.lower() for x in ["date", "time", "timestamp", "created"]):
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert(zona_local).dt.strftime('%Y-%m-%d %H:%M:%S')
    return df

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
    df = flatten_results(raw)
    if df.empty:
        st.warning("⚠️ No se recibieron datos.")
        st.stop()
    st.session_state.df = df
    st.success(f"✅ Datos cargados correctamente ({len(df)} filas).")
else:
    df = st.session_state.df

# ===========================================================
# 📊 TABLA RESUMEN DE ESTADO DE SONDA (corregida para tz Las Vegas)
# ===========================================================
st.subheader("📡 Probes Status")


if "df" in st.session_state and not st.session_state.df.empty:
    df_resumen = st.session_state.df.copy()

    # Detectar columnas clave
    col_probe = next((c for c in ["probe", "probe_id", "probeId"] if c in df_resumen.columns), None)
    col_time = next((c for c in ["dateStart", "timestamp", "createdAt", "datetime"] if c in df_resumen.columns), None)
    col_isp = next((c for c in ["isp", "provider", "network"] if c in df_resumen.columns), None)

    if col_probe and col_time:
        # Tomar serie como strings para inspección
        s_dates = df_resumen[col_time].astype(str)

        # Detectar si existen timestamps con sufijo UTC explícito (Z o +00:00)
        tiene_utc_suffix = s_dates.str.contains(r'Z$|\+00:00$', regex=True).any()

        if tiene_utc_suffix:
            # Parsear como UTC y convertir a zona local Las Vegas
            df_resumen[col_time] = pd.to_datetime(df_resumen[col_time], errors="coerce", utc=True).dt.tz_convert(zona_local)
        else:
            # Si no tienen sufijo UTC, puede que ya estén en formato local (porque fueron formateadas en flatten_results).
            # Intentamos parsear; si resultan naive datetimes los localizamos directamente a zona_local.
            parsed = pd.to_datetime(df_resumen[col_time], errors="coerce")
            # Si la serie resultante es tz-aware, convertir; si es naive, localizar a zona_local
            if parsed.dt.tz is None:
                # Localizamos (asumimos que ya están en hora Las Vegas)
                df_resumen[col_time] = parsed.dt.tz_localize(zona_local)
            else:
                df_resumen[col_time] = parsed.dt.tz_convert(zona_local)

        # Filtrar nulos y preparar último registro por sonda
        df_resumen = df_resumen.dropna(subset=[col_time])
        df_last = df_resumen.sort_values(by=col_time).groupby(col_probe).tail(1).reset_index(drop=True)

        # Calcular estado ON/OFF en base a la hora local (Las Vegas)
        now_local = datetime.now(zona_local)
        # Asegurarnos que col_time sea datetime tz-aware
        df_last[col_time] = pd.to_datetime(df_last[col_time], errors="coerce")
        df_last["minutes_since"] = (now_local - df_last[col_time]).dt.total_seconds() / 60
        df_last["Estado"] = df_last["minutes_since"].apply(lambda x: "🟢 ON" if x <= 20 else "🔴 OFF")

        # Preparar tabla para mostrar
        columnas = [col_probe, col_isp, col_time, "Estado"]
        columnas_presentes = [c for c in columnas if c in df_last.columns]

        df_show = df_last[columnas_presentes].rename(
            columns={col_probe: "Sonda", col_isp: "ISP", col_time: "Último reporte"}
        )

        # Formatear la columna de fecha a string en formato Las Vegas
        # Si por alguna razón 'Último reporte' ya es string, lo reparseamos silenciosamente antes de formatear
        df_show["Último reporte"] = pd.to_datetime(df_show["Último reporte"], errors="coerce").dt.tz_convert(zona_local).dt.strftime('%Y-%m-%d %H:%M:%S')

        # Ordenar: primero las activas
        df_show = df_show.sort_values(by=["Estado", "Último reporte"], ascending=[False, False])

        st.dataframe(df_show[["Sonda", "ISP", "Último reporte", "Estado"]], use_container_width=True, height=300)

    else:
        st.warning("⚠️ No se encontraron columnas de sonda o tiempo en los datos.")
else:
    st.info("👈 Ejecuta la consulta para mostrar el resumen de sondas.")

# ===========================================================
# 📊 TABLAS POR SONDA (acordeones abiertos + columnas fijas + selector opcional)
# ===========================================================

st.markdown("### 📋 Probes Results")

if "df" not in st.session_state or st.session_state.df.empty:
    st.warning("⚠️ Aún no hay datos cargados. Usa el botón 'Consultar API'.")
else:
    df = st.session_state.df.copy()

    # --- 🔹 Columnas fijas (siempre visibles)
    columnas_fijas = ["probeId", "isp", "dateStart", "test", "latitude", "longitude", "success"]  # puedes ajustar las fijas aquí

    # --- 🔹 Detectar columnas adicionales disponibles
    columnas_extra = [c for c in df.columns if c not in columnas_fijas]

    # --- 🔹 Selector de columnas adicionales
    columnas_adicionales = st.multiselect(
        "Columnas adicionales",
        options=columnas_extra,
        default=[],  # no marcadas por defecto
        help="Las columnas base no se pueden quitar. Selecciona columnas extra si quieres ver más datos."
    )

    # --- 🔹 Combinar columnas a mostrar
    columnas_mostrar = columnas_fijas + columnas_adicionales

    # --- 🔹 Detectar nombre de columna de sonda
    col_probe = next((c for c in ["probe", "probe_id", "probeId", "probes_id"] if c in df.columns), None)

    # ✅ Detectar columna de ISP
    col_isp = next((c for c in ["isp", "provider", "network"] if c in df.columns), None)

    if not col_probe:
        st.error("❌ No se encontró columna de sonda ('probeId' o similar).")
    else:
        sondas = sorted(df[col_probe].dropna().unique())

        for sonda in sondas:
            df_sonda = df[df[col_probe] == sonda].copy()

            # Ordenar por fecha si existe (para asegurar que el primer registro sea el más reciente)
            if "dateStart" in df_sonda.columns:
                df_sonda["dateStart"] = pd.to_datetime(df_sonda["dateStart"], errors="coerce")
                df_sonda = df_sonda.sort_values("dateStart", ascending=False)
            else:
                # si no hay dateStart, dejamos el orden original (pero intentamos tener algo)
                df_sonda = df_sonda.copy()

            # Obtener el ISP del registro más reciente (primer valor no-nulo tras ordenar)
            if col_isp and col_isp in df_sonda.columns and not df_sonda.empty:
                isp_vals = df_sonda[col_isp].dropna().astype(str)
                if not isp_vals.empty:
                    isp_label = isp_vals.iloc[0]   # primer valor tras ordenar desc => registro más reciente
                else:
                    isp_label = "N/A"
            else:
                isp_label = "N/A"

            columnas_finales = [c for c in columnas_mostrar if c in df_sonda.columns]

            # --- Acordeón abierto por defecto
            with st.expander(f"📡 Sonda {sonda} | ISP: {isp_label} ({len(df_sonda)} registros)", expanded=False):
                st.dataframe(
                    df_sonda[columnas_finales],
                    use_container_width=True,
                    height=350,
                )


# ===========================================================
# 🗺️ MAPAS POR ISP (colores fijos por operador)
# ===========================================================
st.markdown("#### 🗺️ Samples Map by ISP")

if not df.empty and all(c in df.columns for c in ["latitude", "longitude", "isp"]):
    df_plot = df.copy()
    df_plot["latitude"] = pd.to_numeric(df_plot["latitude"], errors="coerce")
    df_plot["longitude"] = pd.to_numeric(df_plot["longitude"], errors="coerce")
    df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])

    if not df_plot.empty:
        lat_range = df_plot["latitude"].max() - df_plot["latitude"].min()
        lon_range = df_plot["longitude"].max() - df_plot["longitude"].min()

        if lat_range < 0.1 and lon_range < 0.1:
            zoom_default = 10
        elif lat_range < 1 and lon_range < 1:
            zoom_default = 10
        elif lat_range < 5 and lon_range < 5:
            zoom_default = 10
        else:
            zoom_default = 10

        zoom_global = st.sidebar.slider("🔍 Zoom general mapas", 3, 15, int(zoom_default))

        # 🎨 Colores fijos por operador (agrega o ajusta según tus ISPs)
        color_map = {
            "t-mobile_us": "#E20074",    # Magenta
            "att_us": "#00A8E0",        # Azul
            "verizon_wireless_us": "#ff0000",     # Rojo
            "Claro": "#D52B1E",       # Rojo intenso
            "Movistar": "#00A9E0",    # Celeste
            "Liberty": "#6F2DA8",     # Púrpura
            "Kolbi": "#009739",       # Verde
            "Dish": "#FF6600",        # Naranja
        }

        default_color = "#666666"  # Gris por si aparece un ISP no definido

        isps = sorted(df_plot["isp"].unique())
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
                color_isp = color_map.get(isp, default_color)

                fig = px.scatter_mapbox(
                    df_isp,
                    lat="latitude",
                    lon="longitude",
                    hover_name="isp",
                    hover_data=[c for c in ["city", "provider", "subtechnology", "program"] if c in df_isp.columns],
                    color_discrete_sequence=[color_isp],
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

















