# Ligações sintéticas com gabarito

As dez ligações reais que provaram a demo são do cliente, autorizadas só para uso
interno, e não saem do Volume. Esta pasta gera um conjunto que pode circular: 30
ligações de PPM Falta inteiramente sintéticas, no mesmo formato das gravações da
central (WAV G.711 μ-law, 8 kHz, estéreo, agente num canal e consultora no
outro), cada uma com o gabarito do que o gold deveria extrair.

O gabarito é o que os áudios reais não têm. Com ele dá para medir o pipeline de
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

Depois rode o pipeline normal (`01` → `02b` → `03`) com um `.env` que aponte
`VOLUME_LANDING` para esse volume e as tabelas para nomes próprios (por exemplo
`sint_bronze_audio_files`, `sint_silver_transcripts_ait`,
`sint_gold_call_insights_ait`), para não misturar com a demo no ar. Por fim:

```bash
python avaliar.py --profile <perfil> --warehouse <id> \
  --silver <catalog>.<schema>.sint_silver_transcripts_ait \
  --gold <catalog>.<schema>.sint_gold_call_insights_ait
```

## O que sai

| Arquivo | O que é |
|---|---|
| `roteiros.json` | Os 30 diálogos, fala a fala, com gabarito: `tipo_problema`, `suspeita_fraude`, `reincidencia`, `sentiment`, `consultora_codigo`, `pedido_numero`, `produtos_faltantes` |
| `audios/sint_NNN.wav` | As ligações (fora do git; vivem no Volume) |
| `audios/_reference_scripts.json` | Os roteiros com as vozes e o ambiente sorteados em cada ligação |

Os cenários cobrem os cinco valores de `tipo_problema` (de 5 a 7 ligações cada),
com reincidência em 8 delas, os três sentimentos e 6 ligações com consultor homem.

## Resultado no fevm-field-toolbox (16/10/2026)

Pipeline com `ai_transcribe` e o gold em `databricks-claude-sonnet-4-6`, nas 30 ligações:

| Medida | Resultado |
|---|---|
| Taxa de erro de palavras da transcrição | 6,4% em média, 9,3% na pior |
| `suspeita_fraude` | 30/30 |
| `reincidencia` | 30/30 |
| `consultora_codigo` | 29/30 |
| `tipo_problema` | 26/30 |
| `sentiment` | 24/30 |
| `produtos_faltantes` | 18/22 produtos |
| `pedido_numero` | 22/30 |

O que os erros ensinam, e que vale para as ligações reais também:

- **Número ditado dígito a dígito perde dígitos na transcrição.** Dígito repetido
  vira um só ("sete nove três três três sete" sai `7937`) e o "um" do começo vira
  artigo (`140448` sai "um 40448"). O erro já está na silver, não no gold. Ainda
  não sabemos se é a voz sintética emendando as repetições ou o transcritor;
  ouça o trecho antes de afirmar qualquer um dos dois.
- **O gold confunde `violacao_caixa` com `fraude_suspeita`.** Nas cinco ligações de
  fraude ele acerta `suspeita_fraude` em todas, mas classifica três como
  violação. O prompt do gold não define as categorias; os roteiros usam as
  definições de `gerar_roteiros.py`. Defini-las no prompt é o próximo passo.
- **Marca vira palavra comum.** Nome de linha de produto que soa como palavra do
  dia a dia sai grafado como a palavra comum. Um vocabulário de produtos no prompt
  do gold, ou uma correção depois da transcrição, resolve.
- **Sentimento neutro tende a negativo.** Ligação de problema com tom calmo é
  lida como negativa em quatro das seis divergências.

## Limites

- Áudio sintético ainda é mais fácil que ligação real. A degradação de telefone
  pesa — o `ai_transcribe` erra 6,4% aqui contra 3,9% a 4,8% nos três áudios gTTS
  limpos do `00_setup_env` —, mas a voz sintética é regular e articulada, e nas
  ligações reais não há gabarito para saber quanto elas são mais difíceis. Use
  este conjunto para medir o gold e pegar regressão no pipeline, e as reais para
  provar ao cliente que a transcrição aguenta o tronco telefônico dele.
- O Kokoro tem só três vozes de português do Brasil (`pf_dora`, `pm_alex` e
  `pm_santa`). As outras saem de misturas com vozes de espanhol e italiano e de
  variações de tom; duas consultoras podem soar parecidas.
- Os roteiros mantêm o contexto da Aurélia (marca, produtos, "consultora"). São
  fictícios, mas o asset continua de uso interno.

## Licenças

Kokoro-82M e as vozes: Apache-2.0. O espeak-ng (GPL-3.0) é chamado como
programa externo para fonemizar; não é distribuído com o asset.
