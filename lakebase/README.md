# Lakebase — a fila do analista e as decisões dele

O lakehouse decide a ordem da fila; o analista decide cada caso. O Lakebase é onde
as duas coisas se encontram: o app lê a fila sem acordar um warehouse e grava a
decisão numa tabela transacional, e a decisão volta para o lakehouse como rótulo.

```
UC fila_priorizada ──synced table (SNAPSHOT)──► Postgres aurelia_speech_online.fila_priorizada ──► app lê
                                                                                                    │
UC aurelia_speech_investigacao.lb_decisoes_history ◄──CDF (contínuo)── Postgres investigacao.decisoes ◄─ app grava
          │
          ▼
silver_decisoes_analista ──► gold_features_chamado.rotulo_fraude (origem_rotulo = 'analista') ──► retreino
```

| Peça | O que é |
|---|---|
| Projeto `aurelia-speech-ops` | Postgres 17, autoscaling, scale-to-zero, branch `production` |
| `investigacao.decisoes` | uma linha por decisão, só inserção; a vigente é a mais recente por chamado |
| `investigacao.decisao_vigente` | view com a decisão vigente de cada chamado |
| `aurelia_speech_online.fila_priorizada` | synced table da fila pontuada; o job diário dispara o refresh |
| CDF `investigacao` | replica o schema `investigacao` como Delta em `field_toolbox_catalog.aurelia_speech_investigacao` |

```bash
bash lakebase/criar_lakebase.sh field-toolbox   # idempotente
```

## Por que cada escolha

**Decisão só inserção.** Mudar de ideia é uma linha nova, não um `UPDATE`. A trilha
de quem decidiu o quê, e com qual score na tela, é a auditoria que um caso de
fraude pede — e é o que permite medir depois se o analista concordou com o modelo.

**SNAPSHOT na fila.** A pontuação reescreve a fila inteira a cada execução; sync
incremental não teria o que aproveitar. O job roda pipeline → pontuação →
refresh da synced table, nessa ordem.

**CDF de volta, e não uma cópia à mão.** A decisão do analista é o rótulo que falta
para o modelo aprender o sinal da ligação: o treino tem só 90 chamados rotulados
com ligação. O CDF do Lakebase leva cada mudança do Postgres para Delta em
segundos, e o pipeline transforma em rótulo. `origem = 'simulacao'` nunca vira
rótulo, para teste não contaminar o treino.

## Resultado

- 564 chamados da fila no Postgres; leitura da fila em ~160 ms medidos de fora do
  workspace.
- Uma decisão gravada no Postgres apareceu em `lb_decisoes_history` em segundos e,
  depois do pipeline, virou `rotulo_fraude = 1` com `origem_rotulo = 'analista'` num
  chamado ainda aberto. Inserção e exclusão chegam com `_pg_change_type`.

## Armadilhas

- **O banco tem dois nomes.** `databricks_postgres` dentro do Postgres e
  `databricks-postgres` como recurso da API. O CDF pede o segundo e responde
  "CdfConfig not found" com o primeiro.
- **Sem `REPLICA IDENTITY FULL`, o CDF pula a tabela** (`CDF_STATE_SKIPPED`), e
  mudar isso depois não basta: a configuração avalia as tabelas ao ser criada.
  Foi preciso apagar e recriar o CDF config. O `schema.sql` já cria a tabela assim.
- **Role do app pela API, não por `CREATE ROLE`.** O service principal do app
  precisa de um role criado com `databricks postgres create-role`; o `CREATE ROLE`
  do psql cria um role com o mesmo nome que recusa o token OAuth com a mensagem de
  senha errada.
