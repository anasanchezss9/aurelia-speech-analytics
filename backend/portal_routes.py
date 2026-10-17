"""Rotas do portal de análise de áudios da Aurélia.

Endpoints (prefixo /api/portal):
  POST /api/portal/upload         - upload de áudio (.wav/.mp3/.m4a) para o volume
  POST /api/portal/process        - dispara o pipeline imperativo em background
  GET  /api/portal/processing     - status da última execução do pipeline
  GET  /api/portal/calls          - lista chamadas com insights (filtros via query)
  GET  /api/portal/calls/{id}     - detalhe de uma chamada
  GET  /api/portal/audio/{id}     - stream do .wav/.mp3 do volume
  GET  /api/portal/dashboard      - KPIs agregados pra o painel central

Premissas:
- O app roda como Databricks App, com OAuth automático via WorkspaceClient.
- Tabelas e volume vivem em ${CATALOG}.${SCHEMA}.* (config via env).
- O pipeline imperativo é disparado via Jobs API (one-time submit run).
- O volume é acessado pela Files API, não pelo filesystem: o container do app
  não monta /Volumes, então open()/os.path.exists() ali sempre falham.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs as jobs_svc
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("portal")

router = APIRouter(prefix="/api/portal", tags=["portal"])

# ---------- Config ----------

CATALOG = os.getenv("PORTAL_CATALOG", "field_toolbox_catalog")
SCHEMA = os.getenv("PORTAL_SCHEMA", "aurelia_speech")
VOLUME = os.getenv("PORTAL_VOLUME", "audio_landing")
TABLE_BRONZE = os.getenv("PORTAL_TABLE_BRONZE", "bronze_audio_files")
TABLE_SILVER = os.getenv("PORTAL_TABLE_SILVER", "silver_transcripts")
TABLE_GOLD = os.getenv("PORTAL_TABLE_GOLD", "gold_call_insights")
WAREHOUSE_ID = os.getenv("PORTAL_WAREHOUSE_ID", "")  # SQL Warehouse pro app ler
PIPELINE_NOTEBOOK_PREFIX = os.getenv(
    "PORTAL_NOTEBOOK_PREFIX",
    "/Workspace/Users/ana.sanchez@databricks.com/aurelia_speech_pipelines/imperative",
)

VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
UPLOADS_SUBDIR = "uploads"
RECORDINGS_SUBDIR = "recordings"

ALLOWED_EXT = {".wav", ".mp3", ".m4a"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB / áudio

_w: WorkspaceClient | None = None


def workspace() -> WorkspaceClient:
    """Lazy singleton — a auth do App é injetada em runtime."""
    global _w
    if _w is None:
        _w = WorkspaceClient()
    return _w


# ---------- State (em memória, suficiente pra demo single-instance) ----------

_processing_state: dict[str, Any] = {
    "status": "idle",  # idle | running | success | failed
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "message": "",
    "audios_pending_at_start": 0,
}
_processing_lock = threading.Lock()


# ---------- Helpers ----------


def _safe_filename(name: str) -> str:
    """Remove path traversal e caracteres perigosos."""
    base = Path(name).name  # remove diretórios
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "audio"


def _volume_tamanho(caminho: str) -> int:
    """Tamanho em bytes de um arquivo do volume, via Files API."""
    md = workspace().files.get_metadata(caminho)
    return int(md.content_length or 0)


def _volume_leitura(caminho: str, inicio: int | None = None, fim: int | None = None):
    """Abre um arquivo do volume para leitura, opcionalmente só uma faixa.

    O container do Databricks App não tem o volume montado como filesystem — um
    `open("/Volumes/...")` aqui dá FileNotFoundError, mesmo com as permissões
    certas no Unity Catalog. Todo acesso a áudio passa pela Files API.

    Usamos `api_client.do` em vez de `files.download` porque só assim dá para
    repassar o header Range: sem ele o `<audio>` do browser não consegue dar
    seek e trava em arquivos grandes.
    """
    cabecalhos = {}
    if inicio is not None:
        cabecalhos["Range"] = f"bytes={inicio}-{fim}"
    resposta = workspace().api_client.do(
        "GET",
        f"/api/2.0/fs/files{caminho}",
        headers=cabecalhos,
        raw=True,
    )
    return resposta["contents"]


def _sql(stmt: str, parameters: Optional[list[dict]] = None) -> dict:
    """Executa SQL via Statement Execution API."""
    if not WAREHOUSE_ID:
        raise HTTPException(
            status_code=500,
            detail="PORTAL_WAREHOUSE_ID não configurado no app.yaml",
        )
    w = workspace()
    payload: dict[str, Any] = {
        "warehouse_id": WAREHOUSE_ID,
        "statement": stmt,
        "wait_timeout": "30s",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    }
    if parameters:
        payload["parameters"] = parameters

    res = w.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body=payload,
    )
    state = res.get("status", {}).get("state")
    if state != "SUCCEEDED":
        err = res.get("status", {}).get("error", {}).get("message", "")
        logger.error("SQL failed (%s): %s\nstmt=%s", state, err, stmt[:300])
        raise HTTPException(status_code=502, detail=f"SQL {state}: {err}")
    return res


def _rows_to_dicts(res: dict) -> list[dict]:
    """Converte um resultado da Statement API em list[dict]."""
    cols = [c["name"] for c in res.get("manifest", {}).get("schema", {}).get("columns", [])]
    data = res.get("result", {}).get("data_array") or []
    out = []
    for row in data:
        d = dict(zip(cols, row))
        out.append(d)
    return out


# ---------- Models ----------


class UploadOut(BaseModel):
    filename: str
    size_bytes: int
    saved_to: str
    call_id: str


class ProcessOut(BaseModel):
    status: str
    run_id: Optional[int] = None
    message: str = ""


class ProcessingStatusOut(BaseModel):
    status: str
    run_id: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    message: str = ""
    audios_pending_at_start: int = 0


class CallSummary(BaseModel):
    call_id: str
    audio_path: str
    audio_format: str
    source: str
    ingested_at: str
    analyzed_at: Optional[str] = None
    transcript_preview: Optional[str] = None
    sentiment: Optional[str] = None
    tipo_problema: Optional[str] = None
    suspeita_fraude: Optional[bool] = None
    reincidencia: Optional[bool] = None
    consultora_codigo: Optional[str] = None
    pedido_numero: Optional[str] = None
    churn_risk: Optional[float] = None


class CallDetail(CallSummary):
    full_text: Optional[str] = None
    summary: Optional[str] = None
    next_action: Optional[str] = None
    emotion: Optional[str] = None
    intent: Optional[str] = None
    topics: list[str] = []
    # Autodeclarado pelo modelo, não calibrado — ver rótulo na tela de detalhe.
    fraude_confianca: Optional[float] = None
    fraude_indicadores: list[str] = []
    produtos_faltantes: list[str] = []
    compliance_ok: Optional[bool] = None
    compliance_reason: Optional[str] = None


class DashboardKPIs(BaseModel):
    total_calls: int
    analyzed_calls: int
    pending_calls: int
    suspeita_fraude_count: int
    reincidencia_count: int
    avg_churn_risk: float


class DashboardOut(BaseModel):
    kpis: DashboardKPIs
    by_tipo_problema: list[dict[str, Any]]
    by_sentiment: list[dict[str, Any]]
    top_consultoras: list[dict[str, Any]]
    top_produtos_faltantes: list[dict[str, Any]]
    last_calls: list[CallSummary]


# ---------- Endpoints ----------


@router.post("/upload", response_model=UploadOut)
async def upload_audio(file: UploadFile) -> UploadOut:
    """Recebe um áudio do portal e salva em /Volumes/.../uploads/<filename>."""
    name = _safe_filename(file.filename or "audio")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Formato {ext!r} não suportado. Aceito: {sorted(ALLOWED_EXT)}",
        )

    target_path = f"{VOLUME_PATH}/{UPLOADS_SUBDIR}/{name}"

    # O arquivo passa por um temporário local antes de subir pela Files API: o
    # volume não é filesystem dentro do app, e assim o limite de tamanho é
    # aplicado durante a leitura, sem carregar o áudio inteiro em memória.
    total = 0
    try:
        with tempfile.NamedTemporaryFile(suffix=ext) as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Arquivo > {MAX_UPLOAD_BYTES // (1024*1024)}MB",
                    )
                buffer.write(chunk)
            buffer.flush()
            buffer.seek(0)
            workspace().files.upload(target_path, buffer, overwrite=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Falha no upload de %s", name)
        raise HTTPException(status_code=500, detail=f"Falha no upload: {e}")

    call_id = Path(name).stem
    logger.info("Upload OK: %s (%d bytes) -> %s", name, total, target_path)

    return UploadOut(
        filename=name,
        size_bytes=total,
        saved_to=target_path,
        call_id=call_id,
    )


def _count_pending_audios() -> int:
    """Conta quantos arquivos no volume ainda não estão na bronze."""
    try:
        res = _sql(
            f"""
            WITH files AS (
              SELECT path FROM read_files('{VOLUME_PATH}/', format=>'binaryFile')
              WHERE lower(path) RLIKE '\\\\.(mp3|wav|m4a)$'
            ),
            ingested AS (
              SELECT regexp_replace(audio_path, '^dbfs:', '') AS path
              FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE}
            )
            SELECT count(*) AS n
            FROM files f
            LEFT ANTI JOIN ingested i
              ON regexp_replace(f.path, '^dbfs:', '') = i.path
            """
        )
        rows = _rows_to_dicts(res)
        return int(rows[0]["n"]) if rows else 0
    except Exception as e:
        logger.warning("Não foi possível contar pendentes: %s", e)
        return 0


def _run_pipeline_async(run_id_holder: dict[str, Any]) -> None:
    """Roda o pipeline imperativo em background via Jobs API."""
    w = workspace()
    try:
        # Submit one-time run com 3 tasks encadeadas (serverless)
        run = w.jobs.submit(
            run_name=f"aurelia_portal_pipeline_{int(time.time())}",
            tasks=[
                jobs_svc.SubmitTask(
                    task_key="bronze",
                    notebook_task=jobs_svc.NotebookTask(
                        notebook_path=f"{PIPELINE_NOTEBOOK_PREFIX}/01_bronze_audio_ingestion",
                        source=jobs_svc.Source.WORKSPACE,
                    ),
                ),
                jobs_svc.SubmitTask(
                    task_key="silver",
                    depends_on=[jobs_svc.TaskDependency(task_key="bronze")],
                    notebook_task=jobs_svc.NotebookTask(
                        notebook_path=f"{PIPELINE_NOTEBOOK_PREFIX}/02_silver_transcription",
                        source=jobs_svc.Source.WORKSPACE,
                    ),
                ),
                jobs_svc.SubmitTask(
                    task_key="gold",
                    depends_on=[jobs_svc.TaskDependency(task_key="silver")],
                    notebook_task=jobs_svc.NotebookTask(
                        notebook_path=f"{PIPELINE_NOTEBOOK_PREFIX}/03_gold_ai_query_analysis",
                        source=jobs_svc.Source.WORKSPACE,
                    ),
                ),
            ],
        )
        run_id_holder["run_id"] = run.run_id
        with _processing_lock:
            _processing_state["run_id"] = run.run_id

        # Aguarda terminar
        run.result()  # block until terminal state

        final = w.jobs.get_run(run.run_id)
        result_state = (final.state.result_state.value if final.state and final.state.result_state else "UNKNOWN")
        with _processing_lock:
            _processing_state["status"] = "success" if result_state == "SUCCESS" else "failed"
            _processing_state["finished_at"] = datetime.utcnow().isoformat()
            _processing_state["message"] = f"Pipeline {result_state}"
        logger.info("Pipeline terminou: %s", result_state)
    except Exception as e:
        logger.exception("Falha rodando pipeline")
        with _processing_lock:
            _processing_state["status"] = "failed"
            _processing_state["finished_at"] = datetime.utcnow().isoformat()
            _processing_state["message"] = str(e)


@router.post("/process", response_model=ProcessOut)
def trigger_processing() -> ProcessOut:
    """Dispara o pipeline (assíncrono). Apenas 1 execução por vez."""
    with _processing_lock:
        if _processing_state["status"] == "running":
            return ProcessOut(
                status="already_running",
                run_id=_processing_state.get("run_id"),
                message="Já existe um processamento em andamento.",
            )
        _processing_state.update(
            status="running",
            run_id=None,
            started_at=datetime.utcnow().isoformat(),
            finished_at=None,
            message="Disparando pipeline…",
            audios_pending_at_start=_count_pending_audios(),
        )

    holder: dict[str, Any] = {}
    t = threading.Thread(target=_run_pipeline_async, args=(holder,), daemon=True)
    t.start()
    return ProcessOut(status="started", message="Pipeline disparado em background.")


@router.get("/processing", response_model=ProcessingStatusOut)
def processing_status() -> ProcessingStatusOut:
    with _processing_lock:
        return ProcessingStatusOut(**_processing_state)


@router.get("/calls", response_model=list[CallSummary])
def list_calls(
    limit: int = Query(100, ge=1, le=500),
    tipo_problema: Optional[str] = None,
    suspeita_fraude: Optional[bool] = None,
    source: Optional[str] = None,
    only_analyzed: bool = False,
) -> list[CallSummary]:
    """Lista chamadas com seus insights. Bronze LEFT JOIN gold pra mostrar
    também os áudios ainda em processamento."""
    where = []
    if tipo_problema:
        where.append(f"g.tipo_problema = '{tipo_problema}'")
    if suspeita_fraude is not None:
        where.append(f"g.suspeita_fraude = {str(suspeita_fraude).lower()}")
    if source:
        where.append(f"b.source = '{source}'")
    if only_analyzed:
        where.append("g.call_id IS NOT NULL")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    res = _sql(
        f"""
        SELECT
          b.call_id,
          b.audio_path,
          b.audio_format,
          b.source,
          CAST(b.ingested_at AS STRING) AS ingested_at,
          CAST(g.analyzed_at AS STRING) AS analyzed_at,
          substr(s.full_text, 1, 200) AS transcript_preview,
          g.sentiment,
          g.tipo_problema,
          g.suspeita_fraude,
          g.reincidencia,
          g.consultora_codigo,
          g.pedido_numero,
          g.churn_risk
        FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE} b
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_SILVER} s ON s.call_id = b.call_id
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_GOLD}   g ON g.call_id = b.call_id
        {where_sql}
        ORDER BY COALESCE(g.analyzed_at, b.ingested_at) DESC
        LIMIT {limit}
        """
    )
    return [CallSummary(**r) for r in _rows_to_dicts(res)]


def _parse_array(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        # Pode vir como JSON-array string OU CSV
        try:
            j = json.loads(v)
            if isinstance(j, list):
                return [str(x) for x in j]
        except Exception:
            pass
        if v.startswith("[") and v.endswith("]"):
            return [v]
        return [v] if v else []
    return [str(v)]


@router.get("/calls/{call_id}", response_model=CallDetail)
def get_call(call_id: str) -> CallDetail:
    """Detalhe de uma chamada — usado pelo drill-down."""
    res = _sql(
        f"""
        SELECT
          b.call_id,
          b.audio_path,
          b.audio_format,
          b.source,
          CAST(b.ingested_at AS STRING) AS ingested_at,
          CAST(g.analyzed_at AS STRING) AS analyzed_at,
          s.full_text,
          substr(s.full_text, 1, 200) AS transcript_preview,
          g.sentiment, g.emotion, g.intent,
          g.summary, g.next_action,
          g.tipo_problema, g.suspeita_fraude, g.fraude_confianca, g.reincidencia,
          g.consultora_codigo, g.pedido_numero, g.churn_risk,
          g.compliance_ok, g.compliance_reason,
          to_json(g.topics)             AS topics_json,
          to_json(g.fraude_indicadores) AS fraude_indicadores_json,
          to_json(g.produtos_faltantes) AS produtos_faltantes_json
        FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE} b
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_SILVER} s ON s.call_id = b.call_id
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_GOLD}   g ON g.call_id = b.call_id
        WHERE b.call_id = '{call_id}'
        LIMIT 1
        """
    )
    rows = _rows_to_dicts(res)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Call {call_id} não encontrada")
    r = rows[0]
    return CallDetail(
        call_id=r["call_id"],
        audio_path=r["audio_path"],
        audio_format=r.get("audio_format") or "",
        source=r.get("source") or "landing",
        ingested_at=r.get("ingested_at") or "",
        analyzed_at=r.get("analyzed_at"),
        transcript_preview=r.get("transcript_preview"),
        full_text=r.get("full_text"),
        sentiment=r.get("sentiment"),
        emotion=r.get("emotion"),
        intent=r.get("intent"),
        summary=r.get("summary"),
        next_action=r.get("next_action"),
        tipo_problema=r.get("tipo_problema"),
        suspeita_fraude=r.get("suspeita_fraude"),
        fraude_confianca=r.get("fraude_confianca"),
        reincidencia=r.get("reincidencia"),
        consultora_codigo=r.get("consultora_codigo"),
        pedido_numero=r.get("pedido_numero"),
        churn_risk=r.get("churn_risk"),
        compliance_ok=r.get("compliance_ok"),
        compliance_reason=r.get("compliance_reason"),
        topics=_parse_array(r.get("topics_json")),
        fraude_indicadores=_parse_array(r.get("fraude_indicadores_json")),
        produtos_faltantes=_parse_array(r.get("produtos_faltantes_json")),
    )


@router.get("/audio/{call_id}")
def stream_audio(call_id: str, request: Request):
    """Stream do arquivo de áudio diretamente do volume.

    Implementa HTTP Range Requests (RFC 7233) — sem isso o `<audio>` do
    browser não consegue fazer seek nem tocar arquivos grandes (>20 MB)
    porque ele não dá play até ter o arquivo inteiro em memória.
    """
    # Acha o path real via bronze (evita path traversal)
    res = _sql(
        f"""
        SELECT regexp_replace(audio_path, '^dbfs:', '') AS path,
               audio_format
        FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE}
        WHERE call_id = '{call_id}'
        LIMIT 1
        """
    )
    rows = _rows_to_dicts(res)
    if not rows:
        raise HTTPException(status_code=404, detail="Áudio não encontrado")
    path = rows[0]["path"]
    fmt = rows[0].get("audio_format") or "wav"
    media_type = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
    }.get(fmt, "application/octet-stream")

    if not path:
        raise HTTPException(status_code=404, detail="Áudio sem caminho na bronze")
    try:
        file_size = _volume_tamanho(path)
    except Exception as e:
        logger.exception("Áudio %s não acessível no volume", path)
        raise HTTPException(status_code=404, detail=f"Arquivo {path} não está no volume: {e}")

    range_header = request.headers.get("range") or request.headers.get("Range")

    # Caso 1: cliente NÃO pediu range -> mandamos o arquivo inteiro, mas
    # ainda assim sinalizamos que aceitamos ranges (alguns browsers
    # avaliam o header antes de tocar e só dão play se ver Accept-Ranges).
    if not range_header:
        def iter_full():
            fonte = _volume_leitura(path)
            try:
                while True:
                    chunk = fonte.read(1024 * 64)
                    if not chunk:
                        break
                    yield chunk
            finally:
                fonte.close()

        return StreamingResponse(
            iter_full(),
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
            },
        )

    # Caso 2: cliente pediu range "bytes=start-end"
    try:
        units, _, rng = range_header.partition("=")
        if units.strip().lower() != "bytes":
            raise ValueError("apenas bytes")
        start_s, _, end_s = rng.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        if start < 0 or start >= file_size:
            raise ValueError("start fora do arquivo")
        end = min(end, file_size - 1)
    except Exception:
        # Range header malformado -> 416 Range Not Satisfiable
        raise HTTPException(
            status_code=416,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    length = end - start + 1

    def iter_range():
        fonte = _volume_leitura(path, start, end)
        try:
            remaining = length
            while remaining > 0:
                chunk = fonte.read(min(1024 * 64, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            fonte.close()

    return StreamingResponse(
        iter_range(),
        status_code=206,  # Partial Content
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Cache-Control": "no-cache",
        },
    )


@router.get("/dashboard", response_model=DashboardOut)
def dashboard() -> DashboardOut:
    """KPIs + agregações para o painel central."""
    # KPIs principais
    res_kpi = _sql(
        f"""
        SELECT
          (SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE})                       AS total_calls,
          (SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD})                         AS analyzed_calls,
          (SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD} WHERE suspeita_fraude)   AS suspeita_fraude_count,
          (SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD} WHERE reincidencia)      AS reincidencia_count,
          (SELECT COALESCE(avg(churn_risk),0) FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD})      AS avg_churn_risk
        """
    )
    k = _rows_to_dicts(res_kpi)[0]
    kpis = DashboardKPIs(
        total_calls=int(k["total_calls"]),
        analyzed_calls=int(k["analyzed_calls"]),
        pending_calls=max(int(k["total_calls"]) - int(k["analyzed_calls"]), 0),
        suspeita_fraude_count=int(k["suspeita_fraude_count"]),
        reincidencia_count=int(k["reincidencia_count"]),
        avg_churn_risk=float(k["avg_churn_risk"] or 0.0),
    )

    res_tipo = _sql(
        f"""
        SELECT COALESCE(tipo_problema, 'sem_classificacao') AS tipo, count(*) AS n
        FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD}
        GROUP BY 1 ORDER BY n DESC
        """
    )
    by_tipo = [{"tipo": r["tipo"], "n": int(r["n"])} for r in _rows_to_dicts(res_tipo)]

    res_sent = _sql(
        f"""
        SELECT COALESCE(sentiment, 'sem_classificacao') AS sentiment, count(*) AS n
        FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD}
        GROUP BY 1 ORDER BY n DESC
        """
    )
    by_sent = [{"sentiment": r["sentiment"], "n": int(r["n"])} for r in _rows_to_dicts(res_sent)]

    res_cons = _sql(
        f"""
        SELECT consultora_codigo,
               count(*) AS chamadas,
               sum(CASE WHEN suspeita_fraude THEN 1 ELSE 0 END) AS fraudes,
               sum(CASE WHEN reincidencia    THEN 1 ELSE 0 END) AS reincidencias
        FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD}
        WHERE consultora_codigo IS NOT NULL AND consultora_codigo != ''
        GROUP BY consultora_codigo
        ORDER BY chamadas DESC
        LIMIT 10
        """
    )
    top_cons = _rows_to_dicts(res_cons)

    # Top produtos faltantes — explode o array
    res_prod = _sql(
        f"""
        SELECT produto, count(*) AS n
        FROM (
          SELECT explode(produtos_faltantes) AS produto
          FROM {CATALOG}.{SCHEMA}.{TABLE_GOLD}
          WHERE produtos_faltantes IS NOT NULL AND size(produtos_faltantes) > 0
        )
        GROUP BY produto
        ORDER BY n DESC
        LIMIT 10
        """
    )
    top_prod = _rows_to_dicts(res_prod)

    res_last = _sql(
        f"""
        SELECT
          b.call_id, b.audio_path, b.audio_format, b.source,
          CAST(b.ingested_at AS STRING)  AS ingested_at,
          CAST(g.analyzed_at AS STRING)  AS analyzed_at,
          substr(s.full_text, 1, 200)    AS transcript_preview,
          g.sentiment, g.tipo_problema, g.suspeita_fraude, g.reincidencia,
          g.consultora_codigo, g.pedido_numero, g.churn_risk
        FROM {CATALOG}.{SCHEMA}.{TABLE_BRONZE} b
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_SILVER} s ON s.call_id = b.call_id
        LEFT JOIN {CATALOG}.{SCHEMA}.{TABLE_GOLD}   g ON g.call_id = b.call_id
        ORDER BY COALESCE(g.analyzed_at, b.ingested_at) DESC
        LIMIT 8
        """
    )
    last_calls = [CallSummary(**r) for r in _rows_to_dicts(res_last)]

    return DashboardOut(
        kpis=kpis,
        by_tipo_problema=by_tipo,
        by_sentiment=by_sent,
        top_consultoras=top_cons,
        top_produtos_faltantes=top_prod,
        last_calls=last_calls,
    )
