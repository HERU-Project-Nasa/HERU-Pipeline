import argparse
import gzip
import hashlib
import io
from pathlib import Path

import pandas as pd

from heru.config import (CAMADAS, COLUNAS_CONTAGEM, COLUNAS_PONTOS, COLUNAS_VALIDACAO, COLUNAS_VALIDACAO_MENSAL,
                         COM_PASSAGEM, SAIDA)

CABECALHOS = {
    "meta/latest.csv": ["batch", "updated_at"],
    "meta/files.csv": ["path", "kind", "period", "rows", "bytes", "sha256", "updated_at"],
    "meta/coverage.csv": ["layer", "processing", "first_date", "last_date", "rows", "updated_at", "complete_date"],
    "meta/countries.csv": ["iso3", "name", "continent", "first_date", "last_date", "rows"],
    "meta/layers.csv": ["layer", "title", "instrument", "resolution_m", "satellites", "notes"],
    "meta/validation.csv": COLUNAS_VALIDACAO,
    "meta/validation_monthly.csv": COLUNAS_VALIDACAO_MENSAL,
    "series/countries_yearly.csv": ["iso3", "year"] + COLUNAS_CONTAGEM,
    "meta/static_sources.csv": ["lat", "lon"],
}
PROIBIDOS = {"nan", "NaN", "null", "NA", "None", "<NA>"}


def cabecalho_esperado(relativo):
    if relativo in CABECALHOS:
        return CABECALHOS[relativo]
    if relativo.startswith("meta/batches/"):
        return ["path", "kind", "period", "rows", "bytes", "sha256"]
    if relativo.startswith("points/") or relativo.startswith("recent/"):
        return COLUNAS_PONTOS
    if relativo.startswith("grid/"):
        return ["month", "cell_lat", "cell_lon"] + COLUNAS_CONTAGEM
    for escala, coluna in (("daily", "date"), ("monthly", "month"), ("yearly", "year")):
        if relativo.startswith("series/") and relativo.endswith(f"/{escala}.csv"):
            return [coluna] + COLUNAS_CONTAGEM
    raise ValueError(f"arquivo fora do contrato: {relativo}")


def ler_bruto(caminho):
    corpo = caminho.read_bytes()
    texto = gzip.decompress(corpo) if caminho.name.endswith(".gz") else corpo
    return corpo, texto


def conferir_arquivo(relativo, caminho, problemas):
    corpo, texto = ler_bruto(caminho)
    if texto.startswith(b"\xef\xbb\xbf"):
        problemas.append(f"{relativo}: BOM")
    if b"\r" in texto:
        problemas.append(f"{relativo}: CR")
    df = pd.read_csv(io.BytesIO(texto), dtype=str, keep_default_na=False)
    if list(df.columns) != cabecalho_esperado(relativo):
        problemas.append(f"{relativo}: cabeçalho {list(df.columns)}")
        return corpo, df
    ruins = df.isin(PROIBIDOS).any()
    if ruins.any():
        problemas.append(f"{relativo}: valor proibido em {list(ruins[ruins].index)}")
    if relativo.startswith("grid/") or relativo.startswith("series/"):
        for camada in COM_PASSAGEM:
            cheio = df[camada] != ""
            soma = df.loc[cheio, f"{camada}_d"].astype(int) + df.loc[cheio, f"{camada}_n"].astype(int)
            if not soma.equals(df.loc[cheio, camada].astype(int)):
                problemas.append(f"{relativo}: {camada}_d + {camada}_n diferente de {camada}")
            if (df.loc[~cheio, [f"{camada}_d", f"{camada}_n"]] != "").any().any():
                problemas.append(f"{relativo}: passagem cheia com {camada} vazio")
    if relativo.startswith("grid/"):
        ordem = df[["month"]].assign(lat=df["cell_lat"].astype(float), lon=df["cell_lon"].astype(float))
        if not ordem.equals(ordem.sort_values(["month", "lat", "lon"], ignore_index=True)):
            problemas.append(f"{relativo}: fora de ordem")
    if relativo.startswith("grid/monthly/") and (df["month"] != relativo.split("/")[-1][:7]).any():
        problemas.append(f"{relativo}: month diferente do caminho")
        vazias = df[[c for c in CAMADAS]].eq("").all(axis=None)
        if vazias:
            problemas.append(f"{relativo}: todas as camadas vazias")
    if relativo.startswith("points/"):
        if (df["acq_time"].str.len() != 4).any():
            problemas.append(f"{relativo}: acq_time sem 4 caracteres")
        chave = df[["acq_date", "acq_time"]].assign(lat=df["latitude"].astype(float), lon=df["longitude"].astype(float))
        if not chave.equals(chave.sort_values(["acq_date", "acq_time", "lat", "lon"], ignore_index=True)):
            problemas.append(f"{relativo}: fora de ordem")
    return corpo, df


