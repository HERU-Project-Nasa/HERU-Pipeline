import gzip
import zipfile
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from heru import agregar, atualizar, baixar, exportar, tarefas, zips
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


def anotar_janela(quando, arquivo, dias):
    baixar.anotar({"quando_utc": quando, "arquivo": arquivo, "dias": dias}, agregar.MANIFEST)


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


def test_sem_buraco_quando_o_csv_comeca_antes_do_ultimo_dia_fechado():
    assert atualizar.janelas_do_buraco("viirs_noaa20", date(2026, 11, 13), date(2026, 11, 8)) == []
    assert atualizar.janelas_do_buraco("viirs_noaa20", None, date(2026, 11, 8)) == []


def test_buraco_vira_janelas_alinhadas_ate_o_primeiro_dia_do_csv():
    lista = atualizar.janelas_do_buraco("viirs_noaa20", date(2026, 11, 3), date(2026, 11, 12))
    assert [(t.inicio, t.dias, t.custo) for t in lista] == [
        (date(2026, 11, 1), 5, 180), (date(2026, 11, 6), 5, 180), (date(2026, 11, 11), 2, 72)]
    assert {(t.processamento, t.fonte) for t in lista} == {("NRT", "VIIRS_NOAA20_NRT")}


def test_buraco_maior_que_7_dias_vem_da_api_antes_do_csv(loja, monkeypatch):
    destino = janela("viirs_noaa20", date(2026, 11, 1))
    destino.parent.mkdir(parents=True)
    viirs([(-15.0, "2026-11-01", "0100")], "api").to_parquet(destino, index=False)
    anotar_janela("2026-11-04T12:00:00+00:00", "camada=viirs_noaa20/ano=2026/mes=11/NRT_2026-11-01.parquet", 4)
    chamadas = []

    def area(chave, fonte, area_txt, dias, data):
        chamadas.append((data, dias))
        inicio = date.fromisoformat(data)
        return texto_viirs([(-15.0 - i / 10, (inicio + pd.Timedelta(days=i)).isoformat(), "0100") for i in range(dias)])

    monkeypatch.setattr(atualizar.api, "area", area)
    monkeypatch.setattr(atualizar.api, "status", lambda chave: {"current_transactions": 0})
    monkeypatch.setattr(atualizar, "CSV_GLOBAIS", {"viirs_noaa20": ["x"]})
    csv = texto_viirs([(-16.0 - i / 10, f"2026-11-{12 + i}", "0200") for i in range(5)])
    monkeypatch.setattr(atualizar, "baixar_csv_global", lambda camada, janela: ler_csv(csv))
    avisos = []
    atualizar.tempo_real("7d", FronteiraFalsa(), avisos.append, buraco=True, chave="chave")
    assert chamadas == [("2026-11-01", 5), ("2026-11-06", 5), ("2026-11-11", 2)]
    assert any("buraco de 2026-11-04 a 2026-11-12" in a for a in avisos)
    assert len(pq.ParquetFile(destino).read()) == 5
    onze = janela("viirs_noaa20", date(2026, 11, 11))
    assert len(pq.ParquetFile(onze).read()) == 6
    assert manifest_de(onze)["dias"] == 5
    assert atualizar.fechado_ate("viirs_noaa20") is not None


def test_buraco_sem_map_key_so_avisa(loja, monkeypatch):
    destino = janela("viirs_noaa20", date(2026, 11, 1))
    destino.parent.mkdir(parents=True)
    viirs([(-15.0, "2026-11-01", "0100")], "api").to_parquet(destino, index=False)
    anotar_janela("2026-11-04T12:00:00+00:00", "camada=viirs_noaa20/ano=2026/mes=11/NRT_2026-11-01.parquet", 4)
    avisos = []
    assert atualizar.preencher_buraco("viirs_noaa20", date(2026, 11, 12), None, FronteiraFalsa(), avisos.append) is False
    assert avisos[-1] == "ATRASO: viirs_noaa20 sem MAP_KEY para o buraco; rode historico --inicio 2026-11-04"


CSV_MODIS = ("latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,"
             "bright_t31,frp,daynight,type\n-16.5,-64.0,320.1,1.0,1.0,2026-08-02,1405,Aqua,MODIS,85,6.1NRT,290.0,"
             "30.5,D,0\n")


