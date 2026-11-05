-- ===================================================================
-- Gold — Insights de negócio (PPM Falta) com ai_query Claude, 100% SQL
-- ===================================================================
-- Para cada NOVA transcrição, chama o LLM (Claude Sonnet 4.6) com
-- responseFormat JSON Schema, recebendo um payload estruturado pronto
-- para análise de negócio.
--
-- A STREAMING TABLE com FROM STREAM silver_transcricoes garante que cada
-- call_id é analisado uma única vez, venha do áudio ou do texto.
-- Dashboards e alertas podem ler direto desta tabela.
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE gold_call_insights
  COMMENT 'Insights de negócio extraídos das transcrições — projeto PPM Falta.'
  TBLPROPERTIES ('quality' = 'gold')
AS
WITH analise AS (
  SELECT
    call_id,
    full_text,
    current_timestamp() AS analyzed_at,
    '${llm_endpoint}'   AS llm_endpoint,

    ai_query(
      '${llm_endpoint}',
      CONCAT(
        'Você é analista de qualidade da Aurélia, especializado em chamadas ',
        'de consultoras (clientes B2B) reportando problemas em pedidos. ',
        'Estamos no projeto PPM Falta: detectar atrasos, faltas, violações ',
        'de embalagem e suspeitas de fraude.\n\n',
        'Analise a transcrição e devolva JSON aderente ao schema. Seja ',
        'factual: só afirme o que estiver sustentado na transcrição. ',
        'Quando não houver evidência, use null ou string vazia.\n\n',
        'Em fraude_confianca devolva de 0 a 1 o quanto esta transcrição ',
        'sustenta a suspeita de desvio: 0 quando não há indício algum, 1 ',
        'quando a ligação declara o desvio de forma explícita. Julgue só o ',
        'que foi dito nesta ligação — você não tem o histórico da consultora. ',
        'Com suspeita_fraude false o valor deve expressar a dúvida residual, ',
        'em geral abaixo de 0.2, e não simplesmente 0.\n\n',
        'Definições de tipo_problema (escolha a que melhor descreve o caso):\n',
        '- atraso: o pedido não chegou no prazo.\n',
        '- falta_parcial: a caixa chegou íntegra, mas faltam itens, sem indício de desvio.\n',
        '- violacao_caixa: a embalagem chegou danificada ou aberta, mas nenhum produto ',
        'sumiu e ninguém sugere desvio.\n',
        '- fraude_suspeita: há indício de desvio intencional — caixa violada com itens ',
        'de valor sumidos, produtos trocados, enchimento no lugar de produto. Caixa ',
        'aberta COM produto sumido é fraude_suspeita, não violacao_caixa.\n',
        '- outro: qualquer outro assunto (produto avariado ou vencido com caixa ',
        'intacta, cobrança, cadastro, dúvida, elogio).\n\n',
        'Em produtos_faltantes, devolva cada produto com o nome completo como foi ',
        'falado (ex.: "hidratante Diário de macadâmia", "Maré Oceano"), nunca só a ',
        'linha. Use esta lista de linhas da Aurélia apenas para corrigir a grafia ',
        'quando a transcrição errar: Âmbar, Maré, Lua, Memória, Riso, Raízes, ',
        'Diário, Aura (inclui Aura Artisan), Tempo, Brilho, Colo, Tez.\n\n',
        'TRANSCRIÇÃO:\n"""\n', full_text, '\n"""'
      ),
      responseFormat => '{
        "type": "json_schema",
        "json_schema": {
          "name": "ppm_falta_analysis",
          "schema": {
            "type": "object",
            "properties": {
              "sentiment":          {"type": "string", "enum": ["positivo","neutro","negativo"]},
              "emotion":            {"type": "string"},
              "intent":             {"type": "string"},
              "topics":             {"type": "array", "items": {"type": "string"}},
              "summary":            {"type": "string"},
              "next_action":        {"type": "string"},
              "compliance_ok":      {"type": "boolean"},
              "compliance_reason":  {"type": "string"},
              "churn_risk":         {"type": "number", "minimum": 0, "maximum": 1},
              "tipo_problema":      {"type": "string", "enum": ["atraso","falta_parcial","violacao_caixa","fraude_suspeita","outro"]},
              "suspeita_fraude":    {"type": "boolean"},
              "fraude_confianca":   {"type": "number", "minimum": 0, "maximum": 1},
              "fraude_indicadores": {"type": "array", "items": {"type": "string"}},
              "produtos_faltantes": {"type": "array", "items": {"type": "string"}},
              "reincidencia":       {"type": "boolean"},
              "consultora_codigo":  {"type": "string"},
              "pedido_numero":      {"type": "string"}
            },
            "required": ["sentiment","intent","tipo_problema","suspeita_fraude","fraude_confianca","summary","next_action"]
          },
          "strict": true
        }
      }',
      modelParameters => named_struct('temperature', 0.1, 'max_tokens', 2000)
    ) AS j
  FROM STREAM silver_transcricoes
)
SELECT
  call_id,
  analyzed_at,
  llm_endpoint,

  -- Insights gerais
  j:sentiment::STRING                                       AS sentiment,
  j:emotion::STRING                                         AS emotion,
  j:intent::STRING                                          AS intent,
  from_json(j:topics::STRING, 'ARRAY<STRING>')              AS topics,
  j:summary::STRING                                         AS summary,
  j:next_action::STRING                                     AS next_action,
  j:compliance_ok::BOOLEAN                                  AS compliance_ok,
  j:compliance_reason::STRING                               AS compliance_reason,
  j:churn_risk::DOUBLE                                      AS churn_risk,

  -- PPM Falta
  j:tipo_problema::STRING                                   AS tipo_problema,
  j:suspeita_fraude::BOOLEAN                                AS suspeita_fraude,
  j:fraude_confianca::DOUBLE                                AS fraude_confianca,
  from_json(j:fraude_indicadores::STRING, 'ARRAY<STRING>')  AS fraude_indicadores,
  from_json(j:produtos_faltantes::STRING, 'ARRAY<STRING>')  AS produtos_faltantes,
  j:reincidencia::BOOLEAN                                   AS reincidencia,
  j:consultora_codigo::STRING                               AS consultora_codigo,
  j:pedido_numero::STRING                                   AS pedido_numero,

  CAST(j AS STRING)                                         AS raw_response
FROM analise;


-- =====================================================================
-- View pra time de negócio: foco em casos críticos
-- =====================================================================
CREATE OR REFRESH MATERIALIZED VIEW gold_critical_cases
  COMMENT 'Chamadas de risco — fraude suspeita, reincidência ou churn alto.'
  TBLPROPERTIES ('quality' = 'gold')
AS
SELECT
  call_id,
  analyzed_at,
  consultora_codigo,
  pedido_numero,
  tipo_problema,
  suspeita_fraude,
  fraude_confianca,
  reincidencia,
  fraude_indicadores,
  produtos_faltantes,
  churn_risk,
  summary,
  next_action
FROM gold_call_insights
WHERE suspeita_fraude = true
   OR reincidencia    = true
   OR churn_risk      >= 0.7;
