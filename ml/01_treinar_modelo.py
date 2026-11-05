# Databricks notebook source
# MAGIC %md
# MAGIC # Modelo de fraude em chamado de falta
# MAGIC
# MAGIC Prevê a probabilidade de um chamado ser fraude confirmada, a partir de
# MAGIC `gold_features_chamado`. Treina só com os chamados investigados — é o único
# MAGIC rótulo que existe — e testa nos mais recentes, para medir o que o modelo faria
# MAGIC com chamados que ainda não viu.
# MAGIC
# MAGIC Três candidatos, no mesmo teste:
# MAGIC
# MAGIC | Candidato | O que usa |
# MAGIC |---|---|
# MAGIC | `llm_sozinho` | a `fraude_confianca` que o LLM deu à ligação, sem modelo |
# MAGIC | `operacional` | chamado, entrega, peso, histórico e rota, sem a ligação |
# MAGIC | `completo` | o operacional e o sinal da ligação |
# MAGIC
# MAGIC O completo só é campeão se somar ao menos 0,01 de PR-AUC: depender da
# MAGIC ligação tem custo (o gold precisa estar em dia), e empate não paga esse custo.
# MAGIC O campeão é registrado no Unity Catalog com o alias `campeao`. A saída é uma
# MAGIC probabilidade calibrada: o analista lê "0,7" como sete em dez, e o número que o
# MAGIC LLM autodeclara não sustenta essa leitura.

# COMMAND ----------

# MAGIC %pip install -q "mlflow[databricks]>=3.1" "scikit-learn==1.6.1" "matplotlib>=3.8"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "field_toolbox_catalog")
dbutils.widgets.text("schema", "aurelia_speech")
dbutils.widgets.text("experimento", "/Users/ana.sanchez@databricks.com/aurelia_fraude_chamado")

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
NOME_MODELO = f"{CATALOG}.{SCHEMA}.modelo_fraude_chamado"
K = 50  # chamados que o time de investigação consegue abrir por ciclo
GANHO_MINIMO = 0.01  # PR-AUC que a ligação precisa somar para valer a dependência dela

import mlflow
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder

mlflow.set_registry_uri("databricks-uc")
mlflow.set_experiment(dbutils.widgets.get("experimento"))

# COMMAND ----------

# DBTITLE 1,Features e rótulo
CATEGORICAS = ["motivo_declarado", "canal_chamado", "cd", "transportadora", "uf", "regiao",
               "status_entrega", "nivel"]
NUMERICAS = ["valor_reclamado", "qtd_itens_reclamados", "qtd_perfumaria_reclamada",
             "share_valor_perfumaria", "share_pedido_reclamado", "comprovante_assinado", "dias_atraso",
             "div_peso_cd", "div_peso_hub", "pesado_no_hub", "dias_entrega_abertura",
             "rota_chamados_100_pedidos", "tem_app", "meses_casa", "chamados_180d", "pedidos_180d",
             "chamados_por_pedido_180d", "fraudes_confirmadas_antes"]
CATEGORICAS_LIGACAO = ["llm_tipo_problema", "llm_sentiment"]
NUMERICAS_LIGACAO = ["tem_ligacao", "llm_suspeita_fraude", "llm_fraude_confianca",
                     "llm_n_sinais_relato", "llm_relato_consistente"]

df = spark.table(f"{CATALOG}.{SCHEMA}.gold_features_chamado").toPandas()
for c in NUMERICAS + NUMERICAS_LIGACAO:
    df[c] = pd.to_numeric(df[c].astype("float64"), errors="coerce")
for c in CATEGORICAS + CATEGORICAS_LIGACAO:
    df[c] = df[c].astype("string").fillna("ausente")

