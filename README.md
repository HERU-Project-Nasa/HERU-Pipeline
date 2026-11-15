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

- `zips`: MODIS de 2000 em diante e S-NPP de 2012 a 2026 pelos zips anuais por país, sem gastar cota, até o ano anterior ao atual; o zip de um ano novo entra sozinho quando o FIRMS publica. O S-NPP de 2019 em diante vem primeiro, porque o k20 depende dele. Cada ano vira `data/pontos/camada=X/ano=A/mes=M/SP_zip_A-M.parquet`, com o país do polígono, e `data/zips/{camada}_{ano}_paises.csv` guarda a concordância entre o país do arquivo do zip e o do polígono.
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
- `complete_date` é o último dia UTC fechado da linha. O SP e os zips fecham todos os dias. Uma janela NRT fecha o dia D quando foi baixada depois das 06:00 UTC de D+1 (`quando_utc` em `data/manifest.csv`, ou a data do arquivo quando a janela não está lá). O dia depois de `complete_date` está pela metade e fica fora das comparações com outros anos.
- O k (S-NPP/MODIS, 2012–2019) entra quando os zips desses anos terminam; o k20 (S-NPP/NOAA-20, 2019–2025) quando o S-NPP desses anos e todas as janelas SP do NOAA-20 estão no store. Quando os fatores mudam, toda a grade é reescrita.
- `points/{camada}/{aaaa}/{dia}.csv.gz` traz todos os focos do dia, de qualquer `type`; as contagens usam só o `type` 0 do SP e o NRT fora da máscara. Sai um arquivo para cada dia coberto, mesmo sem foco (só o cabeçalho). O dia que tem SP vem do SP; o NRT só preenche os dias sem SP. Os pontos são refeitos quando o arquivo do store muda (`data/agregados/focos.json`), saem num lote separado depois da grade e das séries, e cada exportação grava pontos por no máximo `--minutos-pontos` minutos, com `--processos` processos. Enquanto houver atraso, o loop não espera os N minutos.
- `meta/validation_monthly.csv` traz, mês a mês de 2020 a 2024, o S-NPP observado, o MODIS e o MODIS × k (o S-NPP reconstruído), no mundo e em cada país da `meta/validation.csv`. São os mesmos números do `median_error_2020_2024`.
- `series/countries_yearly.csv` junta o `yearly.csv` de todos os países num arquivo só, com a coluna `iso3` na frente.
- `meta/countries.csv` traz `area_km2` e `area_source`: a área de terra do Banco Mundial (`AG.LND.TOTL.K2`, valor mais recente) e, para os países que ele não tem (ATA, ESH, FLK, TWN, VAT e XKX), a área do polígono do CGAZ numa projeção de área igual. A tabela fica em `heru/dados/area_paises.csv` e é refeita com `.venv\Scripts\python -m heru.area`.
- `points/` e `recent/` saem com latitude e longitude em 5 casas, menos o Landsat, que vem do FIRMS com 6 e sai com 6. Arredondado para 5, um foco perto da borda da quadrícula de 0,01° mudava de quadrícula e ficava fora ou dentro da máscara de fontes estáticas ao contrário das contagens.
- Cada execução publica um lote só com os arquivos cujo sha256 mudou. `--tiles` refaz `grid/tiles/` (no máximo a cada `--horas-tiles` horas, ou quando os fatores mudam) e `--a-cada N` repete a exportação a cada N minutos.

## Tempo real (Fase 4)

```
.venv\Scripts\python -m heru atualizar --a-cada 60 --tiles --horas-tiles 3 --minutos-pontos 15 --processos 3
.venv\Scripts\python -m heru atualizar --diario
```

