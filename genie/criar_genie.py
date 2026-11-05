"""Cria (ou atualiza) o Genie space da prevenção de perdas da Aurélia.

Cada SQL de exemplo é executado no warehouse antes de ir para o space: exemplo
errado ensina o Genie a errar do mesmo jeito.

Uso:
    python criar_genie.py --profile field-toolbox --warehouse 9fc569e6e984dcde
    python criar_genie.py --profile field-toolbox --warehouse 9fc569e6e984dcde --space-id <id>
"""

import argparse
import json
import subprocess
import uuid

S = "field_toolbox_catalog.aurelia_speech"

TITULO = "Aurélia · Chamados de falta e fraude"
DESCRICAO = (
    "Pergunte sobre os chamados de falta de produto das consultoras: onde e quando eles sobem, "
    "quanto foi ressarcido, quanto foi fraude confirmada e quais chamados abertos o modelo manda "
    "investigar primeiro, com o que a ligação disse. Aurélia Cosméticos é fictícia e os dados são "
    "sintéticos."
)

TABELAS = {
    f"{S}.gold_chamados_ciclo": {
        "ciclo": ("Ciclo de vendas de 21 dias; o 1 começa em 05/05/2025 e o 24 é o mais recente.", ["ciclo", "período"]),
        "transportadora": ("Transportadora da entrega.", ["transportadora", "logística"]),
        "motivo_declarado": ("Motivo que a consultora declarou: falta_parcial, caixa_violada, nao_recebido, atraso, produto_avariado, outro.", ["motivo", "tipo de chamado"]),
        "valor_ressarcido": ("Valor devolvido à consultora, em reais.", ["ressarcimento", "reembolso", "crédito"]),
        "n_fraudes_confirmadas": ("Chamados que a investigação concluiu como fraude.", ["fraudes", "fraude confirmada"]),
        "n_investigados": ("Chamados que passaram por investigação formal.", ["investigados"]),
    },
    f"{S}.gold_pedidos_ciclo": {
        "n_pedidos": ("Pedidos do ciclo — denominador das taxas.", ["pedidos", "volume"]),
    },
    f"{S}.fila_priorizada": {
        "posicao": ("Posição na fila de investigação, 1 é o primeiro a abrir.", ["posição", "ordem", "prioridade"]),
        "score_fraude": ("Probabilidade calibrada de fraude dada pelo modelo modelo_fraude_chamado (0 a 1).", ["risco", "probabilidade de fraude", "score"]),
        "faixa_risco": ("alta (≥ 0,5), media (0,2 a 0,5) ou baixa.", ["faixa", "nível de risco"]),
        "resumo_ligacao": ("Resumo da ligação da consultora, gerado pelo LLM.", ["resumo", "o que a ligação disse"]),
        "sinais_relato": ("Sinais no relato da consultora: detalhes vagos, pressa pelo ressarcimento, resiste a enviar evidência, não conferiu na entrega, azar recorrente, acusa terceiros.", ["sinais", "sinais de alerta"]),
    },
    f"{S}.silver_chamados": {
        "resultado": ("Desfecho da investigação: fraude_confirmada, procedente_* ou improcedente. Nulo se não houve investigação.", ["resultado", "desfecho"]),
        "status": ("aberto, investigado ou ressarcido_automatico.", ["status", "situação"]),
    },
    f"{S}.gold_call_insights": {
        "tipo_problema": ("Tipo do problema que o LLM identificou na ligação.", ["tipo de problema"]),
        "summary": ("Resumo da ligação.", ["resumo"]),
    },
    f"{S}.silver_ligacoes": {},
}

INSTRUCOES = """## PROPÓSITO
- Responder em português perguntas do time de Prevenção de Perdas e do Atendimento da Aurélia sobre chamados de falta de produto: onde sobem, quanto custam, quanto é fraude e o que investigar primeiro.
## DEFINIÇÕES
- 'Taxa de chamados' = 100 × chamados / pedidos, por 100 pedidos. Agregue gold_chamados_ciclo e gold_pedidos_ciclo SEPARADAMENTE pelos mesmos cortes (ciclo, cd, transportadora, uf) e só depois junte: gold_chamados_ciclo tem uma linha por motivo e canal, e juntar antes multiplica os pedidos.
- 'Rota' = transportadora × UF.
- 'Fraude confirmada' = resultado = 'fraude_confirmada'. 'Taxa de fraude confirmada' = fraudes confirmadas / chamados investigados; só existe para investigados. Nunca divida fraudes pelo total de chamados.
- 'Fila' = fila_priorizada: chamados abertos, já em ordem de score_fraude. Para 'o que investigar primeiro', ordene por posicao.
- O ciclo mais recente (24) ainda está recebendo chamados. Para tendência, use até o ciclo 23.
- Ligações: silver_ligacoes liga call_id a chamado_id; gold_call_insights traz a análise do LLM por call_id.
## FORMATO
- Valores em reais com separador de milhar; percentuais com uma casa decimal.
- A Aurélia é fictícia e os dados são sintéticos; não afirme que são números reais de nenhuma empresa.
"""