def pontos_do_lote(saida, limite):
    ultimo = saida / "meta" / "latest.csv"
    if not ultimo.exists():
        return {}
    lista = saida / "meta" / "batches" / f"{pd.read_csv(ultimo)['batch'].iloc[0]}.csv"
    if not lista.exists():
        return {}
    tabela = pd.read_csv(lista, dtype=str, keep_default_na=False)
    tabela = tabela[tabela["path"].str.startswith("points/")].sort_values("path")
    passo = max(1, len(tabela) // limite)
    return dict(zip(tabela["path"].iloc[::passo], tabela["sha256"].iloc[::passo]))


def conferir(saida=SAIDA, limite_pontos=200):
    problemas = []
    lidos = {}
    amostra = pontos_do_lote(saida, limite_pontos)
    for caminho in sorted(saida.rglob("*")):
        if caminho.is_file():
            relativo = caminho.relative_to(saida).as_posix()
            if caminho.name.startswith("."):
                problemas.append(f"{relativo}: temporário esquecido")
                continue
            if relativo.startswith("points/") and relativo not in amostra:
                continue
            lidos[relativo] = conferir_arquivo(relativo, caminho, problemas)
    for relativo, sha in amostra.items():
        if relativo in lidos and hashlib.sha256(lidos[relativo][0]).hexdigest() != sha:
            problemas.append(f"{relativo}: sha256 diferente do lote")
    faltando = [m for m in ("meta/files.csv", "meta/latest.csv", "meta/coverage.csv", "meta/countries.csv") if m not in lidos]
    if faltando:
        return problemas + [f"{m}: não existe" for m in faltando]
    arquivos = lidos["meta/files.csv"][1]
    for linha in arquivos.itertuples():
        corpo = lidos[linha.path][0]
        if hashlib.sha256(corpo).hexdigest() != linha.sha256 or len(corpo) != int(linha.bytes):
            problemas.append(f"{linha.path}: sha256 ou bytes diferente de meta/files.csv")
    lote = lidos["meta/latest.csv"][1]["batch"].iloc[0]
    publicados = {r for r in lidos if not r.startswith(("meta/batches/", "points/")) and r != "meta/latest.csv"}
    catalogados = set(arquivos["path"]) | {"meta/files.csv"}
    if catalogados != publicados:
        problemas.append(f"meta/files.csv difere do publicado em {sorted(catalogados ^ publicados)[:5]}")
    do_lote = set(lidos[f"meta/batches/{lote}.csv"][1]["path"])
    if "meta/files.csv" not in do_lote or not all((saida / r).exists() for r in do_lote):
        problemas.append(f"lote {lote}: lista com arquivo ausente ou sem meta/files.csv")
    cobertura = lidos["meta/coverage.csv"][1]
    fechada = cobertura[cobertura["complete_date"] != ""]
    if ((fechada["complete_date"] < fechada["first_date"]) | (fechada["complete_date"] > fechada["last_date"])).any():
        problemas.append("meta/coverage.csv: complete_date fora de first_date..last_date")
    paises = lidos["meta/countries.csv"][1]
    total = cobertura["rows"].astype(int).sum()
    com_pais = paises["rows"].astype(int).sum()
    print(f"{len(lidos)} arquivos lidos ({len(amostra)} de pontos); focos {total:,}; com país {com_pais:,} ({com_pais / total:.2%})")
    return problemas


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--saida", type=Path, default=SAIDA)
    args = p.parse_args(argv)
    problemas = conferir(args.saida)
    for problema in problemas:
        print(problema)
    print("ok" if not problemas else f"{len(problemas)} problemas")
    raise SystemExit(1 if problemas else 0)


if __name__ == "__main__":
    main()