def test_diaria_troca_o_sp_da_api_pelo_zip_do_ano_que_fechou(loja, monkeypatch):
    pasta_zips = loja / "zips"
    pasta_zips.mkdir()
    monkeypatch.setattr(zips, "ZIPS", pasta_zips)
    monkeypatch.setattr(zips, "TRAVA_ZIPS", loja / ".trava_zips")
    for nome in ("modis_2025", "viirs_snpp_2025"):
        (pasta_zips / f"{nome}.ok.json").write_text("{}")
    baixar_zip = zips.baixar_zip

    def so_o_modis(camada, ano, pasta):
        if camada != "modis":
            raise zips.ZipInexistente(camada)
        return baixar_zip(camada, ano, pasta)

    monkeypatch.setattr(zips, "baixar_zip", so_o_modis)
    with zipfile.ZipFile(zips.caminho_zip("modis", 2026, pasta_zips), "w") as z:
        z.writestr("modis/2026/modis_2026_Bolivia.csv", CSV_MODIS)
    for inicio, processamento in ((date(2026, 8, 1), "SP"), (date(2026, 8, 6), "NRT"), (date(2026, 9, 1), "SP")):
        destino = janela("modis", inicio, processamento)
        destino.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"x": [1]}).to_parquet(destino)
    avisos = []
    atualizar.trocar_por_zip(FronteiraFalsa(), avisos.append, date(2027, 1, 10))
    assert sorted(a.name for a in agregar.arquivos_do_store()) == ["SP_2026-09-01.parquet", "SP_zip_2026-08.parquet"]
    assert tarefas.ultimo_zip(pasta_zips) == {"modis": 2026, "viirs_snpp": 2025}
    assert "viirs_snpp 2026: zip ainda não saiu" in avisos
    assert not (loja / ".trava_zips").exists()


@pytest.fixture
def loop(loja, monkeypatch):
    monkeypatch.setattr(atualizar, "TRAVA", loja / ".trava")
    monkeypatch.setattr(exportar, "TRAVA_EXPORTAR", loja / ".trava_exportar")
    monkeypatch.setattr(atualizar, "Fronteira", lambda paises: FronteiraFalsa())
    monkeypatch.setattr(atualizar, "ler_paises", lambda: None)
    monkeypatch.setattr(baixar, "manter_acordado", lambda: None)
    return loja


class Relogio:
    def __init__(self):
        self.agora, self.sonos = 0.0, 0

    def time(self):
        return self.agora

    def sleep(self, segundos):
        self.sonos += 1
        if self.sonos == 2:
            raise KeyboardInterrupt
        self.agora += segundos


def test_erro_na_coleta_nao_impede_a_exportacao(loop, monkeypatch, capsys):
    def quebra(*args):
        raise KeyError("latitude")

    exportados = []
    monkeypatch.setattr(atualizar, "coletar", quebra)
    monkeypatch.setattr(exportar, "exportar", lambda **kw: exportados.append(kw) or False)
    with pytest.raises(SystemExit) as saida:
        atualizar.main([])
    assert saida.value.code == 1 and len(exportados) == 1
    assert "ERRO em coleta: KeyError" in capsys.readouterr().out
    assert atualizar.ler_estado() == {}
    assert not (loop / ".trava").exists()


def test_erro_na_exportacao_vira_log_e_a_rodada_diaria_fica_registrada(loop, monkeypatch, capsys):
    def quebra(**kw):
        raise OSError("disco cheio")

    monkeypatch.setattr(atualizar, "coletar", lambda *args: {})
    monkeypatch.setattr(exportar, "exportar", quebra)
    with pytest.raises(SystemExit):
        atualizar.main([])
    saida = capsys.readouterr()
    assert "ERRO em exportação: OSError: disco cheio" in saida.out
    assert "Traceback" in saida.err
    assert atualizar.ler_estado()["diario"] == baixar.hoje_utc().isoformat()


def test_loop_segue_depois_do_erro_e_refaz_a_rodada_diaria(loop, monkeypatch, capsys):
    coletas = []

    def coletar(fronteira, diario, com_sp, aviso):
        coletas.append(diario)
        if len(coletas) == 1:
            raise RuntimeError("CSV mudou de formato")
        return {}

    monkeypatch.setattr(atualizar, "coletar", coletar)
    monkeypatch.setattr(exportar, "exportar", lambda **kw: False)
    monkeypatch.setattr(atualizar, "time", Relogio())
    with pytest.raises(KeyboardInterrupt):
        atualizar.main(["--a-cada", "60"])
    assert coletas == [True, True]
    assert "ERRO em coleta: RuntimeError: CSV mudou de formato" in capsys.readouterr().out


def test_estado_corrompido_vira_estado_vazio(loja):
    atualizar.caminho_estado().parent.mkdir(parents=True)
    atualizar.caminho_estado().write_text("{corte", encoding="utf-8")
    assert atualizar.ler_estado() == {}


def test_camada_com_erro_nao_impede_as_outras(loja, monkeypatch):
    def baixar_csv(camada, janela):
        if camada == "modis":
            return pd.DataFrame({"coluna_nova": [1]})
        return ler_csv(texto_viirs([(-16.5, "2026-11-15", "0412")]))

    monkeypatch.setattr(atualizar, "CSV_GLOBAIS", {"modis": ["x"], "viirs_noaa20": ["y"]})
    monkeypatch.setattr(atualizar, "baixar_csv_global", baixar_csv)
    avisos = []
    ultimos = atualizar.tempo_real("48h", FronteiraFalsa(), avisos.append)
    assert ultimos == {"viirs_noaa20": date(2026, 11, 15)}
    assert any(a.startswith("ERRO em coleta de modis") for a in avisos)
