"""
Medux Monitoring Dashboard - Vista por Poligonos (Distritos de Costa Rica)
===========================================================================
Portal independiente que reutiliza la misma conexion a la API MedUX IDS del
dashboard principal, pero ubica cada resultado dentro de su poligono
(distrito) usando la capa WFS del Instituto Geografico Nacional (IGN):

    URL WFS   : https://geos.snitcr.go.cr/be/IGN_5_CO/wfs
    Capa      : IGN_5_CO:limitedistrital_5k  (Limite Distrital, 1:5.000)
    CRS nativo: EPSG:8908 (CR-SIRGAS / CRTM05, metros)
    Atributos : PROVINCIA, CANTON, DISTRITO, CODIGO_DTA, ...

Salidas principales:
    1) Mapa choropleth de distritos coloreados por cantidad de pruebas
       (con opcion de ver las muestras individuales encima).
    2) Tabla de conteo de pruebas por Distrito x Program x ISP.

Requisitos adicionales sobre el dashboard original (agregar a requirements.txt):
    shapely>=2.0
    pyproj
    folium
    branca

Notas de rendimiento (ver seccion "OPTIMIZACION"):
    - Los poligonos se simplifican y se cachean 24h (no se recalculan en cada rerun).
    - El spatial join (punto-en-poligono) solo corre una vez por consulta nueva a la
      API, no en cada rerun/click.
    - El mapa se dibuja como UNA sola capa GeoJson (494 features en un solo layer)
      en vez de 494 capas individuales, y se renderiza con components.html en vez
      de streamlit-folium (evita el puente bidireccional que agrega latencia).
"""

import time
from datetime import datetime, timedelta

import pandas as pd
import pytz
import requests
import streamlit as st
import streamlit.components.v1 as components
import folium
import branca.colormap as cm
from shapely.geometry import shape, Point, mapping
from shapely.strtree import STRtree
from shapely.ops import transform as shapely_transform
from pyproj import Transformer

# ===========================================================
# CONFIGURACION WFS (poligonos de distritos)
# ===========================================================
WFS_URL = "https://geos.snitcr.go.cr/be/IGN_5_CO/wfs"
WFS_LAYER = "IGN_5_CO:limitedistrital_5k"
WFS_SRS_NATIVE = "EPSG:8908"   # CR-SIRGAS / CRTM05 (metros)
WFS_SRS_OUTPUT = "EPSG:4326"   # WGS84 lat/lon (lo que trae la API MedUX)

# ===========================================================
# ISP (ajustar segun los codigos reales que devuelva tu perfil,
# ver endpoint /api/profile/isps o c.isps() de la skill sutel-api-extraction)
# ===========================================================
ISP_NAME_MAP = {
    "liberty_cr": "Liberty",
    "claro_cr": "Claro",
    "tigo_cr": "Tigo",
    "kolbi_cr": "Kolbi",
    "telecable_cr": "Telecable",
}
ISP_COLOR_MAP = {
    "Liberty": "#6F2DA8",
    "Claro": "#D52B1E",
    "Tigo": "#0033A0",
    "Kolbi": "#009739",
    "Telecable": "#FF6600",
}

# ===========================================================
# CONFIGURACION INICIAL STREAMLIT
# ===========================================================
st.set_page_config(page_title="Medux - Vista por Poligonos", layout="wide")
st.markdown("### COSTA RICA - RESULTADOS POR DISTRITO (Poligonos WFS / IGN)")

# ===========================================================
# TOKEN Y PROBES DESDE SECRETS
# ===========================================================
st.sidebar.caption("API Setup auto-mode")
try:
    token = st.secrets["token"]
    probes = st.secrets["ids"]
    st.sidebar.caption(f"Token & {len(probes)} sondas cargadas desde secrets")
except Exception as e:
    st.error("No se pudo cargar token o sondas desde secrets.")
    st.exception(e)
    st.stop()

# ===========================================================
# SIDEBAR - Tipos de prueba
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Tipos de prueba (programs)")
programas = st.sidebar.multiselect(
    "Selecciona programs",
    [
        "http-down-burst-test", "http-upload-burst-test", "ping-test", "network",
        "voice-out", "voice-polqa", "sms-mo",
    ],
    default=[
        "ping-test", "http-down-burst-test", "http-upload-burst-test",
        "voice-out", "voice-polqa", "sms-mo",
    ],
)

