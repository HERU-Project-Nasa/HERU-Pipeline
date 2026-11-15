from datetime import date, timedelta

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from heru import api, baixar, tarefas
from heru.cota import Cota
from heru.padronizar import COLUNAS, ler_csv, padronizar

DISP = pd.DataFrame({
    "data_id": ["MODIS_SP", "MODIS_NRT", "VIIRS_SNPP_SP", "VIIRS_SNPP_NRT", "VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT",
                "VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT", "LANDSAT_NRT"],
    "min_date": ["2000-11-01", "2026-09-01", "2012-01-20", "2026-08-01", "2018-04-01", "2026-08-01",
                 "2023-03-01", "2024-01-17", "2022-06-20"],
    "max_date": ["2026-08-31", "2026-11-14", "2026-07-31", "2026-11-02", "2026-07-31", "2026-11-14",
                 "2026-07-31", "2026-11-14", "2026-11-14"],
})
HOJE = date(2026, 11, 14)

CSV_VIIRS = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight,type
-16.50000,-64.00000,330.1,0.4,0.4,2026-07-01,0412,N20,VIIRS,n,2.0NRT,290.2,5.3,N,0
-16.50000,-64.00000,330.1,0.4,0.4,2026-07-01,0412,N20,VIIRS,n,2.0NRT,290.2,5.3,N,0
0.00000,-30.00000,310.0,0.4,0.4,2026-07-02,1530,N20,VIIRS,h,2.0NRT,290.0,1.0,D,3
"""


class FronteiraFalsa:
    def paises(self, lat, lon):
        return np.where(lat < -10, "BOL", "")


def test_janelas_cobrem_tudo_sem_cruzar_o_mes():
    inicio, fim = date(2026, 1, 29), date(2026, 3, 7)
    dias = []
    for d, n, fim_janela in tarefas.janelas(inicio, fim):
        assert 1 <= n <= 5
        assert (d + timedelta(days=n - 1)).month == d.month == fim_janela.month
        dias += [d + timedelta(days=k) for k in range(n)]
    assert dias == [inicio + timedelta(days=k) for k in range((fim - inicio).days + 1)]


def test_zip_cobre_modis_e_snpp_ate_2025():
    lista = tarefas.plano(DISP, HOJE)
    sp_zip = [t for t in lista if t.camada in ("modis", "viirs_snpp") and t.processamento == "SP"]
    assert min(t.inicio for t in sp_zip) == date(2026, 1, 1)
    assert max(t.fim for t in lista if t.camada == "viirs_snpp") == date(2026, 11, 2)


def test_ano_com_zip_sai_do_sp_e_do_nrt_da_api():
    lista = tarefas.plano(DISP, HOJE, zips_ate={"modis": 2026, "viirs_snpp": 2026})
    assert not [t for t in lista if t.camada in ("modis", "viirs_snpp")]
    antes = [t for t in tarefas.plano(DISP, HOJE) if t.camada not in ("modis", "viirs_snpp")]
    assert [t for t in lista if t.camada not in ("modis", "viirs_snpp")] == antes


def test_ultimo_zip_vem_dos_marcadores(tmp_path):
    assert tarefas.ultimo_zip(tmp_path) == {"modis": 2025, "viirs_snpp": 2025}
    for nome in ("modis_2025", "modis_2026", "viirs_snpp_2024"):
        (tmp_path / f"{nome}.ok.json").write_text("{}")
    assert tarefas.ultimo_zip(tmp_path) == {"modis": 2026, "viirs_snpp": 2025}


def test_ordem_traz_o_noaa20_de_2019_antes_do_noaa21():
    lista = tarefas.plano(DISP, HOJE)
    posicao = {(t.camada, t.processamento, t.inicio): i for i, t in enumerate(lista)}
    ultimo_nrt = max(i for i, t in enumerate(lista) if t.processamento == "NRT" and t.inicio >= date(2026, 8, 1))
    primeiro_sp = min(i for i, t in enumerate(lista) if t.processamento == "SP")
    assert ultimo_nrt < primeiro_sp
    assert posicao[("viirs_noaa20", "SP", date(2019, 1, 1))] < posicao[("viirs_noaa21", "SP", date(2026, 7, 31))]
    assert posicao[("viirs_noaa20", "SP", date(2019, 1, 1))] < posicao[("viirs_noaa20", "SP", date(2018, 12, 31))]
    assert lista[-1].camada == "landsat" and lista[-1].inicio == date(2022, 6, 20)


def test_custo_total_como_no_plano():
    custo = sum(t.custo for t in tarefas.plano(DISP, HOJE))
    assert 185_000 < custo < 195_000


def test_nrt_gravado_antes_de_fechar_e_baixado_de_novo(tmp_path):
    tarefa = tarefas.Tarefa("viirs_noaa20", "NRT", "VIIRS_NOAA20_NRT", "world", date(2026, 11, 11), 4,
                            date(2026, 11, 15), 144)
    arquivo = tarefas.caminho_arquivo(tarefa, tmp_path)
    arquivo.parent.mkdir(parents=True)
    arquivo.write_bytes(b"x")
    assert not tarefas.completo(tarefa, tmp_path)
    antigo = tarefas.Tarefa("viirs_noaa20", "SP", "VIIRS_NOAA20_SP", "world", date(2020, 1, 1), 5,
                            date(2020, 1, 5), 180)
    arquivo = tarefas.caminho_arquivo(antigo, tmp_path)
    arquivo.parent.mkdir(parents=True)
    arquivo.write_bytes(b"x")
    assert tarefas.completo(antigo, tmp_path)


def test_cota_espera_a_janela_esvaziar():
    agora = [0.0]
    dormiu = []
    cota = Cota(limite=360, janela_s=600, relogio=lambda: agora[0],
                dormir=lambda s: (dormiu.append(s), agora.__setitem__(0, agora[0] + s)), aviso=lambda _: None)
    cota.esperar(180)
    cota.registrar(180)
    cota.esperar(180)
    cota.registrar(180)
    cota.esperar(180)
    assert dormiu == [601]


def test_padronizar_mantem_hora_e_confianca_como_texto():
    df = padronizar(ler_csv(CSV_VIIRS), "viirs_noaa20", "SP", "api")
    assert list(df.columns) == COLUNAS
    assert df["acq_time"].tolist() == [412, 412, 1530]
    assert df["conf_class"].tolist() == ["nominal", "nominal", "high"]


def test_executar_grava_com_pais_sem_repetidos(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "area", lambda *a: CSV_VIIRS)
    monkeypatch.setattr(api, "status", lambda chave: {"current_transactions": 0})
    tarefa = tarefas.Tarefa("viirs_noaa20", "SP", "VIIRS_NOAA20_SP", "world", date(2026, 7, 1), 5,
                            date(2026, 7, 5), 180)
    cota = Cota(aviso=lambda _: None)
    gasto, falhas = baixar.executar([tarefa], "chave", cota, FronteiraFalsa(), tmp_path, tmp_path / "manifest.csv")
    gravado = pq.ParquetFile(tarefas.caminho_arquivo(tarefa, tmp_path)).read().to_pandas()
    assert (gasto, falhas) == (180, [])
    assert gravado["country"].tolist() == ["BOL", ""]
    manifest = pd.read_csv(tmp_path / "manifest.csv")
    assert manifest[["linhas", "repetidos", "sem_pais"]].values.tolist() == [[2, 1, 1]]


def test_janela_com_foco_fora_do_periodo_falha(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "area", lambda *a: CSV_VIIRS)
    monkeypatch.setattr(baixar.time, "sleep", lambda s: None)
    tarefa = tarefas.Tarefa("viirs_noaa20", "SP", "VIIRS_NOAA20_SP", "world", date(2026, 7, 6), 5,
                            date(2026, 7, 10), 180)
    gasto, falhas = baixar.executar([tarefa], "chave", Cota(aviso=lambda _: None), FronteiraFalsa(),
                                    tmp_path, tmp_path / "manifest.csv")
    assert gasto == 0 and falhas == [tarefa]
    assert not tarefas.caminho_arquivo(tarefa, tmp_path).exists()
