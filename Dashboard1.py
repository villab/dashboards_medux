import streamlit as st
import pandas as pd
import plotly.express as px
import requests
import json

# ================================
# 🔹 Configuración
# ================================
st.title("📊 Dashboard RAW – Medux API")
url = "https://medux-ids.caseonit.com/api/results"
csv_file = "probes.csv"
token_file = "token_fijo.txt"

# Leer token
with open(token_file, "r") as f:
    token = f.read().strip()

# Leer probes
df_probes = pd.read_csv(csv_file)
probes = df_probes["probes_id"].dropna().tolist()

# Headers y body
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

body = {
    "tsStart": 1756464305000,
    "tsEnd": 1756575905000,
    "format": "raw",
    "programs": [
        "http-upload-burst-test",
        "http-down-burst-test",
        "ping-test"
    ],
    "probes": probes,
}

# Obtener datos
@st.cache_data(ttl=3600)
def obtener_datos():
    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 200:
        return response.json()
    else:
        st.error(f"❌ Error API: {response.status_code}")
        return None

data = obtener_datos()
if not data:
    st.stop()

# ======================================
# 🔹 Convertir JSON a DataFrame plano
# ======================================
def flatten_results(raw_json):
    rows = []
    for program, results in raw_json.items():
        for item in results[:10]:  # solo 10 primeros por tipo
            flat = {"program": program}
            if isinstance(item, dict):
                flat.update(item)
            rows.append(flat)
    return pd.DataFrame(rows)

df = flatten_results(data)

# ======================================
# 🔹 Interfaz de selección
# ======================================
st.sidebar.header("⚙️ Configuración del gráfico")

programa = st.sidebar.selectbox("Programa", sorted(df["program"].unique()))
subset = df[df["program"] == programa]

if subset.empty:
    st.warning("No hay datos disponibles para este programa.")
    st.stop()

# Mostrar columnas disponibles
columnas_numericas = subset.select_dtypes(include="number").columns.tolist()
columnas_todas = subset.columns.tolist()

eje_x = st.sidebar.selectbox("Eje X", columnas_todas, index=0)
eje_y = st.sidebar.selectbox("Eje Y", columnas_numericas, index=1 if len(columnas_numericas) > 1 else 0)
tipo = st.sidebar.selectbox("Tipo de gráfico", ["scatter", "line", "bar"])

# ======================================
# 🔹 Render del gráfico
# ======================================
if tipo == "scatter":
    fig = px.scatter(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")
elif tipo == "line":
    fig = px.line(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")
else:
    fig = px.bar(subset, x=eje_x, y=eje_y, title=f"{programa}: {eje_y} vs {eje_x}")

st.plotly_chart(fig, use_container_width=True)

# ======================================
# 🔹 Tabla de datos
# ======================================
with st.expander("📄 Ver datos"):
    st.dataframe(subset)
