"""Wrapper do modelo de fraude, registrado junto com ele por `code_paths`.

Fica num módulo, e não no notebook, porque o cloudpickle guarda por valor o
código de classe definida no notebook, e código serializado não atravessa
versão de Python: o modelo salvo num serverless com 3.10 não carregava no job
com 3.12. Importado de um módulo, ele é guardado por referência.
"""

import mlflow
import numpy as np
import pandas as pd


class ModeloFraude(mlflow.pyfunc.PythonModel):
    """Devolve a probabilidade calibrada de fraude, não a classe."""

    def __init__(self, modelo, colunas_cat, colunas_num):
        self.modelo, self.colunas_cat, self.colunas_num = modelo, colunas_cat, colunas_num

    def predict(self, context, entrada: pd.DataFrame, params=None) -> pd.DataFrame:
        x = entrada.copy()
        for c in self.colunas_num:
            x[c] = pd.to_numeric(x[c].astype("float64"), errors="coerce") if c in x else np.nan
        for c in self.colunas_cat:
            x[c] = x[c].astype("string").fillna("ausente") if c in x else "ausente"
        p = self.modelo.predict_proba(x[self.colunas_cat + self.colunas_num])[:, 1]
        return pd.DataFrame({"score_fraude": p})
