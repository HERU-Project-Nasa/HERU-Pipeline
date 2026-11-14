import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from heru.config import CAMADAS, FONTES, MAX_DIAS, PONTOS, ULTIMO_ANO_ZIP

FOLGA_NRT = timedelta(hours=6)
INICIO_K20 = date(2019, 1, 1)


@dataclass(frozen=True)
class Tarefa:
    camada: str
    processamento: str
    fonte: str
    area: str
    inicio: date
    dias: int
    fim_janela: date
    custo: int

    @property
    def fim(self):
        return self.inicio + timedelta(days=self.dias - 1)


def caminho_arquivo(tarefa, pasta=PONTOS):
    return Path(pasta, f"camada={tarefa.camada}", f"ano={tarefa.inicio.year}", f"mes={tarefa.inicio.month:02d}",
                f"{tarefa.processamento}_{tarefa.inicio.isoformat()}.parquet")


def fim_do_mes(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def inicio_da_janela(d, passo=MAX_DIAS):
    return d.replace(day=((d.day - 1) // passo) * passo + 1)


def fim_da_janela(d, passo=MAX_DIAS):
    return min(inicio_da_janela(d, passo) + timedelta(days=passo - 1), fim_do_mes(d))


def janelas(inicio, fim, passo=MAX_DIAS):
    d = inicio
    while d <= fim:
        fim_janela = fim_da_janela(d, passo)
        ultimo = min(fim_janela, fim)
        yield d, (ultimo - d).days + 1, fim_janela
        d = ultimo + timedelta(days=1)


def datas(disp, fonte):
    linha = disp[disp["data_id"] == fonte]
    if linha.empty:
        raise KeyError(f"fonte {fonte} não aparece em data_availability")
    return date.fromisoformat(linha["min_date"].iloc[0]), date.fromisoformat(linha["max_date"].iloc[0])


def periodos(disp, camada, hoje):
    fonte = FONTES[camada]
    saida = []
    fim_sp = None
    if fonte.sp:
        inicio_sp, fim_sp = datas(disp, fonte.sp)
        if fonte.tem_zip:
            inicio_sp = max(inicio_sp, date(ULTIMO_ANO_ZIP + 1, 1, 1))
        saida.append(("SP", fonte.sp, inicio_sp, min(fim_sp, hoje)))
    inicio_nrt, fim_nrt = datas(disp, fonte.nrt)
    if fim_sp:
        inicio_nrt = max(inicio_nrt, fim_sp + timedelta(days=1))
    saida.append(("NRT", fonte.nrt, inicio_nrt, min(fim_nrt, hoje)))
    return [p for p in saida if p[2] <= p[3]]


def prioridade(tarefa, corte):
    if tarefa.processamento == "NRT":
        return 0 if tarefa.inicio >= corte else 3
    if tarefa.camada in ("modis", "viirs_snpp") or (tarefa.camada == "viirs_noaa20" and tarefa.inicio >= INICIO_K20):
        return 1
    return 2


def plano(disp, hoje, camadas=None, inicio=None, fim=None):
    inicio = inicio_da_janela(inicio) if inicio else None
    fim = fim_da_janela(fim) if fim else None
    tarefas = []
    cortes = []
    for nome in camadas or CAMADAS:
        fonte = FONTES[nome]
        for processamento, codigo, ini, fi in periodos(disp, nome, hoje):
            if processamento == "NRT" and fonte.sp:
                cortes.append(ini)
            ini = max(ini, inicio) if inicio else ini
            fi = min(fi, fim) if fim else fi
            for d, dias, fim_janela in janelas(ini, fi):
                tarefas.append(Tarefa(nome, processamento, codigo, fonte.area, d, dias, fim_janela,
                                      fonte.custo_dia * dias))
    corte = min(cortes, default=hoje)
    return sorted(tarefas, key=lambda t: (prioridade(t, corte), -t.inicio.toordinal(), CAMADAS.index(t.camada)))


def completo(tarefa, pasta=PONTOS):
    arquivo = caminho_arquivo(tarefa, pasta)
    if not arquivo.exists():
        return False
    if tarefa.processamento == "SP":
        return True
    gravado = datetime.fromtimestamp(arquivo.stat().st_mtime, tz=timezone.utc)
    fechou = datetime.combine(tarefa.fim_janela + timedelta(days=1), time(0), tzinfo=timezone.utc)
    return gravado >= fechou + FOLGA_NRT


def pendentes(tarefas, pasta=PONTOS):
    return [t for t in tarefas if not completo(t, pasta)]