rotulado = df[df["rotulo_fraude"].notna()].sort_values("data_abertura").reset_index(drop=True)
corte = int(len(rotulado) * 0.75)
treino, teste = rotulado.iloc[:corte], rotulado.iloc[corte:]
y_treino, y_teste = treino["rotulo_fraude"].astype(int), teste["rotulo_fraude"].astype(int)
data_corte = teste["data_abertura"].min()
print(f"rotulados: {len(rotulado)} · treino {len(treino)} até {treino['data_abertura'].max()} "
      f"· teste {len(teste)} desde {data_corte} · prevalência no teste {y_teste.mean():.1%}")

# COMMAND ----------

# DBTITLE 1,Métricas de fila
def metricas(y, p, prefixo=""):
    ordem = np.argsort(-p)
    topo = ordem[:K]
    return {
        f"{prefixo}pr_auc": average_precision_score(y, p),
        f"{prefixo}roc_auc": roc_auc_score(y, p),
        f"{prefixo}precisao_top{K}": float(np.asarray(y)[topo].mean()),
        f"{prefixo}recall_top{K}": float(np.asarray(y)[topo].sum() / max(1, np.asarray(y).sum())),
        f"{prefixo}brier": brier_score_loss(y, np.clip(p, 0, 1)),
    }


def montar(categoricas, numericas):
    previsor = ColumnTransformer(
        [("cat", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1), categoricas),
         ("num", "passthrough", numericas)])
    gbm = HistGradientBoostingClassifier(
        learning_rate=0.05, max_iter=400, max_leaf_nodes=15, l2_regularization=1.0,
        categorical_features=list(range(len(categoricas))), early_stopping=True,
        validation_fraction=0.15, random_state=42)
    base = Pipeline([("prep", previsor), ("gbm", gbm)])
    # Isotônica com validação cruzada no treino: a ordem já vem do GBM, a
    # calibração só corrige a escala para o score virar probabilidade.
    return CalibratedClassifierCV(base, method="isotonic", cv=5)


class ModeloFraude(mlflow.pyfunc.PythonModel):
    """Devolve a probabilidade calibrada de fraude, não a classe."""

    def __init__(self, modelo, colunas_cat, colunas_num):
        self.modelo, self.colunas_cat, self.colunas_num = modelo, colunas_cat, colunas_num

    def predict(self, context, entrada: pd.DataFrame, params=None) -> pd.DataFrame:
        x = entrada.copy()
        for c in self.colunas_num:
            x[c] = pd.to_numeric(x[c], errors="coerce").astype("float64") if c in x else np.nan
        for c in self.colunas_cat:
            x[c] = x[c].astype("string").fillna("ausente") if c in x else "ausente"
        p = self.modelo.predict_proba(x[self.colunas_cat + self.colunas_num])[:, 1]
        return pd.DataFrame({"score_fraude": p})

# COMMAND ----------

# DBTITLE 1,Candidato 1 — o LLM sozinho
com_ligacao = teste["tem_ligacao"] == 1
with mlflow.start_run(run_name="llm_sozinho"):
    p_llm = teste["llm_fraude_confianca"].fillna(0).to_numpy()
    m = metricas(y_teste, p_llm)
    m.update(metricas(y_teste[com_ligacao], p_llm[com_ligacao.to_numpy()], "com_ligacao_"))
    mlflow.log_metrics(m)
    resultados = {"llm_sozinho": m}
print(pd.Series(resultados["llm_sozinho"]).round(3))

# COMMAND ----------

# DBTITLE 1,Candidatos 2 e 3 — operacional e completo
modelos = {}
for nome, cats, nums in [("operacional", CATEGORICAS, NUMERICAS),
                         ("completo", CATEGORICAS + CATEGORICAS_LIGACAO, NUMERICAS + NUMERICAS_LIGACAO)]:
    with mlflow.start_run(run_name=nome) as run:
        modelo = montar(cats, nums).fit(treino[cats + nums], y_treino)
        p = modelo.predict_proba(teste[cats + nums])[:, 1]
        m = metricas(y_teste, p)
        m.update(metricas(y_teste[com_ligacao], p[com_ligacao.to_numpy()], "com_ligacao_"))
        mlflow.log_params({"features": len(cats + nums), "treino": len(treino), "teste": len(teste),
                           "corte_teste": str(data_corte), "calibracao": "isotonica_cv5", "k_fila": K})
        mlflow.log_metrics(m)
        resultados[nome] = m
        modelos[nome] = (modelo, cats, nums, p, run.info.run_id)

