import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from heru import agregar, focos, harmonizar
from heru.area import ler_areas
from heru.baixar import avisar, manter_acordado, trava
from heru.config import CAMADAS, CELULA, COLUNAS_CONTAGEM, COM_PASSAGEM, DATA, FONTES, INFO_CAMADAS, SAIDA
from heru.fronteira import ler_paises, tabela_paises
from heru.publicar import Lote, ler_anteriores, ultimo_lote
from heru.zips import ZIPS

TRAVA_EXPORTAR = DATA / ".trava_exportar"
ESTADO = agregar.AGREGADOS / "estado.json"
NOMES = np.array([f"{c}_{p}" for c in CAMADAS for p in ("d", "n")])
INICIO_K = 2002 * 12 + 6
INICIO_SNPP = 2012 * 12
INICIO_K20 = 2026 * 12 + 10
ESCALAS = [("daily", "date", "%Y-%m-%d"), ("monthly", "month", "%Y-%m"), ("yearly", "year", "%Y")]
RECENTES = {"24h": pd.Timedelta(hours=24), "48h": pd.Timedelta(hours=48), "7d": pd.Timedelta(days=7)}


def regime(mes):
    if mes < INICIO_K:
        return None
    if mes < INICIO_SNPP:
        return "modis_x_k"
    if mes < INICIO_K20:
        return "viirs_snpp"
    return "viirs_noaa20_x_k20"


def largura(tabela, chaves):
    if tabela.empty:
        larga = pd.DataFrame()
    else:
        coluna = NOMES[tabela["layer"].to_numpy().astype(int) * 2 + tabela["dn"].to_numpy().astype(int)]
        larga = tabela.assign(coluna=coluna).groupby(chaves + ["coluna"])["n"].sum().unstack("coluna", fill_value=0)
    for nome in NOMES:
        if nome not in larga:
            larga[nome] = 0
    for camada in CAMADAS:
        larga[camada] = larga[f"{camada}_d"] + larga[f"{camada}_n"]
    return larga


def vazia(n):
    return pd.DataFrame({"harmonized": np.full(n, np.nan), "harmonized_d": np.full(n, np.nan),
                         "harmonized_n": np.full(n, np.nan), "harmonized_source": np.full(n, "", dtype=object)})


def harmonizada_celulas(larga, bi, bj, mes, fatores, ligado):
    nome = regime(mes)
    saida = vazia(len(larga))
    if nome == "viirs_snpp" and ligado["viirs_snpp"].any():
        saida["harmonized"] = larga["viirs_snpp"].to_numpy(dtype=float)
        saida["harmonized_d"] = larga["viirs_snpp_d"].to_numpy(dtype=float)
        saida["harmonized_n"] = larga["viirs_snpp_n"].to_numpy(dtype=float)
        saida["harmonized_source"] = nome
    elif nome in ("modis_x_k", "viirs_noaa20_x_k20"):
        camada, conjunto = ("modis", fatores["k"]) if nome == "modis_x_k" else ("viirs_noaa20", fatores["k20"])
        if conjunto is not None and ligado[camada].any():
            saida["harmonized"] = larga[camada].to_numpy() * harmonizar.fator(conjunto, bi, bj, "k")
            saida["harmonized_d"] = larga[f"{camada}_d"].to_numpy() * harmonizar.fator(conjunto, bi, bj, "k_d")
            saida["harmonized_n"] = larga[f"{camada}_n"].to_numpy() * harmonizar.fator(conjunto, bi, bj, "k_n")
            saida["harmonized_source"] = nome
    return saida


def preencher(larga, ligado, harmonizada):
    saida = pd.DataFrame(index=range(len(larga)))
    for camada in CAMADAS:
        colunas = [camada] + ([f"{camada}_d", f"{camada}_n"] if camada in COM_PASSAGEM else [])
        for coluna in colunas:
            valores = pd.array(larga[coluna].to_numpy(), dtype="Int64")
            valores[~ligado[camada]] = pd.NA
            saida[coluna] = valores
    for coluna in ("harmonized", "harmonized_d", "harmonized_n"):
        saida[coluna] = harmonizada[coluna].round(2).to_numpy()
    saida["harmonized_source"] = harmonizada["harmonized_source"].to_numpy()
    saida["provisional"] = ligado["provisional"].astype(int)
    return saida[COLUNAS_CONTAGEM]


