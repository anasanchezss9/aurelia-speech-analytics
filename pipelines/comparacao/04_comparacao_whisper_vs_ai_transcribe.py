# Databricks notebook source
# MAGIC %md
# MAGIC # Comparação — Whisper em Model Serving vs `ai_transcribe`
# MAGIC
# MAGIC Roda as duas transcrições sobre os mesmos áudios da bronze e mede três
# MAGIC coisas, cada uma do jeito que o cliente vai perguntar:
# MAGIC
# MAGIC 1. **Performance** — segundos de relógio por ligação e fator de tempo real
# MAGIC    (segundos de processamento por segundo de áudio), chamada a chamada.
# MAGIC 2. **Qualidade** — WER contra o roteiro nos áudios sintéticos, divergência
# MAGIC    entre as duas nos reais (não há gabarito humano), e se o gold extrai os
# MAGIC    mesmos campos de negócio a partir de cada transcrição.
# MAGIC 3. **Custo** — DBU lido do `system.billing.usage` na janela do benchmark e
# MAGIC    projeção mensal para um volume de áudio que você informa.
# MAGIC
# MAGIC Pré-requisitos: endpoint do Whisper existente, preview *AI Transcribe*
# MAGIC ligado, e as silvers e golds das duas versões já populadas (rode `02` e
# MAGIC `02b`, e o `03` duas vezes — ver o fim do `02b`).
# MAGIC
# MAGIC O billing chega com atraso de algumas horas. A seção de custo pode ser
# MAGIC rodada de novo depois, sem repetir o benchmark: ela lê a janela gravada.

# COMMAND ----------

# MAGIC %pip install -q python-dotenv==1.0.1 soundfile==0.12.1 jiwer==3.0.4
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
WHISPER_ENDPOINT = os.getenv("WHISPER_ENDPOINT", "aurelia-whisper-turbo")

FQ = f"{CATALOG}.{SCHEMA}"
BRONZE = f"{FQ}.{os.getenv('TABLE_BRONZE', 'bronze_audio_files')}"
SILVER_W = f"{FQ}.{os.getenv('TABLE_SILVER', 'silver_transcripts')}"
SILVER_A = f"{FQ}.{os.getenv('TABLE_SILVER_AIT', 'silver_transcripts_ait')}"
GOLD_W = f"{FQ}.{os.getenv('TABLE_GOLD', 'gold_call_insights')}"
GOLD_A = f"{FQ}.{os.getenv('TABLE_GOLD_AIT', 'gold_call_insights_ait')}"
BENCH = f"{FQ}.benchmark_transcricao"
VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

dbutils.widgets.dropdown("rodar_benchmark", "sim", ["sim", "nao"], "Rodar o benchmark de latência")
dbutils.widgets.text("horas_audio_mes", "1000", "Horas de áudio por mês (projeção)")
dbutils.widgets.text("execucoes_batch_mes", "30", "Execuções batch por mês (projeção)")
RODAR = dbutils.widgets.get("rodar_benchmark") == "sim"
HORAS_MES = float(dbutils.widgets.get("horas_audio_mes"))
EXECUCOES_MES = int(dbutils.widgets.get("execucoes_batch_mes"))

print(f"Bronze {BRONZE} | Whisper {WHISPER_ENDPOINT}")
print(f"Silvers {SILVER_W} x {SILVER_A}")
print(f"Golds   {GOLD_W} x {GOLD_A}")

# COMMAND ----------

