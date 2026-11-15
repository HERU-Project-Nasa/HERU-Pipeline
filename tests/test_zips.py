import json
import zipfile

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from heru import zips

CABECALHO = "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight,type\n"
BOLIVIA = CABECALHO + (
    "-16.5,-64.0,320.1,1.0,1.0,2024-08-01,1405,Aqua,MODIS,85,6.1NRT,290.0,30.5,D,0\n"
    "-16.6,-64.1,315.0,1.0,1.0,2024-09-02,0310,Terra,MODIS,40,6.1NRT,285.0,10.0,N,0\n"
    "-9.9,-65.0,315.0,1.0,1.0,2024-09-03,0310,Terra,MODIS,40,6.1NRT,285.0,10.0,N,0\n"
)
BRASIL = CABECALHO + (
    "-9.9,-65.0,315.0,1.0,1.0,2024-09-03,0310,Terra,MODIS,40,6.1NRT,285.0,10.0,N,0\n"
    "-5.0,-50.0,330.0,1.0,1.0,2024-09-04,1650,Aqua,MODIS,90,6.1NRT,295.0,50.0,D,0\n"
)


class FronteiraFalsa:
    def paises(self, lat, lon):
        return np.where(lat < -10, "BOL", "BRA")


def test_snpp_de_2019_em_diante_vem_primeiro():
    lista = zips.ordem()
    assert lista[:7] == [("viirs_snpp", a) for a in range(2025, 2018, -1)]
    assert lista[7:9] == [("modis", 2025), ("modis", 2024)]
    assert lista[-1] == ("modis", 2000)
    assert len(lista) == 26 + 14


def test_nome_do_pais_sai_do_arquivo():
    assert zips.pais_do_arquivo("modis/2024/modis_2024_Brazil.csv") == "Brazil"
    assert zips.pais_do_arquivo("viirs-snpp/2024/viirs-snpp_2024_United_States.csv") == "United_States"


def test_ano_do_zip_vira_meses_sem_repetidos_e_com_concordancia(tmp_path):
    caminho = zips.caminho_zip("modis", 2024, tmp_path)
    with zipfile.ZipFile(caminho, "w") as z:
        z.writestr("modis/2024/modis_2024_Bolivia.csv", BOLIVIA)
        z.writestr("modis/2024/modis_2024_Brazil.csv", BRASIL)
    info = zips.processar("modis", 2024, FronteiraFalsa(), tmp_path, tmp_path / "pontos")
    assert info["focos"] == 4 and info["repetidos_entre_paises"] == 1
    agosto = pq.ParquetFile(tmp_path / "pontos/camada=modis/ano=2024/mes=08/SP_zip_2024-08.parquet").read().to_pandas()
    setembro = pq.ParquetFile(tmp_path / "pontos/camada=modis/ano=2024/mes=09/SP_zip_2024-09.parquet").read().to_pandas()
    assert len(agosto) == 1 and len(setembro) == 3
    assert agosto["country"].tolist() == ["BOL"]
    paises = (tmp_path / "modis_2024_paises.csv").read_text().splitlines()
    assert paises == ["arquivo,iso3,focos,concordancia", "Bolivia,BOL,3,0.6667", "Brazil,BRA,2,1.0"]
    assert json.loads(zips.marcador("modis", 2024, tmp_path).read_text())["camada"] == "modis"
    assert not caminho.exists()
    assert not list(tmp_path.glob("tmp_*"))
    assert zips.concluidos(tmp_path) == {("modis", 2024)}


def test_snpp_para_em_2026_e_o_modis_segue():
    lista = zips.ordem(ultimo_ano=2027)
    assert ("modis", 2027) in lista and ("viirs_snpp", 2026) in lista
    assert ("viirs_snpp", 2027) not in lista


def test_ano_novo_so_depois_do_ultimo_zip(tmp_path):
    assert zips.anos_novos(2025, tmp_path) == []
    assert zips.anos_novos(2026, tmp_path) == [("viirs_snpp", 2026), ("modis", 2026)]
    zips.marcador("modis", 2026, tmp_path).write_text("{}")
    assert zips.anos_novos(2027, tmp_path) == [("viirs_snpp", 2026), ("modis", 2027)]


def test_zip_do_ano_troca_o_sp_da_api_e_o_nrt_dos_meses_dele(tmp_path):
    pontos = tmp_path / "pontos"
    ano = pontos / "camada=modis/ano=2024"
    for relativo in ("mes=08/SP_2024-08-01.parquet", "mes=08/NRT_2024-08-06.parquet", "mes=09/SP_2024-09-01.parquet",
                     "mes=10/NRT_2024-10-01.parquet"):
        (ano / relativo).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"x": [1]}).to_parquet(ano / relativo)
    with zipfile.ZipFile(zips.caminho_zip("modis", 2024, tmp_path), "w") as z:
        z.writestr("modis/2024/modis_2024_Bolivia.csv", BOLIVIA)
    info = zips.processar("modis", 2024, FronteiraFalsa(), tmp_path, pontos)
    assert info["trocados"] == 3
    restantes = sorted(a.relative_to(ano).as_posix() for a in pontos.rglob("*.parquet"))
    assert restantes == ["mes=08/SP_zip_2024-08.parquet", "mes=09/SP_zip_2024-09.parquet",
                         "mes=10/NRT_2024-10-01.parquet"]
