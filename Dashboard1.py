import streamlit as st
import pandas as pd
import plotly.express as px
import requests
import json
from io import StringIO

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
    ["http-upload-burst-test", "http-down-burst-test", "ping-test"],
    default=["ping-test"]
)

ts_start = st.sidebar.number_input("Timestamp inicio (ms)", value=1756464305000)
ts_end = st.sidebar.number_input("Timestamp fin (ms)", value=1756575905000)

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
    "probes": probes,
}

# ===========================================================
# 🔹 Llamada a la API
# ===========================================================
@st.cache_data(ttl=1800)
def obtener_datos():
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
    data = obtener_datos()
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

