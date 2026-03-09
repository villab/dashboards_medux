import streamlit as st
import pandas as pd
import plotly.express as px
import requests
from datetime import datetime, timedelta, time
import pytz
from streamlit_autorefresh import st_autorefresh
import time

#-------------------------------------
#Diccionario ISP

ISP_NAME_MAP = {
    "altice": "Altice",
    "claro do": "Claro",
    "viva": "Viva",
}

#-----------Definicion KPIs para tabla resumen------

KPI_DEFINITION = {
    "cloud-download": {
        "speedDl": "Download Speed (Mbps)"
    },
    "cloud-upload": {
        "speedUl": "Upload Speed (Mbps)"
    },
    "ping-test": {
        "avgLatency": "Average Latency (ms)",
        "jitter": "Jitter (ms)",
        "packetLoss": "Packet Loss (%)"
    },
    "voice-out": {
        "callSetUpTimeL3": "Call setup time (ms)",
        "callSetUpSuccessL3": "Call setup success (%)"
    },
    "confess-chrome": {
        "loadingTime": "Loading time (ms)"
    },
    "youtube-test": {
        "avgVideoResolution": "Video resolution (p)",
        "bufferingTime": "Buffering time (ms)",
        "speedDl": "Youtube Speed DL (Mbps)"
    }
}

# ===========================================================
# 🧠 CONFIGURACIÓN INICIAL
# ===========================================================
st.set_page_config(page_title="Medux Monitoring Dashboard", layout="wide")
st.markdown("### INDOTEL 2026 - PROBES MONITOR")

# ===========================================================
# 🔐 TOKEN Y PROBES DESDE SECRETS
# ===========================================================
st.sidebar.caption("API Setup auto-mode")
try:
    token = st.secrets["token"]
    probes = st.secrets["ids"]
    st.sidebar.caption(f"Token & {len(probes)} probes from secrets")
except Exception as e:
    st.caption("❌ No se pudo cargar token o sondas desde secrets.")
    st.exception(e)
    st.stop()
# ----------------------------------------------------------
# Selector Backpacks
#-----------------------------------------------------------

st.sidebar.markdown("---")
st.sidebar.header("Backpack Selector")

backpack_option = st.sidebar.radio(
    "Show data from:",
    ["Backpack 1", "Backpack 2", "Both"],
    index=2  # Ambos por defecto
)

# ===========================================================
# ⚙️ PARÁMETROS DE CONSULTA
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Types of tests")

programas = st.sidebar.multiselect(
    "Select tests",
    ["confess-chrome", "youtube-test", "ping-test", "network", "voice-out", "cloud-download", "cloud-upload"],
    default=["confess-chrome", "youtube-test", "ping-test", "voice-out", "cloud-download", "cloud-upload"]
)

# ===========================================================
# 🌍 TIME ZONE (Selector)
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Time Zone")

tz_map = {
    "Santo Domingo" : "America/Santo_Domingo",
    "UTC": "UTC",
}

tz_label = st.sidebar.selectbox(
    "Date time zone",
    list(tz_map.keys()),
    index=0  # Los Angeles por defecto
)

zona_local = pytz.timezone(tz_map[tz_label])

# ===========================================================
# ⏱️ ACTUALIZACIÓN EN TIEMPO REAL
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Automatic Update")

refresh_seconds = st.sidebar.slider("refresh frequency (seconds)", 10, 300, 30)
usar_real_time = st.sidebar.checkbox("Turn realtime mode on", value=True)

if usar_real_time:
    st_autorefresh(interval=refresh_seconds * 1000, key="real_time_refresh")

# ===========================================================
# 📅 RANGO MANUAL DE FECHAS
# ===========================================================
ahora_local = datetime.now(zona_local)
inicio_defecto_local = ahora_local - timedelta(days=1)

st.sidebar.markdown("---")
st.sidebar.header("Date")

