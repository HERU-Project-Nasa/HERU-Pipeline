import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from heru import agregar, contar
from heru.config import CAMADAS, CELULA, COLUNAS_VALIDACAO, INFO_CAMADAS, SAIDA
from heru.focos import formatar_pontos
from heru.fronteira import Fronteira, chave_fina, ler_paises, tabela_paises
from heru.publicar import Lote, agora

COLUNAS_STORE = ["camada", "satelite", "processamento", "origem", "latitude", "longitude", "acq_date",
                 "acq_time", "daynight", "conf_raw", "conf_class", "frp", "brilho", "scan", "track", "type"]


def arquivos_do_store(store):
    arquivos = sorted(store.glob("camada=*/ano=*/mes=*/*.parquet"))
    return sorted(arquivos, key=lambda a: a.name.startswith("NRT"))


def mes_do_arquivo(arquivo):
    ano = arquivo.parent.parent.name.split("=")[1]
    mes = arquivo.parent.name.split("=")[1]
    return f"{ano}-{mes}"


def ler(arquivo):
    df = pq.ParquetFile(arquivo).read(columns=COLUNAS_STORE).to_pandas(date_as_object=False)
    df["acq_date"] = df["acq_date"].astype("datetime64[ns]")
    df["processamento"] = df["processamento"].str.lower()
    return df


def resumo(df, chave):
    return (df.groupby(chave).agg(first_date=("acq_date", "min"), last_date=("acq_date", "max"),
                                  rows=("acq_date", "size")).reset_index())


def juntar_resumos(partes, chave):
    tudo = pd.concat(partes, ignore_index=True)
    return tudo.groupby(chave).agg(first_date=("first_date", "min"), last_date=("last_date", "max"),
                                   rows=("rows", "sum")).reset_index()


def somar(partes, chave):
    if not partes:
        return pd.DataFrame(columns=chave + ["n"])
    return pd.concat(partes, ignore_index=True).groupby(chave)["n"].sum().reset_index()


def contar_validos(df, chave):
    return df.groupby(chave).size().rename("n").reset_index()


def percorrer(store, fronteira, meses_grade, dia_pontos):
    fontes, mascara = set(), None
    cobertura, paises, diario, grade, pontos, fechados = [], [], [], [], [], []
    for arquivo in arquivos_do_store(store):
        df = ler(arquivo)
        if df.empty:
            continue
        lat, lon = df["latitude"].to_numpy(), df["longitude"].to_numpy()
        df["country"] = fronteira.paises(lat, lon)
        fina = chave_fina(lat, lon)
        tipo = df["type"].fillna(-1).to_numpy()
        sp = (df["processamento"] == "sp").to_numpy()
        if sp.any():
            fontes.update(np.unique(fina[sp & (tipo == 2)]).tolist())
            valido = tipo == 0
        else:
            if mascara is None:
                mascara = np.fromiter(fontes, dtype=np.int64, count=len(fontes))
            valido = ~np.isin(fina, mascara)
        cobertura.append(resumo(df, ["camada", "processamento"]))
        fim = df["acq_date"].max()
        fechado = fim if sp.any() else min(fim, agregar.fechado_em(pd.Timestamp(arquivo.stat().st_mtime, unit="s")))
        if fechado >= df["acq_date"].min():
            fechados.append((df["camada"].iloc[0], df["processamento"].iloc[0], fechado))
        paises.append(resumo(df, ["country"]))
        validos = df[valido]
        diario.append(contar_validos(validos, ["acq_date", "country", "camada", "daynight"]))
        mes = mes_do_arquivo(arquivo)
        if mes in meses_grade:
            celulas = validos.assign(month=mes,
                                     i=np.floor(validos["latitude"] / CELULA).astype(int),
                                     j=np.floor(validos["longitude"] / CELULA).astype(int))
            grade.append(contar_validos(celulas, ["month", "i", "j", "camada", "daynight"]))
        if dia_pontos is not None:
            pontos.append(df[df["acq_date"] == dia_pontos])
        print(f"{arquivo.parent.parent.parent.name} {arquivo.name}: {len(df):,} focos")
    cobertura = juntar_resumos(cobertura, ["camada", "processamento"])
    cobertura = cobertura.rename(columns={"camada": "layer", "processamento": "processing"})
    fechado = pd.DataFrame(fechados, columns=["layer", "processing", "complete_date"])
    cobertura = cobertura.merge(fechado.groupby(["layer", "processing"])["complete_date"].max().reset_index(),
                                on=["layer", "processing"], how="left")
    return {
        "cobertura": cobertura,
        "paises": juntar_resumos(paises, ["country"]),
        "diario": somar(diario, ["acq_date", "country", "camada", "daynight"]).rename(columns={"camada": "layer"}),
        "grade": somar(grade, ["month", "i", "j", "camada", "daynight"]).rename(columns={"camada": "layer"}),
        "pontos": pd.concat(pontos, ignore_index=True) if pontos else None,
        "fontes": sorted(fontes),
    }


