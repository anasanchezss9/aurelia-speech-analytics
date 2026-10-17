"""Confere que o mundo gerado conta a história prometida antes de ele ir para o Volume.

Quatro checagens: as duas ondas aparecem na taxa de chamados, os roteiros viraram
chamados abertos coerentes com o gabarito, a fraude é minoria plausível dos
chamados, e um modelo simples aprende a fraude com as features que o pipeline vai
calcular — com lift real sobre a prevalência, mas sem acertar tudo (se acertasse,
o dado estaria vazando o rótulo).

Uso:
    python checar_sinal.py --dados ./saida
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score


def ler(dados: Path, nome: str) -> pd.DataFrame:
    return pd.read_parquet(dados / nome)


def features(dados: Path) -> pd.DataFrame:
    ch = ler(dados, "chamados")
    ped = ler(dados, "pedidos")
    ent = ler(dados, "entregas")
    cons = ler(dados, "consultoras")
    itens = ler(dados, "chamado_itens").merge(ler(dados, "produtos")[["sku", "categoria"]], on="sku")

    perf = (itens.assign(perf=itens["categoria"].eq("perfumaria") * itens["quantidade"])
            .groupby("chamado_id").agg(n_itens=("quantidade", "sum"), n_perfumaria=("perf", "sum")))
    df = (ch.merge(ped[["pedido_numero", "uf", "transportadora", "cd", "ciclo"]], on="pedido_numero")
          .merge(ent, on="pedido_numero")
          .merge(cons[["consultora_codigo", "data_cadastro", "nivel"]], on="consultora_codigo")
          .merge(perf, left_on="chamado_id", right_index=True, how="left"))
    df[["n_itens", "n_perfumaria"]] = df[["n_itens", "n_perfumaria"]].fillna(0)
    abertura = pd.to_datetime(df["data_abertura"])
    df["dias_entrega_abertura"] = (abertura - pd.to_datetime(df["data_entrega"])).dt.days
    df["div_peso_cd"] = (df["peso_teorico_kg"] - df["peso_expedido_kg"]) / df["peso_teorico_kg"]
    df["div_peso_hub"] = (df["peso_expedido_kg"] - df["peso_hub_kg"]) / df["peso_expedido_kg"]
    df["meses_casa"] = (abertura - pd.to_datetime(df["data_cadastro"])).dt.days / 30.4
    df = df.sort_values("data_abertura")
    df["chamados_180d"] = (df.assign(um=1).set_index(pd.to_datetime(df["data_abertura"]))
                           .groupby("consultora_codigo")["um"].transform(lambda s: s.rolling("180D").sum() - 1)
                           .to_numpy())
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dados", type=Path, required=True)
    ap.add_argument("--roteiros", type=Path, default=Path(__file__).parent.parent / "sintetico" / "roteiros.json")
    args = ap.parse_args()
    d = args.dados

    ped = ler(d, "pedidos")
    ch = ler(d, "chamados").merge(ped[["pedido_numero", "uf", "transportadora", "cd", "ciclo"]], on="pedido_numero")

    print("1. As ondas")
    for rotulo, filtro_ch, filtro_ped in [
        ("TransLeste·RJ", (ch.transportadora == "TransLeste") & (ch.uf == "RJ"),
         (ped.transportadora == "TransLeste") & (ped.uf == "RJ")),
        ("CD Contagem  ", ch.cd == "CD Contagem", ped.cd == "CD Contagem"),
        ("resto        ", ~((ch.transportadora == "TransLeste") & (ch.uf == "RJ")) & (ch.cd != "CD Contagem"),
         ~((ped.transportadora == "TransLeste") & (ped.uf == "RJ")) & (ped.cd != "CD Contagem")),
    ]:
        t = (ch[filtro_ch].groupby("ciclo").size() / ped[filtro_ped].groupby("ciclo").size() * 100).fillna(0)
        print(f"   {rotulo} chamados/100 pedidos por ciclo: " + " ".join(f"{v:4.1f}" for v in t.iloc[:23]))

    print("\n2. Roteiros")
    roteiros = json.loads(args.roteiros.read_text(encoding="utf-8"))
    lig = ler(d, "ligacoes")
    sint = lig[lig.origem == "audio_sintetico"].merge(ler(d, "chamados"), on="chamado_id")
    gab = {r["call_id"]: r["gabarito"] for r in roteiros}
    ok = all(gab[r.call_id]["pedido_numero"] == r.pedido_numero and gab[r.call_id]["consultora_codigo"] == r.consultora_codigo
             for r in sint.itertuples())
    print(f"   {len(sint)}/30 ligações com chamado · códigos batem: {ok} · status: {sint.status.value_counts().to_dict()}")

    print("\n3. Fraude e rótulos")
    gch = ler(d / "_gabarito", "chamados")
    inv = ler(d, "investigacoes")
    print(f"   fraude real nos chamados: {gch.eh_fraude.mean():.1%} · investigados: {len(inv) / len(gch):.1%}"
          f" · fraude confirmada entre investigados: {(inv.resultado == 'fraude_confirmada').mean():.1%}")
    print(f"   resultados: {inv.resultado.value_counts().to_dict()}")

    print("\n4. Dá para aprender?")
    df = features(d).merge(inv[["chamado_id", "resultado"]], on="chamado_id", how="left").merge(gch, on="chamado_id")
    cols_num = ["valor_reclamado", "n_itens", "n_perfumaria", "dias_entrega_abertura", "div_peso_cd",
                "div_peso_hub", "meses_casa", "chamados_180d", "comprovante_assinado"]
    cols_cat = ["motivo_declarado", "canal", "transportadora", "uf", "nivel"]
    X = pd.concat([df[cols_num].astype(float), pd.get_dummies(df[cols_cat]).astype(float)], axis=1)
    rotulado = df["resultado"].notna().to_numpy()
    y = (df["resultado"] == "fraude_confirmada").to_numpy()
    ordem = np.argsort(pd.to_datetime(df["data_abertura"]).to_numpy())
    idx_rot = ordem[rotulado[ordem]]
    corte = int(len(idx_rot) * 0.75)
    treino, teste = idx_rot[:corte], idx_rot[corte:]
    m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=0).fit(X.iloc[treino], y[treino])
    p = m.predict_proba(X.iloc[teste])[:, 1]
    print(f"   teste temporal: {len(teste)} chamados, prevalência {y[teste].mean():.1%}"
          f" · PR-AUC {average_precision_score(y[teste], p):.2f} · ROC-AUC {roc_auc_score(y[teste], p):.2f}")
    aberto = (df["status"] == "aberto").to_numpy()
    pa = m.predict_proba(X[aberto])[:, 1]
    verdade = df.loc[aberto, "eh_fraude"].to_numpy()
    k = 50
    top = np.argsort(-pa)[:k]
    print(f"   fila aberta: {aberto.sum()} chamados, fraude real {verdade.mean():.1%}"
          f" · precisão nos {k} primeiros: {verdade[top].mean():.0%}")
    sint_ids = set(sint.chamado_id)
    abertos = df[aberto].assign(score=pa)
    print("   score dos chamados das ligações de fraude: "
          + ", ".join(f"{s:.2f}" for s in abertos[abertos.chamado_id.isin(sint_ids) & abertos.eh_fraude].score))
    print("   score dos demais chamados das ligações:    "
          + ", ".join(f"{s:.2f}" for s in abertos[abertos.chamado_id.isin(sint_ids) & ~abertos.eh_fraude].score))


if __name__ == "__main__":
    main()
