-- ===================================================================
-- Silver — Transcrição via Whisper, 100% SQL com ai_query
-- ===================================================================
-- Lê os bytes do volume com read_files(format='binaryFile') usando o
-- audio_path da bronze, e chama o endpoint Whisper via ai_query.
--
-- IMPORTANTE: ai_query consegue chamar QUALQUER endpoint de Model
-- Serving — incluindo modelos pyfunc customizados como o Whisper.
-- O endpoint do Whisper é um pyfunc do MLflow que
-- espera DataFrame com coluna `audio` em base64:
--   {"dataframe_records": [{"audio": "<base64_audio>"}]}
-- e devolve:
--   {"predictions": ["<texto transcrito>"]}
--
-- Para o ai_query mandar nesse formato, passamos `request` como
-- STRUCT — o Spark converte cada linha em dataframe_records auto.
--
-- A STREAMING TABLE garante que cada call_id é processado UMA ÚNICA
-- vez — quando uma nova linha chega na bronze, o SDP só roda o
-- Whisper para esse áudio novo, sem nenhum MERGE manual.
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE silver_transcripts
  COMMENT 'Transcrições das chamadas, geradas pelo endpoint Whisper Turbo.'
  TBLPROPERTIES ('quality' = 'silver')
AS
WITH novos_audios_com_bytes AS (
  -- Pega só os áudios novos (STREAM = só linhas inéditas) e lê os
  -- bytes do volume usando uma 2ª chamada read_files. Vira uma linha
  -- por arquivo com a coluna `content`.
  --
  -- ATENÇÃO: STREAMING TABLE expõe `_metadata.file_path` em formato
  -- POSIX puro (`/Volumes/...`), mas read_files BATCH retorna com
  -- prefixo `dbfs:`. Normalizamos no JOIN tirando o prefixo.
  SELECT
    b.call_id,
    b.audio_path,
    base64(f.content) AS audio_b64
  FROM STREAM bronze_audio_files b
  JOIN read_files('/Volumes/${catalog}/${schema}/${volume_landing}/', format => 'binaryFile') f
    ON regexp_replace(f.path, '^dbfs:', '') = b.audio_path
)
SELECT
  call_id,
  audio_path,
  current_timestamp() AS transcribed_at,
  '${whisper_endpoint}' AS whisper_endpoint,

  -- ai_query manda STRUCT(audio=audio_b64) que vira dataframe_records
  -- com a coluna `audio` que o pyfunc do Whisper espera.
  ai_query(
    '${whisper_endpoint}',
    named_struct('audio', audio_b64),
    returnType => 'STRING'
  ) AS full_text
FROM novos_audios_com_bytes;


-- View final achatada — facilita consumo no Gold e no BI
CREATE OR REFRESH MATERIALIZED VIEW silver_transcripts_clean
  COMMENT 'Transcrições normalizadas para consumo downstream.'
  TBLPROPERTIES ('quality' = 'silver')
AS
SELECT
  call_id,
  audio_path,
  transcribed_at,
  whisper_endpoint,
  full_text,
  CASE
    WHEN full_text IS NULL OR length(full_text) < 10
      THEN 'TRANSCRIPTION_EMPTY_OR_FAILED'
    ELSE NULL
  END AS transcription_error
FROM silver_transcripts;
