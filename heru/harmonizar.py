from datetime import date

import numpy as np
import pandas as pd

from heru import tarefas
from heru.config import CAMADAS, COLUNAS_VALIDACAO, COLUNAS_VALIDACAO_MENSAL, FONTES, PONTOS

MINIMO = 1000
MODIS, SNPP, NOAA20 = CAMADAS.index("modis"), CAMADAS.index("viirs_snpp"), CAMADAS.index("viirs_noaa20")
JANELA_K = (2012 * 12 + 1, 2019 * 12 + 11)
JANELA_K20 = (2019 * 12, 2025 * 12 + 11)
JANELA_ERRO = (2020 * 12, 2024 * 12 + 11)
ANOS_K = range(2012, 2020)
ANOS_K20 = range(2019, 2026)
MESES_ERRO = np.arange(JANELA_ERRO[0], JANELA_ERRO[1] + 1)


def zips_prontos(pasta_zips, camada, anos):
    return all((pasta_zips / f"{camada}_{ano}.ok.json").exists() for ano in anos)


def noaa20_pronto(pontos=PONTOS):
    fonte = FONTES["viirs_noaa20"]
    for inicio, dias, fim_janela in tarefas.janelas(date(2019, 1, 1), date(2025, 12, 31)):
        tarefa = tarefas.Tarefa("viirs_noaa20", "SP", fonte.sp, fonte.area, inicio, dias, fim_janela, 0)
        if not tarefas.caminho_arquivo(tarefa, pontos).exists():
            return False
    return True


def somas_por_bloco(grade, camada, janela):
    parte = grade[(grade["layer"] == camada) & grade["month"].between(*janela)]
    tabela = parte.groupby(["bi", "bj", "dn"])["n"].sum().unstack("dn", fill_value=0)
    tabela = tabela.reindex(columns=[0, 1], fill_value=0)
    tabela.columns = ["d", "n"]
    return tabela


def razao(numerador, denominador, padrao):
    return np.where(denominador >= MINIMO, numerador / np.maximum(denominador, 1), padrao)


def fatores(numerador, denominador):
    blocos = numerador.add_suffix("_num").join(denominador.add_suffix("_den"), how="outer").fillna(0)
    num = blocos["d_num"] + blocos["n_num"]
    den = blocos["d_den"] + blocos["n_den"]
    total = (num.sum() / den.sum(), blocos["d_num"].sum() / blocos["d_den"].sum(),
             blocos["n_num"].sum() / blocos["n_den"].sum())
    tabela = pd.DataFrame({
        "k": razao(num, den, total[0]),
        "k_d": razao(blocos["d_num"], blocos["d_den"], total[1]),
        "k_n": razao(blocos["n_num"], blocos["n_den"], total[2]),
        "calib": den.astype("int64"),
    }, index=blocos.index)
    return {"blocos": tabela, "global": total}


def calcular(grade, pasta_zips, pontos=PONTOS):
    resultado = {"k": None, "k20": None}
    if zips_prontos(pasta_zips, "modis", ANOS_K) and zips_prontos(pasta_zips, "viirs_snpp", ANOS_K):
        resultado["k"] = fatores(somas_por_bloco(grade, SNPP, JANELA_K), somas_por_bloco(grade, MODIS, JANELA_K))
    if zips_prontos(pasta_zips, "viirs_snpp", ANOS_K20) and noaa20_pronto(pontos):
        resultado["k20"] = fatores(somas_por_bloco(grade, SNPP, JANELA_K20), somas_por_bloco(grade, NOAA20, JANELA_K20))
    return resultado


def fator(conjunto, bi, bj, coluna):
    posicao = {"k": 0, "k_d": 1, "k_n": 2}[coluna]
    chave = pd.MultiIndex.from_arrays([np.asarray(bi), np.asarray(bj)])
    valores = conjunto["blocos"][coluna].reindex(chave).to_numpy()
    return np.where(np.isnan(valores), conjunto["global"][posicao], valores)


def assinatura(fatores_atuais):
    partes = []
    for nome in ("k", "k20"):
        conjunto = fatores_atuais[nome]
        if conjunto is None:
            partes.append(f"{nome}=")
        else:
            partes.append(f"{nome}={conjunto['global'][0]:.6f}/{len(conjunto['blocos'])}/{conjunto['blocos']['k'].sum():.4f}")
    return ";".join(partes)


