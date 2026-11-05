"""Fila de investigação: o analista lê a fila priorizada e registra a decisão.

Endpoints (prefixo /api/fila):
  GET  /api/fila                       - chamados abertos em ordem de score, com a decisão vigente
  GET  /api/fila/resumo                - contagens para o topo da tela
  GET  /api/fila/{chamado_id}          - um chamado, com a transcrição e o histórico de decisões
  POST /api/fila/{chamado_id}/decisao  - grava a decisão do analista

A fila e as decisões moram no Lakebase: leitura sem acordar o warehouse, escrita
transacional. A transcrição completa fica no lakehouse e vem pelo warehouse, só
quando o analista abre o chamado.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from lakebase import conexao
from portal_routes import CATALOG, SCHEMA, _rows_to_dicts, _sql

logger = logging.getLogger("fila")
router = APIRouter(prefix="/api/fila", tags=["fila"])

FILA = "aurelia_speech_online.fila_priorizada"
DECISOES = "investigacao.decisoes"
VIGENTE = "investigacao.decisao_vigente"


class DecisaoIn(BaseModel):
    decisao: Literal["fraude_confirmada", "procedente", "improcedente", "pedir_evidencia"]
    comentario: Optional[str] = Field(default=None, max_length=2000)


def _lista(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _analista(request: Request) -> str:
    # O proxy do Databricks Apps repassa quem está logado.
    return (request.headers.get("x-forwarded-email")
            or request.headers.get("x-forwarded-preferred-username")
            or "analista-desconhecido")


@router.get("")
def listar(faixa: Optional[str] = Query(None, pattern="^(alta|media|baixa)$"),
           pendentes: bool = False,
           limite: int = Query(100, ge=1, le=600)) -> list[dict]:
    filtros, params = [], []
    if faixa:
        filtros.append("f.faixa_risco = %s")
        params.append(faixa)
    if pendentes:
        filtros.append("(d.decisao IS NULL OR d.decisao = 'pedir_evidencia')")
    where = ("WHERE " + " AND ".join(filtros)) if filtros else ""
    with conexao() as conn:
        linhas = conn.execute(f"""
            SELECT f.posicao, f.chamado_id, f.score_fraude, f.faixa_risco, f.modelo_versao,
                   f.data_abertura, f.consultora_codigo, f.pedido_numero, f.motivo_declarado,
                   f.valor_reclamado, f.transportadora, f.uf, f.cd, f.call_id, f.origem_ligacao,
                   f.sinais_relato, d.decisao, d.analista, d.decidido_em
            FROM {FILA} f
            LEFT JOIN {VIGENTE} d USING (chamado_id)
            {where}
            ORDER BY f.posicao
            LIMIT %s""", [*params, limite]).fetchall()
    for l in linhas:
        l["sinais_relato"] = _lista(l["sinais_relato"])
    return linhas


@router.get("/resumo")
def resumo() -> dict:
    with conexao() as conn:
        return conn.execute(f"""
            SELECT count(*)                                           AS abertos,
                   count(*) FILTER (WHERE f.faixa_risco = 'alta')     AS alto_risco,
                   count(d.chamado_id) FILTER (WHERE d.decisao <> 'pedir_evidencia') AS decididos,
                   count(*) FILTER (WHERE d.decisao = 'fraude_confirmada') AS fraudes_confirmadas,
                   coalesce(sum(f.valor_reclamado) FILTER (WHERE f.faixa_risco = 'alta'), 0) AS valor_alto_risco,
                   max(f.modelo_versao)                               AS modelo_versao
            FROM {FILA} f
            LEFT JOIN {VIGENTE} d USING (chamado_id)""").fetchone()


@router.get("/{chamado_id}")
def detalhe(chamado_id: str) -> dict:
    with conexao() as conn:
        chamado = conn.execute(f"SELECT * FROM {FILA} WHERE chamado_id = %s", [chamado_id]).fetchone()
        if not chamado:
            raise HTTPException(404, f"Chamado {chamado_id} não está na fila")
        historico = conn.execute(
            f"SELECT decisao, analista, comentario, score_no_momento, decidido_em "
            f"FROM {DECISOES} WHERE chamado_id = %s ORDER BY decidido_em DESC", [chamado_id]).fetchall()
    for campo in ("sinais_relato", "indicadores_ligacao", "produtos_citados"):
        chamado[campo] = _lista(chamado.get(campo))

    chamado["transcricao"] = None
    chamado["tem_audio"] = False
    if chamado.get("call_id"):
        try:
            r = _rows_to_dicts(_sql(
                f"SELECT full_text, origem FROM {CATALOG}.{SCHEMA}.silver_transcricoes WHERE call_id = :call_id",
                [{"name": "call_id", "value": chamado["call_id"], "type": "STRING"}]))
            if r:
                chamado["transcricao"] = r[0]["full_text"]
                chamado["tem_audio"] = r[0]["origem"] == "audio"
        except HTTPException:
            logger.warning("Transcrição de %s indisponível", chamado["call_id"])
    chamado["historico"] = historico
    return chamado


@router.post("/{chamado_id}/decisao")
def decidir(chamado_id: str, corpo: DecisaoIn, request: Request) -> dict:
    with conexao() as conn:
        chamado = conn.execute(
            f"SELECT score_fraude, modelo_versao, call_id FROM {FILA} WHERE chamado_id = %s",
            [chamado_id]).fetchone()
        if not chamado:
            raise HTTPException(404, f"Chamado {chamado_id} não está na fila")
        nova = conn.execute(
            f"""INSERT INTO {DECISOES}
                  (chamado_id, decisao, analista, comentario, score_no_momento, modelo_versao, tinha_ligacao, origem)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'app')
                RETURNING decisao_id, decidido_em""",
            [chamado_id, corpo.decisao, _analista(request), corpo.comentario,
             chamado["score_fraude"], chamado["modelo_versao"], chamado["call_id"] is not None]).fetchone()
        conn.commit()
    logger.info("Decisão %s em %s por %s", corpo.decisao, chamado_id, _analista(request))
    return {"chamado_id": chamado_id, "decisao": corpo.decisao, **nova}
