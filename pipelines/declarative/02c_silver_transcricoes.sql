-- ===================================================================
-- Silver — todas as transcrições numa tabela só
-- ===================================================================
-- O gold não deve saber de onde a transcrição veio. Dois fluxos escrevem
-- na mesma STREAMING TABLE: o do áudio, transcrito por ai_transcribe (ou
-- pelo Whisper, conforme o 02_* em `libraries`), e o do texto que a
-- telefonia já exporta. Uma fonte nova é um CREATE FLOW a mais, sem
-- mexer no gold.
-- ===================================================================

CREATE OR REFRESH STREAMING TABLE silver_transcricoes (
  call_id       STRING    NOT NULL,
  full_text     STRING,
  origem        STRING    COMMENT 'audio | texto',
  transcrito_em TIMESTAMP,
  CONSTRAINT texto_suficiente EXPECT (length(full_text) > 20) ON VIOLATION DROP ROW
)
COMMENT 'Transcrições de todas as ligações, de áudio ou já em texto.'
TBLPROPERTIES ('quality' = 'silver');

CREATE FLOW transcricoes_do_audio
AS INSERT INTO silver_transcricoes BY NAME
SELECT call_id, full_text, 'audio' AS origem, transcribed_at AS transcrito_em
FROM STREAM(silver_transcripts)
WHERE full_text IS NOT NULL;

CREATE FLOW transcricoes_em_texto
AS INSERT INTO silver_transcricoes BY NAME
SELECT call_id, full_text, 'texto' AS origem, gerado_em AS transcrito_em
FROM STREAM(bronze_transcricoes_texto);
