"""Conexão do app com o Lakebase (Postgres) do build.

O app se autentica como o próprio service principal: o usuário do Postgres é o
`DATABRICKS_CLIENT_ID` que o Databricks Apps injeta, e a senha é um token OAuth
de uma hora gerado pela API do Lakebase. Sem PGUSER fixo no app.yaml, o mesmo
arquivo serve para qualquer app com qualquer service principal.

O role do service principal precisa existir no Lakebase, criado pela API
(`databricks postgres create-role`) — ver lakebase/README.md.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from databricks.sdk import WorkspaceClient

ENDPOINT = os.getenv("LAKEBASE_ENDPOINT", "projects/aurelia-speech-ops/branches/production/endpoints/primary")
DATABASE = os.getenv("PGDATABASE", "databricks_postgres")

_w: WorkspaceClient | None = None
_cache: dict = {"host": None, "token": None, "expira": 0.0}
_trava = threading.Lock()


def _cliente() -> WorkspaceClient:
    global _w
    if _w is None:
        _w = WorkspaceClient()
    return _w


def _credenciais() -> tuple[str, str]:
    """Host do endpoint e um token válido por pelo menos mais 5 minutos."""
    with _trava:
        if _cache["host"] is None:
            _cache["host"] = _cliente().postgres.get_endpoint(name=ENDPOINT).status.hosts.host
        if time.time() > _cache["expira"] - 300:
            cred = _cliente().postgres.generate_database_credential(endpoint=ENDPOINT)
            _cache["token"] = cred.token
            _cache["expira"] = time.time() + 3600
        return _cache["host"], _cache["token"]


def _usuario() -> str:
    return os.getenv("PGUSER") or os.getenv("DATABRICKS_CLIENT_ID") or _cliente().current_user.me().user_name


@contextmanager
def conexao():
    host, token = _credenciais()
    with psycopg.connect(host=host, dbname=DATABASE, user=_usuario(), password=token,
                         sslmode="require", row_factory=dict_row, connect_timeout=15) as conn:
        yield conn
