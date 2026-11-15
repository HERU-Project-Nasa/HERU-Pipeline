import argparse
import json
import shutil
import time
import zipfile

import pandas as pd
import pyarrow.parquet as pq
import requests

from heru.baixar import avisar, gravar_parquet, hoje_utc, manter_acordado, trava
from heru.config import CAMADAS, DATA, PONTOS, ULTIMO_ANO_ZIP, ZIPS
from heru.fronteira import Fronteira, ler_paises
from heru.padronizar import CHAVE, ler_csv, padronizar
from heru.tarefas import concluidos, ultimo_zip

TRAVA_ZIPS = DATA / ".trava_zips"
URL = "https://firms.modaps.eosdis.nasa.gov/data/country/zips/{nome}_{ano}_all_countries.zip"
NOME_ZIP = {"modis": "modis", "viirs_snpp": "viirs-snpp"}
PRIMEIRO_ANO = {"modis": 2000, "viirs_snpp": 2012}
ULTIMO_ANO = {"viirs_snpp": 2026}
INICIO_K20 = 2019
MINIMO_CONCORDANCIA = 0.99


class ZipInexistente(Exception):
    pass


def caminho_zip(camada, ano, pasta=ZIPS):
    return pasta / f"{NOME_ZIP[camada]}_{ano}_all_countries.zip"


def marcador(camada, ano, pasta=ZIPS):
    return pasta / f"{camada}_{ano}.ok.json"


def ordem(camadas=None, ultimo_ano=ULTIMO_ANO_ZIP):
    lista = [(c, a) for c in camadas or NOME_ZIP
             for a in range(PRIMEIRO_ANO[c], min(ultimo_ano, ULTIMO_ANO.get(c, ultimo_ano)) + 1)]
    return sorted(lista, key=lambda t: (0 if t[0] == "viirs_snpp" and t[1] >= INICIO_K20 else 1,
                                        -t[1], CAMADAS.index(t[0])))


def anos_novos(ano_fechado, pasta=ZIPS):
    feitos = ultimo_zip(pasta)
    return [(c, a) for c, a in ordem(ultimo_ano=ano_fechado) if a > feitos[c]]


def baixar_zip(camada, ano, pasta=ZIPS, tentativas=3):
    destino = caminho_zip(camada, ano, pasta)
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    url = URL.format(nome=NOME_ZIP[camada], ano=ano)
    temp = destino.with_name(destino.name + ".part")
    for tentativa in range(tentativas):
        try:
            with requests.get(url, timeout=300, stream=True) as r:
                if r.status_code == 404:
                    raise ZipInexistente(url)
                r.raise_for_status()
                with open(temp, "wb") as f:
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
            if not zipfile.is_zipfile(temp):
                raise ValueError(f"não é zip: {url}")
            temp.replace(destino)
            return destino
        except ZipInexistente:
            raise
        except (requests.RequestException, ValueError):
            temp.unlink(missing_ok=True)
            time.sleep(10 * (tentativa + 1))
    raise RuntimeError(f"falhou: {url}")


def pais_do_arquivo(nome):
    base = nome.replace("\\", "/").split("/")[-1].removesuffix(".csv")
    partes = base.split("_", 2)
    return partes[2] if len(partes) == 3 else base


def concordancia(arquivo_pais, paises):
    contagem = pd.Series(paises).value_counts()
    principal = contagem.index[0]
    return {"arquivo": arquivo_pais, "iso3": principal, "focos": int(contagem.sum()),
            "concordancia": round(float(contagem.iloc[0] / contagem.sum()), 4)}


def separar_por_mes(caminho, camada, fronteira, temporaria):
    tabela = []
    lidos = 0
    with zipfile.ZipFile(caminho) as z:
        nomes = [n for n in z.namelist() if n.lower().endswith(".csv")]
        for i, nome in enumerate(nomes):
            with z.open(nome) as f:
                bruto = ler_csv(f)
            if not len(bruto):
                continue
            df = padronizar(bruto, camada, "SP", "zip")
            df["country"] = fronteira.paises(df["latitude"].to_numpy(), df["longitude"].to_numpy())
            tabela.append(concordancia(pais_do_arquivo(nome), df["country"]))
            lidos += len(df)
            meses = pd.to_datetime(df["acq_date"]).dt.month
            for mes, parte in df.groupby(meses):
                destino = temporaria / f"mes={mes:02d}" / f"{i:04d}.parquet"
                destino.parent.mkdir(parents=True, exist_ok=True)
                parte.to_parquet(destino, index=False)
    return pd.DataFrame(tabela), lidos


