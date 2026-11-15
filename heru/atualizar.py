import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pyarrow.parquet as pq
import requests

from heru import agregar, api, baixar, exportar, tarefas
from heru.config import CAMADAS, FONTES, TRAVA
from heru.cota import Cota
from heru.fronteira import Fronteira, ler_paises
from heru.padronizar import CHAVE, ler_csv, padronizar

BASE_CSV = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
CSV_GLOBAIS = {
    "modis": ["modis-c6.1/csv/MODIS_C6_1_Global_{janela}.csv"],
    "viirs_noaa20": ["noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_{janela}.csv"],
    "viirs_noaa21": ["noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_{janela}.csv"],
    "landsat": ["landsat/csv/LANDSAT_USA_contiguous_and_Hawaii_{janela}.csv",
                "landsat/csv/LANDSAT_Canada_{janela}.csv"],
}
ATRASO_MAXIMO = timedelta(days=1)


def caminho_estado():
    return agregar.AGREGADOS / "atualizar.json"


def ler_estado():
    caminho = caminho_estado()
    return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {}


def gravar_estado(estado):
    caminho = caminho_estado()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(estado), encoding="utf-8")


def baixar_csv_global(camada, janela):
    partes = [ler_csv(api.buscar(BASE_CSV + p.format(janela=janela))) for p in CSV_GLOBAIS[camada]]
    return pd.concat(partes, ignore_index=True)


def janela_nrt(camada, inicio):
    fonte = FONTES[camada]
    fim_janela = tarefas.fim_da_janela(inicio)
    return tarefas.Tarefa(camada, "NRT", fonte.nrt, fonte.area, inicio, (fim_janela - inicio).days + 1, fim_janela, 0)


def chaves(df):
    return pd.MultiIndex.from_frame(df[CHAVE])


def mesclar(df, camada, quando):
    pasta, manifest = agregar.PONTOS, agregar.MANIFEST
    registro = agregar.baixados()
    ultimo = max(df["acq_date"])
    gravadas = []
    for inicio, parte in df.groupby(df["acq_date"].map(tarefas.inicio_da_janela)):
        tarefa = janela_nrt(camada, inicio)
        destino = tarefas.caminho_arquivo(tarefa, pasta)
        relativo = destino.relative_to(pasta).as_posix()
        antigo = pq.ParquetFile(destino).read().to_pandas() if destino.exists() else parte.iloc[0:0]
        novos = int((~chaves(parte).isin(chaves(antigo))).sum())
        dias_antes = registro.get(relativo, (0, None))[0] if destino.exists() else 0
        dias = max(dias_antes, (min(tarefa.fim_janela, ultimo) - inicio).days + 1)
        if not novos and dias == dias_antes:
            continue
        junto = pd.concat([antigo, parte], ignore_index=True).drop_duplicates(CHAVE, keep="last")
        junto = junto.sort_values(["acq_datetime_utc", "latitude", "longitude"], ignore_index=True)
        baixar.gravar_parquet(junto, destino)
        baixar.anotar({"quando_utc": quando.isoformat(timespec="seconds"), "camada": camada, "processamento": "NRT",
                       "origem": "csv_global", "inicio": inicio.isoformat(), "dias": dias, "linhas": len(junto),
                       "repetidos": 0, "sem_pais": int((junto["country"] == "").sum()),
                       "mb": round(destino.stat().st_size / 1e6, 2), "custo": 0, "segundos": 0, "arquivo": relativo},
                      manifest)
        gravadas.append((relativo, novos))
    return gravadas


def fechado_ate(camada):
    registro = agregar.baixados()
    indice = CAMADAS.index(camada)
    fechados = [agregar.dias_do_arquivo(a, registro)[2] for a in agregar.arquivos_do_store()
                if agregar.camada_do_arquivo(a) == indice]
    return max(fechados).date() if fechados else None


def janelas_do_buraco(camada, fechado, primeiro):
    if fechado is None or primeiro <= fechado:
        return []
    fonte = FONTES[camada]
    lista = []
    inicio = tarefas.inicio_da_janela(fechado + timedelta(days=1))
    while inicio <= primeiro:
        fim_janela = tarefas.fim_da_janela(inicio)
        dias = (min(fim_janela, primeiro) - inicio).days + 1
        lista.append(tarefas.Tarefa(camada, "NRT", fonte.nrt, fonte.area, inicio, dias, fim_janela, fonte.custo_dia * dias))
        inicio = fim_janela + timedelta(days=1)
    return lista


def preencher_buraco(camada, primeiro, chave, fronteira, aviso):
    fechado = fechado_ate(camada)
    lista = janelas_do_buraco(camada, fechado, primeiro)
    if not lista:
        return True
    falta = fechado + timedelta(days=1)
    aviso(f"{camada}: buraco de {falta} a {primeiro} fora do CSV de 7 dias; {len(lista)} janelas pela API, "
          f"{sum(t.custo for t in lista):,} transações")
    if not chave:
        aviso(f"ATRASO: {camada} sem MAP_KEY para o buraco; rode historico --inicio {falta}")
        return False
    cota = Cota(aviso=aviso)
    cota.sincronizar(api.status(chave).get("current_transactions", 0))
    gasto, falhas = baixar.executar(lista, chave, cota, fronteira, agregar.PONTOS, agregar.MANIFEST)
    aviso(f"{camada}: buraco preenchido com {gasto:,} transações, {len(falhas)} janelas com falha")
    return not falhas