# ===========================================================
# SIDEBAR - Zona horaria
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Zona horaria")
tz_map = {
    "Costa Rica (CST)": "America/Costa_Rica",
    "UTC": "UTC",
}
tz_label = st.sidebar.selectbox("Zona horaria de fechas", list(tz_map.keys()), index=0)
zona_local = pytz.timezone(tz_map[tz_label])

# ===========================================================
# SIDEBAR - Actualizacion en tiempo real
# ===========================================================
# NOTA: se elimino el auto-refresh automatico (tanto el componente externo
# streamlit-autorefresh, que fallaba en cargar sus assets JS, como el
# <meta http-equiv="refresh">, que provoca recargas COMPLETAS de pagina no
# cancelables -- si la app esta embebida en un iframe/portal, la sesion se
# puede perder en cada recarga y el checkbox vuelve a su valor por defecto,
# generando un loop de refresco infinito e imposible de apagar desde la UI).
# El flujo ahora es manual: boton "Consultar API" para traer datos nuevos.
st.sidebar.markdown("---")
st.sidebar.header("Actualizacion")
refresh_seconds = st.sidebar.slider(
    "Antiguedad maxima antes de re-consultar (segundos)", 10, 300, 60,
    help="Con 'Modo tiempo real' activo, el boton 'Consultar API' vuelve a "
         "traer datos si la ultima consulta tiene mas de este tiempo. No hay "
         "recarga automatica de pagina: hay que hacer clic en el boton.",
)
usar_real_time = st.sidebar.checkbox("Modo tiempo real", value=True)
if usar_real_time:
    st.sidebar.caption(
        "Sin recarga automatica de pagina. Hace clic en 'Consultar API' "
        "para traer datos frescos (o vuelve a traerlos solos si ya paso el "
        "tiempo configurado arriba y interactuas con algo en la app)."
    )

REALTIME_HOURS = 8

# ===========================================================
# SIDEBAR - Rango manual de fechas
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Fecha")
if "poly_fecha_inicio" not in st.session_state:
    ahora_local = datetime.now(zona_local)
    inicio_defecto_local = ahora_local - timedelta(days=1)
    st.session_state.poly_fecha_inicio = inicio_defecto_local.date()
    st.session_state.poly_hora_inicio = inicio_defecto_local.time()
    st.session_state.poly_fecha_fin = ahora_local.date()
    st.session_state.poly_hora_fin = ahora_local.time()

fecha_inicio = st.sidebar.date_input("Fecha inicio", key="poly_fecha_inicio")
hora_inicio = st.sidebar.time_input("Hora inicio", key="poly_hora_inicio")
fecha_fin = st.sidebar.date_input("Fecha fin", key="poly_fecha_fin")
hora_fin = st.sidebar.time_input("Hora fin", key="poly_hora_fin")

# ===========================================================
# CALCULAR TIMESTAMPS
# ===========================================================
if usar_real_time:
    ts_end = int(datetime.now(pytz.utc).timestamp() * 1000)
    ts_start = int((datetime.now(pytz.utc) - timedelta(hours=REALTIME_HOURS)).timestamp() * 1000)
    st.sidebar.caption(f"Modo tiempo real ON (ultimas {REALTIME_HOURS}h, refresco {refresh_seconds}s)")
else:
    dt_inicio_naive = datetime.combine(fecha_inicio, hora_inicio)
    dt_fin_naive = datetime.combine(fecha_fin, hora_fin)
    dt_inicio_local = zona_local.localize(dt_inicio_naive, is_dst=None)
    dt_fin_local = zona_local.localize(dt_fin_naive, is_dst=None)
    if dt_inicio_local >= dt_fin_local:
        st.error(f"Rango de fechas invalido.\nInicio: {dt_inicio_local}\nFin: {dt_fin_local}")
        st.stop()
    ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
    ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)
    st.sidebar.caption("Rango manual activo")

