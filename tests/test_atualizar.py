import gzip
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from heru import agregar, atualizar, exportar, tarefas
from heru.padronizar import ler_csv, padronizar
from heru.publicar import Lote

CABECALHO_VIIRS = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight\n"
QUANDO = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def texto_viirs(linhas):
    return CABECALHO_VIIRS + "".join(f"{lat},-64.00000,330.1,0.4,0.4,{dia},{hora},N20,nominal,2.0NRT,290.2,5.3,N\n"
                                     for lat, dia, hora in linhas)


def viirs(linhas, origem="csv_global"):
    df = padronizar(ler_csv(texto_viirs(linhas)), "viirs_noaa20", "NRT", origem)
    df["country"] = "BOL"
    return df


class FronteiraFalsa:
    def paises(self, lat, lon):
        return np.where(lat < -10, "BOL", "")


@pytest.fixture
def loja(tmp_path, monkeypatch):
    monkeypatch.setattr(agregar, "PONTOS", tmp_path / "pontos")
    monkeypatch.setattr(agregar, "MANIFEST", tmp_path / "manifest.csv")
    monkeypatch.setattr(agregar, "AGREGADOS", tmp_path / "agregados")
    return tmp_path


def janela(camada, inicio, processamento="NRT"):
    tarefa = tarefas.Tarefa(camada, processamento, "", "world", inicio, 5, tarefas.fim_da_janela(inicio), 0)
    return tarefas.caminho_arquivo(tarefa, agregar.PONTOS)


def manifest_de(arquivo):
    tabela = pd.read_csv(agregar.MANIFEST)
    return tabela[tabela["arquivo"] == arquivo.relative_to(agregar.PONTOS).as_posix()].iloc[-1]


def test_csv_global_completa_a_janela_da_api_sem_repetir(loja):
    destino = janela("viirs_noaa20", date(2026, 11, 11))
    destino.parent.mkdir(parents=True)
    viirs([(-16.5, "2026-11-11", "0412"), (-16.6, "2026-11-12", "0500")], "api").to_parquet(destino, index=False)
    novo = viirs([(-16.5, "2026-11-11", "0412"), (-16.7, "2026-11-13", "0600"), (-16.8, "2026-11-16", "0100")])
    gravadas = atualizar.mesclar(novo, "viirs_noaa20", QUANDO)
    assert sorted(n for _, n in gravadas) == [1, 1]
    junto = pq.ParquetFile(destino).read().to_pandas()
    assert len(junto) == 3
    assert junto["origem"].tolist() == ["csv_global", "api", "csv_global"]
    assert manifest_de(destino)["dias"] == 5
    seguinte = janela("viirs_noaa20", date(2026, 11, 16))
    assert len(pq.ParquetFile(seguinte).read().to_pandas()) == 1
    assert manifest_de(seguinte)["dias"] == 1


def test_csv_sem_foco_novo_nao_regrava(loja):
    novo = viirs([(-16.5, "2026-11-11", "0412")])
    atualizar.mesclar(novo, "viirs_noaa20", QUANDO)
    antes = janela("viirs_noaa20", date(2026, 11, 11)).stat().st_mtime_ns
    assert atualizar.mesclar(novo, "viirs_noaa20", QUANDO) == []
    assert janela("viirs_noaa20", date(2026, 11, 11)).stat().st_mtime_ns == antes


def test_tempo_real_normaliza_modis_e_marca_pais(loja, monkeypatch):
    modis = ("latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,confidence,version,bright_t31,frp,daynight\n"
             "-16.5,-64.0,306.5,2.1,1.4,2026-11-15,0017,T,68,6.1NRT,289.0,22.5,N\n"
             "40.0,-30.0,306.5,2.1,1.4,2026-11-15,0017,A,90,6.1NRT,289.0,22.5,D\n")
    monkeypatch.setattr(atualizar, "CSV_GLOBAIS", {"modis": ["x"]})
    monkeypatch.setattr(atualizar, "baixar_csv_global", lambda camada, janela: ler_csv(modis))
    ultimos = atualizar.tempo_real("48h", FronteiraFalsa(), lambda _: None)
    assert ultimos == {"modis": date(2026, 11, 15)}
    gravado = pq.ParquetFile(janela("modis", date(2026, 11, 11))).read().to_pandas()
    assert gravado["satelite"].tolist() == ["Terra", "Aqua"]
    assert gravado["conf_class"].tolist() == ["nominal", "high"]
    assert gravado["country"].tolist() == ["BOL", ""]


def test_atraso_quando_o_csv_para_ou_falha():
    ultimos = {"modis": date(2026, 11, 14), "viirs_noaa20": date(2026, 11, 13)}
    assert atualizar.atrasos(ultimos, date(2026, 11, 15)) == ["viirs_noaa20", "viirs_noaa21", "landsat"]


def test_nrt_sai_so_quando_o_sp_cobre_todos_os_dias(loja):
    for inicio, processamento in ((date(2026, 8, 1), "SP"), (date(2026, 8, 1), "NRT"), (date(2026, 8, 6), "NRT")):
        destino = janela("viirs_noaa20", inicio, processamento)
        destino.parent.mkdir(parents=True, exist_ok=True)
        viirs([(-16.5, inicio.isoformat(), "0412")]).to_parquet(destino, index=False)
    cobertos = atualizar.nrt_coberto(agregar.arquivos_do_store())
    assert [a.name for a in cobertos] == ["NRT_2026-08-01.parquet"]


def test_recent_corta_pela_hora_da_passagem(loja):
    agora = pd.Timestamp("2026-11-16 12:00", tz="UTC")
    linhas = [(-16.1, "2026-11-16", "1000"), (-16.2, "2026-11-15", "0600"), (-16.3, "2026-11-13", "1200"),
              (-16.4, "2026-11-08", "1100")]
    for inicio in (date(2026, 11, 6), date(2026, 11, 11), date(2026, 11, 16)):
        parte = [l for l in linhas if tarefas.inicio_da_janela(date.fromisoformat(l[1])) == inicio]
        destino = janela("viirs_noaa20", inicio)
        destino.parent.mkdir(parents=True, exist_ok=True)
        viirs(parte).to_parquet(destino, index=False)
    saida = loja / "saida"
    lote = Lote(1, saida)
    exportar.recentes(agregar.arquivos_do_store(), lote, agora)
    contagem = {r["path"]: r["rows"] for r in lote.arquivos}
    assert contagem["recent/viirs_noaa20/24h.csv.gz"] == 1
    assert contagem["recent/viirs_noaa20/48h.csv.gz"] == 2
    assert contagem["recent/viirs_noaa20/7d.csv.gz"] == 3
    assert contagem["recent/viirs_snpp/24h.csv.gz"] == 0
    texto = gzip.decompress((saida / "recent/viirs_noaa20/48h.csv.gz").read_bytes()).decode()
    assert texto.splitlines()[1].startswith("-16.20000,-64.00000,2026-11-15,0600,viirs_noaa20,N20,BOL")
