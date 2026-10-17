"""Cliente para análise via LLM (Claude Sonnet 4.6 + AI Functions)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


# Schema JSON dinâmico — construído a partir das opções escolhidas pelo usuário.
PRESET_FIELDS: dict[str, dict[str, Any]] = {
    "sentiment": {
        "type": "string",
        "enum": ["positivo", "neutro", "negativo"],
        "description": "Sentimento geral do cliente na ligação",
    },
    "emotion": {
        "type": "string",
        "description": "Emoção predominante (ex.: frustração, alegria, raiva, tranquilidade)",
    },
    "intent": {
        "type": "string",
        "enum": [
            "reclamacao",
            "suporte_tecnico",
            "cancelamento",
            "duvida_produto",
            "elogio",
            "outro",
        ],
        "description": "Intenção principal da ligação",
    },
    "topics": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tópicos/assuntos mencionados (até 5)",
    },
    "churn_risk": {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
        "description": "Probabilidade de churn (0=baixo, 1=alto)",
    },
    "compliance_ok": {
        "type": "boolean",
        "description": "True se o agente seguiu o script obrigatório (saudação, gravação, LGPD)",
    },
    "summary": {
        "type": "string",
        "description": "Resumo da ligação em até 3 frases",
    },
    "next_action": {
        "type": "string",
        "description": "Próxima ação recomendada para fechar o ciclo",
    },
}


class LLMClient:
    def __init__(
        self,
        model: str | None = None,
        workspace: WorkspaceClient | None = None,
    ):
        self.model = model or os.getenv("LLM_MODEL", "databricks-claude-sonnet-4-6")
        self.w = workspace or WorkspaceClient()

    def analyze(
        self,
        transcript: str,
        presets: list[str],
        custom_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Roda análise sobre a transcrição.

        - presets: lista de chaves em PRESET_FIELDS (ex. ['sentiment', 'intent'])
        - custom_questions: perguntas livres do usuário (cada uma vira um campo
          'q1', 'q2'... com resposta em string).
        """
        custom_questions = custom_questions or []

        # Monta o JSON Schema dinamicamente com base nas seleções
        properties: dict[str, Any] = {}
        required: list[str] = []
        for key in presets:
            if key in PRESET_FIELDS:
                properties[key] = PRESET_FIELDS[key]
                required.append(key)
        for i, q in enumerate(custom_questions, start=1):
            field = f"q{i}"
            properties[field] = {
                "type": "string",
                "description": f"Resposta para a pergunta: {q}",
            }
            required.append(field)

        if not properties:
            return {"error": "Nenhuma análise selecionada."}

        json_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

        # Prompt: deixa explícito o que precisa retornar
        questions_block = ""
        if custom_questions:
            questions_block = "\n\nPERGUNTAS CUSTOMIZADAS (responda cada uma):\n" + "\n".join(
                f"- q{i}: {q}" for i, q in enumerate(custom_questions, start=1)
            )

        system_msg = (
            "Você é um analista de qualidade da Aurélia especializado em speech analytics "
            "de chamadas entre consultoras/clientes e o atendimento. Analise a transcrição "
            "abaixo e devolva EXCLUSIVAMENTE um JSON aderente ao schema solicitado. "
            "Seja factual: só afirme o que está sustentado na transcrição. "
            "Quando não houver evidência, marque como 'não identificado'."
        )

        user_msg = f"TRANSCRIÇÃO:\n\"\"\"\n{transcript}\n\"\"\"{questions_block}"

        # A SDK do Databricks (.serving_endpoints.query) não expõe
        # response_format/json_schema. Vamos direto na REST API com o token
        # OAuth do app (auto-renovado pelo WorkspaceClient).
        host = self.w.config.host.rstrip("/")
        token = self.w.config.authenticate()  # dict com Authorization header
        url = f"{host}/serving-endpoints/{self.model}/invocations"
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": 1500,
            "temperature": 0.1,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "speech_analysis",
                    "schema": json_schema,
                    "strict": True,
                },
            },
        }

        try:
            r = requests.post(url, json=payload, headers=token, timeout=60)
        except Exception:
            logger.exception("Falha ao chamar LLM %s", self.model)
            raise

        if r.status_code >= 400:
            logger.error("LLM HTTP %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()

        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM não retornou choices: {data}")
        content = choices[0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning("LLM retornou JSON inválido, devolvendo raw")
            return {"raw": content}
