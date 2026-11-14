import gzip
import json
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from heru import agregar
from heru.config import CAMADAS, COLUNAS_PONTOS, SAIDA
from heru.publicar import Lote, agora, gravar_bytes, ler_anteriores, ultimo_lote

ESTADO_FOCOS = agregar.AGREGADOS / "focos.json"
COLUNAS_LIDAS = ["satelite", "processamento", "origem", "latitude", "longitude", "acq_date", "acq_time", "daynight",
                 "conf_raw", "conf_class", "frp", "brilho", "scan", "track", "type", "country"]


def formatar_pontos(df):
    df = df.assign(latitude=df["latitude"].round(5), longitude=df["longitude"].round(5))
    df = df.sort_values(["acq_date", "acq_time", "latitude", "longitude"], ignore_index=True)
    saida = pd.DataFrame({
        "latitude": [f"{v:.5f}" for v in df["latitude"]],
        "longitude": [f"{v:.5f}" for v in df["longitude"]],
        "acq_date": df["acq_date"].dt.strftime("%Y-%m-%d"),
        "acq_time": df["acq_time"].astype(str).str.zfill(4),
        "layer": df["camada"],
        "satellite": df["satelite"],
        "country": df["country"],
        "daynight": df["daynight"],
        "confidence": df["conf_class"],
        "confidence_raw": df["conf_raw"],
        "frp": df["frp"].astype("float64").round(2),
        "brightness": df["brilho"].astype("float64").round(2),
        "scan": df["scan"].astype("float64").round(2),
        "track": df["track"].astype("float64").round(2),
        "type": df["type"],
        "processing": df["processamento"],
        "source": df["origem"],
    })
    return saida[COLUNAS_PONTOS]


def corpo_do_dia(tabela, camada, dia):
    parte = tabela.filter(pc.equal(tabela["acq_date"], pa.scalar(date.fromisoformat(dia), pa.date32())))
    df = parte.to_pandas(date_as_object=False)
    df["acq_date"] = df["acq_date"].astype("datetime64[ns]")
    df["camada"] = camada
    texto = formatar_pontos(df).to_csv(index=False, lineterminator="\n", na_rep="").encode("utf-8")
    return gzip.compress(texto, compresslevel=6, mtime=0), len(df)


def escrever(caminho, camada, dias, saida):
    tabela = pq.ParquetFile(caminho).read(columns=COLUNAS_LIDAS)
    registros = []
    for dia in dias:
        corpo, linhas = corpo_do_dia(tabela, camada, dia)
        registro = gravar_bytes(f"points/{camada}/{dia[:4]}/{dia}.csv.gz", corpo, Path(saida))
        registro.update(rows=linhas, kind="points", period=dia, updated_at=agora())
        registros.append(registro)
    return registros


def ler_estado():
    return json.loads(ESTADO_FOCOS.read_text(encoding="utf-8")) if ESTADO_FOCOS.exists() else {}


def pendentes(arquivos, trechos, estado):
    sp = trechos[trechos["processing"] == "sp"]
    dias_sp = {(linha.layer, d) for linha in sp.itertuples() for d in pd.date_range(linha.inicio, linha.fim)}
    tarefas = []
    for arquivo, linha in zip(arquivos, trechos.itertuples()):
        relativo = arquivo.relative_to(agregar.PONTOS).as_posix()
        marca = agregar.assinatura(arquivo)
        if estado.get(relativo) == marca:
            continue
        dias = [d for d in pd.date_range(linha.inicio, linha.fim)
                if linha.processing == "sp" or (linha.layer, d) not in dias_sp]
        tarefas.append({"arquivo": str(arquivo), "relativo": relativo, "marca": marca, "camada": CAMADAS[linha.layer],
                        "dias": [d.strftime("%Y-%m-%d") for d in dias], "fim": linha.fim})
    return sorted(tarefas, key=lambda t: (t["fim"], t["relativo"]), reverse=True)


def exportar_focos(arquivos, trechos, saida=SAIDA, minutos=15, processos=3, aviso=print):
    estado = ler_estado()
    tarefas = pendentes(arquivos, trechos, estado)
    if not tarefas:
        return False
    inicio = time.time()
    limite = inicio + minutos * 60
    lote = Lote(ultimo_lote(saida) + 1, saida, ler_anteriores(saida))
    feitas = []
    fila = list(tarefas)
    if processos <= 1:
        while fila and time.time() < limite:
            tarefa = fila.pop(0)
            lote.arquivos.extend(escrever(tarefa["arquivo"], tarefa["camada"], tarefa["dias"], str(saida)))
            feitas.append(tarefa)
    else:
        with ProcessPoolExecutor(processos) as pool:
            rodando = {}
            while fila or rodando:
                while fila and len(rodando) < processos and time.time() < limite:
                    tarefa = fila.pop(0)
                    rodando[pool.submit(escrever, tarefa["arquivo"], tarefa["camada"], tarefa["dias"], str(saida))] = tarefa
                if not rodando:
                    break
                prontos, _ = wait(rodando, return_when=FIRST_COMPLETED)
                for futuro in prontos:
                    lote.arquivos.extend(futuro.result())
                    feitas.append(rodando.pop(futuro))
    lote.fechar()
    for tarefa in feitas:
        estado[tarefa["relativo"]] = tarefa["marca"]
    ESTADO_FOCOS.write_text(json.dumps(estado), encoding="utf-8")
    focos = sum(r["rows"] for r in lote.arquivos if r["kind"] == "points")
    aviso(f"pontos: lote {lote.numero}, {len(feitas)} arquivos do store, {focos:,} focos em {time.time() - inicio:.0f} s; "
          f"faltam {len(tarefas) - len(feitas)}")
    return len(feitas) < len(tarefas)
