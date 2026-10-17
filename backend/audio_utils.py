"""
Conversão de áudio: recebe um buffer cumulativo de WebM/Opus (vindo do
MediaRecorder do navegador) e devolve WAV PCM 16kHz mono 16-bit.

Usamos PyAV (wheel binário com FFmpeg embutido) para evitar dependência de
ffmpeg system-wide no Databricks App.
"""

from __future__ import annotations

import io
import logging

import av  # PyAV
import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def webm_to_wav(audio_bytes: bytes, target_sr: int = 16000) -> bytes:
    """
    Converte qualquer container suportado pelo FFmpeg (WebM, Ogg, MP4, WAV,
    MP3...) para WAV PCM 16-bit mono na sample rate desejada.

    Estratégia robusta a chunks parciais do MediaRecorder: usa PyAV para
    decodificar todo o áudio, resample para 16kHz mono, e serializa via
    soundfile.
    """
    if not audio_bytes:
        raise ValueError("audio_bytes vazio")

    # Decode com PyAV
    pcm_chunks: list[np.ndarray] = []
    try:
        container = av.open(io.BytesIO(audio_bytes))
    except av.error.InvalidDataError as exc:  # type: ignore[attr-defined]
        raise ValueError(f"PyAV não conseguiu abrir o stream: {exc}") from exc

    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("Stream de áudio não encontrado")

        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_sr)
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                arr = out_frame.to_ndarray()  # shape (channels, n) ou (1, n)
                if arr.ndim == 2:
                    arr = arr[0]
                pcm_chunks.append(arr)
        # Drena o resampler
        for out_frame in resampler.resample(None):
            arr = out_frame.to_ndarray()
            if arr.ndim == 2:
                arr = arr[0]
            pcm_chunks.append(arr)
    finally:
        container.close()

    if not pcm_chunks:
        raise ValueError("Nenhum frame de áudio decodificado")

    pcm = np.concatenate(pcm_chunks).astype(np.int16)
    logger.debug(
        "webm_to_wav: in=%d bytes -> out=%d samples @ %dHz",
        len(audio_bytes),
        len(pcm),
        target_sr,
    )

    # Serializa como WAV
    out = io.BytesIO()
    sf.write(out, pcm, target_sr, format="WAV", subtype="PCM_16")
    return out.getvalue()
