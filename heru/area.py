import numpy as np
import pandas as pd
import requests
import shapely

from heru.config import DADOS
from heru.fronteira import ler_paises, tabela_paises

AREAS = DADOS / "area_paises.csv"
BANCO_MUNDIAL = "https://api.worldbank.org/v2/country/all/indicator/AG.LND.TOTL.K2"
RAIO_TERRA_KM = 6371.0072


def ler_areas(caminho=AREAS):
    if not caminho.exists():
        return pd.DataFrame(columns=["iso3", "area_km2", "area_source"])
    return pd.read_csv(caminho, dtype=str, keep_default_na=False)


def area_dos_poligonos(paises):
    def igual_area(xy):
        return np.column_stack([np.radians(xy[:, 0]) * RAIO_TERRA_KM, np.sin(np.radians(xy[:, 1])) * RAIO_TERRA_KM])

    geometrias = shapely.transform(shapely.from_wkb(paises["wkb"].to_numpy()), igual_area)
    return pd.Series(shapely.area(geometrias), index=paises["iso3"].to_numpy()).groupby(level=0).sum()


def area_do_banco_mundial():
    resposta = requests.get(BANCO_MUNDIAL, params={"format": "json", "mrnev": 1, "per_page": 1000}, timeout=300)
    resposta.raise_for_status()
    return pd.Series({r["countryiso3code"]: r["value"] for r in resposta.json()[1] if r["value"] is not None})


def texto_area(valor):
    return f"{valor:.2f}".rstrip("0").rstrip(".")


def montar(destino=AREAS):
    paises = ler_paises()
    tabela = tabela_paises(paises)
    banco = area_do_banco_mundial()
    poligonos = area_dos_poligonos(paises[paises["iso3"] != ""])
    do_banco = tabela["iso3"].isin(banco.index)
    area = np.where(do_banco, tabela["iso3"].map(banco), tabela["iso3"].map(poligonos))
    saida = pd.DataFrame({"iso3": tabela["iso3"], "area_km2": [texto_area(v) for v in area],
                          "area_source": np.where(do_banco, "world_bank", "cgaz")})
    saida.to_csv(destino, index=False, lineterminator="\n")
    return saida


if __name__ == "__main__":
    tabela = montar()
    print(tabela["area_source"].value_counts().to_string())
    print(tabela[tabela["area_source"] == "cgaz"].to_string(index=False))
