import gzip
import hashlib
import os
from datetime import datetime, timezone

import pandas as pd

from heru.config import SAIDA

CAMPOS_LOTE = ["path", "kind", "period", "rows", "bytes", "sha256"]


def agora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serializar(relativo, df):
    corpo = df.to_csv(index=False, lineterminator="\n", na_rep="").encode("utf-8")
    if relativo.endswith(".gz"):
        corpo = gzip.compress(corpo, mtime=0)
    return corpo


def gravar_bytes(relativo, corpo, saida=SAIDA):
    destino = saida / relativo
    destino.parent.mkdir(parents=True, exist_ok=True)
    temp = destino.with_name(f".{destino.name}.{os.getpid()}.tmp")
    temp.write_bytes(corpo)
    os.replace(temp, destino)
    return {"path": relativo, "bytes": len(corpo), "sha256": hashlib.sha256(corpo).hexdigest()}


def gravar(relativo, df, saida=SAIDA):
    registro = gravar_bytes(relativo, serializar(relativo, df), saida)
    registro["rows"] = len(df)
    return registro


def ler_anteriores(saida=SAIDA):
    arquivo = saida / "meta" / "files.csv"
    if not arquivo.exists():
        return {}
    tabela = pd.read_csv(arquivo, dtype=str, keep_default_na=False)
    return {linha["path"]: linha for linha in tabela.to_dict("records")}


def ultimo_lote(saida=SAIDA):
    arquivo = saida / "meta" / "latest.csv"
    if not arquivo.exists():
        return 0
    return int(pd.read_csv(arquivo)["batch"].iloc[0])


class Lote:
    def __init__(self, numero, saida=SAIDA, anteriores=None):
        self.numero = numero
        self.saida = saida
        self.anteriores = anteriores or {}
        self.arquivos = []

    def publicar(self, relativo, df, kind, period=""):
        return self.publicar_bytes(relativo, serializar(relativo, df), kind, period, len(df))

    def publicar_bytes(self, relativo, corpo, kind, period, linhas):
        antigo = self.anteriores.get(relativo)
        if antigo and antigo["sha256"] == hashlib.sha256(corpo).hexdigest() and (self.saida / relativo).exists():
            return None
        registro = gravar_bytes(relativo, corpo, self.saida)
        registro.update(rows=linhas, kind=kind, period=period, updated_at=agora())
        self.arquivos.append(registro)
        return registro

    def fechar(self):
        if not self.arquivos:
            return False
        todos = dict(self.anteriores)
        todos.update({r["path"]: r for r in self.arquivos})
        lista = [r for r in todos.values() if r["kind"] != "points" and r["path"] != "meta/files.csv"]
        tabela = pd.DataFrame(lista, columns=CAMPOS_LOTE + ["updated_at"]).sort_values("path")
        registro = gravar_bytes("meta/files.csv", serializar("meta/files.csv", tabela), self.saida)
        registro.update(rows=len(tabela), kind="meta", period="", updated_at=agora())
        self.arquivos.append(registro)
        gravar(f"meta/batches/{self.numero}.csv", pd.DataFrame(self.arquivos, columns=CAMPOS_LOTE), self.saida)
        gravar("meta/latest.csv", pd.DataFrame({"batch": [self.numero], "updated_at": [agora()]}), self.saida)
        return True