def texto_data(serie):
    return serie.dt.strftime("%Y-%m-%d").fillna("")


def publicar_meta(lote, dados, tabela):
    cobertura = dados["cobertura"].sort_values(["layer", "processing"], ignore_index=True)
    lote.publicar("meta/coverage.csv", cobertura.assign(
        first_date=texto_data(cobertura["first_date"]), last_date=texto_data(cobertura["last_date"]),
        complete_date=texto_data(cobertura["complete_date"]), updated_at=agora())[
        ["layer", "processing", "first_date", "last_date", "rows", "updated_at", "complete_date"]], "meta")
    paises = tabela.merge(dados["paises"].rename(columns={"country": "iso3"}), on="iso3", how="left")
    paises["rows"] = paises["rows"].fillna(0).astype(int)
    paises["first_date"] = texto_data(paises["first_date"])
    paises["last_date"] = texto_data(paises["last_date"])
    lote.publicar("meta/countries.csv", paises[["iso3", "name", "continent", "first_date", "last_date", "rows"]], "meta")
    camadas = pd.DataFrame(INFO_CAMADAS, columns=["layer", "title", "instrument", "resolution_m", "satellites", "notes"])
    lote.publicar("meta/layers.csv", camadas, "meta")
    lote.publicar("meta/validation.csv", pd.DataFrame(columns=COLUNAS_VALIDACAO), "meta")
    fontes = np.array(dados["fontes"], dtype=np.int64)
    estaticas = pd.DataFrame({"lat": [f"{v:.2f}" for v in (fontes // 40000 - 9000) / 100],
                              "lon": [f"{v:.2f}" for v in (fontes % 40000 - 18000) / 100]})
    lote.publicar("meta/static_sources.csv", estaticas, "meta")


def publicar_series(lote, dados, tabela, operando):
    diario = dados["diario"]
    for nome, corpo in contar.series(diario, operando).items():
        lote.publicar(f"series/global/{nome}.csv", corpo, "series")
    grupos = dict(tuple(diario.groupby("country")))
    vazio = diario.iloc[0:0]
    for iso3 in tabela["iso3"]:
        for nome, corpo in contar.series(grupos.get(iso3, vazio), operando).items():
            lote.publicar(f"series/countries/{iso3}/{nome}.csv", corpo, "series")


def publicar_grade(lote, dados, operando):
    mensal = contar.por_periodo(operando, "%Y-%m")
    for mes, contagens in dados["grade"].groupby("month"):
        corpo = contar.grade_do_mes(mes, contagens, mensal.loc[mes])
        lote.publicar(f"grid/monthly/{mes[:4]}/{mes}.csv", corpo, "grid", mes)


def publicar_pontos(lote, dados, dia, operando):
    if dados["pontos"] is None:
        return
    texto = dia.strftime("%Y-%m-%d")
    for camada in CAMADAS:
        if not operando.loc[dia, camada]:
            continue
        df = dados["pontos"][dados["pontos"]["camada"] == camada]
        lote.publicar(f"points/{camada}/{texto[:4]}/{texto}.csv.gz", formatar_pontos(df), "points", texto)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--saida", type=Path, default=SAIDA)
    p.add_argument("--meses-grade", default="2026-01,2026-02,2026-03,2026-04,2026-05,2026-06,2026-07,2026-08,2026-09,2026-10,2026-11")
    p.add_argument("--dia-pontos", type=pd.Timestamp, default=pd.Timestamp("2026-08-15"))
    p.add_argument("--lote", type=int, default=1)
    args = p.parse_args(argv)
    inicio = time.time()
    paises = ler_paises()
    tabela = tabela_paises(paises)
    fronteira = Fronteira(paises)
    print(f"fronteira pronta em {time.time() - inicio:.0f} s")
    dados = percorrer(args.store, fronteira, set(args.meses_grade.split(",")), args.dia_pontos)
    print(f"store lido em {time.time() - inicio:.0f} s")
    operando = contar.calendario(dados["cobertura"])
    lote = Lote(args.lote, args.saida)
    publicar_pontos(lote, dados, args.dia_pontos, operando)
    publicar_grade(lote, dados, operando)
    publicar_series(lote, dados, tabela, operando)
    publicar_meta(lote, dados, tabela)
    lote.fechar()
    print(f"lote {args.lote}: {len(lote.arquivos)} arquivos em {time.time() - inicio:.0f} s")


if __name__ == "__main__":
    main()