def tempo_real(janela, fronteira, aviso, buraco=False, chave=None):
    quando = datetime.now(timezone.utc)
    ultimos = {}
    for camada in CSV_GLOBAIS:
        try:
            bruto = baixar_csv_global(camada, janela)
        except (requests.RequestException, api.ErroAPI) as erro:
            aviso(f"{camada}: CSV global de {janela} falhou ({erro})")
            continue
        df = padronizar(bruto, camada, "NRT", "csv_global")
        antes = len(df)
        df = df.drop_duplicates(CHAVE, ignore_index=True)
        if df.empty:
            aviso(f"{camada}: CSV global de {janela} veio vazio")
            continue
        df["country"] = fronteira.paises(df["latitude"].to_numpy(), df["longitude"].to_numpy())
        if buraco:
            try:
                preencher_buraco(camada, min(df["acq_date"]), chave, fronteira, aviso)
            except (requests.RequestException, api.ErroAPI, ValueError) as erro:
                aviso(f"ATRASO: {camada} com buraco que a API não preencheu ({erro})")
        gravadas = mesclar(df, camada, quando)
        ultimos[camada] = max(df["acq_date"])
        aviso(f"{camada}: {len(df):,} focos no CSV de {janela} ({antes - len(df):,} repetidos), "
              f"{sum(n for _, n in gravadas):,} novos em {len(gravadas)} janelas; último dia {ultimos[camada]}")
    return ultimos


def atrasos(ultimos, hoje):
    return [c for c in CSV_GLOBAIS if c not in ultimos or hoje - ultimos[c] > ATRASO_MAXIMO]


def nrt_coberto(arquivos):
    registro = agregar.baixados()
    dias_sp = {}
    for arquivo in arquivos:
        if agregar.eh_sp(arquivo):
            inicio, fim, _ = agregar.dias_do_arquivo(arquivo, registro)
            dias_sp.setdefault(agregar.camada_do_arquivo(arquivo), set()).update(pd.date_range(inicio, fim))
    cobertos = []
    for arquivo in arquivos:
        if not agregar.eh_sp(arquivo):
            inicio, fim, _ = agregar.dias_do_arquivo(arquivo, registro)
            if set(pd.date_range(inicio, fim)) <= dias_sp.get(agregar.camada_do_arquivo(arquivo), set()):
                cobertos.append(arquivo)
    return cobertos


def trocar_por_sp(chave, fronteira, aviso):
    pasta = agregar.PONTOS
    disp = api.disponibilidade(chave)
    fim_sp = ", ".join(f"{c} {tarefas.datas(disp, f.sp)[1]}" for c, f in FONTES.items() if f.sp)
    todas = tarefas.plano(disp, baixar.hoje_utc())
    novas = [t for t in tarefas.pendentes(todas, pasta) if t.processamento == "SP"]
    aviso(f"SP disponível até {fim_sp}; {len(novas)} janelas novas, {sum(t.custo for t in novas):,} transações")
    if novas:
        cota = Cota(aviso=aviso)
        cota.sincronizar(api.status(chave).get("current_transactions", 0))
        gasto, falhas = baixar.executar(novas, chave, cota, fronteira, pasta, agregar.MANIFEST)
        aviso(f"SP novo: {gasto:,} transações, {len(falhas)} janelas com falha")
    velhos = nrt_coberto(agregar.arquivos_do_store())
    for arquivo in velhos:
        arquivo.unlink()
    aviso(f"{len(velhos)} arquivos NRT trocados pelo SP")


def coletar(fronteira, diario, com_sp, aviso):
    hoje = baixar.hoje_utc()
    chave = api.carregar_map_key() if diario else None
    ultimos = tempo_real("7d" if diario else "48h", fronteira, aviso, buraco=diario, chave=chave)
    if diario and com_sp:
        if not chave:
            aviso("sem MAP_KEY: a troca do NRT pelo SP fica para depois")
        else:
            try:
                trocar_por_sp(chave, fronteira, aviso)
            except (requests.RequestException, api.ErroAPI, ValueError) as erro:
                aviso(f"troca do NRT pelo SP falhou ({erro}); tenta de novo amanhã")
    for camada in atrasos(ultimos, hoje):
        aviso(f"ATRASO: {camada} com último dia {ultimos.get(camada, 'desconhecido')} no CSV global")
    return ultimos


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m heru atualizar")
    p.add_argument("--a-cada", type=int, default=0, help="minutos entre coletas; 0 roda uma vez")
    p.add_argument("--diario", action="store_true", help="força os CSVs de 7 dias e a troca do NRT pelo SP")
    p.add_argument("--sem-sp", action="store_true", help="não troca o NRT pelo SP")
    p.add_argument("--tiles", action="store_true")
    p.add_argument("--horas-tiles", type=float, default=0, help="intervalo mínimo entre refações dos tiles")
    p.add_argument("--minutos-pontos", type=float, default=15, help="tempo máximo gravando points/ por exportação")
    p.add_argument("--processos", type=int, default=3)
    args = p.parse_args(argv)
    with baixar.trava(TRAVA), baixar.trava(exportar.TRAVA_EXPORTAR):
        baixar.manter_acordado()
        fronteira = Fronteira(ler_paises())
        forcar = args.diario
        proxima = time.time()
        while True:
            if time.time() >= proxima:
                proxima = time.time() + args.a_cada * 60
                estado = ler_estado()
                hoje = baixar.hoje_utc().isoformat()
                diario = forcar or estado.get("diario") != hoje
                coletar(fronteira, diario, not args.sem_sp, baixar.avisar)
                if diario:
                    gravar_estado({**estado, "diario": hoje})
                forcar = False
            atrasado = exportar.exportar(com_tiles=args.tiles, horas_tiles=args.horas_tiles,
                                         minutos_pontos=args.minutos_pontos, processos=args.processos)
            if not args.a_cada:
                break
            if not atrasado and proxima > time.time():
                time.sleep(proxima - time.time())
