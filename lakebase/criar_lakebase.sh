#!/usr/bin/env bash
# Monta o lado operacional do build no Lakebase. Idempotente.
#
#   bash lakebase/criar_lakebase.sh [perfil]
#
# 1. Projeto Lakebase (Postgres 17, autoscaling, scale-to-zero).
# 2. Schema `investigacao`: as decisões do analista, só inserção.
# 3. Synced table: a fila priorizada do UC → Postgres, para o app ler sem
#    acordar o warehouse. SNAPSHOT, porque a fila é reescrita inteira a cada
#    pontuação; o job diário dispara o refresh depois de pontuar.
# 4. CDF config: o schema `investigacao` → Delta no UC, contínuo. É o caminho
#    de volta: a decisão do analista vira rótulo no lakehouse sem pipeline à mão.
#
# Requer Databricks CLI com o grupo `postgres`, psql e um role seu no Lakebase.
# O token vai do CLI direto para o psql por PGPASSWORD; nada toca o disco.

set -euo pipefail

PERFIL="${1:-field-toolbox}"
CATALOG="field_toolbox_catalog"
PROJETO="aurelia-speech-ops"
BRANCH="projects/${PROJETO}/branches/production"
ENDPOINT="${BRANCH}/endpoints/primary"
DB="databricks_postgres"
# O mesmo banco tem dois nomes: `databricks_postgres` dentro do Postgres e
# `databricks-postgres` como recurso da API (o CDF pede o segundo).
DB_RECURSO="databricks-postgres"
AQUI="$(cd "$(dirname "$0")" && pwd)"

databricks postgres get-project "projects/${PROJETO}" -p "$PERFIL" >/dev/null 2>&1 || \
  databricks postgres create-project "$PROJETO" -p "$PERFIL" \
    --json '{"spec":{"display_name":"Aurélia Speech — investigação de chamados","pg_version":"17"}}'

HOST=$(databricks postgres get-endpoint "$ENDPOINT" -p "$PERFIL" -o json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"]["hosts"]["host"])')
USUARIO=$(databricks current-user me -p "$PERFIL" -o json | python3 -c 'import sys,json;print(json.load(sys.stdin)["userName"])')
export PGPASSWORD=$(databricks postgres generate-database-credential "$ENDPOINT" -p "$PERFIL" -o json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

psql "host=${HOST} dbname=${DB} user=${USUARIO} sslmode=require" -v ON_ERROR_STOP=1 -f "${AQUI}/schema.sql"

# UC: schema das views online da synced table e schema de destino do CDF.
databricks schemas create aurelia_speech_online "$CATALOG" -p "$PERFIL" >/dev/null 2>&1 || true
databricks schemas create aurelia_speech_investigacao "$CATALOG" -p "$PERFIL" >/dev/null 2>&1 || true

SYNCED="${CATALOG}.aurelia_speech_online.fila_priorizada"
databricks api get "/api/2.0/postgres/synced_tables/${SYNCED}" -p "$PERFIL" >/dev/null 2>&1 || \
databricks api post "/api/2.0/postgres/synced_tables?synced_table_id=${SYNCED}" -p "$PERFIL" --json "$(cat <<JSON
{
  "name": "${SYNCED}",
  "spec": {
    "postgres_database": "${DB}",
    "branch": "${BRANCH}",
    "source_table_full_name": "${CATALOG}.aurelia_speech.fila_priorizada",
    "primary_key_columns": ["chamado_id"],
    "scheduling_policy": "SNAPSHOT",
    "create_database_objects_if_missing": true,
    "new_pipeline_spec": {"storage_catalog": "${CATALOG}", "storage_schema": "aurelia_speech_online"}
  }
}
JSON
)" >/dev/null

databricks postgres list-cdf-configs "${BRANCH}/databases/${DB_RECURSO}" -p "$PERFIL" -o json 2>/dev/null \
  | grep -q '"postgres_schema": *"investigacao"' || \
  databricks postgres create-cdf-config "${BRANCH}/databases/${DB_RECURSO}" "$CATALOG" aurelia_speech_investigacao investigacao \
    --cdf-config-id investigacao -p "$PERFIL"

echo "Lakebase pronto: ${HOST}"
echo "  fila (leitura):   aurelia_speech_online.fila_priorizada no Postgres"
echo "  decisões (escrita): investigacao.decisoes → ${CATALOG}.aurelia_speech_investigacao"