def erro(estimado, observado):
    juntos = pd.concat([estimado.rename("est"), observado.rename("obs")], axis=1).fillna(0)
    juntos = juntos[juntos["obs"] > 0]
    if juntos.empty:
        return np.nan, np.nan, 0
    mediana = float((juntos["est"] - juntos["obs"]).abs().div(juntos["obs"]).median())
    positivos = juntos[juntos["est"] > 0]
    r = float(np.corrcoef(np.log10(positivos["est"]), np.log10(positivos["obs"]))[0, 1]) if len(positivos) > 2 else np.nan
    return mediana, r, len(juntos)


def com_fator(tabela, conjunto, coluna="k"):
    return tabela["n"] * fator(conjunto, tabela["bi"], tabela["bj"], coluna)


def concordancia_paises(pasta_zips):
    partes = [pd.read_csv(a, keep_default_na=False) for a in sorted(pasta_zips.glob("*_paises.csv"))]
    if not partes:
        return pd.Series(dtype=float)
    tudo = pd.concat(partes, ignore_index=True)
    tudo = tudo[tudo["iso3"] != ""]
    soma = (tudo["focos"] * tudo["concordancia"]).groupby(tudo["iso3"]).sum()
    return soma / tudo.groupby("iso3")["focos"].sum()


def passagens(tabela, chaves):
    largura = tabela.groupby(chaves + ["dn"])["n"].sum().unstack("dn", fill_value=0).reindex(columns=[0, 1], fill_value=0)
    largura.columns = ["d", "n"]
    return largura


def linha(escopo, ident, k_vals=None, k20_vals=None, metricas=(np.nan, np.nan, 0), concordancia=np.nan):
    registro = {c: np.nan for c in COLUNAS_VALIDACAO}
    registro.update(scope=escopo, id=ident, country_agreement=concordancia)
    if k_vals is not None:
        registro.update(k=k_vals[0], k_d=k_vals[1], k_n=k_vals[2], modis_calib_hotspots=int(k_vals[3]),
                        calib_years="2012-2019", median_error_2020_2024=metricas[0], r_log10=metricas[1],
                        months_used=metricas[2])
    if k20_vals is not None:
        registro.update(k20=k20_vals[0], k20_d=k20_vals[1], k20_n=k20_vals[2], noaa20_calib_hotspots=int(k20_vals[3]),
                        calib_years_k20="2019-2025")
    return registro


def dividir(a, b):
    return a / b if b > 0 else np.nan


def razoes_diretas(num, den):
    total = den["d"] + den["n"]
    return dividir(num["d"] + num["n"], total), dividir(num["d"], den["d"]), dividir(num["n"], den["n"]), total


def do_pais(serie, pais):
    return serie.loc[pais] if pais in serie.index.get_level_values(0) else pd.Series(dtype=float)


def mensal(escopo, ident, snpp, modis, estimado):
    tabela = pd.DataFrame({"viirs_snpp": snpp, "modis": modis, "modis_x_k": estimado}).reindex(MESES_ERRO).fillna(0)
    return pd.DataFrame({
        "scope": escopo, "id": ident, "month": [f"{m // 12}-{m % 12 + 1:02d}" for m in MESES_ERRO],
        "viirs_snpp": tabela["viirs_snpp"].astype("int64").to_numpy(), "modis": tabela["modis"].astype("int64").to_numpy(),
        "modis_x_k": [f"{v:.2f}" for v in tabela["modis_x_k"]],
    })


