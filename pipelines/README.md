# Pipelines de Speech Analytics — Aurélia PPM Falta

Dois pipelines completos, lado a lado, que ingerem áudios de chamadas de
consultoras Aurélia, transcrevem com Whisper e extraem insights de negócio
com Claude — focados no caso de uso **PPM Falta** (atrasos, faltas, possíveis
fraudes em entregas).

```
pipelines/
├── .env.example            ← copie para .env e ajuste catalog/schema
├── 00_setup_env.py         ← cria volume + gera 3 áudios sintéticos via gTTS
│
├── imperative/             ← Pipeline 1: Spark imperativo (PySpark + Auto Loader)
│   ├── 01_bronze_audio_ingestion.py
│   ├── 02_silver_transcription.py      ← Whisper em Model Serving
│   ├── 02b_silver_ai_transcribe.py     ← ai_transcribe, sem endpoint
│   └── 03_gold_ai_query_analysis.py    ← widgets table_silver/table_gold
│
├── declarative/            ← Pipeline 2: Spark Declarative Pipelines (SDP) 100% SQL
│   ├── pipeline.yml        ← config do pipeline serverless
│   ├── 01_bronze.sql
│   ├── 02_silver_transcript.sql                 ← Whisper
│   ├── 02_silver_transcript_ai_transcribe.sql   ← ai_transcribe (troque no pipeline.yml)
│   └── 03_gold_insights.sql
│
└── comparacao/
    └── 04_comparacao_whisper_vs_ai_transcribe.py ← latência, qualidade e custo
```

## Duas versões da transcrição

A silver existe com **Whisper** (endpoint GPU próprio) e com **`ai_transcribe`**
(AI Function serverless, Beta). As duas publicam `call_id` e `full_text`, que é
tudo o que o gold e o portal leem.

No imperativo elas convivem: o `02b` grava em `TABLE_SILVER_AIT` e o `03`, com
os widgets `table_silver` e `table_gold`, gera a gold correspondente. No
declarativo é uma ou outra, porque as duas publicam `silver_transcripts`.

O `ai_transcribe` exige o preview *AI Transcribe* ligado por um admin. O
resultado da comparação está em
[`../../docs/comparacao-whisper-vs-ai-transcribe.md`](../../docs/comparacao-whisper-vs-ai-transcribe.md).

## Os dois pipelines fazem o mesmo

| Etapa | O que acontece |
|---|---|
| **Bronze** | Auto Loader / `read_files` lê novos áudios do volume `/Volumes/.../audio_landing/` |
| **Silver** | Cada áudio é transcrito via endpoint Whisper. **Só áudios novos** são processados |
| **Gold** | `ai_query` chama o Claude, devolvendo JSON estruturado com sentimento, intenção, suspeita de fraude, produtos faltantes, próxima ação… |

Ambos rodam em **modo batch** (`trigger=availableNow` no imperativo, modo
*triggered* no SDP) — alinhado com a decisão da reunião com a Aurélia.

## Diferença entre os dois — quando usar cada um?

| | **Imperativo (PySpark)** | **Declarativo (SDP / Lakeflow)** |
|---|---|---|
| Linguagem | Python + PySpark | SQL puro (100%) |
| Linhas de código | ~300 | ~120 |
| Orquestração | Job multi-task encadeado | Pipeline único declarativo |
| Quem mantém | Engenheiros de dados | Engenheiros + analistas de SQL |
| Idempotência | Manual (checkpoints + MERGE) | Automática (SDP gerencia) |
| CDC / só-novos | `withColumn` + `MERGE INTO` | `STREAMING TABLE` faz sozinho |
| Lineage | Manual no UC | Automático e visual no UI |
| Schedule e retry | Configurar no Job | Built-in no pipeline |
| Observabilidade | Logs do Spark UI | Pipeline UI com expectations, métricas, eventos |
| Recomendado para | Lógica complexa, integrações externas | ELT padrão, governança, multi-time |

> Para o caso da Aurélia, o **SDP é a escolha natural** quando o time já está
> confortável com SQL. Roda fim-a-fim com 1/3 do código, governança
> automática e dashboards de pipeline prontos. Veja vantagens detalhadas
> no `declarative/README.md`.

## Como rodar — primeira vez

### 1. Configure o ambiente

```bash
cd pipelines
cp .env.example .env
# edite .env se quiser mudar catalog/schema
```

### 2. Suba o setup (uma vez só)

Rode o notebook `00_setup_env.py` num cluster ou serverless compute. Ele:
- Cria o volume `/Volumes/{catalog}/{schema}/audio_landing/`
- Gera 3 áudios sintéticos PT-BR via gTTS:
  1. Reclamação de atraso (consultora normal)
  2. Suspeita de fraude (faltou produto cara, suspeita de roubo)
  3. Falta parcial de itens (problema de separação)
- Faz upload dos `.mp3` para o volume

### 3. Endpoint do Whisper

O endpoint `aurelia-whisper-turbo` já está rodando no workspace. Se não
estiver, suba primeiro o notebook `notebooks/deploy_whisper_job.py` da
raiz do projeto.

### 4. Escolha um pipeline

- **Imperativo**: rode os 3 notebooks em sequência ou crie um Job em
  `imperative/` com 3 tarefas encadeadas.
- **Declarativo (SDP)**: importe os SQLs em `declarative/`, crie um
  pipeline serverless apontando pra eles.

## Arquivos `.env`

Os scripts Python lêem o `.env` automaticamente via `python-dotenv` (já
disponível em todos os runtimes Databricks). Os SQLs do SDP usam
[parâmetros do pipeline](declarative/pipeline.yml).
