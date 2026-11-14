import hashlib
import shutil
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from heru import config
from heru.config import CAMADAS, CELULA, DATA, PONTOS
from heru.fronteira import chave_fina
from heru.tarefas import fim_da_janela, fim_do_mes

AGREGADOS = DATA / "agregados"
MANIFEST = config.MANIFEST
TABELAS = ["grade", "dia", "dia_k", "mes_pb", "cobertura", "paises", "fontes"]
COLUNAS_LIDAS = ["camada", "processamento", "latitude", "longitude", "acq_date", "daynight", "type", "country"]
FIM_K = pd.Timestamp("2012-01-01")
INICIO_K20_HARMONIZADA = pd.Timestamp("2026-11-01")


def mes_numero(datas):
    return (datas.dt.year * 12 + datas.dt.month - 1).astype("int32")


def mes_numero_texto(texto):
    return int(texto[:4]) * 12 + int(texto[5:7]) - 1


def mes_texto(numero):
    return f"{numero // 12:04d}-{numero % 12 + 1:02d}"


def arquivos_do_store():
    return sorted(PONTOS.glob("camada=*/ano=*/mes=*/*.parquet"))


def pasta_cache(arquivo):
    return AGREGADOS / arquivo.relative_to(PONTOS).with_suffix("")


def assinatura(arquivo, mascara_id=""):
    estado = arquivo.stat()
    return f"{estado.st_size}-{estado.st_mtime_ns}-{mascara_id}"


def eh_sp(arquivo):
    return arquivo.name.startswith("SP")


def ler_pontos(arquivo):
    df = pq.ParquetFile(arquivo).read(columns=COLUNAS_LIDAS).to_pandas(date_as_object=False)
    df["acq_date"] = df["acq_date"].astype("datetime64[ns]")
    return df


def contar(df, chaves):
    return df.groupby(chaves, observed=True).size().rename("n").reset_index()


