from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
FRONTEIRA = DATA / "fronteira"
PONTOS = DATA / "pontos"
MANIFEST = DATA / "manifest.csv"
TRAVA = DATA / ".trava"
ENV = RAIZ / ".env"
SAIDA = RAIZ / "saida" / "v1"
DADOS = Path(__file__).resolve().parent / "dados"

CAMADAS = ["modis", "viirs_snpp", "viirs_noaa20", "viirs_noaa21", "landsat"]
COM_PASSAGEM = ["modis", "viirs_snpp", "viirs_noaa20", "viirs_noaa21"]

LIMITE_SEGURO = 4500
JANELA_COTA_S = 600
MAX_DIAS = 5
ULTIMO_ANO_ZIP = 2025
AMERICA_DO_NORTE = "-170,15,-50,75"


@dataclass(frozen=True)
class Fonte:
    camada: str
    instrumento: str
    sp: str | None
    nrt: str
    area: str
    custo_dia: int
    tem_zip: bool


FONTES = {f.camada: f for f in (
    Fonte("modis", "MODIS", "MODIS_SP", "MODIS_NRT", "world", 36, True),
    Fonte("viirs_snpp", "VIIRS", "VIIRS_SNPP_SP", "VIIRS_SNPP_NRT", "world", 36, True),
    Fonte("viirs_noaa20", "VIIRS", "VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT", "world", 36, False),
    Fonte("viirs_noaa21", "VIIRS", "VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT", "world", 36, False),
    Fonte("landsat", "OLI", None, "LANDSAT_NRT", AMERICA_DO_NORTE, 4, False),
)}

CELULA = 0.25
BLOCO = 5
GRADE_FONTE = 100

INFO_CAMADAS = [
    ("modis", "MODIS (Aqua & Terra) [1km]", "MODIS", 1000, "Terra;Aqua", ""),
    ("viirs_snpp", "VIIRS (Suomi NPP) [375m]", "VIIRS", 375, "N", "parou de observar em 2026-11-02"),
    ("viirs_noaa20", "VIIRS (NOAA-20) [375m]", "VIIRS", 375, "N20", ""),
    ("viirs_noaa21", "VIIRS (NOAA-21) [375m]", "VIIRS", 375, "N21", ""),
    ("landsat", "Landsat [30m]", "OLI", 30, "L8;L9", "só EUA e Canadá"),
]

COLUNAS_CONTAGEM = (
    CAMADAS
    + ["harmonized", "harmonized_source", "provisional"]
    + [f"{c}_{p}" for c in COM_PASSAGEM for p in ("d", "n")]
    + ["harmonized_d", "harmonized_n"]
)

COLUNAS_PONTOS = [
    "latitude", "longitude", "acq_date", "acq_time", "layer", "satellite", "country",
    "daynight", "confidence", "confidence_raw", "frp", "brightness", "scan", "track",
    "type", "processing", "source",
]

COLUNAS_VALIDACAO = [
    "scope", "id", "k", "k20", "median_error_2020_2024", "r_log10", "months_used",
    "country_agreement", "k_d", "k_n", "calib_years", "modis_calib_hotspots",
    "k20_d", "k20_n", "calib_years_k20", "noaa20_calib_hotspots",
]