- A cada `--a-cada` minutos, `atualizar` baixa os CSVs globais de 48 h do FIRMS (MODIS, NOAA-20, NOAA-21 e Landsat de EUA e Canadá), que não gastam cota, e roda a mesma exportação do `exportar`. O S-NPP fica de fora: parou de observar em 02/11/2026 e o CSV dele vem vazio.
- Os focos do CSV entram na janela NRT de 5 dias do store (`NRT_{inicio}.parquet`), a mesma que a API grava. A janela é lida, recebe os focos novos sem repetir a chave lat, lon, data, hora e satélite (o foco que já existia fica com os valores do CSV) e é regravada só quando ganhou foco ou dia. Cada regravação anota uma linha em `data/manifest.csv` com `origem` = `csv_global` e `dias` até o último dia do CSV, então o `complete_date` avança sozinho.
- Uma vez por dia UTC (e na primeira execução, ou com `--diario`) entram os CSVs de 7 dias, para fechar buracos, e a troca do NRT pelo SP: lê o `data_availability`, baixa pela Area API as janelas SP novas (cerca de 1.100 transações por camada e mês) e apaga cada arquivo NRT cujos dias o SP já cobre por inteiro. Sem `MAP_KEY`, ou com `--sem-sp`, a troca fica para depois. O estado diário fica em `data/agregados/atualizar.json`.
- Na mesma rodada diária entra a troca do SP pelo zip. A partir de janeiro, `atualizar` procura o zip do ano que fechou (MODIS todo ano; S-NPP só até o de 2026, porque ele parou). Quando o zip sai, o ano entra como no `zips`, e cada mês que o zip traz perde os arquivos SP da API e NRT daquele mês. Daí em diante o `historico` e a troca do NRT pelo SP não pedem mais nada daquele ano pela API. As contagens do ano caem de 0,2% a 0,4%, pelos focos no mar que o zip não traz, e `source` passa de `api` para `zip` nos pontos. Enquanto o zip não sai, o log mostra `zip ainda não saiu` uma vez por dia. Se `data/.trava_zips` existe (`zips` rodando à mão), a troca fica para o dia seguinte; `--sem-sp` também desliga essa troca.
- Na mesma rodada diária, antes de juntar o CSV de 7 dias, cada camada confere o último dia fechado do store. Se o CSV começa depois dele (a pipeline ficou parada mais de 7 dias), as janelas NRT do buraco, do início da janela do primeiro dia que falta até o primeiro dia do CSV, vêm pela Area API, como no `historico`, e o CSV entra por cima. Custa 36 transações por dia de MODIS, NOAA-20 e NOAA-21 e 4 do Landsat, cerca de 112 por dia parado. Sem `MAP_KEY` sai uma linha `ATRASO` com o `historico --inicio` a rodar à mão.
- Camada cujo CSV falhou ou cujo último dia está mais de 1 dia atrás de hoje gera uma linha `ATRASO` no log; o resto continua publicado.
- `recent/{camada}/24h.csv.gz`, `48h.csv.gz` e `7d.csv.gz` saem a cada exportação com os focos NRT cuja passagem (`acq_date` + `acq_time`, UTC) cai nas últimas 24 h, 48 h ou 7 dias contadas da hora da exportação, nas mesmas colunas de `points/`. Camada sem foco no período sai só com o cabeçalho.
- `atualizar` segura `data/.trava` e `data/.trava_exportar`: pare o loop antes de rodar `historico`, `zips` ou `exportar` à mão. Uma trava cujo processo já morreu, ou gravada antes do último boot, é apagada sozinha na próxima execução, com uma linha `trava velha apagada` no log.
- `--log ARQUIVO` acrescenta a saída nesse arquivo e os erros no `.err` ao lado, para rodar com `pythonw.exe`, sem console.
- Um erro inesperado não derruba o loop. Falha na coleta de uma camada não impede as outras; falha na coleta inteira não impede a exportação; falha na exportação não impede a próxima rodada. Cada erro sai no log como `ERRO em {etapa}: {tipo}: {mensagem}; a pipeline segue`, com o traceback no `.err`. Se a rodada diária falhou, ela roda de novo na rodada seguinte. Os meses que uma exportação interrompida ia reescrever ficam em `meses_pendentes` (`data/agregados/estado.json`) e saem na exportação seguinte. Com `--a-cada 0`, a execução termina com código 1 quando houve erro.
- Os CSVs globais atrasam: às 05:10 UTC de 15/11/2026 o último foco do CSV de 24 h era de 14/11 às 23:39.

### Início automático no Windows

