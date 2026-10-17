# Ligações sintéticas com gabarito

30 ligações de chamado de falta inteiramente sintéticas, no mesmo formato das
gravações de uma central telefônica (WAV G.711 μ-law, 8 kHz, estéreo, agente num
canal e consultora no outro), cada uma com o gabarito do que o gold deveria
extrair.

O gabarito é o que ligação de verdade não tem. Com ele dá para medir o pipeline de
ponta a ponta — taxa de erro da transcrição e acerto de cada campo de negócio —
em vez de só comparar uma transcrição com a outra.

## Como gerar

```bash
pip install -r requirements.txt
brew install espeak-ng ffmpeg   # o Kokoro usa o espeak-ng para fonemizar português

# 1. Roteiros: 30 diálogos escritos por um LLM do workspace, com o gabarito.
#    Já estão em roteiros.json; só rode se quiser outros cenários.
python gerar_roteiros.py --profile <perfil>

# 2. Áudio: duas vozes por ligação, ambiente, quedas de sinal e banda de telefone.
#    Cerca de uma hora de áudio, de 20 a 40 minutos num Mac com GPU (MPS).
python sintetizar.py --saida ./audios

# 3. Volume: os WAVs e o _reference_scripts.json, que o notebook de comparação lê.
databricks fs cp -r ./audios dbfs:/Volumes/<catalog>/<schema>/<volume> -p <perfil>
```

Depois rode o pipeline (`01` → `02b` → `03`, este último com os widgets
`table_silver=silver_transcripts_ait` e `table_gold=gold_call_insights_ait`) e meça:

```bash
python avaliar.py --profile <perfil> --warehouse <id> \
  --silver <catalog>.<schema>.silver_transcripts_ait \
  --gold <catalog>.<schema>.gold_call_insights_ait
```

## O que sai

| Arquivo | O que é |
|---|---|
| `roteiros.json` | Os 30 diálogos, fala a fala, com gabarito: `tipo_problema`, `suspeita_fraude`, `reincidencia`, `sentiment`, `consultora_codigo`, `pedido_numero`, `produtos_faltantes` |
| `audios/sint_NNN.wav` | As ligações (fora do git; vivem no Volume) |
| `audios/_reference_scripts.json` | Os roteiros com as vozes e o ambiente sorteados em cada ligação |

Os cenários cobrem os cinco valores de `tipo_problema` (de 5 a 7 ligações cada),
com reincidência em 8 delas, os três sentimentos e 6 ligações com consultor homem.

## Resultado no fevm-field-toolbox (17/10/2026)

Pipeline com `ai_transcribe` e o gold em `databricks-claude-sonnet-4-6`, nas 30
ligações, em três rodadas do gold sobre a mesma silver:

| Medida | Prompt original | + definições e linhas de produto | + "nome completo, nunca só a linha" |
|---|---|---|---|
| Erro de palavras da transcrição | 5,2% (pior 8,5%) | 5,2% | 5,2% |
| `tipo_problema` | 23/30 | **30/30** | **30/30** |
| `suspeita_fraude` | 30/30 | 30/30 | 30/30 |
| `reincidencia` | 30/30 | 30/30 | 30/30 |
| `consultora_codigo` | 29/30 | 29/30 | 29/30 |
| `sentiment` | 25/30 | 25/30 | 25/30 |
| `pedido_numero` | 24/30 | 24/30 | 24/30 |
| `produtos_faltantes` | 21/22 | 8/22 | **22/22** |

A primeira rodada está guardada em `gold_call_insights_ait_v1`.

O que as rodadas ensinam:

- **Categoria sem definição vira chute.** No prompt original as cinco ligações de
  fraude saíram como `violacao_caixa`: o modelo acertava que havia suspeita
  (`suspeita_fraude` 30/30), mas não tinha como saber que "caixa aberta com
  produto sumido" é fraude e "caixa aberta sem nada sumido" é violação. As mesmas
  definições que geraram os roteiros, coladas no prompt, levaram a 30/30.
- **Instrução nova tem efeito colateral.** A lista de linhas de produto, posta para
  corrigir grafia, fez o modelo devolver só a linha ("Diário") em vez do produto,
  e os faltantes caíram de 21 para 8. Dizer para que a lista serve resolveu. É o
  motivo de medir todos os campos a cada mudança de prompt, não só o que se quis
  consertar.
- **Número ditado dígito a dígito perde dígitos na transcrição.** Dígito repetido
  vira um só ("sete nove três três três sete" sai `7937`) e o "um" do começo vira
  artigo. O erro já está na silver, não no gold — prompt não conserta. O caminho é
  validar o número contra a tabela de pedidos da consultora.
- **Sentimento neutro tende a negativo.** Ligação de problema com tom calmo é
  lida como negativa na maioria das divergências.

## Limites

- Áudio sintético ainda é mais fácil que ligação real. A degradação de telefone
  pesa, mas a voz sintética é regular e articulada. Use este conjunto para medir o
  gold e pegar regressão no pipeline; antes de prometer acurácia a um cliente,
  rode a transcrição numa amostra de ligações dele.
- O Kokoro tem só três vozes de português do Brasil (`pf_dora`, `pm_alex` e
  `pm_santa`). As outras saem de misturas com vozes de espanhol e italiano e de
  variações de tom; duas consultoras podem soar parecidas.
- A Aurélia, os produtos, as consultoras e os números são fictícios.

## Licenças

Kokoro-82M e as vozes: Apache-2.0. O espeak-ng (GPL-3.0) é chamado como
programa externo para fonemizar; não é distribuído com o asset.
