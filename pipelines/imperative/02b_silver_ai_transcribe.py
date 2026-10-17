# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — Transcrição com `ai_transcribe` (alternativa ao Whisper)
# MAGIC
# MAGIC Mesma função do `02_silver_transcription`, sem endpoint de GPU: a
# MAGIC transcrição é uma AI Function serverless chamada direto no SQL.
# MAGIC
# MAGIC | | Whisper (`02_silver_transcription`) | `ai_transcribe` (este notebook) |
# MAGIC |---|---|---|
# MAGIC | Infra | Endpoint GPU_MEDIUM em Model Serving | Nenhuma — função gerenciada |
# MAGIC | Pré-processamento | Mono, resample 16 kHz, chunks de 5 min | Nenhum — WAV μ-law 8 kHz entra direto |
# MAGIC | Limite | 16 MB de payload por request | 1 h de áudio e 512 MB por chamada |
# MAGIC | Diarização | Não | Sim, `speaker_id` por segmento |
# MAGIC | Cobrança | Hora de GPU enquanto o endpoint está de pé | Produto `AI_FUNCTIONS`, por uso |
# MAGIC | Idiomas | PT-BR suportado | Beta: oficialmente só inglês e espanhol |
# MAGIC
# MAGIC Grava numa silver própria (`TABLE_SILVER_AIT`) com o mesmo contrato da
# MAGIC silver do Whisper — `call_id` e `full_text` — para o gold e o portal lerem
# MAGIC qualquer uma das duas trocando só o nome da tabela.
# MAGIC
# MAGIC **Pré-requisito**: um admin precisa ligar *AI Transcribe* em
# MAGIC Settings → Previews. Sem isso a chamada falha com
# MAGIC `AI Transcribe Preview is not enabled`.

# COMMAND ----------

# MAGIC %pip install -q python-dotenv==1.0.1
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
VOLUME = os.getenv("VOLUME_LANDING", "audio_landing")
TABLE_BRONZE = os.getenv("TABLE_BRONZE", "bronze_audio_files")
TABLE_SILVER_AIT = os.getenv("TABLE_SILVER_AIT", "silver_transcripts_ait")

BRONZE = f"{CATALOG}.{SCHEMA}.{TABLE_BRONZE}"
SILVER = f"{CATALOG}.{SCHEMA}.{TABLE_SILVER_AIT}"
VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

print(f"Bronze: {BRONZE}")
print(f"Silver: {SILVER}")
print(f"Volume: {VOLUME_PATH}")

# COMMAND ----------

# DBTITLE 1,Cria a Silver vazia (se não existir)
# `full_text` é o texto corrido, igual ao da silver do Whisper, e é o que o gold
# lê. `full_text_falantes` prefixa cada trecho com o falante, para quem quiser
# levar a diarização ao prompt. `segments` guarda a saída crua da função.
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {SILVER} (
  call_id             STRING NOT NULL,
  audio_path          STRING NOT NULL,
  transcribed_at      TIMESTAMP,
  full_text           STRING,
  full_text_falantes  STRING,
  duration_seconds    DOUBLE,
  speaker_count       INT,
  segments            ARRAY<STRUCT<start: DOUBLE, `end`: DOUBLE, speaker_id: STRING, text: STRING>>,
  error_message       STRING,
  transcriber         STRING,
  CONSTRAINT {TABLE_SILVER_AIT}_pk PRIMARY KEY (call_id)
) USING DELTA
""")
print("OK — Silver pronta.")

# COMMAND ----------

# DBTITLE 1,Quantos áudios ainda não foram transcritos?
n = spark.sql(f"""
SELECT count(*) AS n FROM {BRONZE} b
LEFT ANTI JOIN {SILVER} s ON b.call_id = s.call_id
""").first()["n"]
print(f"Áudios novos para transcrever: {n}")

if n == 0:
    dbutils.notebook.exit("no_new_audio")

# COMMAND ----------

# DBTITLE 1,ai_transcribe + MERGE numa única instrução
# Um só statement: a função roda uma vez por áudio e o resultado vai direto para
# o MERGE. Não precisa da tabela de staging do notebook do Whisper, que existe
# porque lá a UDF Python seria reexecutada a cada action.
#
# O volume é lido com recursiveFileLookup porque o portal grava em /uploads e
# /recordings. O prefixo `dbfs:` sai dos dois lados do join: a bronze guarda o
# path com ele e o read_files às vezes não.
spark.sql(f"""
MERGE INTO {SILVER} s
USING (
  WITH novos AS (
    SELECT b.call_id, b.audio_path
    FROM {BRONZE} b
    LEFT ANTI JOIN {SILVER} s ON b.call_id = s.call_id
  ),
  transcritos AS (
    SELECT n.call_id, n.audio_path, ai_transcribe(f.content) AS r
    FROM novos n
    JOIN read_files(
      '{VOLUME_PATH}/',
      format => 'binaryFile',
      recursiveFileLookup => true,
      pathGlobFilter => '*.{{mp3,wav,m4a}}'
    ) f
      ON regexp_replace(f.path, '^dbfs:', '') = regexp_replace(n.audio_path, '^dbfs:', '')
  ),
  segmentos AS (
    SELECT
      call_id,
      audio_path,
      r:error_message::STRING              AS error_message,
      r:response.duration_seconds::DOUBLE  AS duration_seconds,
      from_json(
        to_json(r:response.segments),
        'ARRAY<STRUCT<start: DOUBLE, `end`: DOUBLE, speaker_id: STRING, text: STRING>>'
      ) AS segments
    FROM transcritos
  )
  SELECT
    call_id,
    audio_path,
    current_timestamp() AS transcribed_at,
    array_join(transform(segments, x -> trim(x.text)), ' ') AS full_text,
    array_join(
      transform(segments, x -> concat('[Falante ', coalesce(x.speaker_id, '?'), '] ', trim(x.text))),
      '\\n'
    ) AS full_text_falantes,
    duration_seconds,
    size(array_distinct(filter(transform(segments, x -> x.speaker_id), x -> x IS NOT NULL))) AS speaker_count,
    segments,
    error_message,
    'ai_transcribe' AS transcriber
  FROM segmentos
) u
ON s.call_id = u.call_id
WHEN NOT MATCHED THEN INSERT *
""")
print("MERGE concluído.")

# COMMAND ----------

# DBTITLE 1,Resultado
display(spark.sql(f"""
SELECT call_id, duration_seconds, speaker_count, error_message,
       length(full_text) AS chars, substr(full_text, 1, 300) AS preview
FROM {SILVER}
ORDER BY transcribed_at DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Ligando no gold e no portal
# MAGIC
# MAGIC O `03_gold_ai_query_analysis` aceita os widgets `table_silver` e
# MAGIC `table_gold`. Para gerar o gold desta versão sem tocar no do Whisper:
# MAGIC
# MAGIC ```python
# MAGIC dbutils.notebook.run("03_gold_ai_query_analysis", 0, {
# MAGIC     "table_silver": "silver_transcripts_ait",
# MAGIC     "table_gold":   "gold_call_insights_ait",
# MAGIC })
# MAGIC ```
# MAGIC
# MAGIC O portal lê `call_id` e `full_text` da silver. Apontar `PORTAL_TABLE_SILVER`
# MAGIC e `PORTAL_TABLE_GOLD` do `app.yaml` para as tabelas `_ait` troca a versão
# MAGIC exibida sem mudar código.
