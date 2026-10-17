import { useCallback, useEffect, useRef, useState } from "react";
import { buildWsUrl } from "../lib/api";

type Status = "idle" | "connecting" | "recording" | "stopping" | "stopped" | "error";

type UseTranscriptionResult = {
  status: Status;
  transcript: string;
  error: string | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  elapsedSec: number;
};

/**
 * Hook que:
 *  1. abre WebSocket /ws/transcribe
 *  2. captura áudio do microfone via MediaRecorder (WebM/Opus)
 *  3. envia chunks como binário pro backend
 *  4. recebe transcrições parciais/finais e concatena no estado
 */
export function useTranscription(): UseTranscriptionResult {
  const [status, setStatus] = useState<Status>("idle");
  const [transcript, setTranscript] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [elapsedSec, setElapsedSec] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const startTsRef = useRef<number>(0);

  const cleanup = useCallback(() => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      try {
        recorderRef.current.stop();
      } catch {}
    }
    recorderRef.current = null;

    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }

    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {}
      wsRef.current = null;
    }
  }, []);

  useEffect(() => () => cleanup(), [cleanup]);

  const start = useCallback(async () => {
    setError(null);
    setTranscript("");
    setElapsedSec(0);
    setStatus("connecting");

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          channelCount: 1,
          sampleRate: 16000,
        },
      });
      streamRef.current = stream;

      const ws = new WebSocket(buildWsUrl("/ws/transcribe"));
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.onerror = () => reject(new Error("WS connection error"));
      });

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (data.type === "partial" || data.type === "final") {
            // Whisper retransciona o buffer cumulativo a cada flush,
            // então sempre substituímos pelo texto mais recente.
            setTranscript(data.text || "");
          } else if (data.type === "error") {
            setError(data.message || "Erro na transcrição");
          }
        } catch {
          // ignora msgs não-JSON
        }
      };
      ws.onclose = () => {
        // será tratado em stop()
      };

      // Escolhe o melhor MIME suportado
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(
        (m) => MediaRecorder.isTypeSupported(m)
      );
      if (!mime) throw new Error("Browser não suporta MediaRecorder com WebM/Opus");

      const recorder = new MediaRecorder(stream, {
        mimeType: mime,
        audioBitsPerSecond: 64_000,
      });
      recorderRef.current = recorder;

      recorder.ondataavailable = async (ev) => {
        if (ev.data && ev.data.size > 0 && ws.readyState === WebSocket.OPEN) {
          const buf = await ev.data.arrayBuffer();
          ws.send(buf);
        }
      };
      // Emite chunks a cada 1s; o backend acumula e flusha a cada ~3.5s
      recorder.start(1000);

      startTsRef.current = Date.now();
      timerRef.current = window.setInterval(() => {
        setElapsedSec(Math.floor((Date.now() - startTsRef.current) / 1000));
      }, 250);

      setStatus("recording");
    } catch (e) {
      console.error(e);
      setError(e instanceof Error ? e.message : "Falha ao iniciar gravação");
      setStatus("error");
      cleanup();
    }
  }, [cleanup]);

  const stop = useCallback(async () => {
    if (status !== "recording") return;
    setStatus("stopping");

    // Para o recorder primeiro para liberar o último chunk
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      await new Promise<void>((resolve) => {
        const r = recorderRef.current!;
        r.onstop = () => resolve();
        r.stop();
      });
    }

    // Aguarda chegar o último chunk via ondataavailable
    await new Promise((r) => setTimeout(r, 200));

    // Pede flush final ao backend
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "end" }));

      // Aguarda mensagem 'final' (timeout 15s)
      await new Promise<void>((resolve) => {
        const timeout = setTimeout(resolve, 15_000);
        const onMsg = (ev: MessageEvent) => {
          try {
            const data = JSON.parse(ev.data);
            if (data.type === "final") {
              clearTimeout(timeout);
              ws.removeEventListener("message", onMsg);
              resolve();
            }
          } catch {}
        };
        ws.addEventListener("message", onMsg);
      });
    }

    if (timerRef.current) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (ws) {
      try {
        ws.close();
      } catch {}
    }
    wsRef.current = null;

    setStatus("stopped");
  }, [status]);

  return { status, transcript, error, start, stop, elapsedSec };
}
