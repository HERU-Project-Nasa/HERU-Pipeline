import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry import shape

from heru.config import CELULA, DADOS, FRONTEIRA

GEOJSON = FRONTEIRA / "geoBoundariesCGAZ_ADM0.geojson"
CACHE = FRONTEIRA / "cgaz_adm0.parquet"
RAIO_KM = 5
KM_POR_GRAU = 111.32
DISPUTADOS = {"117": ("FLK", "Falkland Islands"), "118": ("PSE", "Palestine"), "129": ("PSE", "Palestine")}


def converter(origem=GEOJSON, destino=CACHE):
    with open(origem, encoding="utf-8") as f:
        feicoes = json.load(f)["features"]
    tabela = pa.table({
        "grupo": [f["properties"]["shapeGroup"] for f in feicoes],
        "nome": [f["properties"]["shapeName"] for f in feicoes],
        "wkb": [shapely.to_wkb(shape(f["geometry"])) for f in feicoes],
    })
    pq.write_table(tabela, destino)


def ler_paises(cache=CACHE):
    if not cache.exists():
        converter(destino=cache)
    df = pq.read_table(cache).to_pandas()
    iso3, nome = [], []
    for grupo, nome_original in zip(df["grupo"], df["nome"]):
        if grupo in DISPUTADOS:
            iso3.append(DISPUTADOS[grupo][0])
            nome.append(DISPUTADOS[grupo][1])
        else:
            iso3.append(grupo if grupo.isalpha() else "")
            nome.append(nome_original)
    df["iso3"] = iso3
    df["nome"] = nome
    return df


def tabela_paises(paises):
    continentes = pd.read_csv(DADOS / "continentes.csv", dtype=str, keep_default_na=False)
    tabela = paises.loc[paises["iso3"] != "", ["iso3", "nome"]].drop_duplicates("iso3")
    tabela = tabela.merge(continentes, on="iso3", how="left")
    faltando = tabela.loc[tabela["continent"].isna(), "iso3"].tolist()
    if faltando:
        raise ValueError(f"país sem continente: {faltando}")
    return tabela.rename(columns={"nome": "name"}).sort_values("iso3", ignore_index=True)


def chave_celula(lat, lon):
    i = np.floor(lat / CELULA).astype(np.int64)
    j = np.floor(lon / CELULA).astype(np.int64)
    return (i + 400) * 2000 + (j + 800)


def chave_fina(lat, lon):
    i = np.floor(lat * 100).astype(np.int64)
    j = np.floor(lon * 100).astype(np.int64)
    return (i + 9000) * 40000 + (j + 18000)


def ainda_nao_vistas(chaves, vistas):
    conhecidas = np.fromiter(vistas.keys(), dtype=np.int64, count=len(vistas))
    return np.setdiff1d(np.unique(chaves), conhecidas)


class Fronteira:
    def __init__(self, paises):
        geometrias = shapely.from_wkb(paises["wkb"].to_numpy())
        self.partes, indice = shapely.get_parts(geometrias, return_index=True)
        self.iso3 = paises["iso3"].to_numpy(dtype=object)[indice]
        shapely.prepare(self.partes)
        self.arvore = shapely.STRtree(self.partes)
        self.celulas = {}
        self.perto_da_costa = {}

    def classificar_celulas(self, chaves):
        novas = ainda_nao_vistas(chaves, self.celulas)
        if not len(novas):
            return
        i = novas // 2000 - 400
        j = novas % 2000 - 800
        caixas = shapely.box(j * CELULA, i * CELULA, (j + 1) * CELULA, (i + 1) * CELULA)
        caixa, parte = self.arvore.query(caixas)
        toca = shapely.intersects(self.partes[parte], caixas[caixa])
        caixa, parte = caixa[toca], parte[toca]
        sozinha = np.bincount(caixa, minlength=len(novas))[caixa] == 1
        caixa, parte = caixa[sozinha], parte[sozinha]
        cheia = shapely.contains(self.partes[parte], caixas[caixa])
        resultado = np.full(len(novas), "?", dtype=object)
        resultado[caixa[cheia]] = self.iso3[parte[cheia]]
        self.celulas.update(zip(novas.tolist(), resultado.tolist()))

    def paises(self, lat, lon):
        chaves = chave_celula(lat, lon)
        self.classificar_celulas(chaves)
        pais = pd.Series(chaves).map(self.celulas).to_numpy(dtype=object)
        duvida = np.flatnonzero(pais == "?")
        if len(duvida):
            pais[duvida] = self.exato(lat[duvida], lon[duvida])
        return pais

    def exato(self, lat, lon):
        resultado = np.full(len(lat), "?", dtype=object)
        ponto, parte = self.arvore.query(shapely.points(lon, lat))
        dentro = shapely.contains_xy(self.partes[parte], lon[ponto], lat[ponto])
        resultado[ponto[dentro]] = self.iso3[parte[dentro]]
        fora = np.flatnonzero(resultado == "?")
        if len(fora):
            resultado[fora] = self.mais_proximo(lat[fora], lon[fora])
        return resultado

    def mais_proximo(self, lat, lon):
        chaves = chave_fina(lat, lon)
        novas = ainda_nao_vistas(chaves, self.perto_da_costa)
        if len(novas):
            la = (novas // 40000 - 9000 + 0.5) / 100
            lo = (novas % 40000 - 18000 + 0.5) / 100
            pontos = shapely.points(lo, la)
            ponto, parte = self.arvore.query_nearest(pontos, max_distance=0.5, all_matches=False)
            linhas = shapely.shortest_line(pontos[ponto], self.partes[parte])
            fim = shapely.get_coordinates(shapely.get_point(linhas, 1))
            km = np.hypot((fim[:, 1] - la[ponto]) * KM_POR_GRAU,
                          (fim[:, 0] - lo[ponto]) * KM_POR_GRAU * np.cos(np.radians(la[ponto])))
            iso3 = np.full(len(novas), "", dtype=object)
            perto = km <= RAIO_KM
            iso3[ponto[perto]] = self.iso3[parte[perto]]
            self.perto_da_costa.update(zip(novas.tolist(), iso3.tolist()))
        return pd.Series(chaves).map(self.perto_da_costa).to_numpy(dtype=object)
