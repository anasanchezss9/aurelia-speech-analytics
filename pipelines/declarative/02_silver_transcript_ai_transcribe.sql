-- ===================================================================
-- Silver — Transcrição com ai_transcribe, 100% SQL e sem endpoint
-- ===================================================================
-- Alternativa ao 02_silver_transcript.sql. Publica as mesmas tabelas
-- (silver_transcripts e silver_transcripts_clean) com as mesmas colunas
-- que o gold lê, então trocar de versão é trocar este arquivo pelo outro
-- em `libraries` do pipeline.yml. Os dois juntos no mesmo pipeline
-- colidem no nome da tabela — é um ou o outro.
--
-- O que muda em relação ao Whisper:
--   - Não há endpoint de GPU para subir, aquecer ou esquecer ligado.
--   - O WAV de telefonia (μ-law 8 kHz estéreo) entra direto: sem mono,
--     sem resample, sem chunking. O limite é 1 h e 512 MB por chamada.
--   - Cada segmento vem com speaker_id (diarização automática).
--   - Beta, e oficialmente só inglês e espanhol. Em PT-BR funciona, mas
--     sem garantia — compare com o Whisper no seu áudio antes de trocar.
--
-- Requer o preview "AI Transcribe" ligado no workspace e serverless
-- environment 3+ (o pipeline serverless já atende).
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE silver_transcripts
  COMMENT 'Transcrições das chamadas, geradas pela AI Function ai_transcribe.'
  TBLPROPERTIES ('quality' = 'silver')
AS
WITH novos_audios AS (
  -- recursiveFileLookup porque o portal grava em /uploads e /recordings.
  SELECT
    b.call_id,
    b.audio_path,
    ai_transcribe(f.content) AS r
  FROM STREAM bronze_audio_files b
  JOIN read_files(
    '/Volumes/${catalog}/${schema}/${volume_landing}/',
    format => 'binaryFile',
    recursiveFileLookup => true,
    pathGlobFilter => '*.{mp3,wav,m4a}'
  ) f
    ON regexp_replace(f.path, '^dbfs:', '') = regexp_replace(b.audio_path, '^dbfs:', '')
),
segmentos AS (
  SELECT
    call_id,
    audio_path,
    r:error_message::STRING             AS error_message,
    r:response.duration_seconds::DOUBLE AS duration_seconds,
    from_json(
      to_json(r:response.segments),
      'ARRAY<STRUCT<start: DOUBLE, `end`: DOUBLE, speaker_id: STRING, text: STRING>>'
    ) AS segments
  FROM novos_audios
)
SELECT
  call_id,
  audio_path,
  current_timestamp() AS transcribed_at,
  'ai_transcribe'     AS whisper_endpoint,
  array_join(transform(segments, x -> trim(x.text)), ' ') AS full_text,
  array_join(
    transform(segments, x -> concat('[Falante ', coalesce(x.speaker_id, '?'), '] ', trim(x.text))),
    '\n'
  ) AS full_text_falantes,
  duration_seconds,
  size(array_distinct(filter(transform(segments, x -> x.speaker_id), x -> x IS NOT NULL))) AS speaker_count,
  segments,
  error_message
FROM segmentos;


-- Mesma view do 02_silver_transcript.sql, com o erro da função quando há.
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
  full_text_falantes,
  speaker_count,
  CASE
    WHEN error_message IS NOT NULL THEN concat('AI_TRANSCRIBE_ERROR: ', error_message)
    WHEN full_text IS NULL OR length(full_text) < 10 THEN 'TRANSCRIPTION_EMPTY_OR_FAILED'
    ELSE NULL
  END AS transcription_error
FROM silver_transcripts;
