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

Salidas principales (2 consultas INDEPENDIENTES, cada una con su propio
rango de fechas y boton):
    1) Mapa choropleth + puntos individuales -- boton "Consultar Mapa (raw)".
       Sigue usando /api/results format=raw (paginado) + spatial join propio,
       pensado para rangos cortos (horas/dias) ya que trae cada muestra.
    2) Tabla de conteo de pruebas por Distrito x Program x ISP (x Tecnologia)
       -- boton "Consultar Tabla (agregados)". Usa /api/results
       format=aggregate (NO pagina, responde casi instantaneo sin importar
       el volumen), asi que su rango puede ser de semanas o meses sin
       volverse lento. Como "aggregate" no soporta breakdown por distrito
       ni por target, se resuelve asi:
         - Distrito: se ubica cada SONDA (probeId) en su distrito UNA sola
           vez (no cada muestra), pidiendo una pagina raw chica y usando
           lat/lon promedio por sonda (con autovalidacion de dispersion: si
           una sonda muestra ubicaciones muy distintas entre si, se excluye
           en vez de asumir que es fija).
         - Ping-test por target: se pide un aggregate POR CADA IP destino
           conocida (PING_TEST_TARGETS), usando el filtro "targets", en vez
           de un breakdown por target (no soportado por la API).
         - La 3ra dimension (tecnologia) se itera aparte (una llamada por
           tecnologia) en vez de pedir 3 dimensiones de breakdown a la vez
           (isp+technology+probeId), porque ese patron no esta confirmado
           en ningun ejemplo de la documentacion de la API (si en el futuro
           se confirma que funciona, se puede simplificar a una sola
           llamada con breakdownBy=["isp","technology","probeId"]).

Requisitos adicionales sobre el dashboard original (agregar a requirements.txt):
    shapely>=2.0
    pyproj
    folium
    branca
    jinja2>=3.1.2   # requerido por pandas Styler (resaltado verde/rojo de la tabla)

Notas de rendimiento:
    - Los poligonos se simplifican y se cachean 24h (no se recalculan en cada rerun).
    - El spatial join (punto-en-poligono) del MAPA es vectorizado (shapely.points +
      STRtree.query con array, sin loop en Python por fila) -- ~30,000 muestras
      pasan de varios segundos a milisegundos.
    - El mapa se dibuja como UNA sola capa GeoJson (494 features en un solo layer)
      en vez de 494 capas individuales, y se renderiza con components.html en vez
      de streamlit-folium (evita el puente bidireccional que agrega latencia).
    - La TABLA ya no depende del volumen de muestras: usa "aggregate" (el
      servidor agrega) en vez de traer y contar cada fila del lado del cliente.

Orden del sidebar (de arriba a abajo):
    1) Filtro Fecha (una seccion para el mapa, otra independiente para la tabla)
    2) Filtro Distrito
    3) Filtro tecnologia y operador
    4) Capas adicionales (manchas de cobertura KMZ / radiobases)
    5) Resto de filtros (tipos de prueba, limite de descarga, detalle del
       mapa, diagnostico, y los botones "Consultar Mapa (raw)" /
       "Consultar Tabla (agregados)")

Capas adicionales (especificas de este proyecto RACSA, no existen en el
dashboard Sutel original):
    - "Manchas de cobertura" (KMZ): poligonos adicionales (no son distritos)
      que vienen de un archivo KMZ exportado de Google Earth
      ("Poligonos de medicion PROD Mayo 2026.kmz"), con un unico Placemark
      por poligono y un nombre numerico (2..24). Se parsean con
      zipfile+ElementTree (sin dependencias nuevas), se simplifican igual
      que los distritos (son MUCHO mas densos: ~140k vertices en total) y
      se dibujan en una capa GeoJson aparte, con estilo distinto (borde azul
      punteado, sin relleno) para no confundirlos con el choropleth de
      distritos.
    - "Radiobases": ~200 puntos (nodos 5G) de un Excel
      ("Listado Nodos RACSA 5G_con_poligonos.xlsx"), cada uno con su
      codigo de sitio, nombre, lat/lon y el numero de "mancha" a la que
      pertenece (columna Poligono, coincide con el nombre del Placemark
      del KMZ). Se dibujan como marcadores individuales (icono de antena) --
      con solo ~200 puntos no hace falta el patron de "una sola capa GeoJson"
      que se uso para los distritos/muestras (miles de elementos).
    Ambos archivos deben subirse al MISMO repo/carpeta que este script
    (rutas relativas, resueltas con el directorio del propio archivo .py
    para que funcione sin importar el working directory de Streamlit Cloud).
