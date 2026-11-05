# Modelo de fraude em chamado de falta

Prevê a probabilidade de um chamado de falta ser fraude confirmada, para o time
de investigação abrir primeiro os casos que mais valem a pena. Lê
`gold_features_chamado` (pipeline Lakeflow) e devolve uma probabilidade calibrada.

| Arquivo | O que faz |
|---|---|
| `01_treinar_modelo.py` | treina três candidatos, compara no mesmo teste, registra o campeão no UC |
| `02_pontuar_chamados.py` | pontua todos os chamados e grava a fila priorizada |

```
gold_features_chamado ──► 01_treinar_modelo ──► field_toolbox_catalog.aurelia_speech.modelo_fraude_chamado @campeao
                                                      │
                     ┌────────────────────────────────┼─────────────────────────────┐
                     ▼                                ▼                             ▼
         02_pontuar_chamados (batch)      endpoint aurelia-fraude-chamado     (retreino com as
         score_fraude_chamado             (score sob demanda no app)           decisões do analista)
         fila_priorizada
```

O job `aurelia-speech-diario` roda o pipeline e depois a pontuação (agendado às
6h, pausado). O treino roda à parte: retreinar todo dia sem rótulo novo só troca
um modelo por outro igual.

## Como o modelo é treinado

- **Só com o que tem rótulo.** Rótulo existe onde houve investigação (~20% dos
  chamados fechados). A política investiga mais o que é caro e reincidente, então o
  modelo aprende a fraude que a investigação costuma achar — viés que está dito no
  README do gerador.
- **Teste temporal.** Os 25% investigados mais recentes ficam de fora (desde
  09/06/2026). Sorteio misturaria futuro e passado e daria uma métrica otimista.
- **Gradient boosting com calibração isotônica.** O GBM ordena; a calibração
  corrige a escala para "0,7" significar sete em dez. A `fraude_confianca` que o LLM
  declara não tem essa propriedade.
- **Três candidatos no mesmo teste**, e o completo só é campeão se somar 0,01 de
  PR-AUC: depender da ligação tem custo, e empate não paga.

## Resultado (teste temporal: 507 chamados, 14% de fraude)

| Candidato | PR-AUC | ROC-AUC | Precisão nos 50 primeiros | Brier |
|---|---|---|---|---|
| LLM sozinho (`fraude_confianca`) | 0,18 | 0,59 | 30% | 0,155 |
| Operacional | **0,82** | 0,97 | **88%** | 0,044 |
| Completo (operacional + ligação) | 0,81 | 0,97 | 86% | 0,045 |

Campeão: **operacional**, versão 2.

Na fila aberta (564 chamados, 10% de fraude pelo gabarito do gerador), em ordem de
score:

| Primeiros | Fraude real | Fraudes achadas de 58 | Valor em fraude na frente |
|---|---|---|---|
| 20 | 100% | 20 | R$ 10,2 mil |
| 50 | 92% | 46 | R$ 20,2 mil |
| 100 | 57% | 57 | R$ 23,0 mil de R$ 23,2 mil |

## Por que a ligação ficou fora do score

A primeira versão do gold extraía `suspeita_fraude`, que responde "houve desvio?".
Ela marca 95% das ligações de furto em trânsito, que o peso da caixa no hub já
explica, e só 23% das fraudes da consultora. Como feature, não somou nada.

A segunda versão extrai `sinais_relato`: o comportamento da consultora no relato,
independente de quem desviou (relato vago ou contraditório, pressa pelo crédito,
resistência a mandar foto, "não deu para conferir na entrega"). Esse sinal separa:

| Chamados rotulados com ligação | Chamados | Fraude |
|---|---|---|
| Nenhum sinal no relato | 217 | 10% |
| Um sinal | 16 | 38% |
| Dois ou mais | 19 | **89%** |

Mesmo assim o completo não ganhou, e o motivo é cobertura: a transcrição começou
nos últimos ciclos, e o treino tem **90 chamados rotulados com ligação, 16 deles
fraude**. É pouco para o GBM aprender o peso do sinal. A decisão foi manter o
score operacional e mostrar os sinais do relato na fila, como evidência para o
analista. Cada decisão dele sobre um chamado com ligação é um rótulo que falta;
com algumas centenas, o completo deve passar o operacional.

## Armadilhas

- **Decimal e booleano com nulo chegam do Spark como `object`.** O wrapper converte
  com `pd.to_numeric`, mas booleano com `None` precisa de `astype("float64")` antes.
  A pontuação faz isso com a lista de colunas que o próprio modelo guarda.
- **`display` de DataFrame com coluna mista falha na conversão para Arrow** — número
  e texto na mesma coluna. É a última célula da pontuação; as tabelas já estavam
  gravadas, mas o job saía com erro.
- **O endpoint é scale-to-zero.** A primeira chamada depois de um tempo parado leva
  alguns segundos a mais. Confira o billing, não só a configuração.