# DBTITLE 1,Tabela de resultados do benchmark
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {BENCH} (
  bench_id        STRING,
  transcriber     STRING,
  call_id         STRING,
  audio_seconds   DOUBLE,
  started_at      TIMESTAMP,
  ended_at        TIMESTAMP,
  latency_s       DOUBLE,
  chunks          INT,
  cold_start      BOOLEAN,
  chars           INT,
  error           STRING
) USING DELTA
""")

# COMMAND ----------

# DBTITLE 1,Whisper: mesmo pré-processamento do 02_silver, cronometrado por chamada
import base64
import io
import time
import uuid
from datetime import datetime, timezone
from math import gcd

import numpy as np
import requests
import soundfile as sf
from scipy.signal import resample_poly

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST = ctx.apiUrl().get()
TOKEN = ctx.apiToken().get()
URL = f"{HOST}/serving-endpoints/{WHISPER_ENDPOINT}/invocations"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TARGET_SR = 16000
CHUNK_SECS = 300
MAX_RAW = 11 * 1024 * 1024


def _send(audio_bytes: bytes) -> str:
    r = requests.post(
        URL,
        headers=HEADERS,
        json={"dataframe_records": [{"audio": base64.b64encode(audio_bytes).decode("ascii")}]},
        timeout=900,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} ({len(audio_bytes):,} bytes): {r.text[:500]}")
    preds = r.json().get("predictions", [])
    return (preds[0] if isinstance(preds, list) and preds else "").strip()


def whisper_transcribe(path: str) -> tuple[str, int, float]:
    y, sr = sf.read(path.replace("dbfs:", ""), dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != TARGET_SR:
        g = gcd(sr, TARGET_SR)
        y = resample_poly(y, TARGET_SR // g, sr // g).astype("float32")
        sr = TARGET_SR
    audio_seconds = len(y) / sr
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    if len(buf.getvalue()) <= MAX_RAW:
        return _send(buf.getvalue()), 1, audio_seconds
    texts = []
    step = CHUNK_SECS * sr
    for i in range(0, len(y), step):
        cbuf = io.BytesIO()
        sf.write(cbuf, y[i : i + step], sr, format="WAV", subtype="PCM_16")
        texts.append(_send(cbuf.getvalue()))
    return " ".join(t for t in texts if t), len(texts), audio_seconds


def endpoint_scaled_to_zero() -> bool:
    r = requests.get(f"{HOST}/api/2.0/serving-endpoints/{WHISPER_ENDPOINT}", headers=HEADERS, timeout=30)
    ents = r.json().get("config", {}).get("served_entities", [])
    return any("zero" in (e.get("state", {}).get("deployment_state_message") or "").lower() for e in ents)

# COMMAND ----------

# DBTITLE 1,Roda o benchmark (sequencial, uma ligação por vez nas duas soluções)
# Sequencial de propósito: mede a latência de uma ligação, que é o que o
# analista sente, e não deixa uma solução competir com a outra pelo mesmo
# minuto de billing. A vazão em lote do ai_transcribe aparece no tempo do 02b.
calls = [r.asDict() for r in spark.sql(f"SELECT call_id, audio_path FROM {BRONZE} ORDER BY call_id").collect()]
BENCH_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
rows = []

if RODAR:
    cold = endpoint_scaled_to_zero()
    print(f"Whisper {'estava em scale-to-zero: a 1ª chamada inclui o cold start' if cold else 'já estava de pé'}")
    for i, c in enumerate(calls):
        t0 = datetime.now(timezone.utc); s = time.perf_counter()
        try:
            text, chunks, dur = whisper_transcribe(c["audio_path"]); err = None
        except Exception as exc:
            text, chunks, dur, err = "", 0, None, f"{type(exc).__name__}: {exc}"
        lat = time.perf_counter() - s
        rows.append((BENCH_ID, "whisper", c["call_id"], dur, t0, datetime.now(timezone.utc), lat, chunks, cold and i == 0, len(text), err))
        print(f"whisper       {c['call_id']:<28} {lat:7.1f}s  chunks={chunks}  {err or ''}")

    for c in calls:
        path = c["audio_path"].replace("dbfs:", "")
        t0 = datetime.now(timezone.utc); s = time.perf_counter()
        r = spark.sql(f"""
            WITH t AS (SELECT ai_transcribe(content) AS r FROM read_files('{path}', format => 'binaryFile'))
            SELECT r:error_message::STRING AS err,
                   r:response.duration_seconds::DOUBLE AS dur,
                   length(CAST(r:response.segments AS STRING)) AS chars
            FROM t
        """).first()
        lat = time.perf_counter() - s
        rows.append((BENCH_ID, "ai_transcribe", c["call_id"], r["dur"], t0, datetime.now(timezone.utc), lat, 1, False, r["chars"] or 0, r["err"]))
        print(f"ai_transcribe {c['call_id']:<28} {lat:7.1f}s  {r['err'] or ''}")

    spark.createDataFrame(rows, spark.table(BENCH).schema).write.mode("append").saveAsTable(BENCH)
else:
    BENCH_ID = spark.sql(f"SELECT max(bench_id) FROM {BENCH}").first()[0]
print(f"bench_id = {BENCH_ID}")

# COMMAND ----------

# DBTITLE 1,Performance por solução
# O audio_seconds do Whisper vem do arquivo decodificado; o do ai_transcribe vem
# da própria função. Para o fator de tempo real usamos o mesmo denominador nas
# duas: a duração que o ai_transcribe reporta, que não depende de resample.
display(spark.sql(f"""
WITH b AS (SELECT * FROM {BENCH} WHERE bench_id = '{BENCH_ID}'),
dur AS (SELECT call_id, max(audio_seconds) AS audio_s FROM b WHERE transcriber = 'ai_transcribe' GROUP BY 1)
SELECT
  b.transcriber,
  count(*)                                         AS ligacoes,
  round(sum(d.audio_s) / 60, 1)                    AS minutos_audio,
  round(sum(b.latency_s), 1)                       AS segundos_total,
  round(percentile(b.latency_s, 0.5), 1)           AS p50_s,
  round(max(b.latency_s), 1)                       AS max_s,
  round(sum(b.latency_s) / sum(d.audio_s), 3)      AS fator_tempo_real,
  round(max(CASE WHEN b.cold_start THEN b.latency_s END), 1) AS cold_start_s,
  count_if(b.error IS NOT NULL)                    AS erros