EXEMPLOS = [
    ("Quais rotas têm mais chamados por 100 pedidos nos últimos três ciclos fechados?", f"""
WITH ch AS (
  SELECT transportadora, uf, SUM(n_chamados) AS n_chamados
  FROM {S}.gold_chamados_ciclo WHERE ciclo BETWEEN 21 AND 23 GROUP BY transportadora, uf),
pe AS (
  SELECT transportadora, uf, SUM(n_pedidos) AS n_pedidos
  FROM {S}.gold_pedidos_ciclo WHERE ciclo BETWEEN 21 AND 23 GROUP BY transportadora, uf)
SELECT pe.transportadora, pe.uf, ch.n_chamados, pe.n_pedidos,
       ROUND(100 * ch.n_chamados / pe.n_pedidos, 2) AS chamados_por_100_pedidos
FROM pe JOIN ch ON ch.transportadora = pe.transportadora AND ch.uf = pe.uf
ORDER BY chamados_por_100_pedidos DESC LIMIT 10"""),
    ("Quanto foi ressarcido e quanto foi fraude confirmada por ciclo?", f"""
SELECT ciclo, SUM(n_chamados) AS chamados, ROUND(SUM(valor_ressarcido), 2) AS valor_ressarcido,
       SUM(n_investigados) AS investigados, SUM(n_fraudes_confirmadas) AS fraudes_confirmadas,
       ROUND(SUM(valor_fraude_confirmada), 2) AS valor_fraude_confirmada
FROM {S}.gold_chamados_ciclo GROUP BY ciclo ORDER BY ciclo"""),
    ("Quais são os 10 primeiros chamados da fila de investigação e o que a ligação disse?", f"""
SELECT posicao, chamado_id, score_fraude, faixa_risco, motivo_declarado, valor_reclamado,
       transportadora, uf, resumo_ligacao, sinais_relato
FROM {S}.fila_priorizada ORDER BY posicao LIMIT 10"""),
    ("Qual a taxa de fraude confirmada entre os investigados, por motivo declarado?", f"""
SELECT motivo_declarado, COUNT(resultado) AS investigados,
       COUNT_IF(resultado = 'fraude_confirmada') AS fraudes_confirmadas,
       ROUND(100 * COUNT_IF(resultado = 'fraude_confirmada') / COUNT(resultado), 1) AS pct_fraude_confirmada
FROM {S}.silver_chamados WHERE resultado IS NOT NULL
GROUP BY motivo_declarado ORDER BY pct_fraude_confirmada DESC"""),
]

PERGUNTAS = [
    "Quais rotas têm mais chamados por 100 pedidos nos últimos três ciclos fechados?",
    "O CD Contagem teve aumento de chamados de falta parcial? Em quais ciclos?",
    "Quanto foi ressarcido e quanto foi fraude confirmada por ciclo?",
    "Quais são os 10 primeiros chamados da fila de investigação e o que a ligação disse?",
    "Qual a taxa de fraude confirmada entre os investigados, por motivo declarado?",
    "Quantos chamados de alto risco estão abertos por transportadora?",
]


def api(perfil, metodo, caminho, corpo=None):
    cmd = ["databricks", "api", metodo, caminho, "-p", perfil, "-o", "json"]
    if corpo is not None:
        cmd += ["--json", json.dumps(corpo)]
    saida = subprocess.run(cmd, capture_output=True, text=True)
    if saida.returncode:
        raise RuntimeError(saida.stderr or saida.stdout)
    return json.loads(saida.stdout or "{}")


def testar(perfil, warehouse, sql):
    r = api(perfil, "post", "/api/2.0/sql/statements",
            {"warehouse_id": warehouse, "statement": sql, "wait_timeout": "50s"})
    if r["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(f"SQL de exemplo falhou: {r['status']}\n{sql}")
    return len(r.get("result", {}).get("data_array", []))


def novo_id():
    return uuid.uuid4().hex


def montar():
    tabelas = []
    for nome, colunas in sorted(TABELAS.items()):
        t = {"identifier": nome}
        if colunas:
            t["column_configs"] = [
                {"column_name": c, "description": [d], "synonyms": s}
                for c, (d, s) in sorted(colunas.items())
            ]
        tabelas.append(t)
    return {
        "version": 2,
        "config": {"sample_questions": sorted(
            [{"id": novo_id(), "question": [q]} for q in PERGUNTAS], key=lambda x: x["id"])},
        "data_sources": {"tables": tabelas},
        "instructions": {
            "text_instructions": [{"id": novo_id(), "content": [l + "\n" for l in INSTRUCOES.strip().split("\n")]}],
            "example_question_sqls": sorted(
                [{"id": novo_id(), "question": [q], "sql": [sql.strip()]} for q, sql in EXEMPLOS],
                key=lambda x: x["id"]),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="field-toolbox")
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--space-id")
    ap.add_argument("--pasta", default="/Users/ana.sanchez@databricks.com")
    a = ap.parse_args()

    for pergunta, sql in EXEMPLOS:
        print(f"ok ({testar(a.profile, a.warehouse, sql)} linhas): {pergunta}")

    corpo = {"title": TITULO, "description": DESCRICAO, "warehouse_id": a.warehouse,
             "serialized_space": json.dumps(montar(), ensure_ascii=False)}
    if a.space_id:
        r = api(a.profile, "patch", f"/api/2.0/genie/spaces/{a.space_id}", corpo)
    else:
        r = api(a.profile, "post", "/api/2.0/genie/spaces", {**corpo, "parent_path": a.pasta})
    print(f"space: {r.get('space_id')} · {r.get('title')}")


if __name__ == "__main__":
    main()