comparacao = pd.DataFrame(resultados).T
display(comparacao.round(3).reset_index().rename(columns={"index": "candidato"}))

# COMMAND ----------

# DBTITLE 1,Curvas e importância do campeão
# O completo depende do gold da ligação estar em dia; só vale se pagar por isso.
ganho = resultados["completo"]["pr_auc"] - resultados["operacional"]["pr_auc"]
campeao = "completo" if ganho >= GANHO_MINIMO else "operacional"
print(f"ganho da ligação em PR-AUC: {ganho:+.3f} → campeão: {campeao}")
modelo, cats, nums, p_camp, run_id = modelos[campeao]

fig, eixos = plt.subplots(1, 3, figsize=(18, 5))
for nome, p in [("llm_sozinho", p_llm), ("operacional", modelos["operacional"][3]), ("completo", modelos["completo"][3])]:
    prec, rec, _ = precision_recall_curve(y_teste, p)
    eixos[0].plot(rec, prec, label=f"{nome} (PR-AUC {resultados[nome]['pr_auc']:.2f})")
eixos[0].axhline(y_teste.mean(), ls="--", c="gray", label="aleatório")
eixos[0].set(xlabel="recall", ylabel="precisão", title="Precisão × recall no teste"); eixos[0].legend()

frac, media = calibration_curve(y_teste, p_camp, n_bins=8, strategy="quantile")
eixos[1].plot(media, frac, "o-", label=campeao)
frac_llm, media_llm = calibration_curve(y_teste, np.clip(p_llm, 0, 1), n_bins=8, strategy="quantile")
eixos[1].plot(media_llm, frac_llm, "s--", label="llm_sozinho")
eixos[1].plot([0, 1], [0, 1], c="gray", ls=":")
eixos[1].set(xlabel="probabilidade prevista", ylabel="fraude observada", title="Calibração"); eixos[1].legend()

imp = permutation_importance(modelo, teste[cats + nums], y_teste, scoring="average_precision",
                             n_repeats=5, random_state=0)
importancia = pd.Series(imp.importances_mean, index=cats + nums).sort_values().tail(12)
importancia.plot.barh(ax=eixos[2], title="Importância por permutação (queda de PR-AUC)")
plt.tight_layout()

with mlflow.start_run(run_id=run_id):
    mlflow.log_figure(fig, "avaliacao.png")
    mlflow.log_text(importancia.sort_values(ascending=False).to_string(), "importancia.txt")
display(fig)

# COMMAND ----------

# DBTITLE 1,Registra o campeão no Unity Catalog
from mlflow.models import infer_signature

exemplo = teste[cats + nums].head(5)
wrapper = ModeloFraude(modelo, cats, nums)
assinatura = infer_signature(exemplo, wrapper.predict(None, exemplo))
with mlflow.start_run(run_id=run_id):
    info = mlflow.pyfunc.log_model(
        name="modelo",
        python_model=wrapper,
        signature=assinatura,
        input_example=exemplo,
        registered_model_name=NOME_MODELO,
        pip_requirements=["mlflow>=3.1", "scikit-learn==1.6.1", "pandas", "numpy"],
    )
versao = info.registered_model_version
cliente = mlflow.MlflowClient()
cliente.set_registered_model_alias(NOME_MODELO, "campeao", versao)
cliente.update_model_version(NOME_MODELO, versao, description=(
    f"Candidato '{campeao}'. Teste temporal desde {data_corte}: PR-AUC {resultados[campeao]['pr_auc']:.3f}, "
    f"precisão nos {K} primeiros {resultados[campeao][f'precisao_top{K}']:.0%}. "
    "Saída: probabilidade calibrada (isotônica) de fraude confirmada."))
print(f"{NOME_MODELO} v{versao} @campeao ({campeao})")
