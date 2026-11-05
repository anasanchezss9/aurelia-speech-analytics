-- ===================================================================
-- Gold — o que o modelo, a fila do analista e o Genie consomem
-- ===================================================================
--   gold_features_chamado     uma linha por chamado, com as features e o rótulo
--   gold_fila_investigacao    os chamados abertos, com a evidência da ligação
--   gold_ligacoes_validadas   o que o LLM ouviu na ligação, conferido com o cadastro
--   gold_chamados_ciclo       chamados e fraude por ciclo, CD, transportadora e UF
--   gold_pedidos_ciclo        o denominador: pedidos pelos mesmos cortes
--
-- Regra das features: só entra o que já se sabia na data de abertura do
-- chamado. Histórico conta para trás a partir da abertura, e fraude
-- anterior só conta se a investigação já tinha concluído. Sem isso o
-- modelo aprende com o futuro e a métrica de teste mente.
-- ===================================================================

CREATE OR REFRESH MATERIALIZED VIEW gold_pedidos_ciclo
COMMENT 'Pedidos e valor por ciclo, CD, transportadora e UF — o denominador das taxas.'
TBLPROPERTIES ('quality' = 'gold')
AS
SELECT
  p.ciclo, p.cd, p.transportadora, p.uf, k.regiao,
  count(*)                    AS n_pedidos,
  round(sum(p.valor_pedido), 2) AS valor_pedidos
FROM silver_pedidos p
JOIN silver_consultoras k USING (consultora_codigo)
GROUP BY ALL;

CREATE OR REFRESH MATERIALIZED VIEW gold_chamados_ciclo
COMMENT 'Chamados, ressarcimento e fraude confirmada por ciclo, CD, transportadora, UF e motivo.'
TBLPROPERTIES ('quality' = 'gold')
AS
SELECT
  p.ciclo, p.cd, p.transportadora, p.uf, k.regiao, c.motivo_declarado, c.canal_chamado,
  count(*)                                                                      AS n_chamados,
  round(sum(c.valor_reclamado), 2)                                              AS valor_reclamado,
  round(sum(c.valor_ressarcido), 2)                                             AS valor_ressarcido,
  count_if(c.status = 'aberto')                                                 AS n_abertos,
  count(c.resultado)                                                            AS n_investigados,
  count_if(c.resultado = 'fraude_confirmada')                                   AS n_fraudes_confirmadas,
  round(sum(CASE WHEN c.resultado = 'fraude_confirmada' THEN c.valor_reclamado ELSE 0 END), 2)
                                                                                AS valor_fraude_confirmada
FROM silver_chamados c
JOIN silver_pedidos p USING (pedido_numero)
JOIN silver_consultoras k ON k.consultora_codigo = c.consultora_codigo
GROUP BY ALL;