def ligados(operando, n):
    return {c: np.repeat(bool(operando[c]), n) for c in operando.index}


def grade_do_mes(arquivos, mes, fatores, operando_mes):
    do_mes = [a for a in arquivos if agregar.mes_do_arquivo(a) == mes]
    tabela = agregar.somar(do_mes, "grade", ["layer", "i", "j", "dn"])
    larga = largura(tabela, ["i", "j"]).sort_index()
    i = larga.index.get_level_values("i").to_numpy().astype(int)
    j = larga.index.get_level_values("j").to_numpy().astype(int)
    ligado = ligados(operando_mes, len(larga))
    corpo = preencher(larga, ligado, harmonizada_celulas(larga, i // 20, j // 20, mes, fatores, ligado))
    corpo.insert(0, "cell_lon", [f"{v:.3f}" for v in j * CELULA + CELULA / 2])
    corpo.insert(0, "cell_lat", [f"{v:.3f}" for v in i * CELULA + CELULA / 2])
    corpo.insert(0, "month", agregar.mes_texto(mes))
    return corpo


def contribuicoes(dia_k, fatores):
    partes = []
    for camada, conjunto in ((harmonizar.MODIS, fatores["k"]), (harmonizar.NOAA20, fatores["k20"])):
        parte = dia_k[dia_k["layer"] == camada]
        if conjunto is None or parte.empty:
            continue
        noite = parte["dn"].to_numpy() == 1
        n = parte["n"].to_numpy()
        partes.append(pd.DataFrame({
            "date": parte["date"].to_numpy(), "country": parte["country"].to_numpy(),
            "h": n * harmonizar.fator(conjunto, parte["bi"], parte["bj"], "k"),
            "hd": np.where(noite, 0, n * harmonizar.fator(conjunto, parte["bi"], parte["bj"], "k_d")),
            "hn": np.where(noite, n * harmonizar.fator(conjunto, parte["bi"], parte["bj"], "k_n"), 0),
        }))
    if not partes:
        return pd.DataFrame(columns=["date", "country", "h", "hd", "hn"])
    return pd.concat(partes, ignore_index=True)


def cobertos(arquivos):
    baixados = agregar.baixados()
    linhas = [(agregar.camada_do_arquivo(a), "sp" if agregar.eh_sp(a) else "nrt", *agregar.dias_do_arquivo(a, baixados))
              for a in arquivos]
    return pd.DataFrame(linhas, columns=["layer", "processing", "inicio", "fim", "fechado"])


def calendario(trechos):
    base = trechos["inicio"].min()
    dias = pd.date_range(base, trechos["fim"].max(), freq="D")
    matriz = np.zeros((len(dias), len(CAMADAS) + 1), dtype=bool)
    for linha in trechos.itertuples():
        a, b = (linha.inicio - base).days, (linha.fim - base).days + 1
        matriz[a:b, linha.layer] = True
        if linha.processing == "nrt" and FONTES[CAMADAS[linha.layer]].sp:
            matriz[a:b, -1] = True
    return pd.DataFrame(matriz, index=dias, columns=CAMADAS + ["provisional"])


def continuo(trechos, cobertura):
    base = trechos["inicio"].min()
    total = (trechos["fim"].max() - base).days + 1
    inicios = []
    for linha in cobertura.itertuples():
        parte = trechos[(trechos["layer"] == linha.layer) & (trechos["processing"] == linha.processing)]
        coberto = np.zeros(total, dtype=bool)
        for t in parte.itertuples():
            coberto[(t.inicio - base).days:(t.fim - base).days + 1] = True
        primeiro = pd.Timestamp(linha.first_date)
        fim = (pd.Timestamp(linha.last_date) - base).days
        if fim >= total or not coberto[fim]:
            inicios.append(primeiro)
            continue
        buracos = np.flatnonzero(~coberto[:fim + 1])
        comeco = base + pd.Timedelta(days=int(buracos[-1]) + 1) if len(buracos) else base
        inicios.append(max(comeco, primeiro))
    return cobertura.assign(first_date=inicios)


def fechados(trechos, cobertura):
    ultimo = trechos[trechos["fechado"] >= trechos["inicio"]].groupby(["layer", "processing"])["fechado"].max()
    chave = pd.MultiIndex.from_arrays([cobertura["layer"], cobertura["processing"]])
    fechado = pd.Series(ultimo.reindex(chave).to_numpy(), index=cobertura.index)
    fim = pd.to_datetime(cobertura["last_date"])
    return cobertura.assign(complete_date=fechado.where(fechado.isna() | (fechado <= fim), fim))


def marcas_mensais(operando_mensal):
    return {m: "".join("1" if v else "0" for v in linha) for m, linha in zip(operando_mensal.index, operando_mensal.to_numpy())}


def contexto(operando, fatores):
    dias = operando.index
    meses = dias.year * 12 + dias.month - 1
    nomes = np.array([regime(m) or "" for m in meses], dtype=object)
    fonte = np.full(len(dias), "", dtype=object)
    snpp = (nomes == "viirs_snpp") & operando["viirs_snpp"].to_numpy()
    fonte[snpp] = "viirs_snpp"
    alvo = np.zeros(len(dias), dtype=bool)
    for nome, camada, conjunto in (("modis_x_k", "modis", fatores["k"]), ("viirs_noaa20_x_k20", "viirs_noaa20", fatores["k20"])):
        if conjunto is not None:
            parte = (nomes == nome) & operando[camada].to_numpy()
            fonte[parte] = nome
            alvo |= parte
    com_h = fonte != ""
    posicao = np.where(com_h, np.arange(len(dias)), -1)
    escalas = []
    for nome, coluna, formato in ESCALAS:
        rotulos = np.asarray(dias.strftime(formato))
        inicio = np.flatnonzero(np.r_[True, rotulos[1:] != rotulos[:-1]])
        ultimo = np.maximum.reduceat(posicao, inicio)
        escalas.append({"nome": nome, "coluna": coluna, "rotulos": rotulos[inicio], "inicio": inicio,
                        "ligado": {c: np.logical_or.reduceat(operando[c].to_numpy(), inicio) for c in operando.columns},
                        "tem_h": np.logical_or.reduceat(com_h, inicio),
                        "fonte": np.where(ultimo >= 0, fonte[np.maximum(ultimo, 0)], "")})
    mensal, anual = escalas[1], escalas[2]
    algum = operando[CAMADAS].to_numpy().any(axis=1)
    falta = np.logical_or.reduceat(algum, mensal["inicio"]) & ~mensal["tem_h"]
    anos = np.array([r[:4] for r in mensal["rotulos"]])
    anual["tem_h"] = anual["tem_h"] & ~np.logical_or.reduceat(falta, np.flatnonzero(np.r_[True, anos[1:] != anos[:-1]]))
    for escala in escalas:
        escala["fonte"] = np.where(escala["tem_h"], escala["fonte"], "")
    return {"dias": dias, "snpp": snpp, "alvo": alvo, "escalas": escalas}


def series(tabela_dia, contrib, ctx):
    dias = ctx["dias"]
    larga = largura(tabela_dia, ["date"]).reindex(dias, fill_value=0)
    h = np.zeros((len(dias), 3))
    snpp, alvo = ctx["snpp"], ctx["alvo"]
    h[snpp] = larga[["viirs_snpp", "viirs_snpp_d", "viirs_snpp_n"]].to_numpy(dtype=float)[snpp]
    if len(contrib) and alvo.any():
        soma = contrib.groupby("date")[["h", "hd", "hn"]].sum().reindex(dias, fill_value=0).to_numpy(dtype=float)
        h[alvo] = soma[alvo]
    valores = larga.to_numpy()
    resultado = {}
    for escala in ctx["escalas"]:
        soma = pd.DataFrame(np.add.reduceat(valores, escala["inicio"], axis=0), columns=larga.columns)
        hs = np.add.reduceat(h, escala["inicio"], axis=0)
        hs[~escala["tem_h"]] = np.nan
        harmonizada = pd.DataFrame({"harmonized": hs[:, 0], "harmonized_d": hs[:, 1], "harmonized_n": hs[:, 2],
                                    "harmonized_source": escala["fonte"]})
        corpo = preencher(soma, escala["ligado"], harmonizada)
        corpo.insert(0, escala["coluna"], escala["rotulos"])
        resultado[escala["nome"]] = corpo
    return resultado


def tiles(arquivos, meses, fatores, operando_mensal, lote, aviso):
    pasta = lote.saida / "grid" / ".tiles_tmp"
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    cabecalho = ",".join(["month", "cell_lat", "cell_lon"] + COLUNAS_CONTAGEM) + "\n"
    linhas = {}
    for mes in sorted(meses):
        corpo = grade_do_mes(arquivos, mes, fatores, operando_mensal.loc[agregar.mes_texto(mes)])
        lat = np.floor(corpo["cell_lat"].astype(float) / 5).astype(int) * 5
        lon = np.floor(corpo["cell_lon"].astype(float) / 5).astype(int) * 5
        for (bloco_lat, bloco_lon), parte in corpo.groupby([lat, lon], sort=False):
            nome = f"{bloco_lat}_{bloco_lon}"
            destino = pasta / f"{nome}.csv"
            novo = not destino.exists()
            with open(destino, "a", encoding="utf-8", newline="") as f:
                if novo:
                    f.write(cabecalho)
                f.write(parte.to_csv(index=False, header=False, lineterminator="\n", na_rep=""))
            linhas[nome] = linhas.get(nome, 0) + len(parte)
    for destino in sorted(pasta.glob("*.csv")):
        nome = destino.stem
        lote.publicar_bytes(f"grid/tiles/{nome}/monthly.csv", destino.read_bytes(), "tile", "", linhas[nome])
    shutil.rmtree(pasta, ignore_errors=True)
    aviso(f"tiles: {len(linhas)} blocos")


def recentes(arquivos, lote, agora):
    corte = agora - max(RECENTES.values())
    primeiro_mes = corte.year * 12 + corte.month - 1
    colunas = focos.COLUNAS_LIDAS + ["acq_datetime_utc"]
    for indice, camada in enumerate(CAMADAS):
        do_periodo = [a for a in arquivos if not agregar.eh_sp(a) and agregar.camada_do_arquivo(a) == indice
                      and agregar.mes_do_arquivo(a) >= primeiro_mes]
        partes = [pq.ParquetFile(a).read(columns=colunas).to_pandas(date_as_object=False) for a in do_periodo]
        partes = [p[p["acq_datetime_utc"] >= corte] for p in partes]
        df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=colunas)
        df = df.assign(acq_date=pd.to_datetime(df["acq_date"]), camada=camada)
        for nome, janela in RECENTES.items():
            parte = df[df["acq_datetime_utc"] >= agora - janela]
            lote.publicar(f"recent/{camada}/{nome}.csv.gz", focos.formatar_pontos(parte, focos.casas(camada)),
                          "recent", nome)


def gravado_em(arquivos):
    mais_recente = {}
    for arquivo in arquivos:
        chave = (agregar.camada_do_arquivo(arquivo), "sp" if agregar.eh_sp(arquivo) else "nrt")
        mais_recente[chave] = max(mais_recente.get(chave, 0), arquivo.stat().st_mtime)
    return {c: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v)) for c, v in mais_recente.items()}