FROM b JOIN dur d USING (call_id)
GROUP BY 1
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Qualidade
# MAGIC
# MAGIC **Sintéticos (com gabarito).** O `00_setup_env` salva o roteiro de cada
# MAGIC áudio gTTS. Normalizamos os dois lados (minúsculas, sem pontuação, sem
# MAGIC acento) e **removemos números**: o roteiro diz "oito quatro sete dois
# MAGIC três" e as transcrições escrevem "84723", o que inflaria o WER sem ser erro.
# MAGIC
# MAGIC **Reais (sem gabarito).** Não há transcrição humana das dez ligações. O
# MAGIC número abaixo é a divergência entre as duas — WER de uma contra a outra.
# MAGIC Divergência alta diz que pelo menos uma errou muito, não qual. Para saber
# MAGIC qual, ouça os trechos que divergem; a célula seguinte lista os piores.

# COMMAND ----------

# DBTITLE 1,WER nos sintéticos e divergência nos reais
import json
import re
import unicodedata

import jiwer

NUM_WORDS = set(
    "zero um uma dois duas tres quatro cinco seis sete oito nove dez onze doze treze quatorze catorze "
    "quinze dezesseis dezessete dezoito dezenove vinte trinta quarenta cinquenta sessenta setenta "
    "oitenta noventa cem cento duzentos trezentos quatrocentos quinhentos mil".split()
)


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(w for w in t.split() if not w.isdigit() and w not in NUM_WORDS)


refs = {}
try:
    with open(f"{VOLUME_PATH}/_reference_scripts.json", encoding="utf-8") as f:
        refs = {c["filename"].rsplit(".", 1)[0]: c["script"] for c in json.load(f)}
except FileNotFoundError:
    print("Sem _reference_scripts.json no volume — WER dos sintéticos fica de fora.")

pares = spark.sql(f"""
SELECT w.call_id, w.full_text AS whisper, a.full_text AS ait, a.speaker_count
FROM {SILVER_W} w JOIN {SILVER_A} a USING (call_id)
""").toPandas()

linhas = []
for _, p in pares.iterrows():
    nw, na = norm(p["whisper"]), norm(p["ait"])
    row = {
        "call_id": p["call_id"],
        "tipo": "sintetico" if p["call_id"] in refs else "real",
        "palavras_whisper": len(nw.split()),
        "palavras_ait": len(na.split()),
        "divergencia_entre_eles": round(jiwer.wer(nw, na), 3) if nw and na else None,
        "falantes_ait": int(p["speaker_count"] or 0),
    }
    if p["call_id"] in refs:
        nr = norm(refs[p["call_id"]])
        row["wer_whisper"] = round(jiwer.wer(nr, nw), 3)
        row["wer_ait"] = round(jiwer.wer(nr, na), 3)
    linhas.append(row)

import pandas as pd

qual = pd.DataFrame(linhas).sort_values(["tipo", "call_id"])
display(qual)

# COMMAND ----------

