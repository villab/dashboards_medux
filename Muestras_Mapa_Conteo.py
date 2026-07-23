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

Orden del sidebar (de arriba a abajo):
    1) Filtro Fecha
    2) Filtro Distrito
    3) Filtro tecnologia y operador
    4) Resto de filtros (tipos de prueba, limite de descarga, detalle del
       mapa, y el boton "Consultar API")
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
    """Spatial join: asigna cada muestra a su distrito."""
    df = df.copy()
    df["distrito"] = None
    df["canton"] = None
    df["provincia"] = None
    df["codigo_dta"] = None

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
    df["codigo_dta"] = idxs.apply(lambda i: distritos[int(i)]["codigo_dta"] if pd.notna(i) else None)
    return df


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


def tabla_conteo_distrito(df, col_tech=None):
    """Conteo de pruebas por distrito, con columnas agrupadas por ISP primero
    y luego por program (mas facil comparar los mismos programs entre los 3
    operadores). Si se pasa col_tech y existe en el df, se agrega como
    dimension extra en el INDICE (una fila por distrito+tecnologia) para no
    mezclar conteos de tecnologias distintas en una misma celda."""
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
    if usar_tech:
        pivot = pivot.rename_axis(index={col_tech: "tecnologia"})
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.reset_index().sort_values("Total", ascending=False)
    return pivot


def construir_mapa(distritos, conteo_por_distrito, df_puntos=None, mostrar_puntos=False,
                    bounds=None, distritos_resaltados=None, paleta=None,
                    usar_escalones=False, n_escalones=6, metodo_escalon="quantiles",
                    redondear_escalones="int"):
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
st.sidebar.markdown("## 📅 Filtro Fecha")

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
st.sidebar.markdown("### Consulta activa")
st.sidebar.write(f"Inicio: {inicio_local_str}")
st.sidebar.write(f"Fin: {fin_local_str}")

# ===========================================================
# 2) FILTRO DISTRITO (sidebar) - WFS de poligonos
# ===========================================================
st.sidebar.markdown("## 📍 Filtro Distrito")

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
distrito_sel = st.sidebar.multiselect(
    "Distrito (podes elegir varios)",
    distritos_tuplas_disponibles,
    format_func=lambda t: f"{t[0]} — {t[1]}, {t[2]}",
    key="poly_distrito_sel",
    help="Sin nada seleccionado = todos los distritos (segun Provincia/Canton "
         "de arriba). Elegir uno o mas manda sobre Provincia/Canton.",
)

seleccion_actual = distritos_seleccionados(distritos, provincia_sel, canton_sel, distrito_sel)
bounds_seleccion = bounds_para_seleccion(seleccion_actual, len(distritos))
nombres_resaltados = {(d["distrito"], d["canton"], d["provincia"]) for d in seleccion_actual} \
    if bounds_seleccion else set()

# ===========================================================
# 3) FILTRO TECNOLOGIA Y OPERADOR (sidebar)
# ===========================================================
st.sidebar.markdown("## 📶 Filtro Tecnologia y Operador")

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
if col_tech:
    tecnologias_disponibles = sorted(df[col_tech].dropna().astype(str).unique())
    tecnologia_sel = st.sidebar.multiselect(
        f"Tecnologia (columna '{col_tech}') — podes elegir varias",
        tecnologias_disponibles,
        help="Sin nada seleccionado = todas las tecnologias.",
    )
else:
    st.sidebar.caption("No se encontro una columna de tecnologia en los datos traidos.")
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
# 4) RESTO DE FILTROS (sidebar): tipos de prueba, limite de descarga,
#    detalle del mapa, diagnostico y el boton "Consultar API"
# ===========================================================
st.sidebar.markdown("## ⚙️ Resto de Filtros")

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
should_fetch = st.sidebar.button("Consultar API")

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

# Se vuelve a leer de session_state (por si el fetch de arriba acaba de
# actualizarlo en esta misma corrida) para que el resto del script -- mapa,
# tabla, y el recalculo de col_tech de abajo -- ya use los datos frescos.
df = st.session_state.poly_df

if st.session_state.poly_last_fetch_ts:
    ultima = datetime.fromtimestamp(st.session_state.poly_last_fetch_ts, tz=zona_local)
    st.caption(f"Ultima consulta a la API: {ultima.strftime('%Y-%m-%d %H:%M:%S')}")

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

# ===========================================================
# MAPA
# ===========================================================
st.markdown("#### 🗺️ Mapa por Distrito")

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
mapa = construir_mapa(
    distritos, conteo_por_distrito, df_puntos=df_filtrado, mostrar_puntos=mostrar_puntos,
    bounds=bounds_seleccion, distritos_resaltados=nombres_resaltados, paleta=paleta_mapa,
)
# components.html (en vez de st_folium) evita el puente bidireccional JS<->Python
# que streamlit-folium reconstruye en cada rerun; aqui es solo un iframe estatico.
# OJO: usar get_root().render() (pagina completa) y NO _repr_html_(), que envuelve
# el mapa en un div con "padding-bottom" de aspect-ratio fijo + un iframe anidado
# -- esa combinacion no calzaba con el height=620 fijo y el mapa se veia
# recortado/corrido hacia arriba, sin quedar centrado en Costa Rica.
components.html(mapa.get_root().render(), height=620, scrolling=False)

# ===========================================================
# TABLA DE CONTEO POR DISTRITO x PROGRAM x ISP
# ===========================================================
st.markdown("#### 📋 Conteo de pruebas por Distrito x Program x ISP")
df_para_tabla, n_targets_ping = preparar_test_con_target(df_filtrado)
tabla = tabla_conteo_distrito(df_para_tabla, col_tech=col_tech)
if n_targets_ping is not None:
    if n_targets_ping == 2:
        st.caption(f"✅ ping-test desglosado por target: {n_targets_ping} IP destino detectadas, como se esperaba.")
    else:
        st.warning(f"⚠️ ping-test desglosado por target: se detectaron {n_targets_ping} IP destino (se esperaban 2). Revisa el campo 'target'/'args' de los resultados.")
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
