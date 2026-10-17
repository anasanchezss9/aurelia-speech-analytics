# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — Transcrição via endpoint Whisper
# MAGIC
# MAGIC Pega áudios da Bronze que ainda **não foram transcritos**, lê os bytes
# MAGIC do volume, manda pro endpoint Whisper, e salva o texto na Silver.
# MAGIC
# MAGIC **Padrão**: `pandas_udf` distribui as chamadas pelo cluster + `MERGE INTO`
# MAGIC para que cada áudio seja transcrito **uma única vez**, mesmo que o
# MAGIC pipeline rode várias vezes ao dia.

# COMMAND ----------

# MAGIC %pip install -q python-dotenv==1.0.1 soundfile==0.12.1
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Config
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
TABLE_BRONZE = os.getenv("TABLE_BRONZE", "bronze_audio_files")
TABLE_SILVER = os.getenv("TABLE_SILVER", "silver_transcripts")
WHISPER_ENDPOINT = os.getenv("WHISPER_ENDPOINT", "aurelia-whisper-turbo")

BRONZE = f"{CATALOG}.{SCHEMA}.{TABLE_BRONZE}"
SILVER = f"{CATALOG}.{SCHEMA}.{TABLE_SILVER}"

print(f"Bronze:           {BRONZE}")
print(f"Silver:           {SILVER}")
print(f"Whisper endpoint: {WHISPER_ENDPOINT}")

# COMMAND ----------

# DBTITLE 1,Cria a tabela Silver vazia (se não existir)
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {SILVER} (
  call_id          STRING NOT NULL,
  audio_path       STRING NOT NULL,
  transcribed_at   TIMESTAMP,
  full_text        STRING,
  whisper_endpoint STRING,
  CONSTRAINT {TABLE_SILVER}_pk PRIMARY KEY (call_id)
) USING DELTA
""")
print("OK — Silver pronta.")

# COMMAND ----------

# DBTITLE 1,Identifica só os áudios NOVOS (anti-join com a Silver)
to_process = spark.sql(f"""
SELECT b.call_id, b.audio_path
FROM {BRONZE} b
LEFT ANTI JOIN {SILVER} s
  ON b.call_id = s.call_id