if "fecha_inicio" not in st.session_state:
    ahora_local = datetime.now(zona_local)
    inicio_defecto_local = ahora_local - timedelta(days=1)

    st.session_state.fecha_inicio = inicio_defecto_local.date()
    st.session_state.hora_inicio = inicio_defecto_local.time()
    st.session_state.fecha_fin = ahora_local.date()
    st.session_state.hora_fin = ahora_local.time()


fecha_inicio = st.sidebar.date_input(
    "Date Start",
    key="fecha_inicio"
)

hora_inicio = st.sidebar.time_input(
    "Start hour",
    key="hora_inicio"
)

fecha_fin = st.sidebar.date_input(
    "Date End",
    key="fecha_fin"
)

hora_fin = st.sidebar.time_input(
    "End Hour",
    key="hora_fin"
)


# ===========================================================
# CALCULAR TIMESTAMPS
# ===========================================================
REALTIME_HOURS = 3

if usar_real_time:
   
    ts_end = int(datetime.now(pytz.utc).timestamp() * 1000)
    ts_start = int(
        (datetime.now(pytz.utc) - timedelta(hours=REALTIME_HOURS))
        .timestamp() * 1000
    )

    st.sidebar.caption(
        f"Realtime mode ON (last {REALTIME_HOURS}h, refresh {refresh_seconds}s)"
    )
else:
    # --- construir datetimes naive ---
    dt_inicio_naive = datetime.combine(fecha_inicio, hora_inicio)
    dt_fin_naive = datetime.combine(fecha_fin, hora_fin)
    
    # --- localizarlos de forma SEGURA ---
    dt_inicio_local = zona_local.localize(dt_inicio_naive, is_dst=None)
    dt_fin_local = zona_local.localize(dt_fin_naive, is_dst=None)
    
    # --- validación estricta ---
    if dt_inicio_local >= dt_fin_local:
        st.error(
            f"⚠️ Invalid datetime range\n"
            f"Start: {dt_inicio_local}\n"
            f"End: {dt_fin_local}"
        )
        st.stop()


    ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)

    st.sidebar.caption("📅 Manual datetime range active")


