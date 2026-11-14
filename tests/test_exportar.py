from datetime import date

import numpy as np
import pandas as pd
import pytest

import gzip

from heru import agregar, exportar, focos, harmonizar
from heru.conferir import conferir
from heru.padronizar import COLUNAS


def pontos(camada, processamento, dia, n, lat=-17.1, lon=-63.1, tipo=0, daynight="D", pais="BOL"):
    satelite = {"modis": "Aqua", "viirs_snpp": "N", "viirs_noaa20": "N20"}[camada]
    return pd.DataFrame({
        "camada": camada, "satelite": satelite, "instrumento": "MODIS" if camada == "modis" else "VIIRS",
        "processamento": processamento, "origem": "zip", "latitude": lat + np.arange(n) * 1e-5, "longitude": lon,
        "acq_date": date.fromisoformat(dia), "acq_time": np.arange(n, dtype="int16") % 2400,
        "acq_datetime_utc": pd.Timestamp(dia, tz="UTC"), "daynight": daynight, "conf_raw": "n", "conf_class": "nominal",
        "frp": np.float32(1), "brilho": np.float32(300), "brilho2": np.float32(290), "scan": np.float32(1),
        "track": np.float32(1), "version": "2", "type": pd.array([tipo] * n if tipo is not None else [pd.NA] * n, dtype="Int8"),
        "path": pd.array([pd.NA] * n, dtype="Int16"), "row": pd.array([pd.NA] * n, dtype="Int16"), "country": pais,
    })[COLUNAS]


def gravar(pasta, camada, nome, df):
    ano, mes = nome.split("_")[-1][:4], nome.split("_")[-1][5:7]
    destino = pasta / f"camada={camada}" / f"ano={ano}" / f"mes={mes}" / f"{nome}.parquet"
    destino.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(destino, index=False)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    loja = tmp_path / "pontos"
    monkeypatch.setattr(agregar, "PONTOS", loja)
    monkeypatch.setattr(agregar, "AGREGADOS", tmp_path / "agregados")
    monkeypatch.setattr(exportar, "ESTADO", tmp_path / "agregados" / "estado.json")
    monkeypatch.setattr(exportar, "ZIPS", tmp_path / "zips")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text("quando_utc,arquivo,dias\n"
                        "2026-11-02T20:00:00+00:00,camada=viirs_noaa20/ano=2026/mes=11/NRT_2026-11-01.parquet,2\n",
                        encoding="utf-8")
    monkeypatch.setattr(agregar, "MANIFEST", manifest)
    monkeypatch.setattr(focos, "ESTADO_FOCOS", tmp_path / "focos.json")
    tabela = pd.DataFrame({"iso3": ["BOL", "PRT"], "name": ["Bolivia", "Portugal"], "continent": ["South America", "Europe"]})
    monkeypatch.setattr(exportar, "ler_paises", lambda: None)
    monkeypatch.setattr(exportar, "tabela_paises", lambda paises: tabela)
    gravar(loja, "modis", "SP_zip_2012-03", pd.concat([pontos("modis", "SP", "2012-03-10", 1500),
                                                        pontos("modis", "SP", "2012-03-11", 500, daynight="N")]))
    gravar(loja, "viirs_snpp", "SP_zip_2012-03", pd.concat([pontos("viirs_snpp", "SP", "2012-03-10", 4500),
                                                             pontos("viirs_snpp", "SP", "2012-03-11", 1500, daynight="N"),
                                                             pontos("viirs_snpp", "SP", "2012-03-12", 3, lat=40.0, lon=-8.0, pais="PRT")]))
    gravar(loja, "modis", "SP_zip_2005-08", pd.concat([pontos("modis", "SP", "2005-08-01", 10),
                                                        pontos("modis", "SP", "2005-08-02", 3, tipo=2, lat=40.0, lon=-8.0, pais="PRT")]))
    gravar(loja, "modis", "SP_zip_2001-08", pontos("modis", "SP", "2001-08-01", 5))
    gravar(loja, "viirs_noaa20", "NRT_2026-11-01", pd.concat([pontos("viirs_noaa20", "NRT", "2026-11-01", 4, tipo=None),
                                                              pontos("viirs_noaa20", "NRT", "2026-11-02", 2, tipo=None, lat=40.0, lon=-8.0, pais="PRT")]))
    return tmp_path


def test_fator_global_quando_o_bloco_tem_poucos_focos():
    num = pd.DataFrame({"d": [3000, 30], "n": [300, 3]}, index=pd.MultiIndex.from_tuples([(0, 0), (1, 1)], names=["bi", "bj"]))
    den = pd.DataFrame({"d": [1000, 10], "n": [100, 1]}, index=num.index)
    resultado = harmonizar.fatores(num, den)
    assert resultado["global"][0] == pytest.approx(3333 / 1111)
    assert resultado["blocos"].loc[(0, 0), "k"] == pytest.approx(3.0)
    assert resultado["blocos"].loc[(0, 0), "k_n"] == pytest.approx(3333 / 1111 * 0 + 303 / 101)
    assert resultado["blocos"].loc[(1, 1), "k"] == pytest.approx(3333 / 1111)
    assert harmonizar.fator(resultado, [5], [5], "k_d")[0] == pytest.approx(3030 / 1010)


def test_sem_k_a_harmonizada_fica_vazia(ambiente):
    exportar.exportar(ambiente / "saida", aviso=lambda _: None)
    grade = pd.read_csv(ambiente / "saida/grid/monthly/2005/2005-08.csv", keep_default_na=False)
    assert grade["harmonized"].eq("").all()
    assert pd.read_csv(ambiente / "saida/meta/validation.csv").empty
    assert conferir(ambiente / "saida") == []


