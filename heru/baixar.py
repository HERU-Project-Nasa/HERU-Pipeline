import argparse
import csv
import ctypes
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone

import requests

from heru import api, tarefas
from heru.config import CAMADAS, LIMITE_SEGURO, MANIFEST, PONTOS, TRAVA
from heru.cota import Cota
from heru.fronteira import Fronteira, ler_paises
from heru.padronizar import CHAVE, ler_csv, padronizar

CAMPOS_MANIFEST = ["quando_utc", "camada", "processamento", "origem", "inicio", "dias",
                   "linhas", "repetidos", "sem_pais", "mb", "custo", "segundos", "arquivo"]
SINCRONIZAR_A_CADA = 20
TENTATIVAS = 3


def avisar(texto):
    print(f"{datetime.now().strftime('%H:%M:%S')} {texto}", flush=True)


def hoje_utc():
    return datetime.now(timezone.utc).date()


def manter_acordado():
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)


@contextmanager
def trava(caminho=TRAVA):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(caminho, "x", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
    except FileExistsError:
        sys.exit(f"outra execução está rodando ({caminho.read_text().strip()}); se não estiver, apague {caminho}")
    try:
        yield
    finally:
        caminho.unlink(missing_ok=True)


def gravar_parquet(df, destino):
    destino.parent.mkdir(parents=True, exist_ok=True)
    temp = destino.with_name(destino.name + ".part")
    df.to_parquet(temp, index=False)
    temp.replace(destino)


def anotar(linha, manifest=MANIFEST):
    novo = not manifest.exists()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest, "a", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=CAMPOS_MANIFEST)
        if novo:
            escritor.writeheader()
        escritor.writerow(linha)


def conferir_datas(df, tarefa):
    fora = df[(df["acq_date"] < tarefa.inicio) | (df["acq_date"] > tarefa.fim)]
    if len(fora):
        raise ValueError(f"{tarefa.fonte} {tarefa.inicio} +{tarefa.dias}d: {len(fora)} focos fora da janela")


def chamar(chave, tarefa, cota):
    while True:
        cota.esperar(tarefa.custo)
        try:
            texto = api.area(chave, tarefa.fonte, tarefa.area, tarefa.dias, tarefa.inicio.isoformat())
            cota.registrar(tarefa.custo)
            return texto
        except api.ErroAPI as erro:
            if not api.erro_de_limite(erro):
                raise
            avisar(f"FIRMS recusou por cota ({erro}); esperando a janela de 10 min esvaziar")
            cota.registrar(cota.limite)


def baixar_janela(chave, tarefa, cota, fronteira, pasta):
    texto = chamar(chave, tarefa, cota)
    df = padronizar(ler_csv(texto), tarefa.camada, tarefa.processamento, "api")
    conferir_datas(df, tarefa)
    antes = len(df)
    df = df.drop_duplicates(CHAVE, ignore_index=True)
    if len(df):
        df["country"] = fronteira.paises(df["latitude"].to_numpy(), df["longitude"].to_numpy())
    destino = tarefas.caminho_arquivo(tarefa, pasta)
    gravar_parquet(df, destino)
    return df, antes - len(df), destino


