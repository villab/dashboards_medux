import streamlit as st
import requests
import pandas as pd
import json
import plotly.express as px

# ================================
# 🔹 Configuración general
# ================================
st.set_page_config(page_title="Dashboard SUTEL RAW", layout="wide")
st.title("📊 Visualización de datos RAW desde API MedUX")

# ================================
# 🔹 Parámetros del usuario
# ================================
url = "https://medux-ids.caseonit.com/api/results"
csv_file = st.text_input("Ruta del archivo CSV con probes_id:", r"C:\Users\Cesar Villate\OneDrive - Case On It\Automatizacion\Sutel\probes.csv")
token_file = st.text_input("Ruta del archivo con token:", r"C:\Users\Cesar Villate\OneDrive - Case On It\Automatizacion\Sutel\token_fijo.txt")

ts_start = st.number_input("Timestamp inicio (ms):", value=1756464305000)
ts_end = st.number_input("Timestamp fin (ms):", value=1756575905000)

programs = st.multiselect(
    "Programas a consultar:",
    ["http-upload-burst-test", "http-down-burst-test", "ping-test"],
    default=["http-upload-burst-test", "http-down-burst-test", "ping-test"]
)

# ================================
# 🔹 Consultar API al hacer clic
# ================================
if st.button("🚀 Consultar API"):
    try:
        # Leer token y probes
        with open(token_file, "r") as f:
            token = f.read().strip()
        probes = pd.read_csv(csv_file)["probes_id"].dropna().tolist()

        # Headers y body
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

            # Crear DataFrames por programa
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

                # Tabla
                st.dataframe(df_prog.head(20))

                # Intentar graficar si hay timestamp o metric
                numeric_cols = df_prog.select_dtypes("number").columns.tolist()
                if len(numeric_cols) >= 2:
                    x_col = st.selectbox(f"Eje X para {name}", numeric_cols, index=0, key=f"x_{name}")
                    y_col = st.selectbox(f"Eje Y para {name}", numeric_cols, index=1, key=f"y_{name}")
                    fig = px.line(df_prog, x=x_col, y=y_col, title=f"{name}: {y_col} vs {x_col}")
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info(f"No se encontraron columnas numéricas para graficar en {name}")

        else:
            st.error(f"❌ Error al consultar API: {response.status_code}")
            st.code(response.text, language="json")

    except Exception as e:
        st.error(f"❌ Error ejecutando la consulta: {e}")
