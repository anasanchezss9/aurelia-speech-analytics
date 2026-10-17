# Databricks notebook source
# MAGIC %md
# MAGIC # Setup — Speech Analytics PPM Falta (Aurélia)
# MAGIC
# MAGIC Este notebook prepara o ambiente para os 2 pipelines (imperativo + SDP).
# MAGIC Roda uma vez só.
# MAGIC
# MAGIC **O que faz:**
# MAGIC 1. Lê configs do `.env`
# MAGIC 2. Cria o volume `audio_landing` no Unity Catalog
# MAGIC 3. Gera 3 áudios sintéticos em PT-BR via Google Text-to-Speech (gTTS):
# MAGIC    - **call_001_atraso.mp3** — consultora reclama de atraso, comum
# MAGIC    - **call_002_suspeita_fraude.mp3** — caixa chegou aberta sem produto
# MAGIC    - **call_003_falta_parcial.mp3** — pedido chegou com itens faltando
# MAGIC 4. Faz upload dos `.mp3` para o volume
# MAGIC
# MAGIC > **Por que gTTS?** Não precisa de API key, gera MP3 PT-BR realista, e
# MAGIC > MP3 é decodificável pelo Whisper (libsndfile + audioread).

# COMMAND ----------

# MAGIC %pip install -q gTTS==2.5.4 python-dotenv==1.0.1
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Carrega configurações do .env (com fallback)
import os
from pathlib import Path
from dotenv import load_dotenv

# Procura .env no mesmo diretório do notebook
here = Path.cwd()
candidates = [
    here / ".env",
    here.parent / ".env",
    Path("/Workspace/Users/ana.sanchez@databricks.com/aurelia_speech_pipelines/.env"),
]
for candidate in candidates:
    if candidate.exists():
        load_dotenv(candidate)
        print(f"Carregou config de: {candidate}")
        break
else:
    print("Nenhum .env encontrado; usando defaults.")

CATALOG = os.getenv("CATALOG", "field_toolbox_catalog")
SCHEMA = os.getenv("SCHEMA", "aurelia_speech")
VOLUME = os.getenv("VOLUME_LANDING", "audio_landing")

VOLUME_PATH = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
print(f"Catalog: {CATALOG}")
print(f"Schema:  {SCHEMA}")
print(f"Volume:  {VOLUME_PATH}")

# COMMAND ----------

# DBTITLE 1,Cria schema e volume se não existirem
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")
print(f"OK — volume disponível em {VOLUME_PATH}")

# COMMAND ----------

# DBTITLE 1,Roteiros realistas (3 cenários)
# Cada chamada é entre uma CONSULTORA (cliente Aurélia) e um AGENTE de SAC.
# Os roteiros foram escritos para parecerem ligações reais, com falas
# naturais, pausas e termos do dia-a-dia.

