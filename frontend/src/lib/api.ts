// Cliente fino para a API FastAPI.

export type Preset = {
  key: string;
  label: string;
  description: string;
};

export type AnalysisConfig = {
  presets: string[];
  customQuestions: string[];
};

export type AnalyzeResponse = {
  result: Record<string, unknown>;
};

export async function fetchPresets(): Promise<Preset[]> {
  const r = await fetch("/api/presets");
  if (!r.ok) throw new Error(`/api/presets ${r.status}`);
  return r.json();
}

export async function analyze(
  transcript: string,
  config: AnalysisConfig
): Promise<AnalyzeResponse> {
  const r = await fetch("/api/analyze", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      transcript,
      presets: config.presets,
      custom_questions: config.customQuestions,
    }),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`/api/analyze ${r.status}: ${text}`);
  }
  return r.json();
}

export function buildWsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}