def juntar_meses(camada, ano, temporaria, pasta_pontos):
    focos = 0
    for pasta_mes in sorted(temporaria.glob("mes=*")):
        partes = [pq.ParquetFile(p).read().to_pandas() for p in sorted(pasta_mes.glob("*.parquet"))]
        df = pd.concat(partes, ignore_index=True).drop_duplicates(CHAVE, ignore_index=True)
        mes = pasta_mes.name.split("=")[1]
        destino = pasta_pontos / f"camada={camada}" / f"ano={ano}" / f"mes={mes}" / f"SP_zip_{ano}-{mes}.parquet"
        gravar_parquet(df, destino)
        focos += len(df)
    return focos


def trocar_pelo_zip(camada, ano, pasta_pontos=PONTOS):
    pasta_ano = pasta_pontos / f"camada={camada}" / f"ano={ano}"
    meses = {a.parent for a in pasta_ano.glob("mes=*/SP_zip_*.parquet")}
    velhos = sorted(a for a in pasta_ano.glob("mes=*/*.parquet")
                    if a.parent in meses and not a.name.startswith("SP_zip_"))
    for arquivo in velhos:
        arquivo.unlink()
    return velhos


def processar(camada, ano, fronteira, pasta_zip=ZIPS, pasta_pontos=PONTOS):
    caminho = baixar_zip(camada, ano, pasta_zip)
    temporaria = pasta_zip / f"tmp_{camada}_{ano}"
    shutil.rmtree(temporaria, ignore_errors=True)
    tabela, lidos = separar_por_mes(caminho, camada, fronteira, temporaria)
    focos = juntar_meses(camada, ano, temporaria, pasta_pontos)
    trocados = trocar_pelo_zip(camada, ano, pasta_pontos)
    tabela.sort_values("arquivo").to_csv(pasta_zip / f"{camada}_{ano}_paises.csv", index=False)
    grandes = tabela[tabela["focos"] > 1000]
    abaixo = grandes[grandes["concordancia"] < MINIMO_CONCORDANCIA]
    info = {"camada": camada, "ano": ano, "focos": focos, "repetidos_entre_paises": lidos - focos,
            "arquivos": len(tabela), "trocados": len(trocados),
            "abaixo_de_99": abaixo[["arquivo", "iso3", "concordancia"]].to_dict("records")}
    marcador(camada, ano, pasta_zip).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(temporaria, ignore_errors=True)
    caminho.unlink()
    return info


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m heru zips")
    p.add_argument("--camadas", help="modis,viirs_snpp")
    args = p.parse_args(argv)
    camadas = args.camadas.split(",") if args.camadas else None
    feitos = concluidos()
    pendentes = [t for t in ordem(camadas, hoje_utc().year - 1) if t not in feitos]
    print(f"{len(pendentes)} anos de zip pendentes: " + ", ".join(f"{c} {a}" for c, a in pendentes), flush=True)
    if not pendentes:
        return
    with trava(TRAVA_ZIPS):
        manter_acordado()
        fronteira = Fronteira(ler_paises())
        for camada, ano in pendentes:
            inicio = time.time()
            try:
                info = processar(camada, ano, fronteira)
            except ZipInexistente as erro:
                avisar(f"{camada} {ano}: sem zip ({erro})")
                continue
            abaixo = ", ".join(f"{a['iso3']} {a['concordancia']:.3f}" for a in info["abaixo_de_99"]) or "nenhum"
            avisar(f"{camada} {ano}: {info['focos']:,} focos de {info['arquivos']} arquivos, "
                   f"{info['repetidos_entre_paises']:,} repetidos, {info['trocados']} arquivos da API e do NRT "
                   f"trocados, abaixo de 99%: {abaixo}, {time.time() - inicio:.0f} s")
