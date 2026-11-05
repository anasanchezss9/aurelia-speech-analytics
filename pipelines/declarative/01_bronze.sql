-- ===================================================================
-- Bronze — tudo o que chega, como chega, em STREAMING TABLE
-- ===================================================================
-- Três origens, um padrão: STREAM read_files, o equivalente declarativo
-- do Auto Loader. O SDP guarda o checkpoint, então cada arquivo é lido
-- uma vez; em modo triggered a tabela processa o backlog e termina.
--
--   áudios          → Volume ${volume_landing} (ponteiro + metadados, sem bytes)
--   operacional     → Volume ${volume_operacional}, uma pasta por tabela
--                     do sistema de pedidos e do de atendimento
--   texto           → transcrições que a telefonia já exporta prontas
--
-- Arquivo sobrescrito com o mesmo nome não é relido. Ao regerar o dado
-- sintético, rode o pipeline com full refresh.
--
-- `data_hora` chega como timestamp sem fuso (TIMESTAMP_NTZ), que a tabela
-- Delta só aceita com a feature timestampNtz ligada; o CAST evita isso.
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE bronze_audio_files
  COMMENT 'Áudios brutos das ligações de consultoras (ponteiro + metadados; sem bytes).'
  TBLPROPERTIES ('quality' = 'bronze', 'pipelines.reset.allowed' = 'false')
AS
SELECT
  regexp_extract(_metadata.file_path, '([^/]+)\\.(mp3|wav|m4a)$', 1) AS call_id,
  _metadata.file_path                                                AS audio_path,
  _metadata.file_size                                                AS size_bytes,
  _metadata.file_modification_time                                   AS source_modified_at,
  current_timestamp()                                                AS ingested_at
FROM STREAM read_files(
  '/Volumes/${catalog}/${schema}/${volume_landing}/',
  format              => 'binaryFile',
  recursiveFileLookup => true,
  pathGlobFilter      => '*.{mp3,wav,m4a}'
);

-- ----- Sistema de pedidos ---------------------------------------------------

CREATE OR REFRESH STREAMING TABLE bronze_consultoras
  COMMENT 'Cadastro de consultoras.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/consultoras', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_produtos
  COMMENT 'Catálogo de produtos, com preço e peso.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/produtos', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_centros_distribuicao
  COMMENT 'Centros de distribuição.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/centros_distribuicao', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_pedidos
  COMMENT 'Pedidos por ciclo, com CD e transportadora.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/pedidos', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_pedido_itens
  COMMENT 'Itens de cada pedido.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/pedido_itens', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_entregas
  COMMENT 'Entregas: datas, status, peso na expedição e no hub, comprovante.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/entregas', format => 'parquet');

-- ----- Sistema de atendimento -----------------------------------------------

CREATE OR REFRESH STREAMING TABLE bronze_chamados
  COMMENT 'Chamados de falta de produto abertos pelas consultoras.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/chamados', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_chamado_itens
  COMMENT 'Itens que a consultora diz que faltaram.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/chamado_itens', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_investigacoes
  COMMENT 'Resultado das investigações — o único rótulo de fraude que existe.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT *, _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/investigacoes', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_ligacoes
  COMMENT 'Ligações da central, com o chamado vinculado no CRM.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT * EXCEPT (data_hora), CAST(data_hora AS TIMESTAMP) AS data_hora,
          _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/ligacoes', format => 'parquet');

CREATE OR REFRESH STREAMING TABLE bronze_transcricoes_texto
  COMMENT 'Transcrições que a plataforma de telefonia já exporta em texto.' TBLPROPERTIES ('quality' = 'bronze')
AS SELECT * EXCEPT (data_hora), CAST(data_hora AS TIMESTAMP) AS data_hora,
          _metadata.file_path AS _arquivo, current_timestamp() AS _ingerido_em
FROM STREAM read_files('/Volumes/${catalog}/${schema}/${volume_operacional}/transcricoes_texto', format => 'parquet');
