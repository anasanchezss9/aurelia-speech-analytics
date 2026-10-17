"""Cliente para o endpoint de Model Serving do Whisper.

Suporta 2 dialetos de pyfunc:
  - 'audio_b64' + 'language' -> {text, language, duration}
  - 'audio'                  -> string com a transcrição direta (o endpoint deste repo)

A escolha é controlada via env WHISPER_SCHEMA (default: 'simple').
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from audio_utils import webm_to_wav

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration: float


class WhisperClient:
    def __init__(
        self,
        endpoint_name: str | None = None,
        workspace: WorkspaceClient | None = None,
    ):
        self.endpoint_name = endpoint_name or os.getenv(
            "WHISPER_ENDPOINT", "aurelia-whisper-turbo"
        )
        # 'simple'   -> {audio: b64} -> string
        # 'rich'     -> {audio_b64, language} -> {text,language,duration}
        self.schema = os.getenv("WHISPER_SCHEMA", "simple").lower()
        self.w = workspace or WorkspaceClient()

    def transcribe(self, audio_bytes: bytes, language: str = "pt") -> TranscriptionResult:
        """
        Recebe os bytes brutos de um áudio (WebM/Opus do MediaRecorder, ou
        WAV/MP3/M4A) e devolve a transcrição.

        Para WebM/Opus convertemos pra WAV no app porque o container do
        Model Serving usa libsndfile que não suporta WebM/Opus.
        Para WAV/MP3/M4A já mandamos os bytes originais.
        """
        # Detecta WebM (header RIFF != WebM EBML 0x1A45DFA3)
        is_webm = audio_bytes[:4] == b"\x1a\x45\xdf\xa3"
        if is_webm:
            try:
                send_bytes = webm_to_wav(audio_bytes, target_sr=16000)
            except Exception:
                logger.exception(
                    "Falha na conversão WebM->WAV (in=%d bytes)", len(audio_bytes)
                )
                raise
        else:
            send_bytes = audio_bytes

        audio_b64 = base64.b64encode(send_bytes).decode("ascii")

        if self.schema == "rich":
            payload = {"audio_b64": audio_b64, "language": language}
        else:
            payload = {"audio": audio_b64}

        try:
            resp = self.w.serving_endpoints.query(
                name=self.endpoint_name,
                dataframe_records=[payload],
            )
        except Exception:
            logger.exception("Falha ao chamar endpoint %s", self.endpoint_name)
            raise

        preds = resp.predictions
        # Schema 'simple': predictions = ["texto transcrito"]
        # Schema 'rich':   predictions = [{text, language, duration}]
        if isinstance(preds, list) and preds:
            row = preds[0]
        elif isinstance(preds, dict):
            row = preds
        else:
            raise RuntimeError(f"Resposta inesperada do endpoint: {preds!r}")

        if isinstance(row, str):
            return TranscriptionResult(text=row.strip(), language=language, duration=0.0)
        return TranscriptionResult(
            text=str(row.get("text", "")).strip(),
            language=str(row.get("language", language)),
            duration=float(row.get("duration", 0.0)),
        )
