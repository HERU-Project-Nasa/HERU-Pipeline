import numpy as np
import pandas as pd

from heru.config import CAMADAS, CELULA, COLUNAS_CONTAGEM, COM_PASSAGEM

ESCALAS = [("daily", "date", "%Y-%m-%d"), ("monthly", "month", "%Y-%m"), ("yearly", "year", "%Y")]


def calendario(cobertura):
    dias = pd.date_range(cobertura["first_date"].min(), cobertura["last_date"].max(), freq="D")
    operando = pd.DataFrame(index=dias)
    for camada in CAMADAS:
        linhas = cobertura[cobertura["layer"] == camada]
        if linhas.empty:
            operando[camada] = False
        else:
            operando[camada] = (dias >= linhas["first_date"].min()) & (dias <= linhas["last_date"].max())
    provisorio = np.zeros(len(dias), dtype=bool)
    for linha in cobertura[cobertura["processing"] == "nrt"].itertuples():
        provisorio |= (dias >= linha.first_date) & (dias <= linha.last_date)
    operando["provisional"] = provisorio
    return operando


def por_periodo(operando, formato):
    return operando.groupby(operando.index.strftime(formato)).any()


def largura(contagens, indice):
    if contagens.empty:
        tabela = pd.DataFrame()
    else:
        coluna = contagens["layer"] + "_" + contagens["daynight"].str.lower()
        tabela = contagens.assign(coluna=coluna).groupby(indice + ["coluna"])["n"].sum().unstack("coluna", fill_value=0)
    for camada in CAMADAS:
        for passagem in ("d", "n"):
            if f"{camada}_{passagem}" not in tabela:
                tabela[f"{camada}_{passagem}"] = 0
        tabela[camada] = tabela[f"{camada}_d"] + tabela[f"{camada}_n"]
    return tabela


def preencher(tabela, operando):
    saida = pd.DataFrame(index=tabela.index, columns=COLUNAS_CONTAGEM, dtype=object)
    for camada in CAMADAS:
        ligada = operando[camada].to_numpy(dtype=bool)
        colunas = [camada] + ([f"{camada}_d", f"{camada}_n"] if camada in COM_PASSAGEM else [])
        for coluna in colunas:
            valores = pd.array(tabela[coluna].to_numpy(), dtype="Int64")
            valores[~ligada] = pd.NA
            saida[coluna] = valores
    saida["provisional"] = operando["provisional"].to_numpy(dtype=int)
    return saida


def series(diario, operando):
    tabela = largura(diario, ["acq_date"]).reindex(operando.index, fill_value=0)
    resultado = {}
    for nome, coluna, formato in ESCALAS:
        soma = tabela.groupby(tabela.index.strftime(formato)).sum()
        corpo = preencher(soma, por_periodo(operando, formato).reindex(soma.index))
        corpo.insert(0, coluna, soma.index)
        resultado[nome] = corpo.reset_index(drop=True)
    return resultado


def grade_do_mes(mes, contagens, operando_mes):
    tabela = largura(contagens, ["i", "j"]).sort_index()
    operando = pd.DataFrame({c: np.repeat(bool(operando_mes[c]), len(tabela)) for c in operando_mes.index})
    corpo = preencher(tabela, operando).reset_index(drop=True)
    i = tabela.index.get_level_values("i").to_numpy()
    j = tabela.index.get_level_values("j").to_numpy()
    corpo.insert(0, "cell_lon", [f"{v:.3f}" for v in j * CELULA + CELULA / 2])
    corpo.insert(0, "cell_lat", [f"{v:.3f}" for v in i * CELULA + CELULA / 2])
    corpo.insert(0, "month", mes)
    return corpo
