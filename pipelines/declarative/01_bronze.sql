-- ===================================================================
-- Bronze — ingestão de áudios em STREAMING TABLE 100% SQL
-- ===================================================================
-- Lê arquivos novos do volume audio_landing usando STREAM read_files,
-- que é o equivalente declarativo do Auto Loader. SDP gerencia
-- checkpoints, idempotência e schema evolution sozinho.
--
-- Em modo "triggered" (batch), esta tabela processa todo o backlog
-- de novos arquivos e termina a execução. Os arquivos já lidos não
-- são reprocessados — o SDP rastreia isso automaticamente.
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE bronze_audio_files
  COMMENT 'Áudios brutos de chamadas de consultoras Aurélia (ponteiro + metadados; sem bytes).'
  TBLPROPERTIES (
    'quality' = 'bronze',
    'pipelines.reset.allowed' = 'false'
  )
AS
SELECT
  -- call_id estável a partir do nome do arquivo (sem extensão)
  regexp_extract(_metadata.file_path, '([^/]+)\\.(mp3|wav|m4a)$', 1) AS call_id,
  _metadata.file_path                                                AS audio_path,
  _metadata.file_size                                                AS size_bytes,
  _metadata.file_modification_time                                   AS source_modified_at,
  current_timestamp()                                                AS ingested_at
FROM STREAM read_files(
  '/Volumes/${catalog}/${schema}/${volume_landing}/',
  format        => 'binaryFile',
  pathGlobFilter => '*.mp3'
);
