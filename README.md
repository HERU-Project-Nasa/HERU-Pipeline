# HERU-Pipeline

Focos de incêndio do FIRMS (MODIS, VIIRS S-NPP, NOAA-20, NOAA-21 e Landsat) publicados como CSV estáticos em `saida/v1/` para o frontend. O contrato dos arquivos está no manual de integração do frontend.

## Ambiente

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

O `pyarrow` fica em 22.0.0: no Windows desta máquina o 26 teve a DLL do Parquet bloqueada pelo Controle de Aplicativo.

Os polígonos de país são o ADM0 do geoBoundaries CGAZ, commit 9469f09 (CC BY 4.0), em `data/fronteira/geoBoundariesCGAZ_ADM0.geojson`:

```
curl -L -o data/fronteira/geoBoundariesCGAZ_ADM0.geojson https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/CGAZ/geoBoundariesCGAZ_ADM0.geojson
```

## Carga do histórico

A `MAP_KEY` fica em `.env` (`MAP_KEY=...`), fora do git.

```
.venv\Scripts\python -m heru status
.venv\Scripts\python -m heru zips
.venv\Scripts\python -m heru historico --simular
.venv\Scripts\python -m heru historico
```

- `zips`: MODIS de 2000 a 2025 e S-NPP de 2012 a 2025 pelos zips anuais por país, sem gastar cota. O S-NPP de 2019 em diante vem primeiro, porque o k20 depende dele. Cada ano vira `data/pontos/camada=X/ano=A/mes=M/SP_zip_A-M.parquet`, com o país do polígono, e `data/zips/{camada}_{ano}_paises.csv` guarda a concordância entre o país do arquivo do zip e o do polígono.
- `historico`: o resto pela Area API (`world`; Landsat no retângulo da América do Norte), janela por janela, respeitando 4.500 transações a cada 10 min. Primeiro vem o NRT dos meses sem SP, depois o SP do mais recente ao mais antigo, com o NOAA-20 até 2019 antes do NOAA-21. Cada janela grava um Parquet com o país e uma linha em `data/manifest.csv`.
- Os dois comandos retomam de onde pararam e podem rodar ao mesmo tempo: gravam partições diferentes e cada um tem a sua trava.

Em 14/11/2026, o `historico` completo tinha 1.459 janelas e 191.080 transações (cerca de 7,1 h), com o NOAA-20 de 2019 completo depois de 130.204 transações.

## Exportação

```
.venv\Scripts\python -m heru exportar
.venv\Scripts\python -m heru exportar --tiles --horas-tiles 3 --a-cada 30 --minutos-pontos 15 --processos 3
.venv\Scripts\python -m heru.conferir
```

- `exportar` agrega cada Parquet do store uma vez, em `data/agregados/`, e só refaz o agregado de um arquivo quando ele muda. O NRT é refeito também quando a máscara de fontes estáticas (`type` 2 do SP) muda.
- Uma camada está "operando" num dia quando algum arquivo do store cobre esse dia. O zip cobre o mês inteiro, e a janela da API vai do início até o número de dias registrado em `data/manifest.csv`. Fora disso a contagem sai vazia, e não zero, então os buracos da carga aparecem como vazios.
- `provisional` = 1 quando algum dia do período vem do NRT de uma camada que ainda vai ter SP. O NRT do Landsat não marca, porque o Landsat não tem SP.
- Em `meta/coverage.csv`, `first_date` é o início do último trecho contínuo que termina em `last_date`; `rows` conta todos os focos.
- O k (S-NPP/MODIS, 2012–2019) entra quando os zips desses anos terminam; o k20 (S-NPP/NOAA-20, 2019–2025) quando o S-NPP desses anos e todas as janelas SP do NOAA-20 estão no store. Quando os fatores mudam, toda a grade é reescrita.
- `points/{camada}/{aaaa}/{dia}.csv.gz` traz todos os focos do dia, de qualquer `type`; as contagens usam só o `type` 0 do SP e o NRT fora da máscara. Sai um arquivo para cada dia coberto, mesmo sem foco (só o cabeçalho). O dia que tem SP vem do SP; o NRT só preenche os dias sem SP. Os pontos são refeitos quando o arquivo do store muda (`data/agregados/focos.json`), saem num lote separado depois da grade e das séries, e cada exportação grava pontos por no máximo `--minutos-pontos` minutos, com `--processos` processos. Enquanto houver atraso, o loop não espera os N minutos.
- Cada execução publica um lote só com os arquivos cujo sha256 mudou. `--tiles` refaz `grid/tiles/` (no máximo a cada `--horas-tiles` horas, ou quando os fatores mudam) e `--a-cada N` repete a exportação a cada N minutos.

## Amostra da Fase 0

```
.venv\Scripts\python -m heru.amostra --store D:\Projetos\firms-mundo\data\pontos
```

Lê o store Parquet do piloto `firms-mundo` e publica um lote em `saida/v1/`: `meta/*`, séries globais e de todos os países, a grade mensal de 2026 e os pontos de 15/08/2026.

O que a amostra ainda não tem:

- S-NPP: o store do piloto não tem essa camada, então `viirs_snpp` fica vazio e a camada não aparece em `meta/coverage.csv`.
- NOAA-20, NOAA-21 e Landsat só a partir de 01/08/2026.
- `harmonized` vazio e `meta/validation.csv` só com o cabeçalho, porque o k depende do S-NPP.
- `meta/static_sources.csv` vem do `type` 2 do MODIS SP.
- Sem `recent/` e sem `grid/tiles/`.

## Testes

```
.venv\Scripts\python -m pytest -q
```

## Histórico do contrato

- 14/11/2026: amostra da Fase 0 publicada.
