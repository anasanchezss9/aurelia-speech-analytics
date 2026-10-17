import { useState } from "react";
import { useTranscription } from "../hooks/useTranscription";
import { analyze } from "../lib/api";

type Props = {
  presets: string[];
  customQuestions: string[];
  onBack: () => void;
};

function formatTimer(sec: number) {
  const m = Math.floor(sec / 60).toString().padStart(2, "0");
  const s = (sec % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function isPrimitive(v: unknown): v is string | number | boolean {
  return ["string", "number", "boolean"].includes(typeof v);
}

function renderValue(value: unknown): React.ReactNode {
  if (value === null || value === undefined) return <em>—</em>;
  if (Array.isArray(value)) {
    return value.map((item, i) => (
      <span className="pill" key={i}>
        {String(item)}
      </span>
    ));
  }
  if (typeof value === "boolean") {
    return <span className={`pill ${value ? "green" : "red"}`}>{value ? "Sim" : "Não"}</span>;
  }
  if (typeof value === "number") {
    return value.toFixed(2);
  }
  if (isPrimitive(value)) return String(value);
  return <pre style={{ fontSize: 12 }}>{JSON.stringify(value, null, 2)}</pre>;
}

const FIELD_LABELS: Record<string, string> = {
  sentiment: "Sentimento",
  emotion: "Emoção",
  intent: "Intenção",
  topics: "Tópicos",
  churn_risk: "Risco de churn",
  compliance_ok: "Compliance",
  summary: "Resumo",
  next_action: "Próxima ação",
};

export function RecordPage({ presets, customQuestions, onBack }: Props) {
  const { status, transcript, error, start, stop, elapsedSec } = useTranscription();
  const [analysis, setAnalysis] = useState<Record<string, unknown> | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  const isRecording = status === "recording" || status === "connecting";
  const canAnalyze = status === "stopped" && transcript.trim().length > 0;

  const onClickRec = async () => {
    if (status === "idle" || status === "error" || status === "stopped") {
      setAnalysis(null);
      setAnalyzeError(null);
      await start();
    } else if (isRecording) {
      await stop();
    }
  };

  const runAnalysis = async () => {
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      const r = await analyze(transcript, { presets, customQuestions });
      setAnalysis(r.result);
    } catch (e) {
      setAnalyzeError(e instanceof Error ? e.message : String(e));
    } finally {
      setAnalyzing(false);
    }
  };

  return (
    <div>
      <h2>Gravar e transcrever</h2>
      <p className="subtitle">
        Clique no botão para gravar. A transcrição aparece em tempo real — quando parar,
        rodamos a análise.
      </p>

      {error && <div className="error-box">{error}</div>}

      <div className="card">
        <div className="recorder">
          <button
            className={`rec-button ${isRecording ? "recording" : ""}`}
            onClick={onClickRec}
            disabled={status === "stopping"}
            aria-label={isRecording ? "Parar gravação" : "Iniciar gravação"}
          >
            <span className="icon" />
          </button>
          <div className={`rec-status ${isRecording ? "recording" : ""}`}>
            {status === "idle" && "Pronto para gravar"}
            {status === "connecting" && "Conectando…"}
            {status === "recording" && "● Gravando — clique para parar"}
            {status === "stopping" && "Finalizando…"}
            {status === "stopped" && "Gravação concluída"}
            {status === "error" && "Erro — clique para tentar novamente"}
          </div>
          <div className="timer">{formatTimer(elapsedSec)}</div>
        </div>
      </div>

      <div className="card">
        <h3>Transcrição em tempo real</h3>
        <div className={`transcript-box ${transcript ? "" : "empty"}`}>
          {transcript || "A transcrição aparecerá aqui conforme você fala…"}
        </div>
      </div>

      <div className="card">
        <h3>Análise inteligente</h3>
        {!analysis && !analyzing && (
          <p style={{ color: "var(--db-muted)", fontSize: 14, marginBottom: 12 }}>
            {canAnalyze
              ? "Clique em 'Analisar' para extrair os insights configurados."
              : "Pare a gravação para habilitar a análise."}
          </p>
        )}

        {analyzeError && <div className="error-box">{analyzeError}</div>}

        {analyzing && (
          <p>
            <span className="spinner" />
            Analisando com Claude Sonnet 4.6…
          </p>
        )}

        {analysis && (
          <div className="analysis-grid">
            {Object.entries(analysis).map(([key, value]) => {
              const label =
                FIELD_LABELS[key] ??
                (key.startsWith("q") ? customQuestions[Number(key.slice(1)) - 1] || key : key);
              const wide =
                key === "summary" ||
                key === "next_action" ||
                key === "topics" ||
                key.startsWith("q");
              return (
                <div key={key} className={`analysis-item ${wide ? "full" : ""}`}>
                  <div className="analysis-key">{label}</div>
                  <div className="analysis-value">{renderValue(value)}</div>
                </div>
              );
            })}
          </div>
        )}

        {canAnalyze && !analysis && (
          <div style={{ marginTop: 16 }}>
            <button onClick={runAnalysis} disabled={analyzing}>
              {analyzing ? "Analisando…" : "Analisar transcrição"}
            </button>
          </div>
        )}
      </div>

      <div className="actions">
        <button className="ghost" onClick={onBack}>
          ← Voltar para configuração
        </button>
        <div />
      </div>
    </div>
  );
}