def executar(lista, chave, cota, fronteira, pasta=PONTOS, manifest=MANIFEST):
    custo_total = sum(t.custo for t in lista)
    gasto = 0
    falhas = []
    for i, tarefa in enumerate(lista, 1):
        for tentativa in range(1, TENTATIVAS + 1):
            inicio = time.time()
            try:
                df, repetidos, destino = baixar_janela(chave, tarefa, cota, fronteira, pasta)
                break
            except (requests.RequestException, api.ErroAPI, ValueError) as erro:
                avisar(f"{tarefa.camada} {tarefa.processamento} {tarefa.inicio}: falhou ({erro}), tentativa {tentativa}")
                if tentativa == TENTATIVAS:
                    falhas.append(tarefa)
                    df = None
                else:
                    time.sleep(60 * tentativa)
        if df is None:
            continue
        gasto += tarefa.custo
        anotar({"quando_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "camada": tarefa.camada, "processamento": tarefa.processamento, "origem": "api",
                "inicio": tarefa.inicio.isoformat(), "dias": tarefa.dias, "linhas": len(df),
                "repetidos": repetidos, "sem_pais": int((df["country"] == "").sum()),
                "mb": round(destino.stat().st_size / 1e6, 2), "custo": tarefa.custo,
                "segundos": round(time.time() - inicio, 1), "arquivo": destino.relative_to(pasta).as_posix()},
               manifest)
        avisar(f"[{i}/{len(lista)}] {tarefa.camada} {tarefa.processamento} {tarefa.inicio} +{tarefa.dias}d: "
               f"{len(df):,} focos, cota {cota.usado()}/{cota.limite}, {gasto:,}/{custo_total:,}")
        if i % SINCRONIZAR_A_CADA == 0:
            extra = cota.sincronizar(api.status(chave).get("current_transactions", 0))
            if extra > 0:
                avisar(f"FIRMS contou {extra} transações a mais que o registro local")
    return gasto, falhas


def resumo(todas, pendentes):
    custo = sum(t.custo for t in pendentes)
    print(f"{len(todas)} janelas, {len(pendentes)} pendentes, {custo:,} transações "
          f"(~{custo / LIMITE_SEGURO * 10 / 60:.1f} h de cota)")
    grupos = {}
    for t in pendentes:
        chave = (t.camada, t.processamento)
        n, c, ini, fim = grupos.get(chave, (0, 0, t.inicio, t.fim))
        grupos[chave] = (n + 1, c + t.custo, min(ini, t.inicio), max(fim, t.fim))
    for (camada, processamento), (n, c, ini, fim) in sorted(grupos.items()):
        print(f"  {camada:13s} {processamento:3s} {ini} a {fim}: {n:5d} janelas, {c:>8,} transações")
    acumulado = 0
    for t in pendentes:
        acumulado += t.custo
        if t.camada == "viirs_noaa20" and t.processamento == "SP" and t.inicio == tarefas.INICIO_K20:
            print(f"  NOAA-20 SP de 2019 completo após {acumulado:,} transações "
                  f"(~{acumulado / LIMITE_SEGURO * 10 / 60:.1f} h)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m heru historico")
    p.add_argument("--camadas", help="separadas por vírgula: " + ",".join(CAMADAS))
    p.add_argument("--inicio", type=date.fromisoformat)
    p.add_argument("--fim", type=date.fromisoformat)
    p.add_argument("--simular", action="store_true")
    args = p.parse_args(argv)
    chave = api.carregar_map_key()
    if not chave:
        sys.exit("sem MAP_KEY: crie .env com MAP_KEY=sua_chave")
    camadas = args.camadas.split(",") if args.camadas else None
    todas = tarefas.plano(api.disponibilidade(chave), hoje_utc(), camadas, args.inicio, args.fim, tarefas.ultimo_zip())
    pendentes = tarefas.pendentes(todas)
    resumo(todas, pendentes)
    if args.simular or not pendentes:
        return
    with trava():
        manter_acordado()
        fronteira = Fronteira(ler_paises())
        cota = Cota(aviso=avisar)
        cota.sincronizar(api.status(chave).get("current_transactions", 0))
        inicio = time.time()
        try:
            gasto, falhas = executar(pendentes, chave, cota, fronteira)
            if falhas:
                avisar(f"refazendo {len(falhas)} janelas que falharam")
                extra, falhas = executar(falhas, chave, cota, fronteira)
                gasto += extra
        except KeyboardInterrupt:
            print(f"\ninterrompido: {cota.total:,} transações gastas; rode de novo para continuar")
            raise SystemExit(130)
        avisar(f"pronto: {gasto:,} transações em {(time.time() - inicio) / 3600:.1f} h; {len(falhas)} janelas com falha")
        if falhas:
            raise SystemExit(1)
