# Pipeline imperativo (PySpark)

Pipeline em 3 etapas, cada uma num notebook separado, encadeáveis num
Job multi-task.

| Etapa | Notebook | Tabela de saída | Custo dominante |
|---|---|---|---|
| Bronze | `01_bronze_audio_ingestion.py` | `bronze_audio_files_imperativo` | I/O do volume |
| Silver | `02_silver_transcription.py` | `silver_transcripts` | GPU do endpoint Whisper |
| Gold | `03_gold_ai_query_analysis.py` | `gold_call_insights` | Tokens do Claude |

## Como roda em batch (apesar de usar Spark Streaming)

A etapa Bronze usa **Auto Loader** com `trigger(availableNow=True)` —
isso significa: "leia tudo que ainda não foi lido, escreva, e termine".
É exatamente o comportamento de um job batch idempotente, mas com a
infra-estrutura do streaming garantindo que **só os arquivos novos**
sejam processados.

A etapa Silver usa `LEFT ANTI JOIN` + `MERGE INTO` — semântica clássica
de batch incremental.

A etapa Gold também usa `LEFT ANTI JOIN` + `INSERT INTO` antes do
`ai_query`, então o LLM **só é chamado para transcrições novas**.

## Como agendar como Job

```yaml
# trecho de databricks.yml
resources:
  jobs:
    aurelia_speech_pipeline:
      name: aurelia-speech-imperative
      schedule:
        quartz_cron_expression: "0 0 */4 * * ?"  # a cada 4 horas
        timezone_id: America/Sao_Paulo
      tasks:
        - task_key: bronze
          notebook_task:
            notebook_path: ./01_bronze_audio_ingestion.py
        - task_key: silver
          depends_on: [{ task_key: bronze }]
          notebook_task:
            notebook_path: ./02_silver_transcription.py
        - task_key: gold
          depends_on: [{ task_key: silver }]
          notebook_task:
            notebook_path: ./03_gold_ai_query_analysis.py
```

## Pontos fortes do imperativo

- **Flexibilidade**: você controla cada passo, pode injetar lógica
  custom (ex: pular áudios > X minutos, regravar com retry específico).
- **Familiar**: time que já conhece PySpark cai logo de cabeça.
- **Integrações Python ricas**: pode usar bibliotecas que não existem
  no SQL (ex: split de áudio com `pydub` antes da transcrição).

## Pontos fracos vs SDP

- **Mais código**: ~300 linhas vs ~120 do SDP.
- **Você gerencia idempotência**: checkpoints, MERGE, ANTI JOIN são suas.
- **Lineage manual**: precisa configurar tags / tabelas para o UC ver.
- **Sem dashboard de pipeline**: você tem o Spark UI, mas não visões
  por etapa, expectations, qualidade de dados consolidada.
