import io

import numpy as np
import pandas as pd

from heru.config import FONTES

CHAVE = ["latitude", "longitude", "acq_date", "acq_time", "satelite"]

COLUNAS = ["camada", "satelite", "instrumento", "processamento", "origem",
           "latitude", "longitude", "acq_date", "acq_time", "acq_datetime_utc", "daynight",
           "conf_raw", "conf_class", "frp", "brilho", "brilho2", "scan", "track",
           "version", "type", "path", "row", "country"]

BRILHO = {"MODIS": ("brightness", "bright_t31"), "VIIRS": ("bright_ti4", "bright_ti5")}
CLASSE_LETRA = {"l": "low", "n": "nominal", "m": "nominal", "h": "high"}
SATELITE = {"T": "Terra", "A": "Aqua"}


def classe_modis(conf):
    conf = pd.to_numeric(conf, errors="raise")
    return pd.Series(np.select([conf < 30, conf < 80], ["low", "nominal"], "high"), index=conf.index)


def classe_letra(conf):
    classe = conf.str.strip().str.lower().str[:1].map(CLASSE_LETRA)
    if classe.isna().any():
        raise ValueError(f"confidence fora de l/n/m/h: {sorted(conf[classe.isna()].unique())}")
    return classe


def decimal(bruto, coluna):
    if coluna and coluna in bruto:
        return pd.to_numeric(bruto[coluna]).astype("float32")
    return pd.Series(np.nan, index=bruto.index, dtype="float32")


def inteiro(bruto, coluna, tipo):
    if coluna in bruto:
        return pd.to_numeric(bruto[coluna]).astype(tipo)
    return pd.Series(pd.NA, index=bruto.index, dtype=tipo)


def padronizar(bruto, camada, processamento, origem):
    fonte = FONTES[camada]
    hora = bruto["acq_time"].astype("str").str.strip().str.zfill(4)
    data = bruto["acq_date"].astype("str").str.strip()
    conf = bruto["confidence"].astype("str").str.strip()
    b1, b2 = BRILHO.get(fonte.instrumento, (None, None))
    versao = bruto["version"].astype("str") if "version" in bruto else pd.Series(pd.NA, index=bruto.index, dtype="str")
    df = pd.DataFrame({
        "camada": camada,
        "satelite": bruto["satellite"].astype("str").str.strip().replace(SATELITE),
        "instrumento": fonte.instrumento,
        "processamento": processamento,
        "origem": origem,
        "latitude": pd.to_numeric(bruto["latitude"]).astype("float64"),
        "longitude": pd.to_numeric(bruto["longitude"]).astype("float64"),
        "acq_date": pd.to_datetime(data, format="%Y-%m-%d").dt.date,
        "acq_time": pd.to_numeric(hora).astype("int16"),
        "acq_datetime_utc": pd.to_datetime(data + hora, format="%Y-%m-%d%H%M", utc=True),
        "daynight": bruto["daynight"].astype("str"),
        "conf_raw": conf,
        "conf_class": classe_modis(conf) if fonte.instrumento == "MODIS" else classe_letra(conf),
        "frp": decimal(bruto, "frp"),
        "brilho": decimal(bruto, b1),
        "brilho2": decimal(bruto, b2),
        "scan": decimal(bruto, "scan"),
        "track": decimal(bruto, "track"),
        "version": versao,
        "type": inteiro(bruto, "type", "Int8"),
        "path": inteiro(bruto, "path", "Int16"),
        "row": inteiro(bruto, "row", "Int16"),
        "country": "",
    }, index=bruto.index)
    return df[COLUNAS].reset_index(drop=True)


def ler_csv(texto_ou_arquivo):
    fonte = io.StringIO(texto_ou_arquivo) if isinstance(texto_ou_arquivo, str) else texto_ou_arquivo
    try:
        return pd.read_csv(fonte, dtype={"acq_time": "str", "confidence": "str", "version": "str",
                                         "acq_date": "str", "satellite": "str", "daynight": "str"})
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=["latitude", "longitude", "acq_date", "acq_time",
                                     "satellite", "confidence", "daynight"])