def texto_data(serie):
    return pd.to_datetime(serie).dt.strftime("%Y-%m-%d").fillna("")


def meta(lote, cobertura, paises_resumo, tabela, validacao, validacao_mensal, mascara, gravacoes):
    cob = cobertura.assign(updated_at=[gravacoes.get((int(c), p), "") for c, p in zip(cobertura["layer"], cobertura["processing"])])
    cob = cob.assign(layer=cob["layer"].map(lambda c: CAMADAS[int(c)])).sort_values(["layer", "processing"], ignore_index=True)
    lote.publicar("meta/coverage.csv", cob.assign(first_date=texto_data(cob["first_date"]),
                                                  last_date=texto_data(cob["last_date"]),
                                                  complete_date=texto_data(cob["complete_date"]))[
        ["layer", "processing", "first_date", "last_date", "rows", "updated_at", "complete_date"]], "meta")
    paises = tabela.merge(paises_resumo.rename(columns={"country": "iso3"}), on="iso3", how="left")
    paises = paises.merge(ler_areas(), on="iso3", how="left").fillna({"area_km2": "", "area_source": ""})
    paises["rows"] = paises["rows"].fillna(0).astype(int)
    paises["first_date"] = texto_data(paises["first_date"])
    paises["last_date"] = texto_data(paises["last_date"])
    lote.publicar("meta/countries.csv", paises[["iso3", "name", "continent", "first_date", "last_date", "rows", "area_km2",
                                                "area_source"]], "meta")
    lote.publicar("meta/layers.csv", pd.DataFrame(INFO_CAMADAS, columns=["layer", "title", "instrument", "resolution_m",
                                                                         "satellites", "notes"]), "meta")
    lote.publicar("meta/validation.csv", validacao, "meta")
    lote.publicar("meta/validation_monthly.csv", validacao_mensal, "meta")
    estaticas = pd.DataFrame({"lat": [f"{v:.2f}" for v in (mascara // 40000 - 9000) / 100],
                              "lon": [f"{v:.2f}" for v in (mascara % 40000 - 18000) / 100]})
    lote.publicar("meta/static_sources.csv", estaticas, "meta")


def ler_estado():
    return json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}


def exportar(saida=SAIDA, com_tiles=False, aviso=avisar, horas_tiles=0, minutos_pontos=15, processos=3):
    inicio = time.time()
    arquivos, novos, mascara = agregar.atualizar(aviso=aviso)
    if not arquivos:
        return
    estado = ler_estado()
    trechos = cobertos(arquivos)
    operando = calendario(trechos)
    operando_mensal = operando.groupby(operando.index.strftime("%Y-%m")).any()
    marcas = marcas_mensais(operando_mensal)
    cobertura = agregar.resumir(arquivos, "cobertura", ["layer", "processing"])
    cobertura["layer"] = cobertura["layer"].astype(int)
    cobertura = fechados(trechos, continuo(trechos, cobertura))
    blocos = agregar.grade_por_bloco(arquivos, [harmonizar.MODIS, harmonizar.SNPP, harmonizar.NOAA20],
                                     (harmonizar.JANELA_K[0], harmonizar.JANELA_K20[1]))
    fatores = harmonizar.calcular(blocos, ZIPS)
    assinatura = harmonizar.assinatura(fatores)
    anteriores = ler_anteriores(saida)
    todos_meses = {agregar.mes_do_arquivo(a) for a in arquivos}
    if assinatura != estado.get("fatores") or not anteriores:
        tocados = todos_meses
    else:
        antigas = estado.get("meses", {})
        mudaram = {agregar.mes_numero_texto(m) for m, v in marcas.items() if antigas.get(m) != v}
        tocados = {agregar.mes_do_arquivo(a) for a in novos} | (mudaram & todos_meses)
    aviso(f"fatores: {assinatura}; {len(tocados)} meses para reescrever")
    lote = Lote(ultimo_lote(saida) + 1, saida, anteriores)
    for mes in sorted(tocados):
        texto = agregar.mes_texto(mes)
        lote.publicar(f"grid/monthly/{texto[:4]}/{texto}.csv", grade_do_mes(arquivos, mes, fatores, operando_mensal.loc[texto]),
                      "grid", texto)
    ultimo_tiles = estado.get("tiles", 0)
    tiles_pendentes = bool(tocados) or estado.get("tiles_pendentes", True)
    vencido = time.time() - ultimo_tiles >= horas_tiles * 3600 or assinatura != estado.get("fatores")
    if com_tiles and tiles_pendentes and vencido:
        tiles(arquivos, todos_meses, fatores, operando_mensal, lote, aviso)
        ultimo_tiles = time.time()
        tiles_pendentes = False
    dia = agregar.somar(arquivos, "dia", ["layer", "date", "country", "dn"])
    dia_k = agregar.somar(arquivos, "dia_k", ["layer", "date", "country", "bi", "bj", "dn"])
    mes_pb = agregar.somar(arquivos, "mes_pb", ["layer", "month", "country", "bi", "bj", "dn"])
    contrib = contribuicoes(dia_k, fatores)
    ctx = contexto(operando, fatores)
    for nome, corpo in series(dia, contrib, ctx).items():
        lote.publicar(f"series/global/{nome}.csv", corpo, "series")
    paises = ler_paises()
    tabela = tabela_paises(paises)
    dia_pais = dict(tuple(dia.groupby("country")))
    contrib_pais = dict(tuple(contrib.groupby("country"))) if len(contrib) else {}
    anuais = []
    for iso3 in tabela["iso3"]:
        corpos = series(dia_pais.get(iso3, dia.iloc[0:0]), contrib_pais.get(iso3, contrib.iloc[0:0]), ctx)
        for nome, corpo in corpos.items():
            lote.publicar(f"series/countries/{iso3}/{nome}.csv", corpo, "series")
        anuais.append(corpos["yearly"].assign(iso3=iso3))
    lote.publicar("series/countries_yearly.csv", pd.concat(anuais, ignore_index=True)[["iso3", "year"] + COLUNAS_CONTAGEM],
                  "series")
    recentes(arquivos, lote, pd.Timestamp.now(tz="UTC"))
    validacao, validacao_mensal = harmonizar.validacao(blocos, dia, mes_pb, fatores, ZIPS)
    meta(lote, cobertura, agregar.resumir(arquivos, "paises", ["country"]), tabela, validacao, validacao_mensal, mascara,
         gravado_em(arquivos))
    publicou = lote.fechar()
    ESTADO.write_text(json.dumps({"fatores": assinatura, "lote": lote.numero if publicou else estado.get("lote"),
                                     "meses": marcas, "tiles": ultimo_tiles, "tiles_pendentes": tiles_pendentes}),
                      encoding="utf-8")
    aviso(f"lote {lote.numero if publicou else '-'}: {len(lote.arquivos)} arquivos mudaram em {time.time() - inicio:.0f} s")
    if minutos_pontos:
        return focos.exportar_focos(arquivos, trechos, saida, minutos_pontos, processos, aviso)
    return False


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m heru exportar")
    p.add_argument("--saida", type=Path, default=SAIDA)
    p.add_argument("--tiles", action="store_true")
    p.add_argument("--horas-tiles", type=float, default=0, help="intervalo mínimo entre refações dos tiles")
    p.add_argument("--a-cada", type=int, default=0, help="minutos entre exportações; 0 roda uma vez")
    p.add_argument("--minutos-pontos", type=float, default=15, help="tempo máximo gravando points/ por exportação")
    p.add_argument("--processos", type=int, default=3)
    args = p.parse_args(argv)
    with trava(TRAVA_EXPORTAR):
        manter_acordado()
        while True:
            atrasado = exportar(args.saida, args.tiles, horas_tiles=args.horas_tiles,
                                minutos_pontos=args.minutos_pontos, processos=args.processos)
            if not args.a_cada:
                break
            if atrasado:
                continue
            time.sleep(args.a_cada * 60)