"""

import os
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
import requests
import streamlit as st
import streamlit.components.v1 as components
import folium
import branca.colormap as cm
from shapely.geometry import shape, mapping, Polygon, MultiPolygon
from shapely import points as shapely_points
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
# API MEDUX - base y targets fijos de ping-test (confirmados por el usuario:
# solo existen estas 2 IPs destino monitoreadas para ping-test en este
# proyecto -- se usan como filtro "targets" en vez de depender de un
# breakdown por target, que la API no soporta).
# ===========================================================
API_BASE = "https://medux-ids.caseonit.com"
PING_TEST_TARGETS = ["84.17.40.24", "138.59.18.180"]

# ===========================================================
# CAPAS ADICIONALES (solo RACSA): manchas de cobertura (KMZ) y radiobases
# (Excel). Rutas relativas al propio archivo .py -- deben subirse ambos
# archivos al mismo repo/carpeta en GitHub para que esto funcione en
# Streamlit Cloud sin importar cual sea el working directory del proceso.
# ===========================================================
DIRECTORIO_SCRIPT = os.path.dirname(os.path.abspath(__file__))
KMZ_MANCHAS_PATH = os.path.join(DIRECTORIO_SCRIPT, "Poligonos de medicion PROD Mayo 2026.kmz")
RADIOBASES_XLSX_PATH = os.path.join(DIRECTORIO_SCRIPT, "Listado Nodos RACSA 5G_con_poligonos.xlsx")
KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}

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
# FUNCIONES (definidas todas aqui arriba para que el orden del sidebar,
# mas abajo, se pueda reacomodar libremente sin preocuparse por dependencias)
# ===========================================================
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
    inicio_descarga = time.time()
    ultima_peticion_ts = 0.0

    while True:
        if pit:
            payload["pit"] = pit
        if search_after:
            payload["search_after"] = search_after

        # La API limita a ~1 req/s. Se mide desde el INICIO de la peticion
        # anterior (no desde que termino): si una pagina de 10,000 filas ya
        # tardo >=1s en responder -- lo normal -- no hace falta esperar nada
        # antes de pedir la siguiente. Un sleep fijo de 1.05s DESPUES de cada
        # respuesta (como antes) suma tiempo muerto innecesario encima del
        # que ya tardo la propia peticion.
        espera = 1.02 - (time.time() - ultima_peticion_ts)
        if espera > 0:
            time.sleep(espera)

        t0 = time.time()
        ultima_peticion_ts = t0
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        duracion_peticion = time.time() - t0
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
            transcurrido = time.time() - inicio_descarga
            velocidad = total_acumulado / transcurrido if transcurrido > 0 else 0
            diag.caption(
                f"📥 Pagina {pagina}: {filas_en_pagina} filas en {duracion_peticion:.1f}s "
                f"(acumulado {total_acumulado:,} / total API reportado: {total_reportado_api}). "
                f"¿vino cursor pit? {'si' if pit else 'NO'} — "
                f"{transcurrido:.0f}s transcurridos, ~{velocidad:,.0f} filas/seg"
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
    """Descarga paginada completa (cacheada 30 min: mismo rango/filtros =
    no vuelve a golpear la API hasta que cambies algo o pase el TTL)."""
    return _descargar_paginado(url, headers, body, debug=debug, limite_filas=limite_filas)


# ===========================================================
# API MEDUX - FORMATO "aggregate" (para la tabla, independiente del mapa)
# ===========================================================
# format:"aggregate" NO pagina -- el servidor ya agrega y responde con un
# payload chico, practicamente instantaneo, sin importar si el rango de
# fechas es de un dia o de varios meses (a diferencia de "raw", donde mas
# datos = mas paginas = mas tiempo). Se usa solo para la TABLA; el mapa
# sigue con "raw" + spatial join (necesita las coordenadas de cada muestra
# para pintar puntos individuales).
@st.cache_data(ttl=1800)
def obtener_agregado(url, headers, body):
    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def _extraer_samples(leaf):
    """El conteo de muestras de una celda de 'aggregate' viene gratis en la
    propiedad 'samples' (confirmado con un ejemplo real de la API, sin
    necesidad de pedir una operacion 'count' aparte). Por si alguna
    variante de la respuesta no la trae, hay un fallback que busca
    cualquier sub-campo con {'count': N}."""
    if not isinstance(leaf, dict):
        return 0
    if "samples" in leaf:
        try:
            return int(leaf["samples"])
        except (TypeError, ValueError):
            pass
    for v in leaf.values():
        if isinstance(v, dict) and "count" in v:
            try:
                return int(v["count"])
            except (TypeError, ValueError):
                continue
    return 0


def parsear_respuesta_aggregate(data, campo_groupby, campos_breakdown):
    """Aplana la respuesta de format=aggregate a una lista de filas planas.

    Confirmado con ejemplos reales de la API (doc sutel-api-extraction):
    - Con 1 sola dimension en breakdownBy: results[valor_groupby][valor] = leaf.
    - Con 2+ dimensiones: UNA sola clave compuesta "valorA|valorB" (mismo
      orden del array breakdownBy enviado), NO multiples niveles anidados.
    Por eso este proyecto nunca pide mas de 2 dimensiones de breakdown a la
    vez (isp + probeId) -- una 3ra dimension (technology) se itera aparte
    como filtro en cada llamada, para no depender de un patron de 3+
    dimensiones que no esta confirmado en ningun ejemplo de la doc.
    """
    filas = []
    resultados = data.get("results", {}) or {}
    for valor_groupby, sub in resultados.items():
        if not isinstance(sub, dict):
            continue
        for clave_breakdown, leaf in sub.items():
            if not isinstance(leaf, dict):
                continue
            fila = {campo_groupby: valor_groupby}
            if campos_breakdown:
                partes = str(clave_breakdown).split("|")
                for campo, valor in zip(campos_breakdown, partes):
                    fila[campo] = valor
            fila["samples"] = _extraer_samples(leaf)
            filas.append(fila)
    return filas


@st.cache_data(ttl=60 * 60 * 24)
def obtener_tecnologias_perfil(api_base, headers):
    """Lista de codigos de tecnologia del perfil (GET /api/profile/technologies,
    cacheada 24h). Se usa para iterar la tabla agregada una tecnologia a la
    vez (ver nota en parsear_respuesta_aggregate)."""
    try:
        r = requests.get(f"{api_base}/api/profile/technologies", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = []
    for item in items or []:
        if isinstance(item, dict) and "id" in item:
            ids.append(str(item["id"]))
        elif not isinstance(item, dict):
            ids.append(str(item))
    return sorted(set(ids))


def resolver_ubicacion_sondas(api_url, headers, probes, ts_start, ts_end, distritos,
                               programas_muestra=None, tolerancia_grados=0.01, timeout=60):
    """Ubica cada sonda (probeId) en su distrito UNA sola vez -- en vez de
    hacer el spatial join por cada muestra individual -- pidiendo una unica
    pagina 'raw' (sin paginar) que alcance para ver cada sonda al menos una
    vez, y usando su lat/lon promedio.

    Autovalidacion: en un proyecto landline se asume que cada sonda esta en
    un sitio fijo, pero en vez de confiar ciegamente en eso, se mide la
    dispersion (max distancia entre muestras) de lat/lon POR sonda; si supera
    'tolerancia_grados' (~1.1km por defecto) se marca esa sonda como
    'ubicacion no confiable' y se excluye de la asignacion por distrito en
    vez de arriesgar un resultado incorrecto.

    Devuelve (ubicacion_por_sonda: dict[str] -> {distrito, canton, provincia,
    codigo_dta, lat, lon}, sondas_inconsistentes: list[str]).
    """
    if programas_muestra is None:
        programas_muestra = [
            "network", "ping-test", "http-down-burst-test", "http-upload-burst-test",
            "voice-out", "voice-polqa", "sms-mo",
        ]
    body = {
        "tsStart": ts_start,
        "tsEnd": ts_end,
        "format": "raw",
        "timezone": "America/Costa_Rica",
        "programs": programas_muestra,
        "probes": [str(p) for p in probes if pd.notna(p)],
        "size": 10000,
    }
    try:
        r = requests.post(api_url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}, []

    df = flatten_results(data)
    if df.empty or "probeId" not in df.columns or "latitude" not in df.columns:
        return {}, []

    df = df.dropna(subset=["latitude", "longitude"]).copy()
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])
    df = df[~((df["latitude"] == 0) & (df["longitude"] == 0))]
    if df.empty:
        return {}, []

    resumen = df.groupby("probeId").agg(
        lat_prom=("latitude", "mean"), lat_min=("latitude", "min"), lat_max=("latitude", "max"),
        lon_prom=("longitude", "mean"), lon_min=("longitude", "min"), lon_max=("longitude", "max"),
        n_muestras=("latitude", "count"),
    ).reset_index()
    resumen["dispersion"] = (resumen["lat_max"] - resumen["lat_min"]).abs() + \
        (resumen["lon_max"] - resumen["lon_min"]).abs()
    sondas_inconsistentes = resumen.loc[resumen["dispersion"] > tolerancia_grados, "probeId"].tolist()
    resumen_confiable = resumen[~resumen["probeId"].isin(sondas_inconsistentes)].copy()

    if resumen_confiable.empty:
        return {}, sondas_inconsistentes

    resumen_confiable = asignar_distritos(
        resumen_confiable, distritos, col_lat="lat_prom", col_lon="lon_prom"
    )

    ubicacion_por_sonda = {}
    for _, row in resumen_confiable.iterrows():
        ubicacion_por_sonda[str(row["probeId"])] = {
            "distrito": row["distrito"],
            "canton": row["canton"],
            "provincia": row["provincia"],
            "codigo_dta": row["codigo_dta"],
            "lat": row["lat_prom"],
            "lon": row["lon_prom"],
        }
    return ubicacion_por_sonda, sondas_inconsistentes


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


def asignar_distritos(df, distritos, col_lat="latitude", col_lon="longitude"):
    """Spatial join: asigna cada muestra a su distrito.

    OPTIMIZACION: la version anterior usaba df.apply(axis=1) llamando una
    funcion Python fila por fila -- con miles de muestras esto es lento por
    el overhead propio de pandas (crear una Series por fila) sumado al de
    cada llamada individual a Point()/tree.query()/.contains(). shapely>=2.0
    permite construir TODOS los puntos de una sola vez (shapely.points, en
    C) y consultar el STRtree con un array completo en una sola llamada
    (tree.query(array, predicate=...)), sin loop en Python por fila. Con
    ~30,000 muestras esto pasa de tomar varios segundos a milisegundos.
    """
    df = df.copy()
    df["distrito"] = None
    df["canton"] = None
    df["provincia"] = None
    df["codigo_dta"] = None

    if df.empty or not distritos:
        return df
    if col_lat not in df.columns or col_lon not in df.columns:
        return df

    lat_arr = pd.to_numeric(df[col_lat], errors="coerce").to_numpy()
    lon_arr = pd.to_numeric(df[col_lon], errors="coerce").to_numpy()
    validos = ~pd.isna(lat_arr) & ~pd.isna(lon_arr) & ~((lat_arr == 0) & (lon_arr == 0))
    if not validos.any():
        return df

    geoms = [d["geometry"] for d in distritos]
    tree = STRtree(geoms)

    idx_validos = np.where(validos)[0]
    puntos = shapely_points(lon_arr[idx_validos], lat_arr[idx_validos])

    # tree.query con un array de geometrias hace la consulta COMPLETA en
    # codigo compilado (GEOS): devuelve pares (indice-en-puntos, indice-en-
    # distritos) para todo el batch de una sola vez.
    pares = tree.query(puntos, predicate="intersects")
    asignado = {}
    for i_pt, i_geom in zip(pares[0], pares[1]):
        idx_original = idx_validos[i_pt]
        if idx_original not in asignado:  # se queda con el primer match
            asignado[idx_original] = i_geom

    n = len(df)
    distrito_arr = [None] * n
    canton_arr = [None] * n
    provincia_arr = [None] * n
    codigo_arr = [None] * n
    for idx_original, i_geom in asignado.items():
        d = distritos[i_geom]
        distrito_arr[idx_original] = d["distrito"]
        canton_arr[idx_original] = d["canton"]
        provincia_arr[idx_original] = d["provincia"]
        codigo_arr[idx_original] = d["codigo_dta"]

    df["distrito"] = distrito_arr
    df["canton"] = canton_arr
    df["provincia"] = provincia_arr
    df["codigo_dta"] = codigo_arr
    return df


def manchas_con_muestras(df_puntos, manchas, col_lat="latitude", col_lon="longitude"):
    """Devuelve el set de nombres de 'mancha' (poligonos del KMZ) que tienen
    AL MENOS una muestra de df_puntos adentro -- mismo spatial join
    vectorizado que asignar_distritos, pero contra los poligonos del KMZ en
    vez de los distritos del IGN, y solo interesa el nombre (no hace falta
    devolver el df completo).

    Esto NO depende de resolver_ubicacion_sondas ni de la tabla de agregados
    (que tiene sus propios problemas con el perfil de RACSA) -- usa
    directamente los puntos crudos (lat/lon) que ya trajo la consulta del
    mapa (raw), asi que es independiente de esa otra logica.
    """
    if df_puntos is None or df_puntos.empty or not manchas:
        return set()
    if col_lat not in df_puntos.columns or col_lon not in df_puntos.columns:
        return set()

    lat_arr = pd.to_numeric(df_puntos[col_lat], errors="coerce").to_numpy()
    lon_arr = pd.to_numeric(df_puntos[col_lon], errors="coerce").to_numpy()
    validos = ~pd.isna(lat_arr) & ~pd.isna(lon_arr) & ~((lat_arr == 0) & (lon_arr == 0))
    if not validos.any():
        return set()

    geoms = [m["geometry"] for m in manchas]
    tree = STRtree(geoms)
    idx_validos = np.where(validos)[0]
    puntos = shapely_points(lon_arr[idx_validos], lat_arr[idx_validos])
    pares = tree.query(puntos, predicate="intersects")

    nombres_con_muestras = {manchas[i_geom]["nombre"] for i_geom in pares[1]}
    return nombres_con_muestras


def _parse_coordenadas_kml(texto_coordenadas):
    """Parsea el texto de un <coordinates> de KML: grupos separados por
    espacios, cada uno 'lon,lat[,alt]' separado por comas."""
    puntos = []
    for grupo in texto_coordenadas.split():
        partes = grupo.split(",")
        lon, lat = float(partes[0]), float(partes[1])
        puntos.append((lon, lat))
    return puntos


@st.cache_data(ttl=60 * 60 * 24, show_spinner="Cargando manchas de cobertura (KMZ)...")
def cargar_manchas_kmz(ruta_kmz, tolerancia_m=30):
    """Extrae cada Placemark/Polygon de un KMZ (zip con un doc.kml adentro)
    y devuelve una lista de dicts {nombre, geometry, geo} -- mismo patron que
    cargar_distritos_wfs (geometry = precision completa, geo = version
    simplificada ya en formato GeoJSON, lista para dibujar).

    Estas 'manchas' NO son distritos administrativos -- son poligonos propios
    del proyecto RACSA (zonas de medicion), por eso se cargan y dibujan
    aparte, con su propio estilo. Vienen mucho mas densos que los distritos
    del IGN (miles de vertices por poligono), de ahi que la tolerancia de
    simplificacion por defecto (30m) sea mayor a la de distritos (10m).
    """
    if not os.path.exists(ruta_kmz):
        return []

    tolerancia_deg = (tolerancia_m / METROS_POR_GRADO) if tolerancia_m > 0 else 0

    with zipfile.ZipFile(ruta_kmz) as z:
        nombre_kml = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
        if nombre_kml is None:
            return []
        kml_bytes = z.read(nombre_kml)

    root = ET.fromstring(kml_bytes)
    manchas = []
    for placemark in root.findall(".//kml:Placemark", KML_NS):
        nombre_el = placemark.find("kml:name", KML_NS)
        nombre = nombre_el.text.strip() if nombre_el is not None and nombre_el.text else "N/D"

        poligonos = []
        for poly_el in placemark.findall(".//kml:Polygon", KML_NS):
            outer = poly_el.find(".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", KML_NS)
            if outer is None or not outer.text:
                continue
            anillo_externo = _parse_coordenadas_kml(outer.text)
            huecos = []
            for inner in poly_el.findall(".//kml:innerBoundaryIs/kml:LinearRing/kml:coordinates", KML_NS):
                if inner.text:
                    huecos.append(_parse_coordenadas_kml(inner.text))
            try:
                poligonos.append(Polygon(anillo_externo, huecos))
            except Exception:
                continue

        if not poligonos:
            continue
        geom = poligonos[0] if len(poligonos) == 1 else MultiPolygon(poligonos)
        if not geom.is_valid:
            geom = geom.buffer(0)

        geom_simplificado = (
            geom.simplify(tolerancia_deg, preserve_topology=True)
            if tolerancia_deg > 0 else geom
        )
        manchas.append({
            "nombre": nombre,
            "geometry": geom,
            "geo": mapping(geom_simplificado),
        })
    return manchas


@st.cache_data(ttl=60 * 60 * 24, show_spinner="Cargando radiobases (Excel)...")
def cargar_radiobases(ruta_xlsx):
    """Carga el listado de radiobases (nodos 5G) desde Excel. Columnas
    esperadas: 'Código sitio', 'Nombre', 'Latitud', 'Longitud', 'Polígono'
    (numero de mancha KMZ a la que pertenece cada radiobase, puede venir
    vacio). Filas con lat/lon invalidas se descartan (se avisa cuantas)."""
    if not os.path.exists(ruta_xlsx):
        return pd.DataFrame(), 0

    df = pd.read_excel(ruta_xlsx)
    df = df.rename(columns={
        "Código sitio": "codigo_sitio", "Nombre": "nombre",
        "Latitud": "lat", "Longitud": "lon", "Polígono": "poligono",
    })
    for col in ["codigo_sitio", "nombre", "lat", "lon", "poligono"]:
        if col not in df.columns:
            df[col] = None

    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    antes = len(df)
    df = df.dropna(subset=["lat", "lon"]).copy()
    descartadas = antes - len(df)
    df["poligono"] = df["poligono"].apply(lambda v: str(int(v)) if pd.notna(v) else None)
    return df, descartadas


def preparar_test_con_target(df):
    """Desglosa 'ping-test' por target/IP destino (se espera que sean 2 IPs)
    en vez de agregar todo bajo una sola etiqueta 'ping-test'. El campo
    'target' puede venir directo o anidado en 'args' segun el program."""
    if df.empty or "test" not in df.columns:
        return df, None

    df = df.copy()
    target_series = None
    if "target" in df.columns:
        target_series = df["target"]
    elif "args" in df.columns:
        target_series = df["args"].apply(lambda v: v.get("target") if isinstance(v, dict) else None)

    if target_series is None:
        return df, None

    es_ping = df["test"] == "ping-test"
    con_target = es_ping & target_series.notna()
    etiqueta = df["test"].astype(str).copy()
    etiqueta.loc[con_target] = "ping-test (" + target_series[con_target].astype(str) + ")"
    df["test"] = etiqueta

    n_targets = df.loc[con_target, "test"].nunique()
    return df, n_targets


COLUMNAS_NO_CONTEO = {"codigo_dta", "distrito", "canton", "provincia", "tecnologia", "Cumple"}

# Liberty en sms-mo nunca llega a 100 muestras (limitacion conocida del
# operador/program) -- se excluye de la evaluacion de "Cumple" para no
# marcar como "No cumple" filas que en realidad estan bien.
def _es_columna_excluida_de_cumple(nombre_columna):
    return nombre_columna.startswith("Liberty") and "sms" in nombre_columna.lower()


def _color_cantidad_muestras(valor):
    """Resalta cada celda de conteo: verde si >= 100, rojo si < 100."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return ""
    if v >= 100:
        return "background-color: #c6efce; color: #006100"
    return "background-color: #ffc7ce; color: #9c0006"


