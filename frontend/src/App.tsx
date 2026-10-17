import { useState } from "react";
import { ConfigPage } from "./pages/ConfigPage";
import { RecordPage } from "./pages/RecordPage";

type Step = "config" | "record";

export default function App() {
  const [step, setStep] = useState<Step>("config");
  const [presets, setPresets] = useState<string[]>([]);
  const [customQuestions, setCustomQuestions] = useState<string[]>([]);

  return (
    <div className="app">
      <header className="app-header">
        <span className="dot" />
        <h1>Aurélia · Speech Analytics</h1>
        <span className="crumbs">
          {step === "config" ? "1. Configurar" : "2. Gravar & analisar"}
        </span>
      </header>

      {step === "config" && (
        <ConfigPage
          initialSelected={presets}
          initialQuestions={customQuestions}
          onContinue={(p, q) => {
            setPresets(p);
            setCustomQuestions(q);
            setStep("record");
          }}
        />
      )}

      {step === "record" && (
        <RecordPage
          presets={presets}
          customQuestions={customQuestions}
          onBack={() => setStep("config")}
        />
      )}

      <div className="footer">
        Powered by Whisper Large V3 Turbo + Claude Sonnet 4.6 · Databricks Mosaic AI
      </div>
    </div>
  );
}
