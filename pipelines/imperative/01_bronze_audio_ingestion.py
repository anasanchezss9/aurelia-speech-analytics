# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze — Ingestão de áudios via Auto Loader
# MAGIC
# MAGIC Lê arquivos novos do volume `audio_landing` e materializa numa tabela
# MAGIC Delta com **uma linha por arquivo de áudio**.
# MAGIC
# MAGIC **Padrão**: Auto Loader em modo `binaryFile` + `trigger(availableNow=True)`
# MAGIC — processa só arquivos novos a cada execução, em batch.
# MAGIC
# MAGIC > Por que `binaryFile` e não armazenar os bytes? Porque `binaryFile` lê
# MAGIC > o arquivo **bruto** e expõe o `path`, `modificationTime` e `length`.
# MAGIC > Não armazenamos os bytes na Bronze (volumes de áudio podem crescer
# MAGIC > muito); só guardamos o ponteiro. Os bytes são lidos no Silver.

# COMMAND ----------

# MAGIC %pip install -q python-dotenv==1.0.1
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configurações do .env
import os
from pathlib import Path
from dotenv import load_dotenv

for candidate in [
    Path.cwd() / ".env",
    Path.cwd().parent / ".env",
    Path("/Workspace/Users/ana.sanchez@databricks.com/aurelia_speech_pipelines/.env"),
]:
    if candidate.exists():
        load_dotenv(candidate)
        break

CATALOG = os.getenv("CATALOG", "field_toolbox_catalog")
SCHEMA = os.getenv("SCHEMA", "aurelia_speech")
VOLUME = os.getenv("VOLUME_LANDING", "audio_landing")
TABLE_BRONZE = os.getenv("TABLE_BRONZE", "bronze_audio_files")

VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
CHECKPOINT = f"{VOLUME_PATH}/_checkpoints/bronze"
TARGET = f"{CATALOG}.{SCHEMA}.{TABLE_BRONZE}"

print(f"Source:     {VOLUME_PATH}")
print(f"Checkpoint: {CHECKPOINT}")
print(f"Target:     {TARGET}")

# COMMAND ----------

# DBTITLE 1,Auto Loader — lê arquivos de áudio novos
from pyspark.sql.functions import (
    col,
    current_timestamp,
    regexp_extract,
    input_file_name,
)

from pyspark.sql.functions import lower, when

stream = (
    spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "binaryFile")
    .option("cloudFiles.includeExistingFiles", "true")
    # Aceita os 3 formatos mais comuns (Havaia/Genesys exportam WAV; gTTS/MP3
    # da demo; M4A/AAC de alguns gravadores móveis).
    .option("pathGlobFilter", "*.{mp3,wav,m4a}")
    .load(VOLUME_PATH)
    .select(
        col("path").alias("audio_path"),
        col("length").alias("size_bytes"),
        col("modificationTime").alias("source_modified_at"),
        # Extrai o nome do arquivo (sem extensão) como call_id estável.
        regexp_extract(col("path"), r"([^/]+)\.(mp3|wav|m4a)$", 1).alias("call_id"),
        # Extrai a extensão como audio_format (mp3, wav, m4a).
        lower(regexp_extract(col("path"), r"\.([a-zA-Z0-9]+)$", 1)).alias("audio_format"),
        # `source` indica de onde veio o áudio:
        #   upload    -> arquivo enviado pelo portal (subpasta /uploads/)
        #   recording -> gravação ao vivo do portal (subpasta /recordings/)
        #   landing   -> drop direto no volume (Havaia, Lakeflow Connect, etc)
        when(col("path").contains("/uploads/"), "upload")
        .when(col("path").contains("/recordings/"), "recording")
        .otherwise("landing")
        .alias("source"),
        current_timestamp().alias("ingested_at"),
    )
)

# COMMAND ----------

# DBTITLE 1,Escreve em batch (trigger=availableNow)
(
    stream.writeStream.format("delta")
    .option("checkpointLocation", CHECKPOINT)
    .option("mergeSchema", "true")
    .outputMode("append")
    .trigger(availableNow=True)  # processa o backlog e termina
    .toTable(TARGET)
    .awaitTermination()
)

# COMMAND ----------

# DBTITLE 1,Confere o resultado
display(spark.table(TARGET).orderBy("ingested_at"))

# COMMAND ----------

# DBTITLE 1,Quantos arquivos novos chegaram nesta execução?
count = spark.table(TARGET).count()
print(f"Total acumulado na bronze: {count} arquivos.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Notas sobre idempotência
# MAGIC
# MAGIC - Auto Loader mantém um `_checkpoint` que rastreia quais arquivos já
# MAGIC   foram lidos. Em uma 2ª execução, **só os novos** entram na bronze.
# MAGIC - Se você apagar o checkpoint, o Auto Loader vai **reprocessar tudo**.
# MAGIC - O `call_id` (extraído do nome do arquivo) é a chave estável usada
# MAGIC   pelo Silver para evitar reprocessar transcrições já feitas.