inicio_local_str = datetime.fromtimestamp(ts_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
fin_local_str = datetime.fromtimestamp(ts_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
st.sidebar.markdown("### Consulta activa")
st.sidebar.write(f"Inicio: {inicio_local_str}")
st.sidebar.write(f"Fin: {fin_local_str}")

st.sidebar.markdown("---")
st.sidebar.header("Limite de descarga")
limite_filas = st.sidebar.number_input(
    "Maximo de filas a traer (0 = sin limite)",
    min_value=0, max_value=2_000_000, value=50_000, step=10_000,
    help="Rangos de fecha muy amplios pueden tener cientos de miles de filas "
         "(cada pagina son ~10,000 y la API limita a ~1 peticion/seg, asi que "
         "traer todo puede tardar varios minutos). Este limite corta la "
         "descarga cuando se alcanza, mostrando un aviso.",
)

# ===========================================================
# CONFIGURACION API MEDUX
# ===========================================================
API_URL = "https://medux-ids.caseonit.com/api/results"
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
body = {
    "tsStart": ts_start,
    "tsEnd": ts_end,
    "format": "raw",
    "timezone": "America/Costa_Rica",
    "programs": programas,
    "probes": [str(p) for p in probes if pd.notna(p)],
}


def flatten_results(raw_json):
    """Aplana la respuesta anidada de /api/results en un DataFrame."""
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
    if "test" not in df.columns:
        df["test"] = df["program"]

    for col in ["dateStart", "dateEnd"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert(zona_local)
    return df


def _descargar_paginado(url, headers, body, debug=False, limite_filas=0):
    """Loop de paginacion PIT/search_after (doc oficial: paginate:true en la
    primera peticion; la respuesta trae next_pagination_data.pit/search_after;
    esos dos valores se reenvian tal cual, junto con paginate:true, hasta que
    ya no venga pit o la pagina llegue vacia).

    limite_filas: si es > 0, corta la descarga apenas se alcanza ese total
    (evita quedarse minutos trayendo cientos de miles de filas crudas para
    rangos de fecha muy amplios). 0 = sin limite.
    """
    todos_los_resultados = {}
    pagina = 1
    total_acumulado = 0
    total_reportado_api = None
    payload = body.copy()
    payload["paginate"] = True
    payload.setdefault("size", 10000)
    pit = None
    search_after = None

    diag = st.empty() if debug else None
    barra = st.progress(0, text="Descargando...") if debug else None

    while True:
        if pit:
            payload["pit"] = pit
        if search_after:
            payload["search_after"] = search_after

        # La API limita a ~1 req/s; el PIT caduca en 1 minuto, asi que se
        # pagina sin pausas largas entre peticiones.
        if pagina > 1:
            time.sleep(1.05)

        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            st.error(f"Error API en pagina {pagina}: {r.status_code} — {r.text[:500]}")
            break

        data = r.json()
        total_reportado_api = data.get("total", total_reportado_api)
        results = data.get("results", {})
        pagina_vacia = True
        filas_en_pagina = 0
        if isinstance(results, list):
            filas_en_pagina = len(results)
            if results:
                pagina_vacia = False
            todos_los_resultados.setdefault("network", []).extend(results)
        elif isinstance(results, dict):
            for prog, res in results.items():
                if isinstance(res, list):
                    filas_en_pagina += len(res)
                    if res:
                        pagina_vacia = False
                    todos_los_resultados.setdefault(prog, []).extend(res)
        total_acumulado += filas_en_pagina

        # El cursor de paginacion viene ANIDADO en "next_pagination_data".
        cursor = data.get("next_pagination_data") or {}
        pit = cursor.get("pit")
        search_after = cursor.get("search_after")

        if diag is not None:
            diag.caption(
                f"📥 Pagina {pagina}: {filas_en_pagina} filas "
                f"(acumulado {total_acumulado} / total API reportado: {total_reportado_api}). "
                f"¿vino cursor pit? {'si' if pit else 'NO'}"
            )
        if barra is not None and total_reportado_api:
            objetivo = min(total_reportado_api, limite_filas) if limite_filas else total_reportado_api
            frac = min(1.0, total_acumulado / objetivo) if objetivo else 1.0
            barra.progress(frac, text=f"{total_acumulado:,} / {objetivo:,} filas")

        if limite_filas and total_acumulado >= limite_filas:
            st.warning(
                f"⏹️ Se alcanzo el limite de {limite_filas:,} filas configurado en el sidebar "
                f"(la API reporta {total_reportado_api:,} filas en total para este rango). "
                f"Angosta el rango de fechas o sube el limite para traer todo."
            )
            break
        if pagina_vacia or not pit:
            break
        pagina += 1
        if pagina > 100:
            st.warning("Limite maximo de 100 paginas alcanzado.")
            break

    return todos_los_resultados


@st.cache_data(ttl=1800)
def obtener_datos_pag(url, headers, body, debug=False, limite_filas=0):
    """Descarga paginada completa (modo historico / cacheado 30 min)."""
    return _descargar_paginado(url, headers, body, debug=debug, limite_filas=limite_filas)


def obtener_datos_pag_no_cache(url, headers, body, debug=False, limite_filas=0):
    """Descarga paginada completa SIN cache (modo tiempo real: siempre trae
    todo el rango solicitado, no solo la primera pagina)."""
    return _descargar_paginado(url, headers, body, debug=debug, limite_filas=limite_filas)


# ===========================================================
# WFS - CARGA DE POLIGONOS DE DISTRITOS (cache 24h, no cambian seguido)
# ===========================================================
# 1 grado ~ 111,320 m cerca del ecuador (Costa Rica ~9-11N, el error de esta
# aproximacion es minimo). "tolerancia_m" se pasa como parametro para que el
# cache se invalide solo cuando cambia el nivel de detalle, no en cada rerun.
METROS_POR_GRADO = 111_320


@st.cache_data(ttl=60 * 60 * 24, show_spinner="Cargando poligonos de distritos (WFS)...")
def cargar_distritos_wfs(tolerancia_m=10):
    tolerancia_deg = (tolerancia_m / METROS_POR_GRADO) if tolerancia_m > 0 else 0
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": WFS_LAYER,
        "outputFormat": "application/json",
        "srsName": WFS_SRS_OUTPUT,  # se pide directamente en WGS84
    }
    r = requests.get(WFS_URL, params=params, timeout=90)
    r.raise_for_status()
    geojson = r.json()

    transformer = Transformer.from_crs(WFS_SRS_NATIVE, WFS_SRS_OUTPUT, always_xy=True)

    distritos = []
    for feat in geojson.get("features", []):
        props = feat.get("properties", {}) or {}
        geom = shape(feat["geometry"])

        # Salvaguarda: si el servidor NO reproyecto (coords fuera de rango lat/lon),
        # se reproyecta en el cliente desde el CRS nativo (EPSG:8908).
        minx, miny, maxx, maxy = geom.bounds
        if abs(minx) > 180 or abs(maxx) > 180 or abs(miny) > 90 or abs(maxy) > 90:
            geom = shapely_transform(transformer.transform, geom)

        # Version simplificada SOLO para dibujar (menos vertices = mapa mucho
        # mas liviano). Se precalcula aqui, una sola vez, y queda cacheada.
        # tolerancia_deg == 0 -> se usa la geometria completa (sin deformar).
        geom_simplificado = (
            geom.simplify(tolerancia_deg, preserve_topology=True)
            if tolerancia_deg > 0 else geom
        )

        distritos.append({
            "distrito": props.get("DISTRITO") or "N/D",
            "canton": props.get("CANTÓN") or "N/D",
            "provincia": props.get("PROVINCIA") or "N/D",
            "codigo_dta": props.get("CÓDIGO_DTA"),
            "geometry": geom,                      # precision completa (spatial join, bounds)
            "geo": mapping(geom_simplificado),      # liviano (solo para el mapa)
        })
    return distritos


# ===========================================================
# SPATIAL JOIN - asignar cada muestra a su distrito
# ===========================================================
def asignar_distritos(df, distritos, col_lat="latitude", col_lon="longitude"):
    df = df.copy()
    df["distrito"] = None
    df["canton"] = None
    df["provincia"] = None

    if df.empty or not distritos:
        return df
    if col_lat not in df.columns or col_lon not in df.columns:
        return df

    geoms = [d["geometry"] for d in distritos]
    tree = STRtree(geoms)

    def lookup(lat, lon):
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None
        if pd.isna(lat) or pd.isna(lon) or (lat == 0 and lon == 0):
            return None
        pt = Point(lon, lat)
        candidatos = tree.query(pt)  # indices de bbox candidatos (shapely >= 2.0)
        for idx in candidatos:
            if geoms[idx].contains(pt) or geoms[idx].intersects(pt):
                return idx
        return None

    # object dtype evita que pandas convierta los indices (int) a float cuando
    # se mezclan con None dentro de la Series.
    idxs = df.apply(lambda row: lookup(row.get(col_lat), row.get(col_lon)), axis=1).astype(object)
    df["distrito"] = idxs.apply(lambda i: distritos[int(i)]["distrito"] if pd.notna(i) else None)
    df["canton"] = idxs.apply(lambda i: distritos[int(i)]["canton"] if pd.notna(i) else None)
    df["provincia"] = idxs.apply(lambda i: distritos[int(i)]["provincia"] if pd.notna(i) else None)
    return df


# ===========================================================
# TABLA DE CONTEO: Distrito x Program x ISP
# ===========================================================
def tabla_conteo_distrito(df):
    cols_needed = ["distrito", "test", "isp"]
    if df.empty or not all(c in df.columns for c in cols_needed):
        return pd.DataFrame()

    df_valid = df.dropna(subset=["distrito"]).copy()
    if df_valid.empty:
        return pd.DataFrame()

    df_valid["isp"] = df_valid["isp"].replace(ISP_NAME_MAP)

    conteo = (
        df_valid.groupby(["distrito", "canton", "provincia", "test", "isp"])
        .size()
        .reset_index(name="Pruebas")
    )

    pivot = conteo.pivot_table(
        index=["distrito", "canton", "provincia"],
        columns=["test", "isp"],
        values="Pruebas",
        fill_value=0,
        aggfunc="sum",
    )
    pivot.columns = [f"{test} · {isp}" for test, isp in pivot.columns]
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.reset_index().sort_values("Total", ascending=False)
    return pivot


# ===========================================================
# MAPA CHOROPLETH POR DISTRITO + PUNTOS OPCIONALES
# ===========================================================
def construir_mapa(distritos, conteo_por_distrito, df_puntos=None, mostrar_puntos=False,
                    bounds=None, distritos_resaltados=None):
    m = folium.Map(location=[9.7489, -83.7534], zoom_start=8, tiles="cartodbpositron")

    distritos_resaltados = distritos_resaltados or set()
    max_count = max(conteo_por_distrito.values(), default=0)
    colormap = cm.linear.YlOrRd_09.scale(0, max_count if max_count > 0 else 1)
    colormap.caption = "Pruebas por distrito"

    # Una sola capa GeoJson con los 494 distritos (mucho mas rapido que 494
    # capas individuales). El color/resaltado se resuelve via style_function
    # leyendo las properties de cada feature.
    # OJO: la clave de conteo_por_distrito debe ser (distrito, canton, provincia).
    # Costa Rica repite nombres de distrito en varios cantones (San Rafael,
    # San Isidro, Concepcion, Mercedes, San Miguel, etc.) -- usar solo el
    # nombre pintaba de mas los distritos "tocayos" sin muestras reales.
    features = []
    for d in distritos:
        clave = (d["distrito"], d["canton"], d["provincia"])
        count = conteo_por_distrito.get(clave, 0)
        resaltado = clave in distritos_resaltados
        features.append({
            "type": "Feature",
            "geometry": d["geo"],
            "properties": {
                "distrito": d["distrito"],
                "canton": d["canton"],
                "provincia": d["provincia"],
                "count": count,
                "resaltado": resaltado,
            },
        })
    feature_collection = {"type": "FeatureCollection", "features": features}

    def estilo(feat):
        p = feat["properties"]
        count = p["count"]
        color = colormap(count) if count > 0 else "#eeeeee"
        return {
            "fillColor": color,
            "color": "#2b6cb0" if p["resaltado"] else "#555555",
            "weight": 3 if p["resaltado"] else 0.4,
            "fillOpacity": 0.65 if count > 0 else 0.12,
        }

    folium.GeoJson(
        data=feature_collection,
        style_function=estilo,
        tooltip=folium.GeoJsonTooltip(
            fields=["distrito", "canton", "provincia", "count"],
            aliases=["Distrito", "Canton", "Provincia", "Pruebas"],
        ),
    ).add_to(m)

    if max_count > 0:
        colormap.add_to(m)

    if mostrar_puntos and df_puntos is not None and not df_puntos.empty:
        for _, row in df_puntos.iterrows():
            lat, lon = row.get("latitude"), row.get("longitude")
            if pd.isna(lat) or pd.isna(lon):
                continue
            isp_label = ISP_NAME_MAP.get(row.get("isp"), row.get("isp"))
            folium.CircleMarker(
                location=[float(lat), float(lon)],
                radius=3,
                color=ISP_COLOR_MAP.get(isp_label, "#333333"),
                fill=True,
                fill_opacity=0.8,
                popup=f"{isp_label} · {row.get('test')} · {row.get('distrito')}",
            ).add_to(m)

    if bounds:
        m.fit_bounds(bounds)

    return m


def distritos_seleccionados(distritos, provincia_sel, canton_sel, distrito_sel):
    """Poligonos que calzan con el filtro Provincia/Canton/Distrito activo."""
    return [
        d for d in distritos
        if (provincia_sel == "Todas" or d["provincia"] == provincia_sel)
        and (canton_sel == "Todos" or d["canton"] == canton_sel)
        and (distrito_sel == "Todos" or d["distrito"] == distrito_sel)
    ]


def bounds_para_seleccion(seleccionados, total_distritos):
    """Bounding box (para hacer zoom) de los poligonos seleccionados.
    Devuelve None si no hay filtro activo (seleccion == universo completo)."""
    if not seleccionados or len(seleccionados) == total_distritos:
        return None
    minx = min(d["geometry"].bounds[0] for d in seleccionados)
    miny = min(d["geometry"].bounds[1] for d in seleccionados)
    maxx = max(d["geometry"].bounds[2] for d in seleccionados)
    maxy = max(d["geometry"].bounds[3] for d in seleccionados)
    return [[miny, minx], [maxy, maxx]]


# ===========================================================
# CARGA DE POLIGONOS + SELECTOR DE DISTRITO (sidebar)
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Detalle del mapa")
simplificacion_m = st.sidebar.slider(
    "Simplificacion de poligonos (metros)",
    min_value=0, max_value=100, value=10, step=5,
    help="0 = geometria original del IGN (mas fiel, mapa mas pesado). "
         "Valores altos deforman distritos pequenos/urbanos.",
)
distritos = cargar_distritos_wfs(simplificacion_m)

st.sidebar.markdown("---")
st.sidebar.header("Filtrar por distrito")

provincias_disponibles = sorted({
    d["provincia"] for d in distritos if d["provincia"] and d["provincia"] != "N/D"
})
provincia_sel = st.sidebar.selectbox("Provincia", ["Todas"] + provincias_disponibles)

cantones_disponibles = sorted({
    d["canton"] for d in distritos
    if d["canton"] and d["canton"] != "N/D"
    and (provincia_sel == "Todas" or d["provincia"] == provincia_sel)
})
canton_sel = st.sidebar.selectbox("Canton", ["Todos"] + cantones_disponibles)

distritos_disponibles = sorted({
    d["distrito"] for d in distritos
    if d["distrito"] and d["distrito"] != "N/D"
    and (provincia_sel == "Todas" or d["provincia"] == provincia_sel)
    and (canton_sel == "Todos" or d["canton"] == canton_sel)
})
distrito_sel = st.sidebar.selectbox("Distrito", ["Todos"] + distritos_disponibles)

seleccion_actual = distritos_seleccionados(distritos, provincia_sel, canton_sel, distrito_sel)
bounds_seleccion = bounds_para_seleccion(seleccion_actual, len(distritos))
nombres_resaltados = {(d["distrito"], d["canton"], d["provincia"]) for d in seleccion_actual} \
    if bounds_seleccion else set()

# ===========================================================
# EJECUTAR CONSULTA API
# ===========================================================
if "poly_last_fetch_ts" not in st.session_state:
    st.session_state.poly_last_fetch_ts = 0.0
if "poly_df" not in st.session_state:
    st.session_state.poly_df = pd.DataFrame()

st.sidebar.markdown("---")
debug_paginacion = st.sidebar.checkbox("🔧 Mostrar diagnostico de paginacion", value=True)

now = time.time()
manual_trigger = st.sidebar.button("Consultar API")
time_trigger = usar_real_time and (now - st.session_state.poly_last_fetch_ts >= refresh_seconds)
should_fetch = manual_trigger or time_trigger

if should_fetch:
    raw = (
        obtener_datos_pag_no_cache(API_URL, headers, body, debug=debug_paginacion, limite_filas=limite_filas)
        if usar_real_time
        else obtener_datos_pag(API_URL, headers, body, debug=debug_paginacion, limite_filas=limite_filas)
    )
    if not raw:
        st.warning("No se recibieron datos de la API.")
        st.stop()
    df_nuevo = flatten_results(raw)
    if df_nuevo.empty:
        st.warning("No se recibieron datos.")
        st.stop()
    # El spatial join corre UNA sola vez por consulta nueva (no en cada rerun:
    # cambiar el filtro de distrito o el checkbox de puntos ya no lo recalcula).
    df_nuevo = asignar_distritos(df_nuevo, distritos)
    st.session_state.poly_df = df_nuevo
    st.session_state.poly_last_fetch_ts = now

df = st.session_state.poly_df

# ===========================================================
# FILTRO POR DISTRITO SELECCIONADO (el spatial join ya se hizo al consultar)
# ===========================================================
st.caption(f"Poligonos de distritos cargados: {len(distritos)}")

if df.empty:
    st.info("👈 Ejecuta la consulta para ver el mapa y la tabla por distrito.")
    st.stop()

sin_match = df["distrito"].isna().sum() if "distrito" in df.columns else 0
if sin_match:
    st.caption(f"⚠️ {sin_match} de {len(df)} muestras sin coordenadas validas o fuera de los poligonos cargados.")

# Filtrar el dataframe segun el selector de Provincia/Canton/Distrito del sidebar
mask = pd.Series(True, index=df.index)
if provincia_sel != "Todas":
    mask &= df["provincia"] == provincia_sel
if canton_sel != "Todos":
    mask &= df["canton"] == canton_sel
if distrito_sel != "Todos":
    mask &= df["distrito"] == distrito_sel
df_filtrado = df[mask]

if distrito_sel != "Todos":
    st.caption(f"📍 Filtrando por distrito: **{distrito_sel}** ({canton_sel}, {provincia_sel}) — {len(df_filtrado)} muestras")
elif canton_sel != "Todos":
    st.caption(f"📍 Filtrando por canton: **{canton_sel}** ({provincia_sel}) — {len(df_filtrado)} muestras")
elif provincia_sel != "Todas":
    st.caption(f"📍 Filtrando por provincia: **{provincia_sel}** — {len(df_filtrado)} muestras")

# ===========================================================
# MAPA
# ===========================================================
st.markdown("#### 🗺️ Mapa por Distrito")

LIMITE_PUNTOS_MAPA = 5000
puntos_disponibles = len(df_filtrado)
if puntos_disponibles > LIMITE_PUNTOS_MAPA:
    st.checkbox("Mostrar muestras individuales sobre el mapa", value=False, disabled=True)
    st.caption(
        f"⚠️ Hay {puntos_disponibles:,} muestras en el rango/filtro actual — "
        f"por encima de {LIMITE_PUNTOS_MAPA:,} no se dibujan puntos individuales "
        f"(el navegador se congelaria). Angosta el rango de fechas o el filtro "
        f"de distrito/canton/provincia para poder verlos."
    )
    mostrar_puntos = False
else:
    mostrar_puntos = st.checkbox("Mostrar muestras individuales sobre el mapa", value=False)

conteo_por_distrito = {
    clave: cantidad
    for clave, cantidad in (
        df_filtrado.dropna(subset=["distrito"])
        .groupby(["distrito", "canton", "provincia"])
        .size()
        .items()
    )
}
mapa = construir_mapa(
    distritos, conteo_por_distrito, df_puntos=df_filtrado, mostrar_puntos=mostrar_puntos,
    bounds=bounds_seleccion, distritos_resaltados=nombres_resaltados,
)
# components.html (en vez de st_folium) evita el puente bidireccional JS<->Python
# que streamlit-folium reconstruye en cada rerun; aqui es solo un iframe estatico.
components.html(mapa._repr_html_(), height=620, scrolling=False)

# ===========================================================
# TABLA DE CONTEO POR DISTRITO x PROGRAM x ISP
# ===========================================================
st.markdown("#### 📋 Conteo de pruebas por Distrito x Program x ISP")
tabla = tabla_conteo_distrito(df_filtrado)
if tabla.empty:
    st.info("No hay suficientes datos (con coordenadas validas) para generar la tabla.")
else:
    st.caption(f"{len(tabla)} distrito(s) con muestras — si no ves todos, desplazate dentro de la tabla (scroll interno).")
    st.dataframe(tabla, use_container_width=True, hide_index=True, height=450)
    st.download_button(
        "⬇️ Descargar tabla (CSV)",
        data=tabla.to_csv(index=False).encode("utf-8"),
        file_name="conteo_distrito_program_isp.csv",
        mime="text/csv",
    )
