-- Decisões do analista sobre a fila de investigação.
--
-- Só inserção: uma decisão nova não apaga a anterior, e a vigente é a mais
-- recente por chamado. É a trilha de auditoria (quem decidiu o quê, com qual
-- score na tela) e é o rótulo que volta para o lakehouse pelo CDF do Lakebase
-- para retreinar o modelo — em especial nos chamados com ligação, que são os
-- que faltam no treino.
--
-- Tipos simples de propósito: o CDF replica este schema como Delta no Unity
-- Catalog, e array do Postgres vira problema do outro lado.

CREATE SCHEMA IF NOT EXISTS investigacao;

CREATE TABLE IF NOT EXISTS investigacao.decisoes (
  decisao_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  chamado_id        TEXT        NOT NULL,
  decisao           TEXT        NOT NULL
                    CHECK (decisao IN ('fraude_confirmada', 'procedente', 'improcedente', 'pedir_evidencia')),
  analista          TEXT        NOT NULL,
  comentario        TEXT,
  score_no_momento  DOUBLE PRECISION,
  modelo_versao     INTEGER,
  tinha_ligacao     BOOLEAN,
  origem            TEXT        NOT NULL DEFAULT 'app' CHECK (origem IN ('app', 'simulacao')),
  decidido_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- O CDF do Lakebase pula a tabela sem isto ("Does not have REPLICA IDENTITY
-- FULL"): a replicação lógica precisa da linha inteira em cada mudança.
ALTER TABLE investigacao.decisoes REPLICA IDENTITY FULL;

CREATE INDEX IF NOT EXISTS decisoes_chamado_recente
  ON investigacao.decisoes (chamado_id, decidido_em DESC);

CREATE OR REPLACE VIEW investigacao.decisao_vigente AS
SELECT DISTINCT ON (chamado_id) *
FROM investigacao.decisoes
ORDER BY chamado_id, decidido_em DESC, decisao_id DESC;