def _color_cumple(valor):
    if valor == "✅ Cumple":
        return "background-color: #c6efce; color: #006100; font-weight: 600"
    if valor == "❌ No cumple":
        return "background-color: #ffc7ce; color: #9c0006; font-weight: 600"
    return ""


def estilizar_tabla_conteo(tabla):
    """Aplica el resaltado verde/rojo a todas las columnas de conteo (ISP ·
    program y Total), dejando sin color las columnas de identificacion del
    distrito (codigo_dta, distrito, canton, provincia, tecnologia) y
    resaltando aparte la columna 'Cumple'."""
    columnas_conteo = [c for c in tabla.columns if c not in COLUMNAS_NO_CONTEO]
    estilo = tabla.style.map(_color_cantidad_muestras, subset=columnas_conteo)
    if "Cumple" in tabla.columns:
        estilo = estilo.map(_color_cumple, subset=["Cumple"])
    return estilo


def pivotear_conteo(conteo, index_cols, col_tech=None):
    """A partir de un dataframe YA CONTADO -- una fila por combinacion de
    index_cols + isp + test, con una columna 'Pruebas' -- arma la tabla final
    (columnas ISP · Program, Total, Cumple). Compartida por los dos caminos
    de datos: el raw (agrupa muestra por muestra) y el de agregados de la
    API (ya viene contado desde el servidor, solo hay que pivotear)."""
    pivot = conteo.pivot_table(
        index=index_cols,
        columns=["isp", "test"],
        values="Pruebas",
        fill_value=0,
        aggfunc="sum",
    )
    # Columnas agrupadas por ISP (bloque de ~6-7 programs por operador) en vez
    # de por program -- con 3 operadores corriendo los mismos programs, es
    # mas facil comparar leyendo un bloque por operador.
    pivot = pivot.sort_index(axis=1, level=["isp", "test"])
    pivot.columns = [f"{isp} · {test}" for isp, test in pivot.columns]
    if col_tech:
        pivot = pivot.rename_axis(index={col_tech: "tecnologia"})
    columnas_indicadores = list(pivot.columns)  # antes de agregar "Total"
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.reset_index().sort_values("Total", ascending=False)

    # "Cumple": verifica que TODOS los indicadores (ISP · program) tengan
    # 100 o mas muestras -- excepto Liberty · sms-mo, que nunca llega a 100
    # (limitacion conocida, no una falla real de cobertura).
    columnas_para_cumplir = [
        c for c in columnas_indicadores if not _es_columna_excluida_de_cumple(c)
    ]
    if columnas_para_cumplir:
        cumple_bool = (pivot[columnas_para_cumplir] >= 100).all(axis=1)
    else:
        cumple_bool = pd.Series(True, index=pivot.index)
    pivot["Cumple"] = cumple_bool.map({True: "✅ Cumple", False: "❌ No cumple"})
    return pivot


