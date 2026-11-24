# Como este build foi feito com IA

A IA trabalhou em dois papéis: como colega de construção (o agente que escreve código
e opera o workspace) e como peça do produto (transcrição, análise das ligações, dado
sintético, Genie). Em todos eles a regra foi a mesma: a IA propõe e executa, e cada
resultado é medido antes de seguir.

## Ferramentas

| Papel | Ferramenta | Para quê |
|---|---|---|
| Agente de código | Cursor com Claude | código do pipeline, do modelo, do app e dos scripts; operação do workspace pelo Databricks CLI |
| Operação | Databricks CLI e Asset Bundles, comandados pelo agente | schema, volumes, jobs, pipeline, endpoint, Lakebase, app |
| Roteiros das ligações | `databricks-claude-sonnet-5-5` | os 30 diálogos com gabarito (`sintetico/gerar_roteiros.py`) |
| Voz das ligações | Kokoro-82M (TTS local) | 30 ligações em áudio de telefonia, 8 kHz μ-law (`sintetico/sintetizar.py`) |
| Transcrições em texto | `databricks-claude-haiku-4-5` | 1.200 ligações que a telefonia exporta em texto (`dados_operacionais/gerar_transcricoes_texto.py`) |
| Transcrição no produto | `ai_transcribe` | áudio de telefonia direto no SQL do pipeline |
| Análise no produto | `ai_query` com `databricks-claude-sonnet-4-6` e JSON Schema | tipo do problema, produtos, números e sinais do relato (`pipelines/declarative/03_gold_insights.sql`) |
| Consulta | Genie | perguntas da prevenção de perdas sobre as tabelas gold (`genie/`) |
| Vídeo da demo | skill de gravação no Cursor, controlando o Chrome | o vídeo do README e o da apresentação |

O modelo que escreve as transcrições em texto (Haiku) é diferente do que as analisa no
gold (Sonnet), para o mesmo modelo não julgar o próprio texto.

## O ciclo de trabalho

1. **Eu defino o problema e as decisões**: o cliente, a pergunta de negócio, o que conta
   como fraude, o que o modelo pode e não pode usar.
2. **O agente propõe e constrói** um passo de cada vez, roda no workspace e devolve o
   número: erro de palavras, acerto por campo, PR-AUC, latência.
3. **Eu leio o número e decido** se o passo fica, muda ou sai. Nenhum passo seguinte
   começa sem o anterior medido.
4. **Cada pasta ganha um README** com as decisões, os números e as armadilhas, escrito
   no fim do passo, enquanto o motivo ainda está fresco.

O padrão que mais rendeu: **gabarito antes de prompt**. As 30 ligações sintéticas têm a
resposta certa, então toda mudança no prompt do gold é medida em todos os campos
(`sintetico/avaliar.py`), e não só no que se quis consertar.

## Pedidos ao agente, como foram escritos

Alguns pedidos que viraram partes do build, nas palavras em que foram feitos:

- *"A solução do speech analytics, será que daria para ficar mais robusta, expandir o
  escopo para adicionar Lakebase, Genie e ML?"* — virou o plano: mundo operacional
  sintético, pipeline de ponta a ponta, modelo, Lakebase e Genie.
- *"Você não consegue gerar os áudios de forma sintética?"* — virou a geração das 30
  ligações com o Kokoro, no formato de telefonia.
- *"Suba primeiro no workspace e execute com esses novos áudios."* — virou a primeira
  medição contra o gabarito, que expôs o erro de categoria no gold.
- *"Subir os dados operacionais, estender o pipeline Lakeflow, incluindo a validação do
  número do pedido contra os pedidos reais da consultora."* — virou a
  `gold_ligacoes_validadas`.

## Onde a IA errou e como foi pego

| O que aconteceu | Como foi pego | O que mudou |
|---|---|---|
| A primeira versão do gerador deixava a fraude fácil demais (PR-AUC 0,89, ROC-AUC 0,97 num modelo simples) | `checar_sinal.py`, escrito para conferir o dado antes de subir | mais ruído na balança, hub sem pesagem em 45% das caixas, fraudes que reclamam pouco para escapar da investigação |
| O prompt do gold não definia as categorias: as cinco fraudes saíam como violação de caixa (23/30) | medição contra o gabarito | as mesmas definições que geraram os roteiros, no prompt: 30/30 |
| A lista de produtos, posta para corrigir grafia, fez o modelo devolver só a linha: faltantes caíram de 21 para 8 em 22 | medição de todos os campos, não só do que se quis consertar | dizer para que a lista serve: 22/22 |
| O corte de teste por posição fazia a PR-AUC oscilar mais que a diferença entre modelos, e uma rodada elegeu o candidato errado | duas execuções seguidas com números diferentes | corte por data, com desempate por id |
| O script que trocou a marca de origem pela fictícia continha os nomes originais | varredura antes do primeiro commit | o script ficou fora do repositório |
| `CREATE ROLE` pelo psql criava um role que recusava o token OAuth do app | o app não conectava no Lakebase | role criado pela API (`databricks postgres create-role`) |

## O que não foi delegado

- A escolha do problema e da métrica de sucesso (valor em fraude encontrado com a mesma equipe).
- Os critérios de aceite de cada passo e a decisão de manter o modelo operacional quando o
  completo não provou ganho.
- O que o dado sintético precisava contar (as três causas de falta e as duas ondas) e o
  que ele não podia ter (nenhum dado de cliente real, nenhum segredo).
- A narrativa de valor, as premissas do business case e o que não afirmar.
