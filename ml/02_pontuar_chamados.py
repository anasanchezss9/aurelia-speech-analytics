# Databricks notebook source
# MAGIC %md
# MAGIC # Pontua os chamados e prioriza a fila do analista
# MAGIC
# MAGIC Carrega o modelo `@campeao` do Unity Catalog, pontua todos os chamados de
# MAGIC `gold_features_chamado` e grava duas tabelas:
# MAGIC
# MAGIC - `score_fraude_chamado` — um score por chamado, com a versão do modelo.
# MAGIC - `fila_priorizada` — os chamados abertos em ordem de score, com a evidência
# MAGIC   da ligação ao lado. É o que o app mostra ao analista.
# MAGIC
# MAGIC O sinal da ligação (sinais no relato da consultora) entra na fila como
# MAGIC evidência e não no score: há poucos chamados rotulados com ligação para o
# MAGIC modelo aprender o peso dele. As decisões do analista sobre esses casos são os
# MAGIC rótulos que faltam.

# COMMAND ----------

# MAGIC %pip install -q "mlflow[databricks]>=3.1" "scikit-learn==1.6.1"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "field_toolbox_catalog")
dbutils.widgets.text("schema", "aurelia_speech")
CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
NOME_MODELO = f"{CATALOG}.{SCHEMA}.modelo_fraude_chamado"

import mlflow
import pandas as pd
from pyspark.sql import functions as F

mlflow.set_registry_uri("databricks-uc")
versao = mlflow.MlflowClient().get_model_version_by_alias(NOME_MODELO, "campeao").version
modelo = mlflow.pyfunc.load_model(f"models:/{NOME_MODELO}@campeao")
print(f"{NOME_MODELO} v{versao}")

# COMMAND ----------

# DBTITLE 1,Score de todos os chamados
features = spark.table(f"{CATALOG}.{SCHEMA}.gold_features_chamado").toPandas()
colunas = modelo.metadata.get_input_schema().input_names()
# Decimal e booleano com nulo chegam do Spark como object; o treino viu float.
for c in modelo.unwrap_python_model().colunas_num:
    features[c] = pd.to_numeric(features[c].astype("float64"), errors="coerce")
features["score_fraude"] = modelo.predict(features[colunas])["score_fraude"].to_numpy()

score = spark.createDataFrame(features[["chamado_id", "score_fraude"]]).select(
    "chamado_id",
    F.round("score_fraude", 4).alias("score_fraude"),
    F.when(F.col("score_fraude") >= 0.5, "alta").when(F.col("score_fraude") >= 0.2, "media")
     .otherwise("baixa").alias("faixa_risco"),
    F.lit(int(versao)).alias("modelo_versao"),
    F.current_timestamp().alias("pontuado_em"),
)
score.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CATALOG}.{SCHEMA}.score_fraude_chamado")
display(score.groupBy("faixa_risco").count())

# COMMAND ----------

# DBTITLE 1,Fila priorizada
fila = spark.sql(f"""
SELECT
  row_number() OVER (ORDER BY s.score_fraude DESC, f.valor_reclamado DESC) AS posicao,
  s.score_fraude, s.faixa_risco, s.modelo_versao,
  f.*
FROM {CATALOG}.{SCHEMA}.gold_fila_investigacao f
JOIN {CATALOG}.{SCHEMA}.score_fraude_chamado s USING (chamado_id)
""")
fila.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{CATALOG}.{SCHEMA}.fila_priorizada")
display(fila.select("posicao", "chamado_id", "score_fraude", "faixa_risco", "motivo_declarado",
                    "valor_reclamado", "transportadora", "uf", "sinais_relato").limit(20))

# COMMAND ----------

# DBTITLE 1,Quanto a fila acerta — só existe porque o dado é sintético
# MAGIC %md
# MAGIC A fila aberta não tem rótulo: ninguém investigou ainda. O gabarito do gerador
# MAGIC sabe a causa real de cada chamado, e é só por isso que dá para medir a fila
# MAGIC antes do analista. No mundo real esta célula não existe.

# COMMAND ----------

gab = spark.read.parquet(f"/Volumes/{CATALOG}/{SCHEMA}/avaliacao/gabarito_chamados")
medida = (spark.table(f"{CATALOG}.{SCHEMA}.fila_priorizada").join(gab, "chamado_id")
          .select("posicao", "eh_fraude", "valor_reclamado").toPandas().sort_values("posicao"))
linhas = []
for k in [20, 50, 100]:
    topo = medida.head(k)
    linhas.append({"primeiros": str(k), "fraude_real": f"{topo.eh_fraude.mean():.0%}",
                   "fraudes_achadas": int(topo.eh_fraude.sum()),
                   "de_um_total_de": int(medida.eh_fraude.sum()),
                   "valor_fraude_na_frente": round(float(topo.loc[topo.eh_fraude, "valor_reclamado"].sum()), 2)})
linhas.append({"primeiros": "fila inteira", "fraude_real": f"{medida.eh_fraude.mean():.0%}",
               "fraudes_achadas": int(medida.eh_fraude.sum()), "de_um_total_de": int(medida.eh_fraude.sum()),
               "valor_fraude_na_frente": round(float(medida.loc[medida.eh_fraude, "valor_reclamado"].sum()), 2)})
display(pd.DataFrame(linhas))
dbutils.notebook.exit(pd.DataFrame(linhas).to_json(orient="records", force_ascii=False))
