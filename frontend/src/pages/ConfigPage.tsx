import { useEffect, useState } from "react";
import { fetchPresets, type Preset } from "../lib/api";

type Props = {
  initialSelected: string[];
  initialQuestions: string[];
  onContinue: (presets: string[], customQuestions: string[]) => void;
};

const DEFAULT_SELECTION = ["sentiment", "intent", "topics", "summary", "next_action"];

export function ConfigPage({ initialSelected, initialQuestions, onContinue }: Props) {
  const [presets, setPresets] = useState<Preset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(initialSelected.length ? initialSelected : DEFAULT_SELECTION)
  );
  const [questions, setQuestions] = useState<string[]>(
    initialQuestions.length ? initialQuestions : [""]
  );

  useEffect(() => {
    fetchPresets()
      .then(setPresets)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const updateQuestion = (i: number, value: string) => {
    setQuestions((prev) => prev.map((q, idx) => (idx === i ? value : q)));
  };

  const addQuestion = () => setQuestions((prev) => [...prev, ""]);
  const removeQuestion = (i: number) =>
    setQuestions((prev) => prev.filter((_, idx) => idx !== i));

  const handleContinue = () => {
    const cleanQuestions = questions.map((q) => q.trim()).filter(Boolean);
    onContinue(Array.from(selected), cleanQuestions);
  };

  const canContinue = selected.size > 0 || questions.some((q) => q.trim());

  return (
    <div>
      <h2>Configurar análise</h2>
      <p className="subtitle">
        Escolha o que extrair do áudio. Você pode marcar análises pré-configuradas e/ou
        adicionar perguntas livres em português.
      </p>

      {error && <div className="error-box">Erro ao carregar presets: {error}</div>}

      <div className="card">
        <h3>Análises pré-configuradas</h3>
        {!presets && <p style={{ color: "var(--db-muted)" }}>Carregando…</p>}
        {presets && (
          <div className="preset-grid">
            {presets.map((p) => (
              <label
                key={p.key}
                className={`preset ${selected.has(p.key) ? "checked" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(p.key)}
                  onChange={() => toggle(p.key)}
                />
                <div className="preset-text">
                  <div className="preset-label">{p.label}</div>
                  <div className="preset-desc">{p.description}</div>
                </div>
              </label>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <h3>Perguntas customizadas (opcional)</h3>
        <p style={{ color: "var(--db-muted)", fontSize: 13, marginBottom: 12 }}>
          Ex.: "O agente ofereceu produto da linha Raízes?", "Houve menção a desconto?"
        </p>
        {questions.map((q, i) => (
          <div className="custom-row" key={i}>
            <input
              type="text"
              placeholder={`Pergunta ${i + 1}`}
              value={q}
              onChange={(e) => updateQuestion(i, e.target.value)}
            />
            {questions.length > 1 && (
              <button
                type="button"
                className="ghost"
                onClick={() => removeQuestion(i)}
                aria-label="Remover"
              >
                ×
              </button>
            )}
          </div>
        ))}
        <button type="button" className="ghost" onClick={addQuestion}>
          + Adicionar pergunta
        </button>
      </div>

      <div className="actions">
        <div />
        <button onClick={handleContinue} disabled={!canContinue}>
          Continuar para gravação →
        </button>
      </div>
    </div>
  );
}