def tabla_conteo_distrito(df, col_tech=None):
    """Conteo de pruebas por distrito a partir de datos RAW (muestra por
    muestra), con columnas agrupadas por ISP primero y luego por program.
    Si se pasa col_tech y existe en el df, se agrega como dimension extra en
    el INDICE (una fila por distrito+tecnologia) para no mezclar conteos de
    tecnologias distintas en una misma celda."""
    cols_needed = ["distrito", "test", "isp"]
    if df.empty or not all(c in df.columns for c in cols_needed):
        return pd.DataFrame()

    df_valid = df.dropna(subset=["distrito"]).copy()
    if df_valid.empty:
        return pd.DataFrame()

    df_valid["isp"] = df_valid["isp"].replace(ISP_NAME_MAP)
    if "codigo_dta" in df_valid.columns:
        df_valid["codigo_dta"] = df_valid["codigo_dta"].astype("Int64")

    index_cols = ["codigo_dta", "distrito", "canton", "provincia"] if "codigo_dta" in df_valid.columns \
        else ["distrito", "canton", "provincia"]
    usar_tech = bool(col_tech and col_tech in df_valid.columns)
    if usar_tech:
        df_valid[col_tech] = df_valid[col_tech].fillna("N/D").astype(str)
        index_cols = index_cols + [col_tech]

    conteo = (
        df_valid.groupby(index_cols + ["isp", "test"])
        .size()
        .reset_index(name="Pruebas")
    )
    return pivotear_conteo(conteo, index_cols, col_tech=col_tech if usar_tech else None)


def construir_tabla_agregada(api_url, headers, ts_start, ts_end, programas, probes,
                              ubicacion_por_sonda, technologies_perfil,
                              tecnologia_sel=None, operador_sel=None):
    """Arma la tabla Distrito x ISP x Program (x Tecnologia) usando SOLO
    consultas 'aggregate' (rapidas, sin paginar) en vez de traer cada
    muestra individual.

    Itera una tecnologia a la vez (para no depender de un breakdown de 3+
    dimensiones no confirmado -- ver parsear_respuesta_aggregate), y separa
    'ping-test' del resto: los programs normales van en UNA sola llamada por
    tecnologia (agrupando por program, desglosando por isp+probeId);
    'ping-test' se pide aparte, una llamada POR CADA target conocido
    (PING_TEST_TARGETS), usando el filtro 'targets' en vez de un breakdown
    por target (no soportado por la API).

    Devuelve (tabla_final, n_filas_crudas_combinadas, conteo_targets:
    dict[ip] -> muestras totales encontradas para ese target).
    """
    tecnologias = tecnologia_sel or technologies_perfil or [None]
    programas_normales = [p for p in programas if p != "ping-test"]
    incluir_ping = "ping-test" in programas
    probes_str = [str(p) for p in probes if pd.notna(p)]
    isps_filtro = None
    if operador_sel:
        isps_filtro = [k for k, v in ISP_NAME_MAP.items() if v in operador_sel] or None

    def _body_base(programs_list, targets=None):
        b = {
            "tsStart": ts_start, "tsEnd": ts_end, "format": "aggregate",
            "timezone": "America/Costa_Rica",
            "programs": programs_list,
            "probes": probes_str,
            "aggregate": {
                "groupBy": {"field": "program", "operation": "value"},
                "breakdownBy": ["isp", "probeId"],
                "values": [{"field": "success", "operation": "count"}],
            },
        }
        if targets:
            b["targets"] = targets
        if isps_filtro:
            b["isps"] = isps_filtro
        return b

    filas_largas = []
    conteo_targets = {ip: 0 for ip in PING_TEST_TARGETS}

    for tech in tecnologias:
        if programas_normales:
            body = _body_base(programas_normales)
            if tech is not None:
                body["technologies"] = [tech]
            data = obtener_agregado(api_url, headers, body)
            filas = parsear_respuesta_aggregate(data, "test", ["isp", "probeId"])
            for f in filas:
                f["tecnologia"] = tech
            filas_largas.extend(filas)

        if incluir_ping:
            for ip in PING_TEST_TARGETS:
                body = _body_base(["ping-test"], targets=[ip])
                if tech is not None:
                    body["technologies"] = [tech]
                data = obtener_agregado(api_url, headers, body)
                filas = parsear_respuesta_aggregate(data, "test", ["isp", "probeId"])
                for f in filas:
                    f["test"] = f"ping-test ({ip})"
                    f["tecnologia"] = tech
                    conteo_targets[ip] += f["samples"]
                filas_largas.extend(filas)

    if not filas_largas:
        return pd.DataFrame(), 0, conteo_targets

    df_largo = pd.DataFrame(filas_largas)
    df_largo["distrito"] = df_largo["probeId"].map(lambda p: ubicacion_por_sonda.get(str(p), {}).get("distrito"))
    df_largo["canton"] = df_largo["probeId"].map(lambda p: ubicacion_por_sonda.get(str(p), {}).get("canton"))
    df_largo["provincia"] = df_largo["probeId"].map(lambda p: ubicacion_por_sonda.get(str(p), {}).get("provincia"))
    df_largo["codigo_dta"] = df_largo["probeId"].map(lambda p: ubicacion_por_sonda.get(str(p), {}).get("codigo_dta"))
    df_largo["isp"] = df_largo["isp"].replace(ISP_NAME_MAP)

    df_valid = df_largo.dropna(subset=["distrito"]).copy()
    if df_valid.empty:
        return pd.DataFrame(), len(df_largo), conteo_targets

    df_valid["codigo_dta"] = df_valid["codigo_dta"].astype("Int64")

    usar_tech = any(t is not None for t in tecnologias)
    index_cols = ["codigo_dta", "distrito", "canton", "provincia"]
    if usar_tech:
        df_valid["tecnologia"] = df_valid["tecnologia"].fillna("N/D").astype(str)
        index_cols = index_cols + ["tecnologia"]

    conteo = (
        df_valid.groupby(index_cols + ["isp", "test"])["samples"]
        .sum()
        .reset_index(name="Pruebas")
    )
    tabla = pivotear_conteo(conteo, index_cols, col_tech="tecnologia" if usar_tech else None)
    return tabla, len(df_largo), conteo_targets