# DBTITLE 1,Onde as duas mais divergem (para conferir ouvindo)
# Alinha as duas transcrições e mostra as trocas mais longas, com a posição
# aproximada em segundos tirada dos segmentos do ai_transcribe.
def trechos_divergentes(a: str, b: str, n: int = 5, min_len: int = 4):
    out = jiwer.process_words(a, b)
    ra, rb = a.split(), b.split()
    blocos = []
    for chunk in out.alignments[0]:
        if chunk.type == "equal":
            continue
        ta = " ".join(ra[chunk.ref_start_idx : chunk.ref_end_idx])
        tb = " ".join(rb[chunk.hyp_start_idx : chunk.hyp_end_idx])
        if max(len(ta.split()), len(tb.split())) >= min_len:
            blocos.append((chunk.ref_start_idx / max(len(ra), 1), ta, tb))
    return sorted(blocos, key=lambda x: -max(len(x[1]), len(x[2])))[:n]


for _, p in pares[~pares["call_id"].isin(list(refs))].iterrows():
    print(f"\n=== {p['call_id']}")
    for pos, ta, tb in trechos_divergentes(norm(p["whisper"]), norm(p["ait"])):
        print(f"  ~{pos:4.0%} do áudio\n    whisper: {ta}\n    ait    : {tb}")

# COMMAND ----------

# DBTITLE 1,O que importa para o negócio: o gold extrai a mesma coisa?
# A transcrição só serve se o gold tirar dela os mesmos campos. Esta célula
# compara campo a campo o gold de cada versão, para as mesmas ligações.
display(spark.sql(f"""
WITH j AS (
  SELECT w.call_id,
         w.tipo_problema  AS tipo_w,  a.tipo_problema  AS tipo_a,
         w.suspeita_fraude AS fraude_w, a.suspeita_fraude AS fraude_a,
         w.reincidencia   AS reinc_w, a.reincidencia   AS reinc_a,
         w.sentiment      AS sent_w,  a.sentiment      AS sent_a,
         w.pedido_numero  AS ped_w,   a.pedido_numero  AS ped_a,
         w.consultora_codigo AS cons_w, a.consultora_codigo AS cons_a,
         w.produtos_faltantes AS prod_w, a.produtos_faltantes AS prod_a
  FROM {GOLD_W} w JOIN {GOLD_A} a USING (call_id)
)
SELECT
  count(*)                                              AS ligacoes,
  count_if(tipo_w <=> tipo_a)                           AS tipo_problema_igual,
  count_if(fraude_w <=> fraude_a)                       AS suspeita_fraude_igual,
  count_if(reinc_w <=> reinc_a)                         AS reincidencia_igual,
  count_if(sent_w <=> sent_a)                           AS sentimento_igual,
  count_if(nullif(ped_w,'') <=> nullif(ped_a,''))       AS pedido_igual,
  count_if(nullif(cons_w,'') <=> nullif(cons_a,''))     AS consultora_igual,
  count_if(size(prod_w) = size(prod_a))                 AS qtd_produtos_igual
FROM j
"""))