""")

n = to_process.count()
print(f"Áudios novos para transcrever: {n}")
display(to_process)

# COMMAND ----------

# DBTITLE 1,Captura host e token no DRIVER e propaga pra UDF
# Em serverless, os executors não herdam credenciais Databricks. A solução é
# extrair host + token no driver e passar pra UDF via broadcast/closure.
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
DATABRICKS_HOST = ctx.apiUrl().get()
DATABRICKS_TOKEN = ctx.apiToken().get()

# Validação rápida — falha cedo se não tiver token
assert DATABRICKS_HOST and DATABRICKS_TOKEN, "Não foi possível obter host/token do contexto."
ENDPOINT_URL = f"{DATABRICKS_HOST}/serving-endpoints/{WHISPER_ENDPOINT}/invocations"
print(f"Endpoint URL: {ENDPOINT_URL}")

# COMMAND ----------

# DBTITLE 1,UDF que chama o endpoint Whisper via REST
import base64
import pandas as pd
from pyspark.sql.functions import pandas_udf

# Capturadas no closure — Spark serializa pros executors
_url = ENDPOINT_URL
_token = DATABRICKS_TOKEN

# Endpoints Model Serving têm limite duro de 16 MB por request. Áudios .wav
# reais podem ter 30+MB. A estratégia:
#   1) soundfile lê WAV nativamente (sem ffmpeg, sem librosa/numba).
#   2) Convertemos pra mono via mean dos canais.
#   3) Resample pra 16kHz com scipy.signal.resample_poly (sem JIT).
#   4) Salvamos como OGG Vorbis (~80x menor que WAV) ou FLAC (lossless ~3x).
#
# Evitamos librosa porque ele carrega numba que tenta cachear JIT em
# /local_disk0/.ephemeral_nfs/, indisponível pra escrita em serverless.
@pandas_udf("string")
def transcribe_udf(audio_paths: pd.Series) -> pd.Series:
    import io
    import requests
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    TARGET_SR = 16000

    out = []
    headers = {"Authorization": f"Bearer {_token}", "Content-Type": "application/json"}

    def _send(audio_bytes: bytes) -> str:
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        r = requests.post(
            _url,
            headers=headers,
            json={"dataframe_records": [{"audio": audio_b64}]},
            timeout=600,
        )
        r.raise_for_status()
        preds = r.json().get("predictions", [])
        return (preds[0] if isinstance(preds, list) and preds else "").strip()

    for path in audio_paths:
        try:
            local = path.replace("dbfs:", "") if path.startswith("dbfs:") else path

            # 1) Lê o WAV (soundfile cobre WAV/FLAC nativamente)
            y, sr = sf.read(local, dtype="float32", always_2d=False)
            if y.ndim > 1:
                y = y.mean(axis=1)  # mono

            # 2) Resample pra 16kHz mono (formato nativo Whisper)
            if sr != TARGET_SR:
                from math import gcd
                g = gcd(sr, TARGET_SR)
                y = resample_poly(y, TARGET_SR // g, sr // g).astype("float32")
                sr = TARGET_SR

            # 3) Salva como WAV PCM-16 e tenta enviar.
            # Para áudios longos isso pode passar de 16MB; nesse caso fazemos
            # chunking de 8 minutos (= ~7.7 MB cada) e concatenamos os textos.
            #
            # Por que não FLAC/OGG? Em DBR Serverless o libvorbis dá segfault
            # e o libFLAC ocasionalmente também. WAV puro é o caminho mais
            # estável + chunking resolve o limite de payload.
            buf = io.BytesIO()
            sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
            audio_bytes = buf.getvalue()

            # Limite duro de payload do endpoint: 16 MiB. Base64 usa 4/3 do
            # volume original, então o WAV raw máximo é ~12 MB. Margem de
            # segurança: 11 MB.
            #
            # Dimensionamento: 16 kHz mono PCM-16 = 32_000 bytes/segundo.
            # 11 MB / 32_000 ≈ 360 segundos ⇒ chunks de 5 minutos.
            MAX_RAW = 11 * 1024 * 1024

            if len(audio_bytes) <= MAX_RAW:
                text = _send(audio_bytes)
            else:
                chunk_secs = 300  # 5 min
                samples_per_chunk = chunk_secs * sr
                texts = []
                for i in range(0, len(y), samples_per_chunk):
                    chunk = y[i : i + samples_per_chunk]
                    cbuf = io.BytesIO()
                    sf.write(cbuf, chunk, sr, format="WAV", subtype="PCM_16")
                    texts.append(_send(cbuf.getvalue()))
                text = " ".join(t for t in texts if t)

            out.append(text)
        except Exception as exc:
            out.append(f"[ERRO_TRANSCRICAO: {type(exc).__name__}: {exc}]")
    return pd.Series(out)

# COMMAND ----------

# DBTITLE 1,Aplica a UDF e prepara o DF para MERGE
if n == 0:
    print("Nada a fazer — todos os áudios já estão transcritos.")
    dbutils.notebook.exit("no_new_audio")

transcribed = (
    to_process
    .withColumn("full_text", transcribe_udf("audio_path"))
    .selectExpr(
        "call_id",
        "audio_path",
        "current_timestamp() AS transcribed_at",
        "full_text",
        f"'{WHISPER_ENDPOINT}' AS whisper_endpoint",
    )
)

# Materializa numa tabela de staging antes do MERGE.
#
# O caminho óbvio seria `.cache()`, mas serverless recusa com
# NOT_SUPPORTED_WITH_SERVERLESS: PERSIST TABLE — e serverless é exatamente o que
# o botão "Processar" do app usa, porque ele chama `jobs.submit` sem cluster spec.
# Sem materializar, cada action (count, display, merge) reexecutaria a UDF e
# pagaria uma chamada nova ao Whisper por áudio.
STAGING = f"{SILVER}_staging"
transcribed.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(STAGING)
staged = spark.table(STAGING)
print(f"Transcritos {staged.count()} áudios.")
display(staged.select("call_id", "full_text"))

# COMMAND ----------

# DBTITLE 1,MERGE INTO Silver — só insere o que é novo
from delta.tables import DeltaTable

silver_table = DeltaTable.forName(spark, SILVER)
(
    silver_table.alias("s")
    .merge(
        staged.alias("u"),
        "s.call_id = u.call_id",
    )
    .whenNotMatchedInsertAll()
    # Não fazemos UPDATE: se um áudio já foi transcrito, mantemos a versão original.
    # Para reprocessar, basta deletar a linha da Silver e rodar de novo.
    .execute()
)
print("MERGE concluído.")

spark.sql(f"DROP TABLE IF EXISTS {STAGING}")

# COMMAND ----------

# DBTITLE 1,Resultado
display(spark.table(SILVER).orderBy("transcribed_at"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Por que MERGE e não append?
# MAGIC
# MAGIC O `LEFT ANTI JOIN` da etapa 3 já garante que só os áudios novos chegam
# MAGIC ao DataFrame. Mas em ambientes com retries (job falha no meio, reroda),
# MAGIC dois processos podem competir. O `MERGE INTO ... WHEN NOT MATCHED`
# MAGIC garante **idempotência transacional** — Delta gerencia isso atomicamente.