def construir_mapa(distritos, conteo_por_distrito, df_puntos=None, mostrar_puntos=False,
                    bounds=None, distritos_resaltados=None, paleta=None,
                    usar_escalones=False, n_escalones=6, metodo_escalon="quantiles",
                    redondear_escalones="int", manchas=None, mostrar_manchas=False,
                    radiobases=None, mostrar_radiobases=False):
    # prefer_canvas=True: los puntos se dibujan en un solo <canvas> en vez de
    # un nodo SVG por marcador -- clave para poder mostrar miles de muestras
    # sin que el navegador se ponga lento al hacer pan/zoom.
    m = folium.Map(location=[9.7489, -83.7534], zoom_start=8, tiles="cartodbpositron", prefer_canvas=True)

    distritos_resaltados = distritos_resaltados or set()
    max_count = max(conteo_por_distrito.values(), default=0)
    paleta = paleta or cm.linear.YlOrRd_09

    # Escalones (bins) en vez de degradado continuo: mejor cuando hay muchos
    # distritos con pocas pruebas y unos pocos con muchas (caso tipico) --
    # "quantiles" reparte los cortes segun la distribucion real de los datos
    # en vez de repartir el rango 0-max en partes iguales.
    counts_no_cero = [c for c in conteo_por_distrito.values() if c > 0]
    if usar_escalones and counts_no_cero:
        try:
            colormap = paleta.to_step(
                n=n_escalones, data=counts_no_cero,
                method=metodo_escalon, round_method=redondear_escalones,
            )
        except Exception:
            colormap = paleta.scale(0, max_count if max_count > 0 else 1)
    else:
        colormap = paleta.scale(0, max_count if max_count > 0 else 1)
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
                "codigo_dta": d.get("codigo_dta"),
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
            fields=["codigo_dta", "distrito", "canton", "provincia", "count"],
            aliases=["Codigo DTA", "Distrito", "Canton", "Provincia", "Pruebas"],
        ),
    ).add_to(m)

    if max_count > 0:
        colormap.add_to(m)

    if mostrar_puntos and df_puntos is not None and not df_puntos.empty:
        # Una sola capa GeoJson para TODOS los puntos (igual optimizacion que
        # los distritos): con miles de CircleMarker individuales, cada uno
        # generaba su propio objeto JS -- el HTML resultante se volvia enorme
        # y lento de construir. Con una FeatureCollection + un solo marker
        # "molde" reutilizado por Leaflet, el mismo volumen de puntos se
        # arma muchisimo mas rapido y pesa una fraccion del HTML.
        punto_features = []
        for _, row in df_puntos.iterrows():
            lat, lon = row.get("latitude"), row.get("longitude")
            if pd.isna(lat) or pd.isna(lon):
                continue
            isp_label = ISP_NAME_MAP.get(row.get("isp"), row.get("isp"))
            punto_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": {
                    "isp": isp_label,
                    "test": row.get("test"),
                    "distrito": row.get("distrito"),
                    "color": ISP_COLOR_MAP.get(isp_label, "#333333"),
                },
            })

        if punto_features:
            def estilo_punto(feat):
                color = feat["properties"]["color"]
                return {"radius": 3, "fillColor": color, "color": color, "weight": 1, "fillOpacity": 0.8}

            folium.GeoJson(
                data={"type": "FeatureCollection", "features": punto_features},
                marker=folium.CircleMarker(radius=3, fill=True),
                style_function=estilo_punto,
                tooltip=folium.GeoJsonTooltip(
                    fields=["isp", "test", "distrito"],
                    aliases=["ISP", "Program", "Distrito"],
                ),
            ).add_to(m)

            isps_presentes = sorted({f["properties"]["isp"] for f in punto_features if f["properties"]["isp"]})
            _agregar_leyenda_isp(m, isps_presentes)

    # --- Capa de "manchas" de cobertura (KMZ, especifico de RACSA) ---------
    # Poligonos propios del proyecto, NO son distritos administrativos --
    # se dibujan en su propia capa GeoJson (igual patron de rendimiento que
    # los distritos: una sola capa con todas las features) con un estilo
    # bien distinto (borde azul punteado, relleno minimo) para que no se
    # confundan con el choropleth de distritos.
    if mostrar_manchas and manchas:
        mancha_features = [
            {
                "type": "Feature",
                "geometry": mancha["geo"],
                "properties": {"nombre": mancha["nombre"]},
            }
            for mancha in manchas
        ]
        if mancha_features:
            folium.GeoJson(
                data={"type": "FeatureCollection", "features": mancha_features},
                style_function=lambda feat: {
                    "fillColor": "#1f6feb",
                    "color": "#1f6feb",
                    "weight": 2,
                    "dashArray": "6, 4",
                    "fillOpacity": 0.06,
                },
                tooltip=folium.GeoJsonTooltip(fields=["nombre"], aliases=["Mancha"]),
                name="Manchas de cobertura (KMZ)",
            ).add_to(m)

    # --- Capa de radiobases (Excel, especifico de RACSA) -------------------
    # Solo ~200 puntos -- a diferencia de las muestras (miles), aca un
    # folium.Marker individual por radiobase es perfectamente rapido, no
    # hace falta consolidar en una sola capa GeoJson.
    if mostrar_radiobases and radiobases is not None and not radiobases.empty:
        for _, rb in radiobases.iterrows():
            popup_html = (
                f"<b>{rb.get('codigo_sitio', 'N/D')}</b><br>"
                f"{rb.get('nombre', '')}<br>"
                f"Mancha: {rb.get('poligono') or 'N/D'}"
            )
            folium.Marker(
                location=[rb["lat"], rb["lon"]],
                tooltip=f"{rb.get('codigo_sitio', '')} · {rb.get('nombre', '')}",
                popup=folium.Popup(popup_html, max_width=250),
                icon=folium.Icon(icon="broadcast-tower", prefix="fa", color="darkred"),
            ).add_to(m)

    if bounds:
        m.fit_bounds(bounds)

    return m


def _agregar_leyenda_isp(m, isps_presentes):
    """Caja de leyenda fija (convenciones de color) para los puntos por
    operador -- el colormap de distritos ya trae su propia leyenda via
    branca (colormap.add_to(m)), esta es solo para los puntos individuales."""
    if not isps_presentes:
        return
    filas = "".join(
        f'<div style="margin-bottom:4px;">'
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        f'background:{ISP_COLOR_MAP.get(isp, "#333333")};margin-right:6px;'
        f'vertical-align:middle;border:1px solid rgba(0,0,0,0.25);"></span>{isp}</div>'
        for isp in isps_presentes
    )
    legend_html = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index:9999;
                background: white; padding: 10px 14px; border:1px solid #999;
                border-radius:6px; font-size:13px; box-shadow: 0 1px 4px rgba(0,0,0,0.35);
                font-family: sans-serif; line-height:1.3;">
        <div style="font-weight:600; margin-bottom:6px;">Operador (muestras)</div>
        {filas}
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))


