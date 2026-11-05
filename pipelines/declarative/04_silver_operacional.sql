-- ===================================================================
-- Silver — o operacional tipado, validado e com as contas por linha
-- ===================================================================
-- Cada tabela silver resolve uma pergunta de uma vez, para o gold não
-- repetir a conta: quanto vale o pedido, quanto a caixa perdeu de peso e
-- onde, quanto o chamado reclama e em perfumaria, qual foi o desfecho.
--
-- As expectativas derrubam a linha que não faz sentido (código de
-- consultora fora do formato, peso zerado, entrega antes do envio) e o
-- SDP registra quantas caíram em cada uma, no event log do pipeline.
-- ===================================================================

CREATE OR REFRESH MATERIALIZED VIEW silver_consultoras (
  CONSTRAINT codigo_valido EXPECT (consultora_codigo RLIKE '^[0-9]{5}$') ON VIOLATION DROP ROW
)
COMMENT 'Consultoras, uma linha por código.'
TBLPROPERTIES ('quality' = 'silver')
AS
SELECT consultora_codigo, uf, regiao, data_cadastro, nivel, tem_app
FROM bronze_consultoras
QUALIFY row_number() OVER (PARTITION BY consultora_codigo ORDER BY _ingerido_em DESC) = 1;

CREATE OR REFRESH MATERIALIZED VIEW silver_produtos
COMMENT 'Catálogo de produtos.'
TBLPROPERTIES ('quality' = 'silver')
AS
SELECT sku, nome, linha, categoria, preco, peso_kg
FROM bronze_produtos
QUALIFY row_number() OVER (PARTITION BY sku ORDER BY _ingerido_em DESC) = 1;

CREATE OR REFRESH MATERIALIZED VIEW silver_pedidos (
  CONSTRAINT pedido_com_itens EXPECT (qtd_itens > 0) ON VIOLATION DROP ROW
)
COMMENT 'Pedidos com valor, quantidade de itens e participação de perfumaria.'
TBLPROPERTIES ('quality' = 'silver')
AS
WITH itens AS (
  SELECT
    i.pedido_numero,
    sum(i.quantidade)                                                      AS qtd_itens,
    sum(i.quantidade * i.preco_unitario)                                   AS valor_pedido,
    sum(CASE WHEN p.categoria = 'perfumaria' THEN i.quantidade ELSE 0 END) AS qtd_perfumaria
  FROM bronze_pedido_itens i
  JOIN silver_produtos p USING (sku)
  GROUP BY i.pedido_numero
)
SELECT
  p.pedido_numero, p.consultora_codigo, p.ciclo, p.data_pedido, p.uf, p.cd,
  p.transportadora, p.canal AS canal_pedido,
  i.qtd_itens, round(i.valor_pedido, 2) AS valor_pedido, i.qtd_perfumaria
FROM bronze_pedidos p
LEFT JOIN itens i USING (pedido_numero);

-- A caixa é pesada duas vezes: na expedição do CD e no hub da última milha.
-- O peso teórico vem da soma dos itens. As duas divergências separam as
-- causas de falta: o CD expedir leve aponta erro de separação; o hub pesar
-- menos que a expedição aponta perda no transporte; tudo batendo com o
-- comprovante assinado é o que sobra para a fraude explicar.
CREATE OR REFRESH MATERIALIZED VIEW silver_entregas (
  CONSTRAINT peso_teorico_positivo EXPECT (peso_teorico_kg > 0) ON VIOLATION DROP ROW,
  CONSTRAINT entrega_depois_do_envio EXPECT (data_entrega IS NULL OR data_entrega >= data_envio) ON VIOLATION DROP ROW
)
COMMENT 'Entregas com a divergência de peso no CD e no hub.'
TBLPROPERTIES ('quality' = 'silver')
AS
SELECT
  pedido_numero, data_envio, data_prevista, data_entrega, status_entrega,
  comprovante_assinado,
  peso_teorico_kg, peso_expedido_kg, peso_hub_kg,
  datediff(coalesce(data_entrega, data_prevista), data_prevista)                AS dias_atraso,
  round((peso_teorico_kg - peso_expedido_kg) / peso_teorico_kg, 4)              AS div_peso_cd,
  round((peso_expedido_kg - peso_hub_kg) / peso_expedido_kg, 4)                 AS div_peso_hub,
  peso_hub_kg IS NOT NULL                                                       AS pesado_no_hub
FROM bronze_entregas;

CREATE OR REFRESH MATERIALIZED VIEW silver_chamados (
  CONSTRAINT valor_nao_negativo EXPECT (valor_reclamado >= 0) ON VIOLATION DROP ROW,
  CONSTRAINT pedido_conhecido EXPECT (pedido_existe) ON VIOLATION DROP ROW
)
COMMENT 'Chamados com o que foi reclamado e o desfecho da investigação, quando houve.'
TBLPROPERTIES ('quality' = 'silver')
AS
WITH itens AS (
  SELECT
    ci.chamado_id,
    sum(ci.quantidade)                                                                  AS qtd_itens_reclamados,
    sum(CASE WHEN p.categoria = 'perfumaria' THEN ci.quantidade ELSE 0 END)             AS qtd_perfumaria_reclamada,
    sum(CASE WHEN p.categoria = 'perfumaria' THEN ci.quantidade * p.preco ELSE 0 END)   AS valor_perfumaria_reclamada,
    collect_list(p.nome)                                                                AS produtos_reclamados
  FROM bronze_chamado_itens ci
  JOIN silver_produtos p USING (sku)
  GROUP BY ci.chamado_id
)
SELECT
  c.chamado_id, c.pedido_numero, c.consultora_codigo, c.data_abertura,
  c.canal AS canal_chamado, c.motivo_declarado, c.valor_reclamado,
  c.status, c.valor_ressarcido,
  coalesce(i.qtd_itens_reclamados, 0)        AS qtd_itens_reclamados,
  coalesce(i.qtd_perfumaria_reclamada, 0)    AS qtd_perfumaria_reclamada,
  coalesce(i.valor_perfumaria_reclamada, 0)  AS valor_perfumaria_reclamada,
  i.produtos_reclamados,
  inv.resultado, inv.data_conclusao, inv.analista_id,
  p.pedido_numero IS NOT NULL                AS pedido_existe
FROM bronze_chamados c
LEFT JOIN itens i USING (chamado_id)
LEFT JOIN bronze_investigacoes inv USING (chamado_id)
LEFT JOIN silver_pedidos p ON p.pedido_numero = c.pedido_numero;

CREATE OR REFRESH MATERIALIZED VIEW silver_ligacoes
COMMENT 'Ligações da central com o chamado vinculado e a origem da transcrição.'
TBLPROPERTIES ('quality' = 'silver')
AS
SELECT call_id, chamado_id, data_hora, duracao_s, origem, arquivo
FROM bronze_ligacoes;
