import gzip

import numpy as np
import pandas as pd

from heru import contar
from heru.config import COLUNAS_CONTAGEM
from heru.publicar import Lote, gravar


def cobertura_exemplo():
    return pd.DataFrame({
        "layer": ["modis", "modis", "viirs_noaa20"],
        "processing": ["sp", "nrt", "nrt"],
        "first_date": pd.to_datetime(["2026-07-01", "2026-09-01", "2026-08-01"]),
        "last_date": pd.to_datetime(["2026-08-31", "2026-09-30", "2026-09-30"]),
        "rows": [10, 5, 7],
    })


def diario_exemplo():
    return pd.DataFrame({
        "acq_date": pd.to_datetime(["2026-07-10", "2026-07-10", "2026-08-02", "2026-09-05"]),
        "country": ["BOL", "BOL", "BOL", "BRA"],
        "layer": ["modis", "modis", "viirs_noaa20", "modis"],
        "daynight": ["D", "N", "N", "D"],
        "n": [3, 2, 4, 1],
    })


def test_csv_sem_bom_com_lf_e_vazio_no_lugar_de_nan(tmp_path):
    df = pd.DataFrame({"a": [1, None], "b": ["x,y", "z"]})
    gravar("t/a.csv", df, tmp_path)
    corpo = (tmp_path / "t/a.csv").read_bytes()
    assert not corpo.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in corpo
    assert corpo.decode() == 'a,b\n1.0,"x,y"\n,z\n'
    assert not list((tmp_path / "t").glob(".*.tmp"))


def test_gzip_tem_sempre_o_mesmo_sha(tmp_path):
    df = pd.DataFrame({"a": [1, 2]})
    primeiro = gravar("p/a.csv.gz", df, tmp_path)
    segundo = gravar("p/a.csv.gz", df, tmp_path)
    assert primeiro["sha256"] == segundo["sha256"]
    assert gzip.decompress((tmp_path / "p/a.csv.gz").read_bytes()).decode() == "a\n1\n2\n"


def test_latest_sai_por_ultimo_e_lista_tudo(tmp_path):
    lote = Lote(7, tmp_path)
    lote.publicar("series/global/daily.csv", pd.DataFrame({"date": ["2026-01-01"]}), "series")
    lote.fechar()
    lista = pd.read_csv(tmp_path / "meta/batches/7.csv")
    assert set(lista["path"]) == {"series/global/daily.csv", "meta/files.csv"}
    assert pd.read_csv(tmp_path / "meta/latest.csv")["batch"].tolist() == [7]


def test_serie_vazia_fora_da_cobertura_e_passagens_somam():
    operando = contar.calendario(cobertura_exemplo())
    series = contar.series(diario_exemplo()[lambda d: d["country"] == "BOL"], operando)
    mensal = series["monthly"].set_index("month")
    assert list(series["monthly"].columns) == ["month"] + COLUNAS_CONTAGEM
    assert mensal.loc["2026-07", "modis"] == 5
    assert mensal.loc["2026-07", "modis_d"] + mensal.loc["2026-07", "modis_n"] == 5
    assert pd.isna(mensal.loc["2026-07", "viirs_noaa20"])
    assert mensal.loc["2026-08", "viirs_noaa20_n"] == 4
    assert pd.isna(mensal.loc["2026-08", "viirs_snpp"])
    assert pd.isna(mensal.loc["2026-08", "harmonized"])
    assert mensal["provisional"].tolist() == [0, 1, 1]
    diaria = series["daily"]
    assert diaria["date"].iloc[0] == "2026-07-01"
    assert len(diaria) == 92


def test_grade_usa_centro_da_celula_e_ordem_fixa():
    contagens = pd.DataFrame({
        "month": ["2026-08"] * 3,
        "i": [-80, -80, -81],
        "j": [-260, -259, -260],
        "layer": ["modis", "modis", "viirs_noaa20"],
        "daynight": ["D", "N", "D"],
        "n": [2, 1, 5],
    })
    operando = contar.por_periodo(contar.calendario(cobertura_exemplo()), "%Y-%m").loc["2026-08"]
    grade = contar.grade_do_mes("2026-08", contagens, operando)
    assert grade[["cell_lat", "cell_lon"]].values.tolist() == [
        ["-20.125", "-64.875"], ["-19.875", "-64.875"], ["-19.875", "-64.625"]]
    assert grade["modis"].tolist() == [0, 2, 1]
    assert grade["provisional"].tolist() == [1, 1, 1]
    assert np.all(grade["landsat"].isna())