display(spark.sql(f"""
SELECT w.call_id,
       w.tipo_problema AS tipo_whisper, a.tipo_problema AS tipo_ait,
       w.suspeita_fraude AS fraude_whisper, a.suspeita_fraude AS fraude_ait,
       w.pedido_numero AS pedido_whisper, a.pedido_numero AS pedido_ait,
       w.produtos_faltantes AS produtos_whisper, a.produtos_faltantes AS produtos_ait
FROM {GOLD_W} w JOIN {GOLD_A} a USING (call_id)
WHERE NOT (w.tipo_problema <=> a.tipo_problema)
   OR NOT (w.suspeita_fraude <=> a.suspeita_fraude)
   OR NOT (nullif(w.pedido_numero,'') <=> nullif(a.pedido_numero,''))
ORDER BY 1
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Custo
# MAGIC
# MAGIC Os dois são cobrados no SKU `SERVERLESS_REAL_TIME_INFERENCE`, mas de
# MAGIC formas diferentes:
# MAGIC
# MAGIC - **Whisper**: DBU por hora de GPU **enquanto o endpoint está de pé**,
# MAGIC   com ou sem request. O billing marca `endpoint_name`.
# MAGIC - **ai_transcribe**: DBU pelo áudio processado, no produto `AI_FUNCTIONS`
# MAGIC   com `product_features.ai_functions.ai_function = 'AI_TRANSCRIBE'`.
# MAGIC
# MAGIC Se a seção abaixo vier vazia, o billing ainda não chegou. Rode de novo
# MAGIC com `rodar_benchmark = nao` daqui a algumas horas.

# COMMAND ----------

# DBTITLE 1,DBU observado na janela do benchmark
janela = spark.sql(f"""
SELECT min(started_at) AS ini, max(ended_at) AS fim,
       sum(CASE WHEN transcriber = 'ai_transcribe' THEN audio_seconds END) / 3600 AS horas_audio
FROM {BENCH} WHERE bench_id = '{BENCH_ID}'
""").first()

ws_id = ctx.workspaceId().get()

custo = spark.sql(f"""
WITH u AS (
  SELECT u.*, p.pricing.default AS preco
  FROM system.billing.usage u
  JOIN system.billing.list_prices p
    ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.price_end_time IS NULL
  WHERE u.workspace_id = '{ws_id}'
    AND u.usage_end_time   >= timestamp'{janela['ini']}' - INTERVAL 5 MINUTES
    AND u.usage_start_time <= timestamp'{janela['fim']}' + INTERVAL 5 MINUTES
)
SELECT 'ai_transcribe' AS solucao, round(sum(usage_quantity), 3) AS dbu, round(sum(usage_quantity * preco), 4) AS usd_lista
FROM u WHERE product_features.ai_functions.ai_function = 'AI_TRANSCRIBE'
UNION ALL
SELECT 'whisper', round(sum(usage_quantity), 3), round(sum(usage_quantity * preco), 4)
FROM u WHERE usage_metadata.endpoint_name = '{WHISPER_ENDPOINT}'
""")
display(custo)
print(f"Janela {janela['ini']} → {janela['fim']}, {janela['horas_audio']:.3f} h de áudio por solução")

# COMMAND ----------

# DBTITLE 1,Projeção mensal
# Whisper: o custo é tempo de GPU de pé. Numa execução batch, isso é cold start
# + processamento + a cauda até o scale-to-zero desligar, que o Model Serving
# só faz depois de um período sem tráfego. Modelamos a cauda como 30 minutos.
#
# ai_transcribe: o custo é proporcional ao áudio. Usamos o DBU por hora de
# áudio medido acima; enquanto o billing não chega, a projeção fica vazia.
WHISPER_DBU_H = spark.sql(f"""
SELECT sum(usage_quantity) / (count(DISTINCT usage_start_time) * 5 / 60) AS dbu_h
FROM system.billing.usage
WHERE workspace_id = '{ws_id}' AND usage_metadata.endpoint_name = '{WHISPER_ENDPOINT}'
  AND usage_date >= current_date() - 7
""").first()["dbu_h"] or 20.0
PRECO = 0.07
CAUDA_H = 0.5

perf = spark.sql(f"""
SELECT transcriber, sum(latency_s) AS lat, sum(CASE WHEN NOT cold_start THEN latency_s END) AS lat_quente,
       max(CASE WHEN cold_start THEN latency_s END) AS cold
FROM {BENCH} WHERE bench_id = '{BENCH_ID}' GROUP BY 1
""").toPandas().set_index("transcriber")
rtf_whisper = float(perf.loc["whisper", "lat_quente"]) / (janela["horas_audio"] * 3600)
cold_h = float(perf.loc["whisper", "cold"] or 0) / 3600

ait = {r["solucao"]: r for r in custo.collect()}
ait_dbu_por_hora_audio = (ait["ai_transcribe"]["dbu"] or 0) / janela["horas_audio"] if janela["horas_audio"] else 0

gpu_h_mes = HORAS_MES * rtf_whisper + EXECUCOES_MES * (cold_h + CAUDA_H)
proj = pd.DataFrame([
    {"cenario": "Whisper, scale-to-zero funcionando", "dbu_mes": gpu_h_mes * WHISPER_DBU_H},
    {"cenario": "Whisper, endpoint de pé 24x7", "dbu_mes": 730 * WHISPER_DBU_H},
    {"cenario": "ai_transcribe", "dbu_mes": HORAS_MES * ait_dbu_por_hora_audio or None},
])
proj["usd_lista_mes"] = proj["dbu_mes"] * PRECO
print(f"{HORAS_MES:.0f} h de áudio/mês em {EXECUCOES_MES} execuções | Whisper {WHISPER_DBU_H:.1f} DBU/h de GPU, "
      f"fator de tempo real {rtf_whisper:.3f} | ai_transcribe {ait_dbu_por_hora_audio:.2f} DBU por hora de áudio")
display(proj.round(2))