def validacao(grade, dia, mes_pb, fatores_atuais, pasta_zips):
    k, k20 = fatores_atuais["k"], fatores_atuais["k20"]
    if k is None and k20 is None:
        return pd.DataFrame(columns=COLUNAS_VALIDACAO), pd.DataFrame(columns=COLUNAS_VALIDACAO_MENSAL)
    linhas, mensais = [], []
    erro_grade = grade[(grade["month"].between(*JANELA_ERRO))]
    modis_erro = erro_grade[erro_grade["layer"] == MODIS]
    snpp_erro = erro_grade[erro_grade["layer"] == SNPP]
    metricas_mundo = (np.nan, np.nan, 0)
    if k is not None:
        modis_erro = modis_erro.assign(est=com_fator(modis_erro, k))
        por_mes = modis_erro.groupby("month")[["n", "est"]].sum()
        obs_mes = snpp_erro.groupby("month")["n"].sum()
        metricas_mundo = erro(por_mes["est"], obs_mes)
        mensais.append(mensal("world", "world", obs_mes, por_mes["n"], por_mes["est"]))
    k_mundo = (*k["global"], k["blocos"]["calib"].sum()) if k else None
    k20_mundo = (*k20["global"], k20["blocos"]["calib"].sum()) if k20 else None
    linhas.append(linha("world", "world", k_mundo, k20_mundo, metricas_mundo))
    blocos = sorted(set(k["blocos"].index if k else []) | set(k20["blocos"].index if k20 else []))
    est_bloco = modis_erro.groupby(["bi", "bj", "month"])["est"].sum() if k else None
    obs_bloco = snpp_erro.groupby(["bi", "bj", "month"])["n"].sum()
    for bi, bj in blocos:
        k_vals = k20_vals = None
        metricas = (np.nan, np.nan, 0)
        if k is not None and (bi, bj) in k["blocos"].index:
            b = k["blocos"].loc[(bi, bj)]
            k_vals = (b["k"], b["k_d"], b["k_n"], b["calib"])
            est = est_bloco.loc[(bi, bj)] if (bi, bj) in est_bloco.index else pd.Series(dtype=float)
            obs = obs_bloco.loc[(bi, bj)] if (bi, bj) in obs_bloco.index else pd.Series(dtype=float)
            metricas = erro(est, obs)
        if k20 is not None and (bi, bj) in k20["blocos"].index:
            b = k20["blocos"].loc[(bi, bj)]
            k20_vals = (b["k"], b["k_d"], b["k_n"], b["calib"])
        linhas.append(linha("block", f"{int(bi) * 5}_{int(bj) * 5}", k_vals, k20_vals, metricas))
    concordancia = concordancia_paises(pasta_zips)
    snpp_mes = dia[dia["layer"] == SNPP].assign(month=lambda d: (d["date"].dt.year * 12 + d["date"].dt.month - 1))
    s_k = passagens(snpp_mes[snpp_mes["month"].between(*JANELA_K)], ["country"])
    s_k20 = passagens(snpp_mes[snpp_mes["month"].between(*JANELA_K20)], ["country"])
    m_k = passagens(mes_pb[(mes_pb["layer"] == MODIS) & mes_pb["month"].between(*JANELA_K)], ["country"])
    n_k20 = passagens(mes_pb[(mes_pb["layer"] == NOAA20) & mes_pb["month"].between(*JANELA_K20)], ["country"])
    if k is not None:
        modis_pais = mes_pb[(mes_pb["layer"] == MODIS) & mes_pb["month"].between(*JANELA_ERRO)]
        modis_pais = modis_pais.assign(est=com_fator(modis_pais, k)).groupby(["country", "month"])[["n", "est"]].sum()
        est_pais = modis_pais["est"]
        obs_pais = snpp_mes[snpp_mes["month"].between(*JANELA_ERRO)].groupby(["country", "month"])["n"].sum()
    paises = sorted(c for c in set(m_k.index) | set(n_k20.index) if c)
    for pais in paises:
        k_vals = k20_vals = None
        metricas = (np.nan, np.nan, 0)
        if k is not None and pais in m_k.index:
            r = razoes_diretas(s_k.reindex([pais]).fillna(0).iloc[0], m_k.loc[pais])
            k_vals = r
            est, obs = do_pais(est_pais, pais), do_pais(obs_pais, pais)
            metricas = erro(est, obs)
            mensais.append(mensal("country", pais, obs, do_pais(modis_pais["n"], pais), est))
        if k20 is not None and pais in n_k20.index:
            k20_vals = razoes_diretas(s_k20.reindex([pais]).fillna(0).iloc[0], n_k20.loc[pais])
        linhas.append(linha("country", pais, k_vals, k20_vals, metricas, concordancia.get(pais, np.nan)))
    tabela = pd.DataFrame(linhas, columns=COLUNAS_VALIDACAO)
    for coluna in ("k", "k20", "k_d", "k_n", "k20_d", "k20_n", "median_error_2020_2024", "r_log10", "country_agreement"):
        tabela[coluna] = tabela[coluna].map(lambda v: "" if pd.isna(v) else f"{v:.4f}")
    for coluna in ("months_used", "modis_calib_hotspots", "noaa20_calib_hotspots"):
        tabela[coluna] = tabela[coluna].map(lambda v: "" if pd.isna(v) else str(int(v)))
    for coluna in ("months_used",):
        tabela.loc[tabela["k"] == "", coluna] = ""
    mensal_tabela = pd.concat(mensais, ignore_index=True) if mensais else pd.DataFrame(columns=COLUNAS_VALIDACAO_MENSAL)
    return tabela.astype(object).fillna(""), mensal_tabela[COLUNAS_VALIDACAO_MENSAL]
