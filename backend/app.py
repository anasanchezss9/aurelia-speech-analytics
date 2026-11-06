"""FastAPI app: Speech Analytics demo (Aurélia).

Endpoints:
  GET  /health                  - liveness
  GET  /api/recursos            - o que está ligado neste deploy (gravação ao vivo)
  GET  /api/presets             - opções de análise pré-configuradas
  POST /api/analyze             - roda LLM sobre uma transcrição
  WS   /ws/transcribe           - streaming: recebe chunks de áudio,
                                  devolve transcrição parcial
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from llm_client import PRESET_FIELDS, LLMClient
from fila_routes import router as fila_router
from portal_routes import router as portal_router
from whisper_client import WhisperClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("speech-analytics")

app = FastAPI(title="Aurélia Speech Analytics Demo", version="0.1.0")

# CORS — em dev rodamos React em :5173 e backend em :8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

whisper = WhisperClient()
llm = LLMClient()

# Rotas do portal (upload, dashboard, calls, audio streaming) e da fila (Lakebase)
app.include_router(portal_router)
app.include_router(fila_router)


# ---------- Models ----------


class PresetOut(BaseModel):
    key: str
    label: str
    description: str


class AnalyzeIn(BaseModel):
    transcript: str = Field(..., min_length=1)
    presets: list[str] = Field(default_factory=list)
    custom_questions: list[str] = Field(default_factory=list)


class AnalyzeOut(BaseModel):
    result: dict


# Labels amigáveis para a UI
PRESET_LABELS: dict[str, str] = {
    "sentiment": "Sentimento (positivo/neutro/negativo)",
    "emotion": "Emoção predominante",
    "intent": "Intenção da ligação",
    "topics": "Tópicos / assuntos",
    "churn_risk": "Risco de churn (0 a 1)",
    "compliance_ok": "Compliance — script obrigatório",
    "summary": "Resumo executivo",
    "next_action": "Próxima ação recomendada",
}


# ---------- REST ----------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/recursos")
def recursos() -> dict[str, bool]:
    # A gravação ao vivo depende de um endpoint Whisper em GPU; sem ele, as abas
    # Configurar e Gravar somem em vez de falhar na primeira fala.
    return {"gravacao_ao_vivo": bool(os.getenv("WHISPER_ENDPOINT"))}


@app.get("/api/presets", response_model=list[PresetOut])
def list_presets() -> list[PresetOut]:
    return [
        PresetOut(
            key=k,
            label=PRESET_LABELS.get(k, k),
            description=PRESET_FIELDS[k].get("description", ""),
        )
        for k in PRESET_FIELDS
    ]


@app.post("/api/warmup")
async def warmup() -> dict[str, str]:
    """
    Aquece o endpoint Whisper enviando um WAV minúsculo de silêncio.
    Chame ao entrar na tela de gravação para evitar cold start de ~10s
    na primeira chamada real.
    """
    import io
    import numpy as np
    import soundfile as sf

    sr = 16000
    silence = np.zeros(int(sr * 0.5), dtype=np.int16)
    buf = io.BytesIO()
    sf.write(buf, silence, sr, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    import base64
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

    def _call():
        return whisper.w.serving_endpoints.query(
            name=whisper.endpoint_name,
            dataframe_records=[{"audio_b64": audio_b64, "language": "pt"}],
        )

    try:
        await asyncio.to_thread(_call)
        return {"status": "warm"}
    except Exception as exc:
        logger.exception("Falha no warmup")
        return {"status": "error", "message": str(exc)}


@app.post("/api/analyze", response_model=AnalyzeOut)
async def analyze(payload: AnalyzeIn) -> AnalyzeOut:
    logger.info(
        "analyze: presets=%s custom=%d transcript_len=%d",
        payload.presets,
        len(payload.custom_questions),
        len(payload.transcript),
    )
    result = await asyncio.to_thread(
        llm.analyze,
        payload.transcript,
        payload.presets,
        payload.custom_questions,
    )
    return AnalyzeOut(result=result)


# ---------- WebSocket: streaming de áudio ----------
#
# Protocolo simples (texto JSON ou binário):
#   - cliente envia binário: chunk de áudio (WebM/Opus do MediaRecorder)
#   - cliente envia texto:   {"type":"flush"} -> força transcrição agora
#                            {"type":"end"}   -> fecha
#   - servidor envia texto:  {"type":"partial", "text": "..."}
#                            {"type":"final",   "text": "..."}
#                            {"type":"error",   "message": "..."}
#
# Estratégia: acumulamos os chunks num buffer; a cada FLUSH ou a cada N segundos,
# concatenamos e mandamos pro Whisper. WebM/Opus é cumulativo (cada chunk
# precisa ser decodificado junto com os anteriores), por isso enviamos o buffer
# inteiro a cada flush — o backend é stateless por sessão.


SESSION_FLUSH_SECONDS = float(os.getenv("WHISPER_FLUSH_SECONDS", "3.5"))


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket) -> None:
    await ws.accept()
    logger.info("WS conectado")

    audio_buffer = io.BytesIO()
    last_flush = asyncio.get_event_loop().time()
    closed = False
    chunk_count = 0
    total_bytes = 0
    flush_count = 0

    async def do_transcribe(final: bool = False) -> None:
        nonlocal last_flush, flush_count
        data = audio_buffer.getvalue()
        flush_count += 1
        logger.info(
            "flush#%d final=%s buffer_bytes=%d", flush_count, final, len(data)
        )
        if len(data) < 2048:
            logger.info("flush#%d: buffer pequeno demais, skip", flush_count)
            return
        try:
            t0 = asyncio.get_event_loop().time()
            result = await asyncio.to_thread(whisper.transcribe, data, "pt")
            dt = asyncio.get_event_loop().time() - t0
            logger.info(
                "flush#%d: whisper ok em %.2fs, text_len=%d",
                flush_count,
                dt,
                len(result.text),
            )
        except Exception as exc:
            logger.exception("Falha na transcrição (flush#%d)", flush_count)
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
            return

        msg_type: Literal["partial", "final"] = "final" if final else "partial"
        await ws.send_json(
            {
                "type": msg_type,
                "text": result.text,
                "duration": result.duration,
            }
        )
        last_flush = asyncio.get_event_loop().time()

    try:
        while not closed:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                logger.info("WS recebeu disconnect")
                break

            if "bytes" in msg and msg["bytes"]:
                chunk = msg["bytes"]
                audio_buffer.write(chunk)
                chunk_count += 1
                total_bytes += len(chunk)
                if chunk_count <= 3 or chunk_count % 5 == 0:
                    logger.info(
                        "chunk#%d size=%d total=%d",
                        chunk_count,
                        len(chunk),
                        total_bytes,
                    )

                now = asyncio.get_event_loop().time()
                if now - last_flush >= SESSION_FLUSH_SECONDS:
                    await do_transcribe(final=False)
            elif "text" in msg and msg["text"]:
                import json

                try:
                    cmd = json.loads(msg["text"])
                except json.JSONDecodeError:
                    cmd = {}
                ctype = cmd.get("type")
                logger.info(
                    "cmd=%s chunks=%d bytes=%d", ctype, chunk_count, total_bytes
                )
                if ctype == "flush":
                    await do_transcribe(final=False)
                elif ctype == "end":
                    await do_transcribe(final=True)
                    closed = True
                    break
    except WebSocketDisconnect:
        logger.info(
            "WS desconectado pelo cliente (chunks=%d bytes=%d)",
            chunk_count,
            total_bytes,
        )
    except Exception:
        logger.exception("Erro no WS")
        try:
            await ws.send_json({"type": "error", "message": "internal"})
        except Exception:
            pass
    finally:
        logger.info(
            "WS encerrando: chunks=%d bytes=%d flushes=%d",
            chunk_count,
            total_bytes,
            flush_count,
        )
        try:
            await ws.close()
        except Exception:
            pass


# ---------- Servir frontend ----------
# Estratégia simples: HTML/CSS/JS standalone em ./static, sem build step.
# O portal é HTML e JS puros em static/, servidos pelo próprio FastAPI.

_HERE = Path(__file__).parent
STATIC_DIR = _HERE / "static"

if STATIC_DIR.exists():
    logger.info("Servindo frontend estático de %s", STATIC_DIR)
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static",
    )

    @app.get("/")
    def root():
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"error": "index.html not found"}
else:
    logger.warning("Pasta static/ não encontrada — rodando apenas API.")