CALLS = [
    {
        "filename": "call_001_atraso.mp3",
        "scenario": "Reclamação de atraso simples",
        "script": (
            "Olá, tudo bem? Aqui é a Maria Aparecida, sou consultora Aurélia "
            "lá de Belo Horizonte, código de consultora oito quatro sete dois "
            "três. Estou ligando porque meu pedido número nove zero dois um "
            "está atrasado já tem quase dez dias e minhas clientes estão "
            "me cobrando. "
            "Tudo bem, dona Maria. Lamento o ocorrido. Deixa eu verificar "
            "aqui no sistema. Aproveitando, a senhora não gostaria de "
            "conhecer a nossa promoção da linha Raízes? "
            "Não, eu realmente preciso resolver esse atraso. Eu não posso "
            "ficar com as clientes esperando, isso prejudica meu trabalho. "
            "Por favor, me diga só quando vai chegar. "
            "Entendo. Vou abrir um protocolo de urgência aqui pra senhora. "
            "Em até quarenta e oito horas a transportadora retorna com um "
            "prazo concreto. Posso confirmar o seu telefone para contato? "
            "Pode confirmar sim, é o mesmo cadastrado. Obrigada."
        ),
    },
    {
        "filename": "call_002_suspeita_fraude.mp3",
        "scenario": "Caixa chegou aberta com produtos faltando — possível roubo no transporte",
        "script": (
            "Alô, boa tarde. Aqui é o José Carlos, consultor Aurélia, "
            "código sete dois nove cinco um. Estou bem chateado, viu. "
            "Recebi minha caixa hoje pela manhã e ela veio com a fita "
            "violada, parecia que tinha sido aberta. Quando eu fui "
            "conferir, faltavam dois perfumes Âmbar Exclusivo "
            "masculino e três cremes Diário. Justo os produtos mais "
            "caros do pedido, isso aqui foi roubo na certeza. "
            "Lamento muito o ocorrido, senhor José. O senhor consegue "
            "tirar fotos da caixa e da nota fiscal? "
            "Já tirei, mandei pelo aplicativo. Mas olha, isso não é a "
            "primeira vez que acontece comigo, no mês passado também "
            "veio faltando uma coisinha. Eu acho que tem alguém na "
            "transportadora mexendo nas caixas. Eu vou ter que devolver "
            "o dinheiro pras minhas clientes do meu bolso, isso é um "
            "prejuízo enorme. "
            "Vou registrar como ocorrência de violação de embalagem e "
            "encaminhar pra área de fraude e segurança. O senhor recebe "
            "um retorno em até cinco dias úteis, com o ressarcimento ou "
            "o reenvio dos produtos. "
            "Cinco dias úteis é muito tempo, viu. Mas tudo bem, vou "
            "esperar. Mas por favor, vê com a transportadora, isso "
            "está demais."
        ),
    },
    {
        "filename": "call_003_falta_parcial.mp3",
        "scenario": "Pedido com falta de itens — possível erro de separação",
        "script": (
            "Oi, boa tarde. Sou a Cláudia, consultora aqui de Recife, "
            "meu código é um cinco quatro sete dois oito. Tô ligando "
            "porque meu pedido chegou hoje, mas faltou bastante coisa. "
            "Veio um batom Aura a menos, faltou um sabonete líquido "
            "Erva Doce, e veio só um esmalte quando eu pedi quatro. "
            "Entendi, dona Cláudia. A caixa estava lacrada quando "
            "chegou? "
            "Estava sim, a fita tava normal, parece que eles esqueceram "
            "de colocar mesmo lá no centro de distribuição. Eu confiro "
            "tudinho com a nota e tá faltando exatamente esses itens. "
            "Provavelmente foi erro de separação na expedição. Vou "
            "abrir aqui um chamado de complemento de pedido. Os produtos "
            "vão ser enviados sem custo no próximo ciclo. "
            "Tudo bem. Mas pode olhar isso direitinho lá, porque a "
            "última vez também veio faltando. Tá ficando frequente "
            "esses errinhos."
        ),
    },
]

print(f"Vão ser gerados {len(CALLS)} áudios sintéticos.")

# COMMAND ----------

# DBTITLE 1,Gera os MP3s e faz upload no volume
# Em serverless compute (Shared UC mode) NÃO podemos usar `dbutils.fs.cp`
# para copiar de `file:/tmp/...`. A solução é escrever os MP3s direto no
# Volume via filesystem do Python — paths /Volumes/... são tratados como
# filesystem local pelo runtime e funcionam em serverless.

import os
import time
from gtts import gTTS

for call in CALLS:
    target = f"{VOLUME_PATH}/{call['filename']}"
    # Idempotente: pula se já existe
    if os.path.exists(target):
        size = os.path.getsize(target)
        print(f"[skip] {call['filename']} já existe ({size:,} bytes)")
        continue

    print(f"[gerando] {call['filename']} — {call['scenario']}")
    tts = gTTS(text=call["script"], lang="pt", slow=False)
    # gTTS aceita um filename qualquer, incluindo paths em /Volumes
    tts.save(target)
    size = os.path.getsize(target)
    print(f"   -> {target} ({size:,} bytes)")
    time.sleep(0.3)  # cortesia com a API gratuita do gTTS

print("Pronto.")

# COMMAND ----------

# DBTITLE 1,Salva também os roteiros como referência (para validar o LLM depois)
import json

ref_path = f"{VOLUME_PATH}/_reference_scripts.json"
with open(ref_path, "w", encoding="utf-8") as f:
    json.dump(CALLS, f, ensure_ascii=False, indent=2)
print(f"Roteiros de referência salvos em: {ref_path}")

# COMMAND ----------

# DBTITLE 1,Lista os arquivos prontos pra ingestão
files = sorted(os.listdir(VOLUME_PATH))
print(f"\n{len(files)} arquivos em {VOLUME_PATH}:\n")
for name in files:
    full = f"{VOLUME_PATH}/{name}"
    size = os.path.getsize(full)
    print(f"  {name:<40} {size:>10,} bytes")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pronto
# MAGIC
# MAGIC Próximos passos:
# MAGIC
# MAGIC | Pipeline | Onde |
# MAGIC |---|---|
# MAGIC | Imperativo (PySpark) | `pipelines/imperative/01_..02_..03_*.py` |
# MAGIC | Declarativo (SDP SQL) | `pipelines/declarative/*.sql` |
# MAGIC
# MAGIC Adicione novos áudios depois com:
# MAGIC ```python
# MAGIC dbutils.fs.cp("file:/path/local.mp3", "/Volumes/.../audio_landing/")
# MAGIC ```
# MAGIC e os pipelines processam automaticamente só os novos arquivos.