CREATE OR REFRESH MATERIALIZED VIEW gold_features_chamado
COMMENT 'Uma linha por chamado: features conhecidas na abertura, sinal da ligação e rótulo da investigação.'
TBLPROPERTIES ('quality' = 'gold')
AS
WITH base AS (
  SELECT
    c.chamado_id, c.consultora_codigo, c.pedido_numero, c.data_abertura, c.status,
    c.canal_chamado, c.motivo_declarado, c.valor_reclamado, c.qtd_itens_reclamados,
    c.qtd_perfumaria_reclamada, c.valor_perfumaria_reclamada, c.resultado, c.data_conclusao,
    p.ciclo, p.cd, p.transportadora, p.uf, p.valor_pedido, p.qtd_itens,
    e.status_entrega, e.data_entrega, e.comprovante_assinado, e.dias_atraso,
    e.div_peso_cd, e.div_peso_hub, e.pesado_no_hub,
    k.regiao, k.nivel, k.tem_app, k.data_cadastro
  FROM silver_chamados c
  JOIN silver_pedidos p      USING (pedido_numero)
  JOIN silver_entregas e     USING (pedido_numero)
  JOIN silver_consultoras k  ON k.consultora_codigo = c.consultora_codigo
),
decisao AS (
  SELECT chamado_id, decisao AS decisao_analista, decidido_em FROM silver_decisoes_analista
),
chamados_anteriores AS (
  SELECT
    chamado_id,
    count(*) OVER (PARTITION BY consultora_codigo ORDER BY unix_date(data_abertura)
                   RANGE BETWEEN 180 PRECEDING AND 1 PRECEDING) AS chamados_180d
  FROM base
),
fraudes_anteriores AS (
  SELECT a.chamado_id, count(b.chamado_id) AS fraudes_confirmadas_antes
  FROM base a
  LEFT JOIN base b
    ON b.consultora_codigo = a.consultora_codigo
   AND b.resultado = 'fraude_confirmada'
   AND b.data_conclusao < a.data_abertura
  GROUP BY a.chamado_id
),
pedidos_anteriores AS (
  SELECT a.chamado_id, count(p.pedido_numero) AS pedidos_180d
  FROM base a
  LEFT JOIN silver_pedidos p
    ON p.consultora_codigo = a.consultora_codigo
   AND p.data_pedido >= date_sub(a.data_abertura, 180)
   AND p.data_pedido <  a.data_abertura
  GROUP BY a.chamado_id
),
-- Taxa de chamados da rota (transportadora × UF) nos dois ciclos anteriores:
-- é o que separa "esta consultora reclama muito" de "esta rota está sendo furtada".
rota AS (
  SELECT
    b.chamado_id,
    round(100 * sum(ch.n_chamados) / nullif(sum(pe.n_pedidos), 0), 2) AS rota_chamados_100_pedidos
  FROM base b
  LEFT JOIN (SELECT ciclo, transportadora, uf, sum(n_chamados) AS n_chamados
             FROM gold_chamados_ciclo GROUP BY ALL) ch
    ON ch.transportadora = b.transportadora AND ch.uf = b.uf AND ch.ciclo BETWEEN b.ciclo - 2 AND b.ciclo - 1
  LEFT JOIN (SELECT ciclo, transportadora, uf, sum(n_pedidos) AS n_pedidos
             FROM gold_pedidos_ciclo GROUP BY ALL) pe
    ON pe.transportadora = ch.transportadora AND pe.uf = ch.uf AND pe.ciclo = ch.ciclo
  GROUP BY b.chamado_id
),
ligacao AS (
  SELECT
    l.chamado_id, l.call_id, l.origem AS origem_ligacao,
    g.suspeita_fraude, g.fraude_confianca, g.tipo_problema, g.sentiment,
    size(g.sinais_relato) AS n_sinais_relato, g.relato_consistente
  FROM silver_ligacoes l
  JOIN gold_call_insights g USING (call_id)
  QUALIFY row_number() OVER (PARTITION BY l.chamado_id ORDER BY l.data_hora DESC) = 1
)
SELECT
  b.chamado_id, b.consultora_codigo, b.pedido_numero, b.data_abertura, b.ciclo, b.status,

  -- O chamado
  b.motivo_declarado, b.canal_chamado, b.valor_reclamado, b.qtd_itens_reclamados,
  b.qtd_perfumaria_reclamada,
  round(b.valor_perfumaria_reclamada / nullif(b.valor_reclamado, 0), 4) AS share_valor_perfumaria,
  round(b.valor_reclamado / nullif(b.valor_pedido, 0), 4)               AS share_pedido_reclamado,

  -- A entrega
  b.cd, b.transportadora, b.uf, b.regiao, b.status_entrega, b.comprovante_assinado,
  b.dias_atraso, b.div_peso_cd, b.div_peso_hub, b.pesado_no_hub,
  datediff(b.data_abertura, b.data_entrega)                             AS dias_entrega_abertura,
  r.rota_chamados_100_pedidos,

  -- A consultora, até a abertura
  b.nivel, b.tem_app,
  round(months_between(b.data_abertura, b.data_cadastro), 1)            AS meses_casa,
  ca.chamados_180d,
  pa.pedidos_180d,
  round(ca.chamados_180d / nullif(pa.pedidos_180d, 0), 4)               AS chamados_por_pedido_180d,
  fa.fraudes_confirmadas_antes,

  -- A ligação, quando houve
  l.call_id, l.origem_ligacao, l.call_id IS NOT NULL                    AS tem_ligacao,
  l.suspeita_fraude                                                     AS llm_suspeita_fraude,
  l.fraude_confianca                                                    AS llm_fraude_confianca,
  l.tipo_problema                                                       AS llm_tipo_problema,
  l.sentiment                                                           AS llm_sentiment,
  l.n_sinais_relato                                                     AS llm_n_sinais_relato,
  l.relato_consistente                                                  AS llm_relato_consistente,

  -- O rótulo: a investigação formal e, onde ela não houve, a decisão do analista
  -- no app. "Pedir evidência" ainda não é decisão e não rotula.
  b.resultado,
  d.decisao_analista,
  CASE WHEN b.resultado IS NOT NULL THEN 'investigacao'
       WHEN d.decisao_analista IN ('fraude_confirmada', 'procedente', 'improcedente') THEN 'analista'
  END                                                                   AS origem_rotulo,
  CASE WHEN b.resultado IS NOT NULL THEN IF(b.resultado = 'fraude_confirmada', 1, 0)
       WHEN d.decisao_analista = 'fraude_confirmada' THEN 1
       WHEN d.decisao_analista IN ('procedente', 'improcedente') THEN 0
  END                                                                   AS rotulo_fraude