def agregar_arquivo(arquivo, mascara):
    df = ler_pontos(arquivo)
    camada = CAMADAS.index(arquivo.parent.parent.parent.name.split("=")[1])
    lat, lon = df["latitude"].to_numpy(), df["longitude"].to_numpy()
    fina = chave_fina(lat, lon)
    tipo = df["type"].fillna(-1).to_numpy()
    if eh_sp(arquivo):
        fontes = np.unique(fina[tipo == 2])
        valido = tipo == 0
    else:
        fontes = np.array([], dtype=np.int64)
        valido = ~np.isin(fina, mascara)
    v = df[valido]
    i = np.floor(v["latitude"].to_numpy() / CELULA).astype("int16")
    j = np.floor(v["longitude"].to_numpy() / CELULA).astype("int16")
    base = pd.DataFrame({
        "date": v["acq_date"].to_numpy(),
        "month": mes_numero(v["acq_date"]).to_numpy(),
        "i": i, "j": j, "bi": (i // 20).astype("int16"), "bj": (j // 20).astype("int16"),
        "dn": (v["daynight"] == "N").to_numpy().astype("int8"),
        "country": v["country"].to_numpy(),
    })
    tabelas = {
        "grade": contar(base, ["month", "i", "j", "dn"]),
        "dia": contar(base, ["date", "country", "dn"]),
        "dia_k": contar(base.iloc[0:0], ["date", "country", "bi", "bj", "dn"]),
        "mes_pb": contar(base.iloc[0:0], ["month", "country", "bi", "bj", "dn"]),
    }
    if CAMADAS[camada] == "modis":
        tabelas["dia_k"] = contar(base[base["date"] < FIM_K], ["date", "country", "bi", "bj", "dn"])
    if CAMADAS[camada] == "viirs_noaa20":
        tabelas["dia_k"] = contar(base[base["date"] >= INICIO_K20_HARMONIZADA], ["date", "country", "bi", "bj", "dn"])
    if CAMADAS[camada] in ("modis", "viirs_noaa20"):
        tabelas["mes_pb"] = contar(base, ["month", "country", "bi", "bj", "dn"])
    for nome in ("grade", "dia", "dia_k", "mes_pb"):
        tabelas[nome].insert(0, "layer", np.int8(camada))
    processamento = "sp" if eh_sp(arquivo) else "nrt"
    tabelas["cobertura"] = pd.DataFrame({
        "layer": [camada], "processing": [processamento],
        "first_date": [df["acq_date"].min()], "last_date": [df["acq_date"].max()], "rows": [len(df)]})
    paises = df.groupby("country").agg(first_date=("acq_date", "min"), last_date=("acq_date", "max"),
                                       rows=("acq_date", "size")).reset_index()
    tabelas["paises"] = paises
    tabelas["fontes"] = pd.DataFrame({"fina": fontes.astype("int64")})
    return tabelas


def gravar_cache(pasta, tabelas, marca):
    temporaria = pasta.with_name(pasta.name + ".tmp")
    shutil.rmtree(temporaria, ignore_errors=True)
    temporaria.mkdir(parents=True)
    for nome, tabela in tabelas.items():
        tabela.to_parquet(temporaria / f"{nome}.parquet", index=False)
    (temporaria / "assinatura.txt").write_text(marca, encoding="utf-8")
    shutil.rmtree(pasta, ignore_errors=True)
    temporaria.rename(pasta)


def cache_valido(pasta, marca):
    arquivo = pasta / "assinatura.txt"
    return arquivo.exists() and arquivo.read_text(encoding="utf-8") == marca


def id_mascara(mascara):
    return hashlib.sha1(np.ascontiguousarray(mascara).tobytes()).hexdigest()[:12]


def atualizar(aviso=print):
    arquivos = arquivos_do_store()
    novos = []
    for arquivo in [a for a in arquivos if eh_sp(a)]:
        pasta, marca = pasta_cache(arquivo), assinatura(arquivo)
        if not cache_valido(pasta, marca):
            gravar_cache(pasta, agregar_arquivo(arquivo, None), marca)
            novos.append(arquivo)
    mascara = np.unique(np.concatenate([np.array([], dtype=np.int64)] + [
        pq.read_table(pasta_cache(a) / "fontes.parquet").column("fina").to_numpy()
        for a in arquivos if eh_sp(a)]))
    mid = id_mascara(mascara)
    for arquivo in [a for a in arquivos if not eh_sp(a)]:
        pasta, marca = pasta_cache(arquivo), assinatura(arquivo, mid)
        if not cache_valido(pasta, marca):
            gravar_cache(pasta, agregar_arquivo(arquivo, mascara), marca)
            novos.append(arquivo)
    aviso(f"agregados: {len(arquivos)} arquivos no store, {len(novos)} recalculados, máscara com {len(mascara):,} quadrículas")
    return arquivos, novos, mascara


def juntar(arquivos, nome):
    partes = [pq.read_table(pasta_cache(a) / f"{nome}.parquet").to_pandas() for a in arquivos]
    partes = [p for p in partes if len(p)]
    if not partes:
        return pd.DataFrame()
    return pd.concat(partes, ignore_index=True)


def somar(arquivos, nome, chaves):
    tudo = juntar(arquivos, nome)
    if tudo.empty:
        return pd.DataFrame(columns=chaves + ["n"])
    return tudo.groupby(chaves, observed=True)["n"].sum().reset_index()


def resumir(arquivos, nome, chaves):
    tudo = juntar(arquivos, nome)
    return tudo.groupby(chaves).agg(first_date=("first_date", "min"), last_date=("last_date", "max"),
                                    rows=("rows", "sum")).reset_index()


def mes_do_arquivo(arquivo):
    ano = int(arquivo.parent.parent.name.split("=")[1])
    mes = int(arquivo.parent.name.split("=")[1])
    return ano * 12 + mes - 1


def camada_do_arquivo(arquivo):
    return CAMADAS.index(arquivo.parent.parent.parent.name.split("=")[1])


def grade_por_bloco(arquivos, camadas, janela):
    partes = []
    for arquivo in arquivos:
        if camada_do_arquivo(arquivo) in camadas and janela[0] <= mes_do_arquivo(arquivo) <= janela[1]:
            grade = pq.read_table(pasta_cache(arquivo) / "grade.parquet").to_pandas()
            if len(grade):
                grade = grade.assign(bi=grade["i"] // 20, bj=grade["j"] // 20)
                partes.append(grade.groupby(["layer", "month", "bi", "bj", "dn"])["n"].sum().reset_index())
    if not partes:
        return pd.DataFrame(columns=["layer", "month", "bi", "bj", "dn", "n"])
    return pd.concat(partes, ignore_index=True).groupby(["layer", "month", "bi", "bj", "dn"])["n"].sum().reset_index()


def dias_baixados():
    if not MANIFEST.exists():
        return {}
    tabela = pd.read_csv(MANIFEST, usecols=["arquivo", "dias"], dtype={"arquivo": str})
    return dict(zip(tabela["arquivo"], tabela["dias"].astype(int)))


def dias_do_arquivo(arquivo, baixados):
    texto = arquivo.stem.split("_")[-1]
    if len(texto) == 7:
        inicio = date.fromisoformat(texto + "-01")
        return pd.Timestamp(inicio), pd.Timestamp(fim_do_mes(inicio))
    inicio = date.fromisoformat(texto)
    dias = baixados.get(arquivo.relative_to(PONTOS).as_posix())
    fim = inicio + timedelta(days=dias - 1) if dias else fim_da_janela(inicio)
    return pd.Timestamp(inicio), pd.Timestamp(fim)
