# Aurélia · Speech Analytics Demo

Demo end-to-end mostrando **transcrição de áudio em tempo real com Whisper Turbo** +
**análise com Claude Sonnet 4.6**, tudo rodando dentro do Databricks (Model Serving GPU
+ Foundation Model API + Databricks Apps).

## Fluxo da demo

1. **Tela 1 — Configurar**: usuário escolhe quais análises rodar (sentimento, intenção,
   risco de churn, compliance…) e/ou escreve perguntas customizadas em PT-BR.
2. **Tela 2 — Gravar**: clica num botão estilo voicemail, fala no microfone. A
   transcrição aparece **em tempo real** via WebSocket conforme o Whisper processa
   janelas de ~3.5s do áudio.
3. Quando a gravação termina, um botão **Analisar** dispara o Claude com a configuração
   da Tela 1 e mostra o JSON estruturado de insights na própria tela.

## Arquitetura

```
[Browser]
   │ MediaRecorder (WebM/Opus) ──────► /ws/transcribe ──► Whisper Endpoint (GPU)
   │                                                         (Model Serving)
   │ POST transcript + config ───────► /api/analyze   ──► databricks-claude-sonnet-4-6
   │                                                         (Foundation Model API)
   ▼
React (Vite) servido pelo FastAPI no Databricks App
```

- **Backend**: FastAPI + WebSocket. Stateless por sessão; reusa o cliente
  `databricks-sdk` para chamar os endpoints com OAuth do app.
- **Frontend**: React 18 + Vite + TypeScript. Sem dependências de UI extras — CSS
  puro estilizado como Databricks.
- **Modelo Whisper**: empacotado como `mlflow.pyfunc` e deployado com
  `WORKLOAD_TYPE=GPU_MEDIUM` (A10G) e scale-to-zero.

## Estrutura

```
speech-analytics-demo/
├── notebooks/
│   └── 01_deploy_whisper_turbo.py   # 1. Cria o endpoint Whisper
├── backend/
│   ├── app.py                        # FastAPI + WebSocket
│   ├── whisper_client.py             # Cliente do endpoint
│   ├── llm_client.py                 # Cliente Claude + JSON Schema
│   ├── requirements.txt
│   └── app.yaml                      # Config do Databricks App
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/{ConfigPage,RecordPage}.tsx
│   │   ├── hooks/useTranscription.ts # WebSocket + MediaRecorder
│   │   └── lib/api.ts
│   └── package.json
└── databricks.yml                    # DAB para deploy do app
```

---

## Setup — passo a passo

### Pré-requisitos

- Databricks CLI configurado: profile `fe-vm-casas-bahia-genie-demo` (ou ajuste no
  `databricks.yml`).
- Permissões: criar serving endpoints, criar Databricks Apps, `CREATE MODEL` no UC.
- Localmente: Node 18+ e Python 3.11+.

### 1. Deploy do Whisper Turbo

✅ **Já feito** — o modelo `field_toolbox_catalog.aurelia_speech.whisper_large_v3_turbo`
está registrado no UC e o endpoint `aurelia-whisper-turbo` (GPU_MEDIUM, scale-to-zero)
está sendo criado.

Verifique status:

```bash
databricks --profile fe-vm-casas-bahia-genie-demo \
  serving-endpoints get aurelia-whisper-turbo
```

> O modelo fica no mesmo schema das tabelas do pipeline:
> `field_toolbox_catalog.aurelia_speech`.

### 2. Rodar localmente (desenvolvimento)

Em dois terminais:

**Backend:**
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABRICKS_CONFIG_PROFILE=fe-vm-casas-bahia-genie-demo
export WHISPER_ENDPOINT=aurelia-whisper-turbo
export LLM_MODEL=databricks-claude-sonnet-4-6
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

**Frontend:**
```bash
cd frontend
bun install        # ou npm install
bun run dev        # ou npm run dev
```

Abra http://localhost:5173 — o Vite faz proxy de `/api` e `/ws` para o backend
em :8000.

> ⚠️ Para usar o microfone, navegadores exigem `https` ou `localhost`. Em dev, use
> `localhost` (não `127.0.0.1` em Safari).

### 3. Deploy como Databricks App

Build do frontend e empacotar junto com o backend:

```bash
cd frontend
bun install && bun run build           # gera frontend/dist
cd ..
```

O `app.py` já serve o `frontend/dist` em produção quando ele existe — então copiamos
para dentro de `backend/` antes do deploy:

```bash
cp -r frontend/dist backend/frontend_dist
```

E ajustamos `app.py` para também procurar em `./frontend_dist` (já feito via
`Path(__file__).parent.parent / "frontend" / "dist"` — alternativamente edite o path).

Deploy via DAB:

```bash
databricks bundle deploy --profile fe-vm-casas-bahia-genie-demo
databricks bundle run --profile fe-vm-casas-bahia-genie-demo speech_analytics
```

A URL do app aparece no output. O OAuth é automático: o app usa o service principal
gerado pelo Databricks Apps para chamar os endpoints (precisa de `CAN_QUERY`,
declarado em `databricks.yml`).

---

## Decisões de arquitetura (por que assim)

- **WebSocket cumulativo, não streaming chunked-decoding real**: WebM/Opus do
  MediaRecorder não é facilmente decodificável incrementalmente. Estratégia: cliente
  envia chunks de 1s, backend acumula no buffer e a cada ~3.5s pega o buffer
  inteiro, manda pro Whisper, devolve a transcrição cumulativa atualizada. É
  **near real-time** (latência de ~3-4s), simples e robusto.
- **Whisper Turbo, não Large V3**: 8x mais rápido com qualidade equivalente para
  PT-BR conversacional. A diferença vira UX perceptível.
- **JSON Schema dinâmico no Claude**: o usuário escolhe quais campos quer; nós
  construímos o `response_format` com `json_schema` correspondente. Isso garante
  saída estruturada e evita parse de free-text.
- **Sem AI Functions SQL aqui**: para a demo interativa, chamar `ai_query` via SQL
  warehouse seria 1 hop a mais. A Foundation Model API direta é mais rápida e
  suficiente. Mostre o `ai_query` na slide 9 da apresentação como o **padrão
  batch/SQL**.

---

## Roteiro de demo (sugestão para a Aurélia)

1. Abrir a apresentação `speech-analytics-aurelia.html` (slides 1-9).
2. Quando chegar no slide 8 (código batch), abrir o app de demo.
3. Tela 1: deixar o default + adicionar pergunta livre, ex.: *"O cliente mencionou
   produto da linha Raízes?"*.
4. Tela 2: gravar 30-60s simulando uma reclamação ("comprei um batom da Aurélia,
   chegou derretido pela segunda vez, quero ressarcimento…").
5. Mostrar o texto aparecendo conforme se fala.
6. Clicar em **Analisar** e mostrar o JSON estruturado: sentimento `negativo`,
   intenção `reclamacao`, churn_risk alto, próxima ação sugerida.
7. Voltar pros slides (10-14): streaming, casos de uso, fechamento.

Tempo total da demo ao vivo: **3-5 minutos**.
