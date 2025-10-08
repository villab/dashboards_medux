import streamlit as st
import requests
import pandas as pd
import json
import plotly.express as px
from io import StringIO

# ================================
# 🔹 Configuración general
# ================================
st.set_page_config(page_title="Dashboard SUTEL RAW", layout="wide")
st.title("📊 Visualización de datos RAW desde API MedUX")

# ================================
# 🔹 Entrada de archivos y parámetros
# ================================
st.markdown("### Archivos necesarios")

csv_file = st.file_uploader("📄 Cargar archivo CSV con probes_id", type=["csv"])
token_file = st.file_uploader("🔑 Cargar archivo con token (.txt)", type=["txt"])

ts_start = st.number_input("Timestamp inicio (ms):", value=1756464305000)
ts_end = st.number_input("Timestamp fin (ms):", value=1756575905000)

programs = st.multiselect(
    "Programas a consultar:",
    ["http-upload-burst-test", "http-down-burst-test", "ping-test"],
    default=["http-upload-burst-test", "http-down-burst-test", "ping-test"]
)

url = "https://medux-ids.caseonit.com/api/results"

# ================================
# 🔹 Consultar API
# ================================
if st.button("🚀 Consultar API"):
    if not csv_file or not token_file:
        st.error("⚠️ Debes subir el archivo CSV y el archivo de token.")
    else:
        try:
            # Leer token
            token = token_file.read().decode("utf-8").strip()

            # Leer probes desde CSV subido
            df = pd.read_csv(csv_file)
            probes = df["probes_id"].dropna().tolist()

            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            body = {
                "tsStart": ts_start,
                "tsEnd": ts_end,
                "format": "raw",
                "programs": programs,
                "probes": probes,
            }

            st.info("⏳ Consultando API, espera un momento...")
            response = requests.post(url, headers=headers, json=body)

            if response.status_code == 200:
                data = response.json()
                dfs = {}

                for program_name, program_data in data.items():
                    if isinstance(program_data, list) and len(program_data) > 0:
                        df_program = pd.DataFrame(program_data)

                        # Expandir campo 'results' si existe
                        if "results" in df_program.columns:
                            df_results = pd.json_normalize(df_program["results"])
                            df_program = pd.concat(
                                [df_program.drop(columns=["results"]), df_results], axis=1
                            )

                        dfs[program_name] = df_program
                    else:
                        st.warning(f"⚠️ Sin datos para {program_name}")

                # ======================
                # 🔹 Visualización
                # ======================
                for name, df_prog in dfs.items():
                    st.subheader(f"📘 {name}")
                    st.write(f"{len(df_prog)} registros - {df_prog.shape[1]} columnas")

                    st.dataframe(df_prog.head(20))

                    numeric_cols = df_prog.select_dtypes("number").columns.tolist()
                    if len(numeric_cols) >= 2:
                        x_col = st.selectbox(f"Eje X para {name}", numeric_cols, index=0, key=f"x_{name}")
                        y_col = st.selectbox(f"Eje Y para {name}", numeric_cols, index=1, key=f"y_{name}")
                        fig = px.line(df_prog, x=x_col, y=y_col, title=f"{name}: {y_col} vs {x_col}")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info(f"No hay columnas numéricas para graficar en {name}")

            else:
                st.error(f"❌ Error al consultar API: {response.status_code}")
                st.code(response.text, language="json")

        except Exception as e:
            st.error(f"❌ Error ejecutando la consulta: {e}")