FROM base b
JOIN chamados_anteriores ca USING (chamado_id)
JOIN fraudes_anteriores  fa USING (chamado_id)
JOIN pedidos_anteriores  pa USING (chamado_id)
JOIN rota                r  USING (chamado_id)
LEFT JOIN ligacao        l  USING (chamado_id)
LEFT JOIN decisao        d  USING (chamado_id);

CREATE OR REFRESH MATERIALIZED VIEW gold_fila_investigacao
COMMENT 'Chamados abertos com a evidência da ligação — a fila que o analista trabalha.'
TBLPROPERTIES ('quality' = 'gold')
AS
SELECT
  f.chamado_id, f.data_abertura, f.consultora_codigo, f.pedido_numero,
  f.motivo_declarado, f.valor_reclamado, f.transportadora, f.uf, f.cd,
  f.comprovante_assinado, f.div_peso_cd, f.div_peso_hub,
  f.chamados_180d, f.fraudes_confirmadas_antes, f.meses_casa,
  f.call_id, f.origem_ligacao,
  g.summary            AS resumo_ligacao,
  g.fraude_indicadores AS indicadores_ligacao,
  g.produtos_faltantes AS produtos_citados,
  g.sinais_relato,
  f.llm_suspeita_fraude, f.llm_fraude_confianca, f.llm_relato_consistente,
  f.decisao_analista
FROM gold_features_chamado f
LEFT JOIN gold_call_insights g USING (call_id)
WHERE f.status = 'aberto';

-- O número ditado ao telefone é o campo mais frágil da transcrição: dígito
-- repetido some ("793337" sai "7937"). Prompt não conserta o que já se
-- perdeu na silver. Aqui o que o LLM extraiu é conferido com o cadastro:
-- a consultora extraída vale se existe; senão, a do chamado vinculado no
-- CRM. O pedido extraído vale se é dela; senão, o pedido dela mais
-- parecido, com até 2 dígitos de diferença. A comparação é com todos os
-- pedidos da consultora: limitar a uma janela de ciclos descartava o pedido
-- certo de ligação mais antiga.
CREATE OR REFRESH MATERIALIZED VIEW gold_ligacoes_validadas
COMMENT 'Consultora e pedido extraídos da ligação, conferidos e corrigidos contra o cadastro.'
TBLPROPERTIES ('quality' = 'gold')
AS
WITH chamada AS (
  SELECT
    g.call_id, l.chamado_id, l.origem,
    regexp_replace(g.consultora_codigo, '[^0-9]', '') AS consultora_extraida,
    regexp_replace(g.pedido_numero, '[^0-9]', '')     AS pedido_extraido,
    c.consultora_codigo                               AS consultora_chamado,
    c.pedido_numero                                   AS pedido_chamado,
    k.consultora_codigo IS NOT NULL                   AS consultora_extraida_existe
  FROM gold_call_insights g
  JOIN silver_ligacoes l USING (call_id)
  LEFT JOIN silver_chamados c USING (chamado_id)
  LEFT JOIN silver_consultoras k ON k.consultora_codigo = regexp_replace(g.consultora_codigo, '[^0-9]', '')
),
resolvida AS (
  SELECT *, IF(consultora_extraida_existe, consultora_extraida, consultora_chamado) AS consultora_validada
  FROM chamada
),
pares AS (
  SELECT
    r.call_id, p.pedido_numero, p.data_pedido,
    levenshtein(coalesce(r.pedido_extraido, ''), p.pedido_numero) AS distancia
  FROM resolvida r
  JOIN silver_pedidos p
    ON p.consultora_codigo = r.consultora_validada
),
candidato AS (
  SELECT call_id, pedido_numero, distancia
  FROM (
    SELECT *, row_number() OVER (PARTITION BY call_id ORDER BY distancia, data_pedido DESC) AS rn
    FROM pares
  )
  WHERE rn = 1
)
SELECT
  r.call_id, r.chamado_id, r.origem,
  r.consultora_extraida, r.consultora_validada, r.consultora_chamado,
  r.pedido_extraido,
  CASE WHEN c.distancia <= 2 THEN c.pedido_numero END AS pedido_validado,
  c.distancia                                         AS distancia_pedido,
  r.pedido_chamado,
  r.consultora_extraida = r.consultora_chamado        AS consultora_extraida_confere,
  r.pedido_extraido = r.pedido_chamado                AS pedido_extraido_confere,
  (CASE WHEN c.distancia <= 2 THEN c.pedido_numero END) = r.pedido_chamado AS pedido_validado_confere
FROM resolvida r
LEFT JOIN candidato c USING (call_id);
