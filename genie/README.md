# Genie — perguntas da Prevenção de Perdas

Space **Aurélia · Chamados de falta e fraude** (`01f1d96b0d6f1bd9b132a3ea3ac0cf4d`),
sobre as tabelas gold do pipeline e a fila pontuada. Para quem não abre o app:
gerente de prevenção de perdas, atendimento, logística.

| Tabela | Para responder |
|---|---|
| `gold_chamados_ciclo` | chamados, ressarcimento e fraude confirmada por ciclo, CD, transportadora, UF e motivo |
| `gold_pedidos_ciclo` | o denominador: pedidos pelos mesmos cortes |
| `fila_priorizada` | o que investigar primeiro, com score e o que a ligação disse |
| `silver_chamados` | um chamado por linha, com o desfecho da investigação |
| `gold_call_insights`, `silver_ligacoes` | a análise do LLM de cada ligação e o chamado dela |

```bash
python genie/criar_genie.py --profile field-toolbox --warehouse <id>                 # cria
python genie/criar_genie.py --profile field-toolbox --warehouse <id> --space-id <id> # atualiza
python genie/testar_genie.py --space-id <id> "pergunta"                               # conversa
```

O `criar_genie.py` executa cada SQL de exemplo no warehouse antes de publicar o
space: exemplo errado ensina o Genie a errar do mesmo jeito.

## As instruções que fazem diferença

- **Taxa de chamados agrega antes de juntar.** `gold_chamados_ciclo` tem uma linha
  por motivo e canal; juntar com os pedidos antes de agregar multiplica o
  denominador e a taxa sai errada sem dar erro.
- **Taxa de fraude confirmada é sobre investigados**, nunca sobre o total.
- **O ciclo mais recente está incompleto** (chamados ainda chegando); tendência vai
  até o ciclo 23.

## Resultado — perguntas fora dos exemplos (Conversation API, 05/11/2026)

| Pergunta | O que o Genie fez |
|---|---|
| O CD Contagem teve aumento de chamados de falta parcial? Em quais ciclos? | achou o salto no ciclo 9 (de 10 para 43) — a troca de WMS plantada no dado |
| A TransLeste no RJ piorou? Chamados por 100 pedidos por ciclo | agregou chamados e pedidos separadamente, parou no ciclo 23, achou a virada no ciclo 14 (de ~4 para 10–13) |
| Quantos chamados de alto risco estão abertos por transportadora e quanto reclamam? | usou a fila priorizada; a TransLeste, rota com furto real, tem só 5 — o modelo não confunde furto com fraude |