# Mostrar rango activo
st.sidebar.markdown("### Active Query")
inicio_local_str = datetime.fromtimestamp(ts_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
fin_local_str = datetime.fromtimestamp(ts_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
st.sidebar.write(f"Start : {inicio_local_str}")
st.sidebar.write(f"End : {fin_local_str}")

# ===========================================================
# CONFIGURACIÓN API
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

    # Asegurar que se habilite la paginación
    payload = body.copy()
    payload["paginate"] = True

    pit = None
    search_after = None

    while True:
        if pit:
            payload["pit"] = pit
        if search_after:
            payload["search_after"] = search_after

        st.info(f" Downloading page {pagina}...")
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            st.error(f"❌ Error API: {r.status_code}")
            break

        data = r.json()

        # Extraer resultados
        results = data.get("results", {})
        if isinstance(results, list):
            todos_los_resultados.setdefault("network", []).extend(results)
            total += len(results)
        elif isinstance(results, dict):
            for prog, res in results.items():
                if isinstance(res, list):
                    todos_los_resultados.setdefault(prog, []).extend(res)
                    total += len(res)

        st.write(f"📄 Página {pagina}: {len(results) if isinstance(results, list) else sum(len(v) for v in results.values())} registros")

        # Actualizar cursores de paginación
        pit = data.get("pit")
        search_after = data.get("search_after")

        # Si no hay más cursores, terminamos
        if not search_after or not pit:
            st.success(f"✅ Download complete: {total:,} registers in {pagina} pages.")
            break

        pagina += 1
        if pagina > 100:  # seguridad
            st.warning("⚠️ limit max of 100 pages.")
            break

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

    # 🔹 Conversión segura de fechas
    
    COLUMNAS_FECHA = ["dateStart", "dateEnd"]
    
    for col in COLUMNAS_FECHA:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert(zona_local)


    return df
    

def filtrar_por_backpack(df, opcion, col_probe):
    if opcion == "Both":
        return df

    secretos = st.secrets
    key = "Backpack_1" if opcion == "Backpack 1" else "Backpack_2"

    if key not in secretos:
        return df

    sondas = [str(x) for x in secretos[key]]
    return df[df[col_probe].astype(str).isin(sondas)]


def resumen_kpis_por_isp(df, kpi_def, isp_map=None):
    filas = []

    for test, metrics in kpi_def.items():
        df_test = df[df["test"] == test]

        if df_test.empty:
            continue

        for field, label in metrics.items():
            if field not in df_test.columns:
                continue

            df_test[field] = pd.to_numeric(df_test[field], errors="coerce")
            
            # Convertir a porcentaje SOLO para call setup success
            if field == "callSetUpSuccessL3":
                df_test[field] = df_test[field] * 100

            resumen = (
                df_test
                .dropna(subset=["isp", field])
                .groupby("isp")[field]
                .mean()
            )

            for isp, value in resumen.items():
                filas.append({
                    "KPI": label,
                    "ISP": isp_map.get(isp, isp) if isp_map else isp,
                    "Value": value
                })

    df_out = pd.DataFrame(filas)

    if df_out.empty:
        return df_out

    return (
        df_out
        .pivot(index="KPI", columns="ISP", values="Value")
        .reset_index()
    )


# ===========================================================
# 🚀 CONSULTAR API
# ===========================================================

if "last_fetch_ts" not in st.session_state:
    st.session_state.last_fetch_ts = 0.0

if "df" not in st.session_state:
    st.session_state.df = pd.DataFrame()

now = time.time()

manual_trigger = st.sidebar.button("🚀 Consultar API")

time_trigger = (
    usar_real_time and
    (now - st.session_state.last_fetch_ts >= refresh_seconds)
)

should_fetch = manual_trigger or time_trigger

if should_fetch:
    raw = (
        obtener_datos_pag_no_cache(url, headers, body)
        if usar_real_time
        else obtener_datos_pag(url, headers, body)
    )

    if not raw:
        st.warning("⚠️ No se recibieron datos de la API.")
        st.stop()

    df = flatten_results(raw)

    if df.empty:
        st.warning("⚠️ No se recibieron datos.")
        st.stop()

    st.session_state.df = df
    st.session_state.last_fetch_ts = now


    st.markdown(
        f"<span style='font-size:0.9em; color:gray;'> Data load successfull ({len(df):,} rows)</span>",
        unsafe_allow_html=True
    )
else:
    df = st.session_state.df

# ===========================================================
# 📡 Probes Status dividido por Backpack 
# ===========================================================
st.subheader("Probes Status")

if "df" not in st.session_state or st.session_state.df.empty:
    st.info("👈 Ejecuta la consulta para mostrar el resumen de sondas.")
else:
    df_resumen = st.session_state.df.copy()

    # detectar columnas
    col_probe = next((c for c in ["probe", "probe_id", "probeId", "probes_id"] if c in df_resumen.columns), None)
    col_time = next((c for c in ["dateStart", "timestamp", "createdAt", "datetime"] if c in df_resumen.columns), None)
    col_isp = next((c for c in ["isp", "provider", "network"] if c in df_resumen.columns), None)

    if not (col_probe and col_time):
        st.warning("⚠️ No se encontraron columnas de sonda o tiempo en los datos.")
    else:
        # --- Cargar grupos desde secrets ---
        secretos = st.secrets if hasattr(st, "secrets") else {}
        grupos = {}
        if isinstance(secretos.get("grupos_sondas"), dict):
            grupos = secretos.get("grupos_sondas")
        else:
            if secretos.get("Backpack_1"):
                grupos["Backpack_1"] = secretos.get("Backpack_1")
            if secretos.get("Backpack_2"):
                grupos["Backpack_2"] = secretos.get("Backpack_2")

        if not grupos:
            st.info("ℹ️ No se encontraron grupos (Backpack_1 / Backpack_2) en secrets.")
        else:
            # --- Convertir fechas a zona horaria Las Vegas ---
            s_dates = df_resumen[col_time].astype(str)
            tiene_utc_suffix = s_dates.str.contains(r'Z$|\+00:00$', regex=True).any()

            if tiene_utc_suffix:
                df_resumen[col_time] = pd.to_datetime(df_resumen[col_time], errors="coerce", utc=True).dt.tz_convert(zona_local)
            else:
                parsed = pd.to_datetime(df_resumen[col_time], errors="coerce")
                if hasattr(parsed.dt, "tz") and parsed.dt.tz is None:
                    df_resumen[col_time] = parsed.dt.tz_localize(zona_local)
                else:
                    df_resumen[col_time] = parsed.dt.tz_convert(zona_local)

            df_resumen = df_resumen.dropna(subset=[col_time])
            df_last = df_resumen.sort_values(by=col_time).groupby(col_probe).tail(1).reset_index(drop=True)

            # --- Calcular estado ON/OFF ---
            now_local = datetime.now(zona_local)
            if df_last[col_time].dt.tz is None:
                df_last[col_time] = df_last[col_time].dt.tz_localize(zona_local)
            df_last["minutes_since"] = (now_local - df_last[col_time]).dt.total_seconds() / 60
            df_last["Estado"] = df_last["minutes_since"].apply(lambda x: "🟢 ON" if x <= 20 else "🔴 OFF")

            # --- Formatear tabla base ---
            columnas = [col_probe, col_isp, col_time, "Estado"]
            columnas_presentes = [c for c in columnas if c in df_last.columns]
            df_last_present = df_last[columnas_presentes].rename(
                columns={col_probe: "Sonda", col_isp: "ISP", col_time: "Último reporte"}
            )

            # --- Normalizar y convertir zona horaria ---
            df_last_present["Último reporte"] = pd.to_datetime(df_last_present["Último reporte"], errors="coerce")
            if df_last_present["Último reporte"].dt.tz is None:
                df_last_present["Último reporte"] = df_last_present["Último reporte"].dt.tz_localize(zona_local)
            df_last_present["Último reporte"] = df_last_present["Último reporte"].dt.tz_convert(zona_local).dt.strftime('%Y-%m-%d %H:%M:%S')

     
            df_last_present["ISP"] = df_last_present["ISP"].replace(ISP_NAME_MAP)

            # --- Crear dos columnas para mostrar tablas lado a lado ---
            col1, col2 = st.columns(2)
            # 🔧 Normalizar IDs de Backpacks a STRING para evitar mismatch
            grupos = {
                nombre: [str(x) for x in lista]
                for nombre, lista in grupos.items()
            }            
            grupos_orden = list(grupos.items())[:2]  # Backpack_1 y Backpack_2

            for idx, (nombre_grupo, lista_sondas) in enumerate(grupos_orden):
                nombre_vis = str(nombre_grupo).replace("_", " ")
            
                # 🔹 Filtrar solo sondas que realmente tienen datos en df_last_present
                sondas_presentes = df_last_present["Sonda"].unique().tolist()
                sondas_con_datos = [s for s in lista_sondas if s in sondas_presentes]
            
                df_grupo = (
                    df_last_present[df_last_present["Sonda"].isin(sondas_con_datos)]
                    .dropna(subset=["Sonda", "Último reporte"])
                    .copy()
                )
            
                # 🔹 Eliminar filas vacías o con columnas sin valor visible
                df_grupo = df_grupo[df_grupo["Sonda"].notna() & (df_grupo["Sonda"] != "")]
                df_grupo = df_grupo[df_grupo["Estado"].notna() & (df_grupo["Estado"] != "")]
                df_grupo = df_grupo.reset_index(drop=True)
            
                # 🔹 Mostrar tabla en columna correspondiente
                with (col1 if idx == 0 else col2):
                    st.markdown(f"#### {nombre_vis} ({len(df_grupo)} active probes)")
                    if df_grupo.empty:
                        st.info(f"ℹ️ No hay datos disponibles para **{nombre_vis}**.")
                    else:
                        num_filas = len(df_grupo)
                        # Ajustar altura de acuerdo a la cantidad de filas (cada fila ≈ 35 px, margen mínimo 150 px)
                        altura_tabla = max(150, num_filas * 35)

                        df_tabla = df_grupo[["Estado", "Sonda", "ISP", "Último reporte"]].rename(
                            columns={
                                "Estado": "Status",
                                "Sonda": "Probe ID",
                                "ISP": "Operator",
                                "Último reporte": "Last Report"
                            }
                        )


                        
                        st.dataframe(
                            df_tabla,
                            use_container_width=True,
                            hide_index=True,
                            height=len(df_grupo) * 48 + 38,
                        

                        )





# ===========================================================
# 📊 TABLAS POR SONDA (acordeones abiertos + columnas fijas + selector opcional)
# ===========================================================

st.markdown("### 📋 Probes Results")

if "df" not in st.session_state or st.session_state.df.empty:
    st.warning("⚠️ Aún no hay datos cargados. Usa el botón 'Consultar API'.")
else:
    df = st.session_state.df.copy()

    # --- 🔹 Columnas fijas (siempre visibles)
    columnas_fijas = ["probeId", "isp", "dateStart", "test", "latitude", "longitude", "success", "subtechnology","technology","speedDl","speedUl","avgLatency"]  # puedes ajustar las fijas aquí

    # --- 🔹 Detectar columnas adicionales disponibles
    columnas_extra = [c for c in df.columns if c not in columnas_fijas]

    # --- 🔹 Selector de columnas adicionales
    columnas_adicionales = st.multiselect(
        "Additional columns",
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

        # ====== AGRUPAR SONDA POR BACKPACK ======
        grupos = {
            "Backpack 1": st.secrets["Backpack_1"],
            "Backpack 2": st.secrets["Backpack_2"],
        }
        
        for nombre_grupo, lista_sondas in grupos.items():
            # Crear sección principal
            st.markdown(f"### {nombre_grupo}")
        
            # Filtrar solo sondas que existan en la data recibida
            sondas_en_data = [s for s in lista_sondas if s in sondas]
        
            if len(sondas_en_data) == 0:
                st.warning(f"⚠️ No hay datos para sondas de {nombre_grupo}")
                continue
        
            # Crear un expander por cada sonda
            for sonda in sondas_en_data:
                df_sonda = df[df[col_probe] == sonda].copy()
        
                if "dateStart" in df_sonda.columns:
                    if not pd.api.types.is_datetime64_any_dtype(df_sonda["dateStart"]):
                        df_sonda["dateStart"] = pd.to_datetime(
                            df_sonda["dateStart"],
                            errors="coerce",
                            utc=True
                        ).dt.tz_convert(zona_local)
                
                    df_sonda = df_sonda.sort_values("dateStart", ascending=False)

        
                # Obtener ISP del registro más reciente
                if col_isp in df_sonda.columns and not df_sonda.empty:
                    isp_vals = df_sonda[col_isp].dropna().astype(str)
                    isp_label = isp_vals.iloc[0] if not isp_vals.empty else "N/A"
                else:
                    isp_label = "N/A"
        
                columnas_finales = [c for c in columnas_mostrar if c in df_sonda.columns]
                
                # 🔒 FIX 2 — Evitar epoch falso (1969-12-31) en columnas adicionales
                for c in columnas_finales:
                    if c in df_sonda.columns and pd.api.types.is_datetime64_any_dtype(df_sonda[c]):
                        df_sonda[c] = df_sonda[c].where(df_sonda[c].notna(), "")

                with st.expander(f"📡 Probe {sonda} | ISP: {isp_label} ({len(df_sonda)} tests)", expanded=False):
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
    
    # ❌ Eliminar NaN y coordenadas inválidas (0,0)
    df_plot = df_plot.dropna(subset=["latitude", "longitude", "isp"])
    df_plot = df_plot[
        ~((df_plot["latitude"] == 0) & (df_plot["longitude"] == 0))
    ]


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

        zoom_global = st.sidebar.slider("Global zoom map", 3, 15, int(zoom_default))

        # 🎨 Colores fijos por operador (agrega o ajusta según tus ISPs)
        color_map = {
            "altice": "#1260F0",    
            "claro do": "#DC0612",        
            "viva": "#94C915",     
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

#------------------------------------------########
#--------------GRAFICA DE KPIS POR ISP

def grafica_kpi(df, y_field, titulo, freq="5min", agg_func="mean", color_by="isp"):
    # 1. VALIDACIÓN RADICAL
    if df is None or df.empty:
        return
    
    # Asegurar que las columnas existen y no tienen nulos en la categoría
    if y_field not in df.columns or "dateStart" not in df.columns or color_by not in df.columns:
        return

    df_plot = df.copy()
    
    # 2. LIMPIEZA DE TIPOS (Evita el TypeError de Plotly)
    # Forzamos que la columna de color sea SIEMPRE string y no tenga nulos
    df_plot[color_by] = df_plot[color_by].fillna("Unknown").astype(str)
    
    # Fechas
    df_plot["dateStart"] = pd.to_datetime(df_plot["dateStart"], errors="coerce")
    if df_plot["dateStart"].dt.tz is not None:
        df_plot["dateStart"] = df_plot["dateStart"].dt.tz_localize(None)
    
    # KPI numérico
    df_plot[y_field] = pd.to_numeric(df_plot[y_field], errors="coerce")
    
    # Quitar filas donde el KPI o la Fecha fallaron
    df_plot = df_plot.dropna(subset=["dateStart", y_field])

    if df_plot.empty:
        return

    # 3. AGRUPACIÓN SEGURA
    try:
        # Redondeo manual para agrupar puntos
        df_plot["time_idx"] = df_plot["dateStart"].dt.floor(freq)
        
        # Agrupamos por color y tiempo
        df_agg = (
            df_plot.groupby([color_by, "time_idx"], as_index=False)[y_field]
            .mean()
            .rename(columns={"time_idx": "dateStart"})
            .sort_values("dateStart")
        )
    except:
        # Si falla el agrupamiento, usamos los datos limpios directamente
        df_agg = df_plot.sort_values("dateStart")

    # 4. MAPA DE COLORES DINÁMICO
    # Si es ISP usamos los tuyos, si es Target dejamos que Plotly elija pero sin romperse
    color_map_final = {
        "altice": "#1260F0", "Altice": "#1260F0",
        "claro do": "#DC0612", "Claro": "#DC0612",
        "viva": "#94C915", "Viva": "#94C915"
    }
    
    # Solo aplicamos el mapa si la columna es de ISP
    use_map = color_map_final if color_by.lower() in ["isp", "provider", "operator"] else None

    # 5. CONSTRUCCIÓN DEL GRÁFICO (Cuidado con los parámetros)
    try:
        fig = px.line(
            df_agg,
            x="dateStart",
            y=y_field,
            color=color_by,
            markers=True,
            title=titulo,
            color_discrete_map=use_map,
            template="plotly_white",
            # Esto previene el error m.val_map[val] al forzar el orden de las leyendas
            category_orders={color_by: sorted(df_agg[color_by].unique().tolist())}
        )

        # 6. ESTÉTICA Y CONECTIVIDAD
        fig.update_traces(connectgaps=True)
        
        # Traducir leyendas de ISP si aplica
        if color_by.lower() in ["isp", "provider"]:
            fig.for_each_trace(lambda t: t.update(
                name=ISP_NAME_MAP.get(t.name.lower(), t.name)
            ))

        fig.update_layout(
            xaxis_title="Tiempo",
            yaxis_title=y_field,
            hovermode="x unified",
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5, title_text="")
        )
        
        # Ajuste de escala
        if "Success" in titulo or "Success" in y_field:
            fig.update_yaxes(range=[0, 105])
        else:
            fig.update_yaxes(rangemode="tozero")

        st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.error(f"Error renderizando {titulo}: {e}")
# Filtrar dataframe según Backpack seleccionado
df_kpi = filtrar_por_backpack(df, backpack_option, col_probe)

if "df" not in st.session_state or st.session_state.df.empty:
    st.info("👈 Consulta primero la API para mostrar las gráficas KPI.")
else:
    df_kpi = filtrar_por_backpack(st.session_state.df, backpack_option, col_probe)

    # ================== Velocidad  ==================
    st.header("Speed Performance")  # Más grande

    df_dl = df_kpi[df_kpi["test"] == "cloud-download"]
    if not df_dl.empty:
        grafica_kpi(df_dl, "speedDl", "Download Speed (Mbps)")

    df_ul = df_kpi[df_kpi["test"] == "cloud-upload"]
    if not df_ul.empty:
        grafica_kpi(df_ul, "speedUl", "Upload Speed (Mbps)")

    # ================== Ping ==================
    st.header("Ping")

    df_ping = df_kpi[df_kpi["test"] == "ping-test"]
    if not df_ping.empty:
        grafica_kpi(df_ping, "avgLatency", "Average Latency (ms)")
        grafica_kpi(df_ping, "jitter", "Jitter (ms)")
        grafica_kpi(df_ping, "packetLoss", "Packet Loss (%)")

    # ================== Web Browsing ==================

    st.header("Web Browsing")
    
    df_confess = df_kpi[df_kpi["test"] == "confess-chrome"]
    if not df_confess.empty:
    
        # Gráfica general (por ISP)
        grafica_kpi(df_confess, "loadingTime", "Loading time (ms)")
    
    st.subheader("Loading time by target per operator")
    
    isps = sorted(df_confess["isp"].unique())
    cols = st.columns(len(isps))
    
    for col, isp in zip(cols, isps):
        with col:
            # 🔹 Nombre bonito reutilizando el mapa
            isp_label = ISP_NAME_MAP.get(isp, isp)
    
            df_isp = df_confess[df_confess["isp"] == isp]
    
            grafica_kpi(
                df_isp,
                "loadingTime",
                isp_label,   # 👈 SOLO el nombre bonito como título
                color_by="target"
            )




    #------------------ Voice Out ----------------------
    st.header("Voice")

    df_voice = df_kpi[df_kpi["test"] == "voice-out"]
    if not df_voice.empty:
        grafica_kpi(df_voice, "callSetUpTimeL3", "Call set up time (ms)")
    
        grafica_kpi(
            df_voice,
            "callSetUpSuccessL3",
            "Call set up success"
        )

# ================== Voice Out ==================
    st.header("Voice")
    
    df_voice = df_kpi[df_kpi["test"] == "voice-out"]
    if not df_voice.empty:
        # 1. Grafica de tiempo (ms) - Se queda igual
        grafica_kpi(df_voice, "callSetUpTimeL3", "Call set up time (ms)")
        
        # 2. Grafica de éxito (%) - AJUSTE AQUÍ MISMO
        df_voice_pct = df_voice.copy()
        
        # Solo multiplicamos si los datos vienen en formato 0-1
        if df_voice_pct["callSetUpSuccessL3"].max() <= 1.0:
            df_voice_pct["callSetUpSuccessL3"] = df_voice_pct["callSetUpSuccessL3"] * 100
            
        grafica_kpi(
            df_voice_pct, 
            "callSetUpSuccessL3", 
            "Call set up success (%)" # Cambiamos el título para que la función sepa que es porcentaje
        )

    # ================== Streaming ==================
    st.header("Streaming")

    df_youtube = df_kpi[df_kpi["test"] == "youtube-test"]
    if not df_youtube.empty:
        grafica_kpi(df_youtube, "avgVideoResolution", "Video resolution (p)")
        grafica_kpi(df_youtube, "speedDl", "Youtube Speed DL (Mbps)")
        grafica_kpi(df_youtube, "bufferingTime", "Buffering Time (ms)")


    st.header("KPI Summary by Operator")

    df_summary = resumen_kpis_por_isp(
        df_kpi,
        KPI_DEFINITION,
        isp_map=ISP_NAME_MAP
    )
    
    if df_summary.empty:
        st.info("ℹ️ No hay datos suficientes para generar el resumen.")
    else:
        st.dataframe(
            df_summary,
            use_container_width=True,
            #hide_index=True
            height=450
        )
    