def distritos_seleccionados(distritos, provincia_sel, canton_sel, distrito_sel):
    """Poligonos que calzan con el filtro activo.
    distrito_sel es una LISTA de tuplas (distrito, canton, provincia) --
    puede venir vacia (sin filtro de distrito especifico). Si tiene algo,
    manda sobre Provincia/Canton (es el filtro mas especifico)."""
    if distrito_sel:
        claves = set(distrito_sel)
        return [d for d in distritos if (d["distrito"], d["canton"], d["provincia"]) in claves]
    return [
        d for d in distritos
        if (provincia_sel == "Todos" or d["provincia"] == provincia_sel)
        and (canton_sel == "Todos" or d["canton"] == canton_sel)
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
# 1) FILTRO FECHA (sidebar)
# ===========================================================
st.sidebar.markdown("---")
st.sidebar.header("Zona horaria")
tz_map = {
    "Costa Rica (CST)": "America/Costa_Rica",
    "UTC": "UTC",
}
tz_label = st.sidebar.selectbox("Zona horaria de fechas", list(tz_map.keys()), index=0)
zona_local = pytz.timezone(tz_map[tz_label])

# NOTA: se elimino el modo "tiempo real" / auto-refresh (tanto el componente
# externo streamlit-autorefresh, que fallaba en cargar sus assets JS, como el
# <meta http-equiv="refresh">, que provoca recargas COMPLETAS de pagina no
# cancelables -- si la app esta embebida en un iframe/portal, la sesion se
# puede perder en cada recarga, generando un loop de refresco imposible de
# apagar desde la UI). El flujo es manual: boton "Consultar API".
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

# --- Calcular timestamps ---
dt_inicio_naive = datetime.combine(fecha_inicio, hora_inicio)
dt_fin_naive = datetime.combine(fecha_fin, hora_fin)
dt_inicio_local = zona_local.localize(dt_inicio_naive, is_dst=None)
dt_fin_local = zona_local.localize(dt_fin_naive, is_dst=None)
if dt_inicio_local >= dt_fin_local:
    st.error(f"Rango de fechas invalido.\nInicio: {dt_inicio_local}\nFin: {dt_fin_local}")
    st.stop()
ts_start = int(dt_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
ts_end = int(dt_fin_local.astimezone(pytz.utc).timestamp() * 1000)

inicio_local_str = datetime.fromtimestamp(ts_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
fin_local_str = datetime.fromtimestamp(ts_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')
st.sidebar.markdown("### Consulta activa (mapa)")
st.sidebar.write(f"Inicio: {inicio_local_str}")
st.sidebar.write(f"Fin: {fin_local_str}")

# --- Fecha INDEPENDIENTE para la tabla (agregados) -----------------------
# El mapa sigue con el rango de arriba (raw + spatial join, limitado en
# puntos). La tabla usa "aggregate" -- no pagina, responde rapido sin
# importar el volumen -- asi que su rango puede ser mucho mas amplio
# (semanas o meses) sin que la consulta se vuelva lenta.
st.sidebar.markdown("---")
st.sidebar.header("Fecha (Tabla · agregados)")
if "poly_tabla_fecha_inicio" not in st.session_state:
    ahora_local = datetime.now(zona_local)
    inicio_defecto_tabla = ahora_local - timedelta(days=30)
    st.session_state.poly_tabla_fecha_inicio = inicio_defecto_tabla.date()
    st.session_state.poly_tabla_hora_inicio = inicio_defecto_tabla.time()
    st.session_state.poly_tabla_fecha_fin = ahora_local.date()
    st.session_state.poly_tabla_hora_fin = ahora_local.time()

tabla_fecha_inicio = st.sidebar.date_input("Fecha inicio (tabla)", key="poly_tabla_fecha_inicio")
tabla_hora_inicio = st.sidebar.time_input("Hora inicio (tabla)", key="poly_tabla_hora_inicio")
tabla_fecha_fin = st.sidebar.date_input("Fecha fin (tabla)", key="poly_tabla_fecha_fin")
tabla_hora_fin = st.sidebar.time_input("Hora fin (tabla)", key="poly_tabla_hora_fin")

dt_tabla_inicio_naive = datetime.combine(tabla_fecha_inicio, tabla_hora_inicio)
dt_tabla_fin_naive = datetime.combine(tabla_fecha_fin, tabla_hora_fin)
dt_tabla_inicio_local = zona_local.localize(dt_tabla_inicio_naive, is_dst=None)
dt_tabla_fin_local = zona_local.localize(dt_tabla_fin_naive, is_dst=None)
if dt_tabla_inicio_local >= dt_tabla_fin_local:
    st.error(f"Rango de fechas de la tabla invalido.\nInicio: {dt_tabla_inicio_local}\nFin: {dt_tabla_fin_local}")
    st.stop()
ts_tabla_start = int(dt_tabla_inicio_local.astimezone(pytz.utc).timestamp() * 1000)
ts_tabla_end = int(dt_tabla_fin_local.astimezone(pytz.utc).timestamp() * 1000)
st.sidebar.markdown("### Consulta activa (tabla)")
st.sidebar.write(f"Inicio: {datetime.fromtimestamp(ts_tabla_start / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.write(f"Fin: {datetime.fromtimestamp(ts_tabla_end / 1000, tz=zona_local).strftime('%Y-%m-%d %H:%M:%S')}")

# ===========================================================
# 2) FILTRO DISTRITO (sidebar) - WFS de poligonos
# ===========================================================
# La simplificacion de poligonos (slider) vive en "Resto de filtros", mas
# abajo, pero el valor ya elegido (o el default 10m la primera vez) hace
# falta AHORA para cargar los distritos que alimentan este selector. Como
# Streamlit ya deja el valor del slider guardado en session_state entre
# reruns, leerlo aqui (antes de que el slider se dibuje mas abajo) siempre
# refleja el ultimo valor elegido por el usuario.
if "poly_simplificacion_m" not in st.session_state:
    st.session_state["poly_simplificacion_m"] = 10
distritos = cargar_distritos_wfs(st.session_state["poly_simplificacion_m"])

st.sidebar.markdown("---")
st.sidebar.header("Filtrar por distrito")

# --- Selector por Codigo DTA: al elegir uno, autocompleta Provincia/Canton/
# Distrito de abajo via un callback (se ejecuta ANTES del rerun, por eso hay
# que definir este selector primero en el script).
codigos_por_valor = {d["codigo_dta"]: d for d in distritos if d.get("codigo_dta") is not None}
codigos_disponibles = sorted(codigos_por_valor.keys())


def _aplicar_codigo_dta():
    codigo = st.session_state.get("poly_codigo_sel")
    match = codigos_por_valor.get(codigo)
    if match:
        st.session_state["poly_provincia_sel"] = match["provincia"]
        st.session_state["poly_canton_sel"] = match["canton"]
        # "Distrito" es un multiselect: al elegir un codigo, se selecciona
        # SOLO ese distrito (reemplaza cualquier seleccion multiple previa).
        st.session_state["poly_distrito_sel"] = [
            (match["distrito"], match["canton"], match["provincia"])
        ]


codigo_sel = st.sidebar.selectbox(
    "Codigo DTA",
    ["Todos"] + codigos_disponibles,
    format_func=lambda c: c if c == "Todos" else (
        f"{c} — {codigos_por_valor[c]['distrito']} "
        f"({codigos_por_valor[c]['canton']}, {codigos_por_valor[c]['provincia']})"
    ),
    key="poly_codigo_sel",
    on_change=_aplicar_codigo_dta,
    help="Selecciona un codigo DTA para autocompletar Provincia/Canton/Distrito.",
)

provincias_disponibles = sorted({
    d["provincia"] for d in distritos if d["provincia"] and d["provincia"] != "N/D"
})
provincia_sel = st.sidebar.selectbox("Provincia", ["Todos"] + provincias_disponibles, key="poly_provincia_sel")

cantones_disponibles = sorted({
    d["canton"] for d in distritos
    if d["canton"] and d["canton"] != "N/D"
    and (provincia_sel == "Todos" or d["provincia"] == provincia_sel)
})
canton_sel = st.sidebar.selectbox("Canton", ["Todos"] + cantones_disponibles, key="poly_canton_sel")

distritos_tuplas_disponibles = sorted({
    (d["distrito"], d["canton"], d["provincia"])
    for d in distritos
    if d["distrito"] and d["distrito"] != "N/D"
    and (provincia_sel == "Todos" or d["provincia"] == provincia_sel)
    and (canton_sel == "Todos" or d["canton"] == canton_sel)
})
# Codigo DTA por tupla (distrito, canton, provincia): se antepone en la
# etiqueta de cada opcion para poder buscar/filtrar tambien por codigo
# dentro de este mismo multiselect (no solo por nombre).
codigo_por_tupla = {
    (d["distrito"], d["canton"], d["provincia"]): d.get("codigo_dta") for d in distritos
}


def _formato_opcion_distrito(t):
    codigo = codigo_por_tupla.get(t)
    prefijo = f"{codigo} — " if codigo is not None else ""
    return f"{prefijo}{t[0]} — {t[1]}, {t[2]}"


distrito_sel = st.sidebar.multiselect(
    "Distrito (podes elegir varios, por nombre o codigo DTA)",
    distritos_tuplas_disponibles,
    format_func=_formato_opcion_distrito,
    key="poly_distrito_sel",
    help="Sin nada seleccionado = todos los distritos (segun Provincia/Canton "
         "de arriba). Elegir uno o mas manda sobre Provincia/Canton. Podes "
         "escribir el nombre o el codigo DTA para buscar.",
)

seleccion_actual = distritos_seleccionados(distritos, provincia_sel, canton_sel, distrito_sel)
bounds_seleccion = bounds_para_seleccion(seleccion_actual, len(distritos))
nombres_resaltados = {(d["distrito"], d["canton"], d["provincia"]) for d in seleccion_actual} \
    if bounds_seleccion else set()

# ===========================================================
# 3) FILTRO TECNOLOGIA Y OPERADOR (sidebar)
# ===========================================================
if "poly_last_fetch_ts" not in st.session_state:
    st.session_state.poly_last_fetch_ts = 0.0
if "poly_df" not in st.session_state:
    st.session_state.poly_df = pd.DataFrame()

# Los datos usados para poblar estas opciones vienen de la ULTIMA consulta ya
# guardada en session_state (puede estar vacia si todavia no se ha consultado
# la API -- el boton "Consultar API" vive en "Resto de filtros", mas abajo).
df = st.session_state.poly_df
col_tech = next((c for c in ["technology", "subtechnology", "tech", "accessTechnology"] if c in df.columns), None)

st.sidebar.markdown("---")
st.sidebar.header("Filtrar por tecnologia y operador")
if df.empty:
    st.sidebar.caption(
        "ℹ️ Estas opciones se llenan con los datos de la ultima consulta. "
        "Baja hasta 'Consultar API' (en Resto de filtros) y ejecuta una "
        "consulta para poder filtrar por tecnologia/operador."
    )
if col_tech:
    tecnologias_disponibles = sorted(df[col_tech].dropna().astype(str).unique())
    tecnologia_sel = st.sidebar.multiselect(
        f"Tecnologia (columna '{col_tech}') — podes elegir varias",
        tecnologias_disponibles,
        help="Sin nada seleccionado = todas las tecnologias.",
    )
elif not df.empty:
    st.sidebar.caption("No se encontro una columna de tecnologia en los datos traidos.")
    tecnologia_sel = []
else:
    tecnologia_sel = []

# --- Selector de Operador (ISP), mismo estilo que Distrito/Tecnologia (multiselect).
operadores_disponibles = sorted({
    ISP_NAME_MAP.get(v, v) for v in df["isp"].dropna().unique()
}) if "isp" in df.columns else []
operador_sel = st.sidebar.multiselect(
    "Operador — podes elegir varios", operadores_disponibles,
    help="Sin nada seleccionado = todos los operadores.",
)

# ===========================================================
# 4) CAPAS ADICIONALES (sidebar) -- especifico de RACSA: manchas de
#    cobertura (KMZ) y radiobases (Excel). Ambos archivos son estaticos
#    (no dependen de la API ni del rango de fechas), se cargan una sola vez
#    y quedan cacheados 24h.
# ===========================================================
if "racsa_simplif_manchas_m" not in st.session_state:
    st.session_state["racsa_simplif_manchas_m"] = 30
manchas_kmz = cargar_manchas_kmz(KMZ_MANCHAS_PATH, tolerancia_m=st.session_state["racsa_simplif_manchas_m"])
radiobases_df, radiobases_descartadas = cargar_radiobases(RADIOBASES_XLSX_PATH)

st.sidebar.markdown("---")
st.sidebar.header("Capas adicionales")
if not manchas_kmz:
    st.sidebar.caption(f"⚠️ No se encontro/parseo el KMZ ({os.path.basename(KMZ_MANCHAS_PATH)}).")
if radiobases_df.empty:
    st.sidebar.caption(f"⚠️ No se encontro/parseo el Excel de radiobases ({os.path.basename(RADIOBASES_XLSX_PATH)}).")

mostrar_manchas = st.sidebar.checkbox(
    f"Mostrar manchas de cobertura (KMZ) — {len(manchas_kmz)} poligono(s)",
    value=bool(manchas_kmz),
    disabled=not manchas_kmz,
)
simplificacion_manchas_m = st.sidebar.slider(
    "Simplificacion de manchas KMZ (metros)",
    min_value=0, max_value=200, step=10,
    key="racsa_simplif_manchas_m",
    disabled=not manchas_kmz,
    help="Las manchas del KMZ traen muchos mas vertices que los distritos "
         "del IGN; valores mas altos aligeran el mapa a costa de precision.",
)
mostrar_radiobases = st.sidebar.checkbox(
    f"Mostrar radiobases — {len(radiobases_df)} nodo(s) totales"
    + (f" ({radiobases_descartadas} descartada(s) por coordenadas invalidas)" if radiobases_descartadas else ""),
    help="Solo se dibujan las radiobases cuya 'mancha' (poligono KMZ) tiene "
         "al menos una muestra en la consulta/filtro actual del mapa (raw). "
         "Hace falta correr '🗺️ Consultar Mapa (raw)' primero.",
    value=not radiobases_df.empty,
    disabled=radiobases_df.empty,
)

# ===========================================================
# 5) RESTO DE FILTROS (sidebar): tipos de prueba, limite de descarga,
#    detalle del mapa, diagnostico y el boton "Consultar API"
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
solo_validas = st.sidebar.checkbox(
    "Traer solo muestras validas (success=1, exitCode=0)",
    value=False,
    help="Filtra del lado del servidor (menos filas para transferir y "
         "paginar = mas rapido), pero el conteo de pruebas por distrito ya "
         "no incluira los intentos fallidos/de sonda con averia.",
)

st.sidebar.markdown("---")
st.sidebar.header("Detalle del mapa")
simplificacion_m = st.sidebar.slider(
    "Simplificacion de poligonos (metros)",
    min_value=0, max_value=100, step=5,
    key="poly_simplificacion_m",
    help="0 = geometria original del IGN (mas fiel, mapa mas pesado). "
         "Valores altos deforman distritos pequenos/urbanos.",
)

PALETAS_MAPA = {
    "Amarillo-Naranja-Rojo": "YlOrRd_09",
    "Amarillo-Verde-Azul": "YlGnBu_09",
    "Azules": "Blues_09",
    "Verdes": "Greens_09",
    "Purpuras": "Purples_09",
    "Rojo-Purpura": "RdPu_09",
    "Naranjas": "OrRd_09",
    "Viridis": "viridis",
    "Plasma": "plasma",
}
paleta_label = st.sidebar.selectbox("Escala de color del mapa", list(PALETAS_MAPA.keys()), index=0)
paleta_mapa = getattr(cm.linear, PALETAS_MAPA[paleta_label])

# ===========================================================
# CONFIGURACION API MEDUX (necesita programas/fechas/limite ya elegidos arriba)
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
if solo_validas:
    body["conditions"] = [
        {"parameters": [{"field": "success"}], "operator": "eq", "value": 1},
        {"parameters": [{"field": "exitCode"}], "operator": "eq", "value": 0},
    ]

st.sidebar.markdown("---")
debug_paginacion = st.sidebar.checkbox("🔧 Mostrar diagnostico de paginacion", value=True)

now = time.time()
should_fetch = st.sidebar.button("🗺️ Consultar Mapa (raw)")

if should_fetch:
    raw = obtener_datos_pag(API_URL, headers, body, debug=debug_paginacion, limite_filas=limite_filas)
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
    # El filtro de "Tecnologia y Operador" (sidebar) se dibuja MAS ARRIBA en
    # el script que este boton -- en esta misma corrida ya se renderizo con
    # el df VIEJO (antes de este fetch), asi que sus opciones quedarian
    # desactualizadas hasta la proxima interaccion. Forzar un rerun aqui hace
    # que la corrida siguiente ya lea el df fresco de session_state ANTES de
    # dibujar ese filtro, sin necesidad de que el usuario toque nada mas.
    st.rerun()

# --- Boton independiente para la TABLA (agregados) -----------------------
# Usa su propio rango de fechas (arriba, "Fecha (Tabla · agregados)") y NO
# depende de que el mapa ya se haya consultado.
if "poly_tabla_df" not in st.session_state:
    st.session_state.poly_tabla_df = pd.DataFrame()
if "poly_tabla_last_fetch_ts" not in st.session_state:
    st.session_state.poly_tabla_last_fetch_ts = 0.0
if "poly_tabla_n_filas" not in st.session_state:
    st.session_state.poly_tabla_n_filas = 0
if "poly_tabla_conteo_targets" not in st.session_state:
    st.session_state.poly_tabla_conteo_targets = {}
if "poly_tabla_sondas_inconsistentes" not in st.session_state:
    st.session_state.poly_tabla_sondas_inconsistentes = []

should_fetch_tabla = st.sidebar.button("📋 Consultar Tabla (agregados)")

if should_fetch_tabla:
    with st.spinner("Resolviendo ubicacion de sondas..."):
        ubicacion_sondas, sondas_inconsistentes = resolver_ubicacion_sondas(
            API_URL, headers, probes, ts_tabla_start, ts_tabla_end, distritos,
        )
    if not ubicacion_sondas:
        st.warning(
            "No se pudo ubicar ninguna sonda en un distrito (revisa el rango de "
            "fechas de la tabla o si las sondas reportan latitude/longitude)."
        )
        st.stop()
    with st.spinner("Consultando agregados (rapido, sin paginar)..."):
        tecnologias_perfil = obtener_tecnologias_perfil(API_BASE, headers)
        tabla_nueva, n_filas_crudas, conteo_targets = construir_tabla_agregada(
            API_URL, headers, ts_tabla_start, ts_tabla_end, programas, probes,
            ubicacion_sondas, tecnologias_perfil,
            tecnologia_sel=tecnologia_sel or None, operador_sel=operador_sel or None,
        )
    st.session_state.poly_tabla_df = tabla_nueva
    st.session_state.poly_tabla_last_fetch_ts = now
    st.session_state.poly_tabla_n_filas = n_filas_crudas
    st.session_state.poly_tabla_conteo_targets = conteo_targets
    st.session_state.poly_tabla_sondas_inconsistentes = sondas_inconsistentes
    st.rerun()

# Se vuelve a leer de session_state (por si el fetch de arriba acaba de
# actualizarlo en esta misma corrida) para que el resto del script -- mapa,
# tabla, y el recalculo de col_tech de abajo -- ya use los datos frescos.
df = st.session_state.poly_df

if st.session_state.poly_last_fetch_ts:
    ultima = datetime.fromtimestamp(st.session_state.poly_last_fetch_ts, tz=zona_local)
    st.caption(f"Ultima consulta al mapa (raw): {ultima.strftime('%Y-%m-%d %H:%M:%S')}")

# ===========================================================
# MAPA (independiente de la tabla -- sigue en raw + spatial join)
# ===========================================================
st.markdown("#### 🗺️ Mapa por Distrito")
st.caption(f"Poligonos de distritos cargados: {len(distritos)}")

if df.empty:
    st.info("👈 Ejecuta '🗺️ Consultar Mapa (raw)' para ver el mapa.")
else:
    sin_match = df["distrito"].isna().sum() if "distrito" in df.columns else 0
    if sin_match:
        st.caption(f"⚠️ {sin_match} de {len(df)} muestras sin coordenadas validas o fuera de los poligonos cargados.")

    # Recalculo de la columna de tecnologia con el df YA fresco (el que se uso
    # para poblar el selector de "Filtro Tecnologia y Operador" mas arriba pudo
    # quedarse con la version anterior si esta es la primera consulta).
    col_tech = next((c for c in ["technology", "subtechnology", "tech", "accessTechnology"] if c in df.columns), None)

    # Filtrar el dataframe segun Provincia/Canton/Distrito/Tecnologia/Operador.
    # "Distrito" (multiselect de tuplas distrito+canton+provincia) es el mas
    # especifico: si tiene algo seleccionado, manda sobre Provincia/Canton.
    # Tecnologia y Operador son multiselect: vacio = sin filtro (todos).
    mask = pd.Series(True, index=df.index)
    if distrito_sel:
        claves_sel = {f"{d}||{c}||{p}" for d, c, p in distrito_sel}
        claves_df = df["distrito"].astype(str) + "||" + df["canton"].astype(str) + "||" + df["provincia"].astype(str)
        mask &= claves_df.isin(claves_sel)
    else:
        if provincia_sel != "Todos":
            mask &= df["provincia"] == provincia_sel
        if canton_sel != "Todos":
            mask &= df["canton"] == canton_sel
    if col_tech and tecnologia_sel:
        mask &= df[col_tech].astype(str).isin(tecnologia_sel)
    if operador_sel and "isp" in df.columns:
        mask &= df["isp"].apply(lambda v: ISP_NAME_MAP.get(v, v)).isin(operador_sel)
    df_filtrado = df[mask]

    if distrito_sel:
        nombres_distritos = ", ".join(d for d, _, _ in distrito_sel)
        st.caption(f"📍 Filtrando por distrito(s): **{nombres_distritos}** — {len(df_filtrado)} muestras")
    elif canton_sel != "Todos":
        st.caption(f"📍 Filtrando por canton: **{canton_sel}** ({provincia_sel}) — {len(df_filtrado)} muestras")
    elif provincia_sel != "Todos":
        st.caption(f"📍 Filtrando por provincia: **{provincia_sel}** — {len(df_filtrado)} muestras")

    # Los puntos se dibujan como UNA sola capa GeoJson + canvas (no un CircleMarker
    # por muestra), asi que el techo real subio bastante: 50,000 puntos arman el
    # mapa en menos de 1s. Igual queda ajustable por si tu maquina/navegador
    # prefiere un limite mas bajo.
    limite_puntos_mapa = st.sidebar.number_input(
        "Limite de puntos a dibujar en el mapa",
        min_value=1000, max_value=200_000, value=30_000, step=5_000,
        help="Los puntos se renderizan en una sola capa optimizada (canvas), asi "
             "que soporta bastante mas que un CircleMarker por muestra. Si tu "
             "navegador se siente lento al mover/hacer zoom, baja este numero.",
    )
    puntos_disponibles = len(df_filtrado)
    if puntos_disponibles > limite_puntos_mapa:
        st.checkbox("Mostrar muestras individuales sobre el mapa", value=False, disabled=True)
        st.caption(
            f"⚠️ Hay {puntos_disponibles:,} muestras en el rango/filtro actual — "
            f"por encima de {limite_puntos_mapa:,} (configurable en el sidebar) no "
            f"se dibujan puntos individuales. Angosta el rango de fechas o el "
            f"filtro de distrito/canton/provincia, o sube el limite."
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

    # Radiobases: solo se dibujan las que pertenecen a una "mancha" (poligono
    # KMZ) que tiene AL MENOS una muestra en la consulta/filtro actual del
    # mapa (df_filtrado, el mismo que ya se uso arriba para el choropleth de
    # distritos). Independiente de resolver_ubicacion_sondas/tabla agregada.
    if mostrar_radiobases and manchas_kmz and not radiobases_df.empty:
        manchas_activas = manchas_con_muestras(df_filtrado, manchas_kmz)
        radiobases_a_dibujar = radiobases_df[radiobases_df["poligono"].isin(manchas_activas)]
        st.caption(
            f"📡 Radiobases: mostrando {len(radiobases_a_dibujar)} de {len(radiobases_df)} "
            f"(solo las de manchas con muestras en esta consulta/filtro: "
            f"{len(manchas_activas)} de {len(manchas_kmz)} manchas)."
        )
    else:
        radiobases_a_dibujar = radiobases_df.iloc[0:0]

    mapa = construir_mapa(
        distritos, conteo_por_distrito, df_puntos=df_filtrado, mostrar_puntos=mostrar_puntos,
        bounds=bounds_seleccion, distritos_resaltados=nombres_resaltados, paleta=paleta_mapa,
        manchas=manchas_kmz, mostrar_manchas=mostrar_manchas,
        radiobases=radiobases_a_dibujar, mostrar_radiobases=mostrar_radiobases,
    )
    # components.html (en vez de st_folium) evita el puente bidireccional JS<->Python
    # que streamlit-folium reconstruye en cada rerun; aqui es solo un iframe estatico.
    # OJO: usar get_root().render() (pagina completa) y NO _repr_html_(), que envuelve
    # el mapa en un div con "padding-bottom" de aspect-ratio fijo + un iframe anidado
    # -- esa combinacion no calzaba con el height=620 fijo y el mapa se veia
    # recortado/corrido hacia arriba, sin quedar centrado en Costa Rica.
    components.html(mapa.get_root().render(), height=620, scrolling=False)

# ===========================================================
# TABLA DE CONTEO POR DISTRITO x PROGRAM x ISP (independiente -- via aggregate)
# ===========================================================
st.markdown("#### 📋 Conteo de pruebas por Distrito x Program x ISP (agregados)")

tabla = st.session_state.poly_tabla_df
if st.session_state.poly_tabla_last_fetch_ts:
    ultima_tabla = datetime.fromtimestamp(st.session_state.poly_tabla_last_fetch_ts, tz=zona_local)
    st.caption(
        f"Ultima consulta a la tabla (agregados): {ultima_tabla.strftime('%Y-%m-%d %H:%M:%S')} "
        f"— {st.session_state.poly_tabla_n_filas:,} filas agregadas combinadas."
    )

sondas_inconsistentes = st.session_state.poly_tabla_sondas_inconsistentes
if sondas_inconsistentes:
    st.warning(
        f"⚠️ {len(sondas_inconsistentes)} sonda(s) con ubicacion inconsistente entre "
        f"muestras (posible reinstalacion o error de reporte) se excluyeron de la "
        f"tabla: {', '.join(sondas_inconsistentes)}"
    )

conteo_targets = st.session_state.poly_tabla_conteo_targets
if conteo_targets:
    detalle_targets = " · ".join(f"{ip}: {n:,} muestras" for ip, n in conteo_targets.items())
    if all(n > 0 for n in conteo_targets.values()):
        st.caption(f"✅ ping-test desglosado por target ({len(conteo_targets)} IP conocidas) — {detalle_targets}")
    else:
        st.warning(f"⚠️ Alguno de los targets de ping-test no trajo muestras — {detalle_targets}")

if tabla.empty:
    st.info(
        "👈 Ejecuta '📋 Consultar Tabla (agregados)' para ver el conteo. "
        "Podes usar un rango de fechas mucho mas amplio que el mapa (hasta meses) "
        "sin que se vuelva lento."
    )
else:
    st.caption(f"{len(tabla)} fila(s) con muestras — si no ves todas, desplazate dentro de la tabla (scroll interno).")
    st.caption(
        "🟩 Verde: 100 o mas muestras · 🟥 Rojo: menos de 100 muestras (por celda). "
        "Columna **Cumple**: ✅ si todos los indicadores llegan a 100+ muestras, "
        "❌ si falta alguno (Liberty · sms-mo se excluye de esta evaluacion)."
    )
    st.dataframe(estilizar_tabla_conteo(tabla), use_container_width=True, hide_index=True, height=450)
    st.download_button(
        "⬇️ Descargar tabla (CSV)",
        data=tabla.to_csv(index=False).encode("utf-8"),
        file_name="conteo_distrito_program_isp.csv",
        mime="text/csv",
    )
