# Modelo de fraude em chamado de falta

Prevê a probabilidade de um chamado de falta ser fraude confirmada, para o time
de investigação abrir primeiro os casos que mais valem a pena. Lê
`gold_features_chamado` (pipeline Lakeflow) e devolve uma probabilidade calibrada.

| Arquivo | O que faz |
|---|---|
| `01_treinar_modelo.py` | treina três candidatos, compara no mesmo teste, registra o campeão no UC |
| `modelo_fraude.py` | o wrapper pyfunc que devolve a probabilidade; vai com o modelo por `code_paths` |
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

O job `aurelia-speech-diario` roda o pipeline, a pontuação e o refresh da fila no
Lakebase (agendado às 6h, pausado). O treino roda à parte: retreinar todo dia sem rótulo novo só troca
um modelo por outro igual.

## Como o modelo é treinado

- **Só com o que tem rótulo.** Rótulo existe onde houve investigação (~20% dos
  chamados fechados). A política investiga mais o que é caro e reincidente, então o
  modelo aprende a fraude que a investigação costuma achar — viés que está dito no
  README do gerador.
- **Rótulo de duas fontes.** A investigação formal e, onde ela não houve, a decisão
  do analista no app, que volta do Lakebase pelo CDF (`origem_rotulo`).
- **Teste temporal, cortado por data.** Os chamados investigados a partir de
  09/06/2026 ficam de fora. Sorteio misturaria futuro e passado e daria uma métrica
  otimista.
- **Gradient boosting com calibração isotônica.** O GBM ordena; a calibração
  corrige a escala para "0,7" significar sete em dez. A `fraude_confianca` que o LLM
  declara não tem essa propriedade.
- **Três candidatos no mesmo teste**, e o completo só é campeão se o ganho de
  PR-AUC sobre o operacional for real: intervalo de 95% de um bootstrap pareado
  (1.000 reamostras do teste) inteiro acima de zero. Depender da ligação tem custo,
  e ruído não paga.

## Resultado (teste temporal: 512 chamados, 14% de fraude)

| Candidato | PR-AUC | Precisão nos 50 primeiros | Brier |
|---|---|---|---|
| LLM sozinho (`fraude_confianca`) | 0,18 | 30% | 0,157 |
| Operacional | **0,83** | **88%** | 0,044 |
| Completo (operacional + ligação) | 0,83 | 84% | 0,044 |

Ganho do completo sobre o operacional: −0,001 de PR-AUC, IC 95% de −0,022 a
+0,021. Campeão: **operacional**, versão 5, servido em `aurelia-fraude-chamado`.

Na fila aberta (564 chamados, 10% de fraude pelo gabarito do gerador), em ordem de
score:

| Primeiros | Fraude real | Fraudes achadas de 58 | Valor em fraude na frente |
|---|---|---|---|
| 20 | 100% | 20 | R$ 11,4 mil |
| 50 | 96% | 48 | R$ 21,7 mil |
| 100 | 58% | 58 | todos os R$ 23,2 mil |

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

- **O corte por posição fazia a métrica oscilar.** Ordenar por data com sort
  instável e cortar nos 75% mandava os chamados do mesmo dia na fronteira para o
  treino ou para o teste conforme a ordem em que o Spark devolvia as linhas. A
  PR-AUC variava ~0,02 entre execuções com o mesmo dado — mais que a diferença
  entre os candidatos. Uma rodada chegou a eleger o completo com 0,840 contra
  0,812. O corte agora é por data, com desempate por id, e duas execuções seguidas
  dão o mesmo número.
- **Classe definida no notebook não atravessa versão de Python.** O cloudpickle
  guarda por valor o código do wrapper; salvo num serverless com 3.10, o modelo
  não carregava no job com 3.12 (`code() argument 13 must be str, not int`). O
  wrapper mora em `modelo_fraude.py` e vai por `code_paths`, guardado por referência.

- **Decimal e booleano com nulo chegam do Spark como `object`.** O wrapper converte
  com `pd.to_numeric`, mas booleano com `None` precisa de `astype("float64")` antes.
  A pontuação faz isso com a lista de colunas que o próprio modelo guarda.
- **`display` de DataFrame com coluna mista falha na conversão para Arrow** — número
  e texto na mesma coluna. É a última célula da pontuação; as tabelas já estavam
  gravadas, mas o job saía com erro.
- **O endpoint é scale-to-zero.** A primeira chamada depois de um tempo parado leva
  alguns segundos a mais. Confira o billing, não só a configuração.