Enquanto não há VPS, o loop sobe no login do Windows por um atalho na pasta Inicializar do usuário (`shell:startup`), criado assim:

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Startup")) "HERU atualizar.lnk"))
$lnk.TargetPath = "D:\Projetos\HERU-Pipeline\.venv\Scripts\pythonw.exe"
$lnk.Arguments = "-m heru atualizar --a-cada 60 --tiles --horas-tiles 3 --minutos-pontos 15 --processos 3 --log D:\Projetos\HERU-Pipeline\data\atualizar.log"
$lnk.WorkingDirectory = "D:\Projetos\HERU-Pipeline"
$lnk.WindowStyle = 7
$lnk.Save()
```

- Depois de desligar ou reiniciar o PC, basta fazer login: o loop sobe sem janela e apaga as travas velhas. Sem login (por exemplo, parado na tela de entrada depois de uma atualização do Windows), nada roda.
- Para parar: encerrar os dois processos `pythonw` (o PID do filho está em `data/.trava`). Para desligar o início automático, apagar o atalho.

### VPS (Fase 5)

Na VPS (Ubuntu com Python 3.12 ou mais novo), a pipeline roda como o usuário de sistema `heru`, em `/srv/heru/HERU-Pipeline`, pelo serviço systemd `deploy/heru-atualizar.service`. Os argumentos são os do loop acima, com `--a-cada 10` e sem `--log`: a saída e os tracebacks vão para o journal.

- A coleta sai a cada 10 minutos, contados do início da anterior, e a exportação vem logo depois, no mesmo ciclo. Os CSVs globais não gastam transações da `MAP_KEY`. O limite de 5.000 transações a cada 10 minutos vale só para a Area API, que entra na rodada diária e no preenchimento de buraco, e o `cota` já espaça essas chamadas dentro da janela.
- Na VPS (2 vCPU), um ciclo comum leva menos de 1 minuto: em 15/11/2026, coleta às 17:38:32 UTC e lote publicado às 17:39:18. O ciclo que refaz os tiles (no máximo a cada 3 horas) leva alguns minutos a mais. Se um ciclo passar de 10 minutos, a coleta seguinte começa assim que ele termina.
- Depois de copiar o `data/` de outra máquina, o primeiro ciclo recalcula todos os agregados e regrava `points/` inteiro, porque a cópia muda o `st_mtime_ns` dos arquivos. Na VPS isso levou cerca de 37 minutos, em três lotes, com pico de 5,7 GB de RAM.

Instalação, uma vez, como root:

```
apt install -y git python3-venv
useradd --system --create-home --home-dir /srv/heru --shell /bin/bash heru
sudo -u heru git clone https://github.com/HERU-Project-Nasa/HERU-Pipeline.git /srv/heru/HERU-Pipeline
cd /srv/heru/HERU-Pipeline
sudo -u heru python3 -m venv .venv
sudo -u heru .venv/bin/pip install -r requirements.txt
install -m 644 deploy/heru-atualizar.service /etc/systemd/system/
systemctl daemon-reload
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab
```

Depois vêm o `.env` (dono `heru`, `chmod 600`) e o `data/` e o `saida/` do loop anterior, copiados com esse loop parado. Só então o serviço é ligado, com `systemctl enable --now heru-atualizar`. Ligado sem o `data/`, o `atualizar` começa o store do zero.

- Atualizar o código depois de um merge na `main`: `ssh root@IP_DA_VPS /srv/heru/HERU-Pipeline/deploy/deploy.sh`. O script faz `git pull`, instala as dependências, roda os testes, reinstala o arquivo do serviço e reinicia o serviço se ele estiver rodando. Com um teste falhando, o script para antes de reiniciar.
- Log: `journalctl -u heru-atualizar -f`. Só os avisos: `journalctl -u heru-atualizar -g "ERRO|ATRASO"`.
- Parar: `systemctl stop heru-atualizar`. O systemd manda SIGINT e o `atualizar` solta as travas antes de sair. Depois de uma queda, o serviço volta sozinho em 60 s; depois de um boot, sobe sem login.
- O swap de 4 GB segura picos da exportação com 3 processos numa VPS de 8 GB de RAM.

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
- 15/11/2026: Fase 4 no ar: `recent/` e lotes de hora em hora com os CSVs globais do FIRMS.
- 15/11/2026: `meta/validation_monthly.csv`, `series/countries_yearly.csv`, área dos países em `meta/countries.csv` e Landsat com 6 casas em `points/` e `recent/`.
- 15/11/2026: troca do SP da API pelo zip anual quando o FIRMS publica o zip do ano (a primeira será a de 2026, em 2027).