def test_k_por_bloco_e_harmonizada_por_regime(ambiente, monkeypatch):
    monkeypatch.setattr(harmonizar, "zips_prontos", lambda *a: True)
    exportar.exportar(ambiente / "saida", com_tiles=True, aviso=lambda _: None)
    saida = ambiente / "saida"
    grade = pd.read_csv(saida / "grid/monthly/2005/2005-08.csv", keep_default_na=False)
    bolivia = grade[grade["modis"] == 10].iloc[0]
    assert float(bolivia["harmonized"]) == pytest.approx(30.0)
    assert bolivia["harmonized_source"] == "modis_x_k"
    assert float(bolivia["harmonized_d"]) == pytest.approx(30.0)
    mensal = pd.read_csv(saida / "series/countries/BOL/monthly.csv", keep_default_na=False).set_index("month")
    assert float(mensal.loc["2005-08", "harmonized"]) == pytest.approx(30.0)
    assert mensal.loc["2001-08", "harmonized"] == ""
    assert float(mensal.loc["2012-03", "harmonized"]) == 6000
    assert mensal.loc["2026-11", "harmonized"] == ""
    anual = pd.read_csv(saida / "series/countries/BOL/yearly.csv", keep_default_na=False, dtype={"year": str}).set_index("year")
    assert anual.loc["2001", "harmonized"] == "" and anual.loc["2026", "harmonized"] == ""
    assert float(anual.loc["2005", "harmonized"]) == pytest.approx(30.0)
    validacao = pd.read_csv(saida / "meta/validation.csv", keep_default_na=False)
    mundo = validacao[validacao["scope"] == "world"].iloc[0]
    assert float(mundo["k"]) == pytest.approx(6003 / 2000, rel=1e-3) and mundo["calib_years"] == "2012-2019"
    assert validacao[validacao["scope"] == "block"]["id"].tolist() == ["-20_-65", "40_-10"]
    portugal = pd.read_csv(saida / "series/countries/PRT/daily.csv", keep_default_na=False).set_index("date")
    assert portugal.loc["2026-11-02", "viirs_noaa20"] == "0"
    assert mensal.loc["2003-01", "modis"] == "" and mensal.loc["2005-08", "modis"] == "10"
    assert mensal.loc["2012-03", "viirs_noaa20"] == "" and mensal.loc["2026-11", "provisional"] == 1
    assert portugal.index[-1] == "2026-11-02"
    cobertura = pd.read_csv(saida / "meta/coverage.csv", keep_default_na=False).set_index(["layer", "processing"])
    assert cobertura.loc[("modis", "sp"), "first_date"] == "2012-03-01"
    assert cobertura.loc[("modis", "sp"), "rows"] == 2018
    assert cobertura.loc[("modis", "sp"), "complete_date"] == cobertura.loc[("modis", "sp"), "last_date"]
    assert cobertura.loc[("viirs_noaa20", "nrt"), "last_date"] == "2026-11-02"
    assert cobertura.loc[("viirs_noaa20", "nrt"), "complete_date"] == "2026-11-01"
    noaa20 = pd.read_csv(saida / "points/viirs_noaa20/2026/2026-11-02.csv.gz", keep_default_na=False)
    assert len(noaa20) == 2 and noaa20["country"].tolist() == ["PRT", "PRT"] and noaa20["processing"].iloc[0] == "NRT"
    assert not (saida / "points/viirs_noaa20/2026/2026-11-03.csv.gz").exists()
    assert len(gzip.decompress((saida / "points/modis/2012/2012-03-31.csv.gz").read_bytes()).splitlines()) == 1
    tile = pd.read_csv(saida / "grid/tiles/-20_-65/monthly.csv", keep_default_na=False)
    assert tile["month"].tolist() == sorted(tile["month"])
    assert conferir(saida) == []


def test_segunda_exportacao_sem_mudanca_nao_gera_lote(ambiente):
    exportar.exportar(ambiente / "saida", aviso=lambda _: None, processos=1)
    assert pd.read_csv(ambiente / "saida/meta/latest.csv")["batch"].tolist() == [2]
    assert exportar.exportar(ambiente / "saida", aviso=lambda _: None, processos=1) is False
    assert pd.read_csv(ambiente / "saida/meta/latest.csv")["batch"].tolist() == [2]


def test_dia_com_sp_nao_sai_do_nrt(tmp_path, monkeypatch):
    monkeypatch.setattr(agregar, "PONTOS", tmp_path)
    arquivos = []
    for nome in ("SP_2026-07-26", "NRT_2026-07-26"):
        arquivo = tmp_path / "camada=viirs_noaa21/ano=2026/mes=07" / f"{nome}.parquet"
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_bytes(b"x")
        arquivos.append(arquivo)
    trechos = pd.DataFrame({"layer": [3, 3], "processing": ["sp", "nrt"],
                            "inicio": pd.to_datetime(["2026-07-26", "2026-07-26"]),
                            "fim": pd.to_datetime(["2026-07-28", "2026-07-31"])})
    dias = {t["relativo"].split("/")[-1]: t["dias"] for t in focos.pendentes(arquivos, trechos, {})}
    assert dias["SP_2026-07-26.parquet"] == ["2026-07-26", "2026-07-27", "2026-07-28"]
    assert dias["NRT_2026-07-26.parquet"] == ["2026-07-29", "2026-07-30", "2026-07-31"]
