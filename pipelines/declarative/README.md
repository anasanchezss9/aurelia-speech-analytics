# Pipeline declarativo (Spark Declarative Pipelines / Lakeflow) — 100% SQL

Mesmo pipeline da pasta `imperative/`, mas escrito **só em SQL**, com
SDP cuidando de toda a parte mecânica.

```
declarative/
├── pipeline.yml              ← config do pipeline serverless
├── 01_bronze.sql             ← STREAMING TABLE com STREAM read_files
├── 02_silver_transcript.sql  ← STREAMING TABLE chama Whisper via ai_query
├── 03_gold_insights.sql      ← STREAMING TABLE chama Claude via ai_query
└── README.md                 ← este arquivo
```

## Sim, dá pra fazer 100% em SQL

E é o ponto que mais impressiona: **transcrição com Whisper é uma
chamada `ai_query` em SQL**. Como o endpoint do Whisper é um modelo
pyfunc registrado no Unity Catalog, o `ai_query` aceita um `STRUCT` no
parâmetro `request`, mapeando direto para o `dataframe_records` que o
pyfunc espera (`audio_b64` + `language`).

```sql
ai_query(
  'aurelia-whisper-turbo',
  named_struct('audio_b64', audio_b64, 'language', 'pt'),
  returnType => 'STRUCT<text: STRING, language: STRING, duration: DOUBLE>',
  failOnError => false
)
```

Ou seja, o time de SQL do PIM da Aurélia **não precisa escrever uma
linha de Python** para rodar este pipeline ponta-a-ponta.

---

## Vantagens do SDP fim a fim — comparado ao imperativo

### 1. **Menos código, mais semântica**
- ~120 linhas de SQL contra ~300 de PySpark.
- O que está escrito **é** o pipeline: ler arquivos → transcrever → analisar.

### 2. **Idempotência grátis**
- `STREAMING TABLE` + `FROM STREAM` garante que cada `call_id` é
  processado uma única vez. Nada de `MERGE INTO`, `LEFT ANTI JOIN`,
  nem checkpoints manuais.

### 3. **Lineage automática no Unity Catalog**
- O SDP registra a relação entre `bronze_audio_files` →
  `silver_transcripts_clean` → `gold_call_insights` automaticamente.
- Aparece direto no UC e no Catalog Explorer.

### 4. **Pipeline UI: visual, métricas, expectations**
- Cada execução tem um painel com:
  - Mapa do DAG colorido por etapa (sucesso/erro/skipped).
  - Linhas processadas por etapa em cada run.
  - `EXPECT` clauses para data quality (ex: rejeitar transcrições com `duration_s = 0`).
  - Eventos de mudança de schema, falhas, retries.

### 5. **Modo triggered = batch puro**
- `continuous: false` no `pipeline.yml` faz o SDP rodar como um job
  batch: lê o que tiver de novo, processa, termina. Custo só durante
  a execução. Combina com o que a Aurélia quer (batch).

### 6. **Schedule, retry e alertas no próprio pipeline**
- Não precisa de Job multi-task encadeado. O pipeline tem schedule
  próprio, retries built-in, e alertas via webhook.

### 7. **Multi-ambiente nativo via Asset Bundles**
- Mesmo arquivo, parâmetros diferentes para `dev`, `staging`, `prod`.
- O `pipeline.yml` só muda 5 valores em `configuration`.

### 8. **Quality, Quarantine e CDC prontos**
- Adicionar `EXPECT (length(full_text) > 50) ON VIOLATION DROP ROW`
  vira 1 linha SQL.
- `AUTO CDC` permite slowly-changing dimensions sem código extra
  (útil quando a Aurélia quiser dimensão de Consultora histórica).

### 9. **Mesmo otimizador, sem tunar manualmente**
- `pipelines.autoOptimize.managed = true` (default) cuida de Z-Order,
  Auto Compaction, vacuum.
- Photon ativado por padrão em serverless.

### 10. **Custo previsível**
- Serverless cobra por unidade de cluster ativo. Em modo triggered,
  só paga durante a execução. Os áudios e o LLM cobram à parte (Whisper
  por GPU enquanto o endpoint estiver up; Claude por token).

---

## Quando o imperativo ainda faz sentido?

- Quando a lógica precisa de bibliotecas Python que SQL não tem
  (ex: pré-processamento de áudio com `pydub`, OCR com `pytesseract`,
  modelos local sem endpoint).
- Quando há lógica condicional pesada que ficaria ilegível em SQL.
- Quando você precisa controlar fino o paralelismo (ex: GPU pinning,
  batch size custom para um modelo).

Para o caso PPM Falta da Aurélia, **nenhum desses se aplica**: o
pipeline é ELT padrão (extract bytes → transform via modelos → load
em tabelas). SDP em SQL é a recomendação.

---

## Como deployar

### Via Asset Bundles (recomendado)

O `pipeline.yml` desta pasta é um snippet do bundle. Adicione ao
`databricks.yml` do projeto:

```yaml
include:
  - pipelines/declarative/pipeline.yml
```

Depois:

```bash
databricks bundle deploy --target dev
databricks bundle run aurelia_speech_sdp --target dev
```

### Via UI (rápido para prototipar)

1. Crie um pipeline novo: **Workflows > Pipelines > Create pipeline**
2. Escolha **Serverless**, **Triggered**.
3. Em **Notebook libraries**, adicione os 3 arquivos SQL desta pasta.
4. Em **Configuration**, adicione os 5 parâmetros do `pipeline.yml`:
   - `catalog`, `schema`, `volume_landing`, `whisper_endpoint`, `llm_endpoint`.
5. Salve e clique em **Start**.

### Adicionando novos áudios

Faça `cp` para `/Volumes/{catalog}/{schema}/audio_landing/` e dispare
o pipeline. Ele detecta automaticamente os novos arquivos e processa
**só eles** (Whisper só roda nos novos, ai_query do Claude também só
roda nos novos).
