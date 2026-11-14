import io
import os
import time

import pandas as pd
import requests

from heru.config import ENV, MAX_DIAS

BASE = "https://firms.modaps.eosdis.nasa.gov"
URL_AREA = BASE + "/api/area/csv/{key}/{fonte}/{area}/{dias}/{data}"
URL_DISPONIBILIDADE = BASE + "/api/data_availability/csv/{key}/ALL"
URL_STATUS = BASE + "/mapserver/mapkey_status/?MAP_KEY={key}"


class ErroAPI(Exception):
    pass


def carregar_map_key(env=ENV):
    chave = os.environ.get("MAP_KEY")
    if chave:
        return chave.strip()
    if env.exists():
        for linha in env.read_text(encoding="utf-8-sig").splitlines():
            nome, _, valor = linha.partition("=")
            if nome.strip() == "MAP_KEY" and valor.strip():
                return valor.strip().strip('"').strip("'")
    return None


def erro_de_limite(erro):
    texto = str(erro).lower()
    return any(p in texto for p in ("limit", "exceed", "too many", "429"))


def buscar(url, tentativas=3, pausa=5):
    for i in range(tentativas):
        try:
            r = requests.get(url, timeout=180)
            if 400 <= r.status_code < 500:
                raise ErroAPI(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            if r.text.lstrip().lower().startswith(("invalid", "error", "<", "exceed")):
                raise ErroAPI(r.text[:200])
            return r.text
        except requests.RequestException:
            if i == tentativas - 1:
                raise
            time.sleep(pausa * (i + 1))


def status(chave):
    return requests.get(URL_STATUS.format(key=chave), timeout=60).json()


def disponibilidade(chave):
    return pd.read_csv(io.StringIO(buscar(URL_DISPONIBILIDADE.format(key=chave))))


def area(chave, fonte, area_txt, dias, data):
    if not 1 <= dias <= MAX_DIAS:
        raise ValueError(f"dias deve ir de 1 a {MAX_DIAS}")
    return buscar(URL_AREA.format(key=chave, fonte=fonte, area=area_txt, dias=dias, data=data))
