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
st.markdown("## 📊 Dashboard Verveba Mobile")

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
    while True:
        st.info(f"📡 Descargando página {pagina}...")
        r = requests.post(url, headers=headers, json=body)
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
        body["pagination_data"] = next_data
        pagina += 1
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
# 📊 TABLA RESUMEN DE ESTADO DE SONDA
# ===========================================================
st.markdown("### 📡 Estado de sondas")

if "df" in st.session_state and not st.session_state.df.empty:
    df_resumen = st.session_state.df.copy()
    col_probe = next((c for c in ["probe", "probe_id", "probeId"] if c in df_resumen.columns), None)
    col_time = next((c for c in ["dateStart", "timestamp", "createdAt"] if c in df_resumen.columns), None)
    col_isp = next((c for c in ["isp", "provider"] if c in df_resumen.columns), None)
    if col_probe and col_time:
        df_resumen[col_time] = pd.to_datetime(df_resumen[col_time], errors="coerce", utc=True).dt.tz_convert(zona_local)
        df_resumen = df_resumen.dropna(subset=[col_time])
        df_last = df_resumen.sort_values(by=col_time).groupby(col_probe).tail(1).reset_index(drop=True)
        now_local = datetime.now(zona_local)
        df_last["minutes_since"] = (now_local - df_last[col_time]).dt.total_seconds() / 60
        df_last["Estado"] = df_last["minutes_since"].apply(lambda x: "🟢 ON" if x <= 20 else "🔴 OFF")
        df_show = df_last.rename(columns={col_probe: "Sonda", col_isp: "ISP", col_time: "Último reporte"})
        df_show["Último reporte"] = df_show["Último reporte"].dt.strftime('%Y-%m-%d %H:%M:%S')
        df_show = df_show.sort_values(by=["Estado", "Último reporte"], ascending=[False, False])
        st.dataframe(df_show[["Sonda", "ISP", "Último reporte", "Estado"]], use_container_width=True, height=300)
else:
    st.info("👈 Ejecuta la consulta para mostrar el resumen de sondas.")

# ===========================================================
# 📋 TABLAS POR SONDA
# ===========================================================
st.markdown("### 📋 Resultados por Sonda")

if df.empty:
    st.warning("⚠️ Aún no hay datos cargados. Usa el botón 'Consultar API'.")
else:
    columnas_fijas = ["probeId", "isp", "dateStart", "test", "latitude", "longitude", "success"]
    columnas_extra = [c for c in df.columns if c not in columnas_fijas]
    columnas_adicionales = st.multiselect("Columnas adicionales", options=columnas_extra, default=[])
    columnas_mostrar = columnas_fijas + columnas_adicionales
    col_probe = next((c for c in ["probe", "probe_id", "probeId"] if c in df.columns), None)
    if col_probe:
        for sonda in sorted(df[col_probe].dropna().unique()):
            df_sonda = df[df[col_probe] == sonda].copy()
            if "dateStart" in df_sonda.columns:
                df_sonda["dateStart"] = pd.to_datetime(df_sonda["dateStart"], errors="coerce")
                df_sonda = df_sonda.sort_values("dateStart", ascending=False)
            columnas_finales = [c for c in columnas_mostrar if c in df_sonda.columns]
            with st.expander(f"📡 Sonda {sonda} ({len(df_sonda)} registros)", expanded=True):
                st.dataframe(df_sonda[columnas_finales], use_container_width=True, height=350)
