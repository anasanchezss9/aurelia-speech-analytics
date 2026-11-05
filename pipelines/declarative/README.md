# Pipeline declarativo (Lakeflow) — 100% SQL

Uma jornada só, do arquivo à feature do modelo. A ligação vira transcrição, a
transcrição vira análise estruturada, e a análise se junta ao operacional —
pedido, entrega, consultora, investigação — nas tabelas gold que o modelo de
fraude, a fila do analista e o Genie leem.

```
declarative/
├── pipeline.yml                            config do pipeline (snippet de bundle)
├── 01_bronze.sql                           áudios, 10 tabelas operacionais e transcrições em texto
├── 02_silver_transcript_ai_transcribe.sql  áudio → texto com ai_transcribe (padrão)
├── 02_silver_transcript.sql                áudio → texto com Whisper em Model Serving (alternativa)
├── 02c_silver_transcricoes.sql             as duas origens de transcrição numa tabela, por dois flows
├── 03_gold_insights.sql                    ai_query com JSON Schema → campos de negócio por ligação
├── 04_silver_operacional.sql               operacional tipado, com expectativas de qualidade
└── 05_gold_operacional.sql                 features por chamado, fila, validação, agregados por ciclo
```

```
Volume audio_landing ──► bronze_audio_files ──► silver_transcripts (ai_transcribe) ─┐
Volume operacional/transcricoes_texto ──► bronze_transcricoes_texto ───────────────┴► silver_transcricoes
                                                                                         │
                                                                                         ▼
                                                                                gold_call_insights (ai_query)
Volume operacional/* ──► bronze_* ──► silver_pedidos, silver_entregas,                   │
                                      silver_chamados, silver_consultoras ───────────────┤
                                                                                         ▼
                                       gold_features_chamado · gold_fila_investigacao · gold_ligacoes_validadas
                                       gold_chamados_ciclo · gold_pedidos_ciclo
```

## As decisões que valem explicar

**Duas origens de transcrição, um gold.** Parte das ligações chega como áudio e
precisa ser transcrita; parte a telefonia já exporta em texto. Os dois flows de
`02c` escrevem na mesma `silver_transcricoes`, e o gold lê só ela. Fonte nova é um
`CREATE FLOW` a mais, sem mexer no gold.

**Cada ligação passa pelo LLM uma vez.** O gold é `STREAMING TABLE` sobre
`STREAM silver_transcricoes`: o `ai_query` roda só no que é novo. Reprocessar tudo
a cada execução multiplicaria o custo de token pelo número de execuções.

**O peso da caixa separa as causas.** `silver_entregas` calcula a divergência de
peso no CD (expediu mais leve que o teórico → erro de separação) e no hub (pesou
menos que a expedição → perda no transporte). A ligação diz *que* algo sumiu; o
peso diz *onde*.

**Feature só com o que se sabia na abertura.** Em `gold_features_chamado`, o
histórico da consultora conta para trás a partir da data do chamado, e fraude
anterior só conta se a investigação já tinha concluído. Sem isso o modelo aprende
com o futuro e a métrica de teste mente.

**O número ditado ao telefone é conferido, não confiado.** Dígito repetido some na
transcrição ("793337" sai "7937"), e prompt nenhum recupera. `gold_ligacoes_validadas`
confere a consultora e o pedido extraídos contra o cadastro e, quando não batem,
escolhe o pedido da consultora com até 2 dígitos de diferença.

## Resultado no fevm-field-toolbox (05/11/2026)

1.229 ligações (30 em áudio, 1.199 em texto), 9.884 chamados, 210.909 pedidos.
Execução completa em ~10 min, serverless.

| Medida | Resultado |
|---|---|
| Gabarito das 30 ligações em áudio | erro de palavras 5,2%; `tipo_problema`, `suspeita_fraude` e produtos 100% |
| Pedido certo, só com o que o LLM extraiu | 80% no áudio · 86,6% no texto |
| Pedido certo, depois da validação contra o cadastro | **100%** no áudio · **97,7%** no texto |
| Expectativas de qualidade | 8, nenhuma linha derrubada (dado sintético limpo) |

O que o LLM vê na ligação, pela causa real do chamado (1.199 ligações em texto):

| Causa real | `suspeita_fraude` marcada |
|---|---|
| Furto em trânsito | 100% |
| Fraude da consultora | 23% |
| Fraude oportunista | 18% |
| Erro de separação, atraso, extravio, outro | 0% |

É a razão de existir o modelo: pela ligação, "caixa aberta e o perfume sumiu" é
igual quando quem levou foi o transportador e quando foi a consultora. O LLM acerta
que houve desvio e não tem como saber quem desviou. O sinal da ligação entra como
feature, ao lado do peso, do histórico e da rota.

## Como deployar

O `pipeline.yml` é um snippet de bundle (`include: pipelines/declarative/pipeline.yml`
no `databricks.yml`). Os parâmetros em `configuration` viram `${var}` nos SQLs:
`catalog`, `schema`, `volume_landing`, `volume_operacional`, `whisper_endpoint`,
`llm_endpoint`.

Requer o preview **AI Transcribe** ligado no workspace. Arquivo sobrescrito com o
mesmo nome não é relido pelo `read_files`: ao regerar o dado sintético, rode com
**full refresh**.

## Quando o imperativo (`../imperative/`) ainda faz sentido

Quando a lógica precisa de biblioteca Python que SQL não tem (pré-processamento de
áudio, OCR, modelo local), ou de controle fino de paralelismo. Ele grava em
tabelas próprias (`bronze_audio_files_imperativo`, `*_ait`), porque uma tabela de
pipeline declarativo não aceita escrita de fora dele.
