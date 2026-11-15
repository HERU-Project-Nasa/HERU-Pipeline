# HERU-Pipeline

FIRMS fire detections (MODIS, VIIRS S-NPP, NOAA-20, NOAA-21 and Landsat) published as static CSV files in `saida/v1/` for the frontend. The file contract is described in the frontend integration manual.

## Environment

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

`pyarrow` is pinned to 22.0.0: on this Windows machine, App Control blocked the Parquet DLL of version 26.

Country polygons are the geoBoundaries CGAZ ADM0, commit 9469f09 (CC BY 4.0), in `data/fronteira/geoBoundariesCGAZ_ADM0.geojson`:

```
curl -L -o data/fronteira/geoBoundariesCGAZ_ADM0.geojson https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/CGAZ/geoBoundariesCGAZ_ADM0.geojson
```

## Historical load

The `MAP_KEY` lives in `.env` (`MAP_KEY=...`), outside git.

```
.venv\Scripts\python -m heru status
.venv\Scripts\python -m heru zips
.venv\Scripts\python -m heru historico --simular
.venv\Scripts\python -m heru historico
```

- `zips`: MODIS from 2000 onward and S-NPP from 2012 to 2026 come from the yearly per-country zips, at no quota cost, up to the year before the current one; a new year's zip is picked up automatically when FIRMS publishes it. S-NPP from 2019 onward comes first, because k20 depends on it. Each year becomes `data/pontos/camada=X/ano=A/mes=M/SP_zip_A-M.parquet`, with the country from the polygon, and `data/zips/{camada}_{ano}_paises.csv` records how well the country of each zip file agrees with the polygon country.
- `historico`: everything else comes from the Area API (`world`; Landsat in the North America rectangle), window by window, staying within 4,500 transactions every 10 min. First the NRT for months without SP, then SP from the most recent to the oldest, with NOAA-20 up to 2019 before NOAA-21. Each window writes a Parquet file with the country and a line in `data/manifest.csv`.
- Both commands resume where they stopped and can run at the same time: they write different partitions and each has its own lock.

On 2026-11-14, the full `historico` had 1,459 windows and 191,080 transactions (about 7.1 h), with NOAA-20 for 2019 complete after 130,204 transactions.

## Export

```
.venv\Scripts\python -m heru exportar
.venv\Scripts\python -m heru exportar --tiles --horas-tiles 3 --a-cada 30 --minutos-pontos 15 --processos 3
.venv\Scripts\python -m heru.conferir
```

- `exportar` aggregates each Parquet file in the store once, into `data/agregados/`, and only rebuilds a file's aggregate when the file changes. NRT is also rebuilt when the static-source mask (SP `type` 2) changes.
- A layer is "operating" on a day when some file in the store covers that day. A zip covers the whole month, and an API window runs from its start up to the number of days recorded in `data/manifest.csv`. Outside that, the count is empty rather than zero, so gaps in the load show up as empty.
- `provisional` = 1 when any day in the period comes from the NRT of a layer that will still get SP. Landsat NRT does not set it, because Landsat has no SP.
- In `meta/coverage.csv`, `first_date` is the start of the last continuous stretch that ends at `last_date`; `rows` counts every detection.
- `complete_date` is the row's last closed UTC day. SP and zips close every day. An NRT window closes day D when it was downloaded after 06:00 UTC on D+1 (`quando_utc` in `data/manifest.csv`, or the file date when the window is not listed there). The day after `complete_date` is partial and is left out of comparisons with other years.
- k (S-NPP/MODIS, 2012–2019) is applied once the zips for those years are done; k20 (S-NPP/NOAA-20, 2019–2025) once S-NPP for those years and all NOAA-20 SP windows are in the store. When the factors change, the whole grid is rewritten.
- `points/{camada}/{aaaa}/{dia}.csv.gz` holds every detection of the day, of any `type`; the counts use only SP `type` 0 and NRT outside the mask. One file is written for each covered day, even with no detections (header only). A day that has SP comes from SP; NRT only fills days without SP. Points are rebuilt when the store file changes (`data/agregados/focos.json`), go out in a separate batch after the grid and the series, and each export writes points for at most `--minutos-pontos` minutes, with `--processos` processes. While there is a backlog, the loop does not wait the N minutes.
- `meta/validation_monthly.csv` has, month by month from 2020 to 2024, the observed S-NPP, MODIS and MODIS × k (the reconstructed S-NPP), for the world and for each country in `meta/validation.csv`. These are the same numbers behind `median_error_2020_2024`.
- `series/countries_yearly.csv` combines every country's `yearly.csv` into one file, with the `iso3` column first.
- `meta/countries.csv` has `area_km2` and `area_source`: the World Bank land area (`AG.LND.TOTL.K2`, most recent value) and, for countries it does not cover (ATA, ESH, FLK, TWN, VAT and XKX), the CGAZ polygon area in an equal-area projection. The table lives in `heru/dados/area_paises.csv` and is rebuilt with `.venv\Scripts\python -m heru.area`.
- `points/` and `recent/` carry latitude and longitude with 5 decimals, except Landsat, which comes from FIRMS with 6 and is written with 6. Rounded to 5, a detection near the edge of a 0.01° cell moved to another cell and fell inside or outside the static-source mask the opposite way from the counts.
- Each run publishes a batch with only the files whose sha256 changed. `--tiles` rebuilds `grid/tiles/` (at most every `--horas-tiles` hours, or when the factors change) and `--a-cada N` repeats the export every N minutes.

## Real time (Phase 4)

```
.venv\Scripts\python -m heru atualizar --a-cada 60 --tiles --horas-tiles 3 --minutos-pontos 15 --processos 3
.venv\Scripts\python -m heru atualizar --diario
```

- Every `--a-cada` minutes, `atualizar` downloads the FIRMS 48 h global CSVs (MODIS, NOAA-20, NOAA-21 and Landsat for the US and Canada), which cost no quota, and runs the same export as `exportar`. S-NPP is left out: it stopped observing on 2026-11-02 and its CSV comes back empty.
- CSV detections go into the store's 5-day NRT window (`NRT_{inicio}.parquet`), the same one the API writes. The window is read, receives the new detections without repeating the key latitude, longitude, date, time and satellite (an existing detection takes the CSV values), and is rewritten only when it gained a detection or a day. Each rewrite adds a line to `data/manifest.csv` with `origem` = `csv_global` and `dias` up to the CSV's last day, so `complete_date` moves forward on its own.
- Once per UTC day (and on the first run, or with `--diario`), the 7-day CSVs come in to close gaps, together with the NRT-to-SP swap: it reads `data_availability`, downloads the new SP windows from the Area API (about 1,100 transactions per layer and month) and deletes each NRT file whose days SP already covers in full. Without `MAP_KEY`, or with `--sem-sp`, the swap is postponed. The daily state lives in `data/agregados/atualizar.json`.
- The same daily round runs the SP-to-zip swap. From January on, `atualizar` looks for the zip of the year that just closed (MODIS every year; S-NPP only up to 2026, because it stopped). When the zip is out, the year is loaded as in `zips`, and each month the zip contains loses its API SP and NRT files for that month. From then on, neither `historico` nor the NRT-to-SP swap asks the API for anything from that year. The year's counts drop by 0.2% to 0.4%, because of the detections at sea that the zip does not include, and `source` changes from `api` to `zip` in the points. While the zip is not out, the log shows `zip ainda não saiu` once a day. If `data/.trava_zips` exists (`zips` running by hand), the swap moves to the next day; `--sem-sp` also turns this swap off.
- In the same daily round, before merging the 7-day CSV, each layer checks the store's last closed day. If the CSV starts after it (the pipeline was stopped for more than 7 days), the NRT windows for the gap, from the start of the window of the first missing day up to the first day of the CSV, come from the Area API, as in `historico`, and the CSV is merged on top. It costs 36 transactions per day for MODIS, NOAA-20 and NOAA-21 and 4 for Landsat, about 112 per day of downtime. Without `MAP_KEY`, an `ATRASO` line is logged with the `historico --inicio` command to run by hand.
- A layer whose CSV failed, or whose last day is more than 1 day behind today, produces an `ATRASO` line in the log; everything else is still published.
- `recent/{camada}/24h.csv.gz`, `48h.csv.gz` and `7d.csv.gz` are written on every export with the NRT detections whose overpass (`acq_date` + `acq_time`, UTC) falls within the last 24 h, 48 h or 7 days counted from the export time, with the same columns as `points/`. A layer with no detections in the period is written with the header only.
- `atualizar` holds `data/.trava` and `data/.trava_exportar`: stop the loop before running `historico`, `zips` or `exportar` by hand. A lock whose process has died, or that was written before the last boot, is removed automatically on the next run, with a `trava velha apagada` line in the log.
- `--log FILE` appends the output to that file and the errors to the `.err` file next to it, to run with `pythonw.exe`, without a console.
- An unexpected error does not bring the loop down. A failure collecting one layer does not stop the others; a failure in the whole collection does not stop the export; a failure in the export does not stop the next round. Each error is logged as `ERRO em {etapa}: {tipo}: {mensagem}; a pipeline segue`, with the traceback in the `.err` file. If the daily round failed, it runs again on the next round. The months an interrupted export was about to rewrite stay in `meses_pendentes` (`data/agregados/estado.json`) and go out on the next export. With `--a-cada 0`, the run exits with code 1 when there was an error.
- The global CSVs lag: at 05:10 UTC on 2026-11-15, the last detection in the 24 h CSV was from 2026-11-14 at 23:39.

### Automatic start on Windows

Before the VPS, the loop started at Windows login through a shortcut in the user's Startup folder (`shell:startup`), created like this:

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Startup")) "HERU atualizar.lnk"))
$lnk.TargetPath = "D:\Projetos\HERU-Pipeline\.venv\Scripts\pythonw.exe"
$lnk.Arguments = "-m heru atualizar --a-cada 60 --tiles --horas-tiles 3 --minutos-pontos 15 --processos 3 --log D:\Projetos\HERU-Pipeline\data\atualizar.log"
$lnk.WorkingDirectory = "D:\Projetos\HERU-Pipeline"
$lnk.WindowStyle = 7
$lnk.Save()
```

- After shutting down or restarting the PC, logging in is enough: the loop starts without a window and removes stale locks. Without a login (for example, stuck at the sign-in screen after a Windows update), nothing runs.
- To stop it: end both `pythonw` processes (the child PID is in `data/.trava`). To turn off the automatic start, delete the shortcut.

### VPS (Phase 5)

On the VPS (Ubuntu with Python 3.12 or newer), the pipeline runs as the `heru` system user, in `/srv/heru/HERU-Pipeline`, through the systemd service `deploy/heru-atualizar.service`. The arguments are the same as the loop above, with `--a-cada 10` and without `--log`: output and tracebacks go to the journal.

- Collection runs every 10 minutes, counted from the start of the previous one, and the export follows right after, in the same cycle. The global CSVs do not spend `MAP_KEY` transactions. The limit of 5,000 transactions every 10 minutes applies only to the Area API, which is used in the daily round and for gap filling, and `cota` already spaces those calls within the window.
- On the VPS (2 vCPU), a normal cycle takes less than 1 minute: on 2026-11-15, collection at 17:38:32 UTC and batch published at 17:39:18. The cycle that rebuilds the tiles (at most every 3 hours) takes a few minutes longer. If a cycle goes past 10 minutes, the next collection starts as soon as it ends.
- After copying `data/` from another machine, the first cycle recomputes every aggregate and rewrites all of `points/`, because the copy changes the files' `st_mtime_ns`. On the VPS this took about 37 minutes, in three batches, with a peak of 5.7 GB of RAM.

One-time installation, as root:

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

Next come `.env` (owned by `heru`, `chmod 600`) and the `data/` and `saida/` folders from the previous loop, copied while that loop is stopped. Only then is the service turned on, with `systemctl enable --now heru-atualizar`. If it starts without `data/`, `atualizar` builds the store from scratch.

- To update the code after a merge into `main`: `ssh root@VPS_IP /srv/heru/HERU-Pipeline/deploy/deploy.sh`. The script runs `git pull`, installs the dependencies, runs the tests, reinstalls the service file and restarts the service if it is running. If a test fails, the script stops before restarting.
- Log: `journalctl -u heru-atualizar -f`. Warnings only: `journalctl -u heru-atualizar -g "ERRO|ATRASO"`.
- To stop: `systemctl stop heru-atualizar`. systemd sends SIGINT and `atualizar` releases the locks before exiting. After a crash, the service comes back on its own in 60 s; after a boot, it starts without a login.
- The 4 GB swap absorbs export peaks with 3 processes on an 8 GB RAM VPS.

## Phase 0 sample

```
.venv\Scripts\python -m heru.amostra --store D:\Projetos\firms-mundo\data\pontos
```

Reads the Parquet store of the `firms-mundo` pilot and publishes a batch in `saida/v1/`: `meta/*`, global and per-country series, the 2026 monthly grid and the points for 2026-08-15.

What the sample still lacks:

- S-NPP: the pilot store does not have this layer, so `viirs_snpp` is empty and the layer does not appear in `meta/coverage.csv`.
- NOAA-20, NOAA-21 and Landsat only from 2026-08-01.
- `harmonized` is empty and `meta/validation.csv` has only the header, because k depends on S-NPP.
- `meta/static_sources.csv` comes from MODIS SP `type` 2.
- No `recent/` and no `grid/tiles/`.

## Tests

```
.venv\Scripts\python -m pytest -q
```

## Contract history

- 2026-11-14: Phase 0 sample published.
- 2026-11-15: Phase 4 live: `recent/` and hourly batches from the FIRMS global CSVs.
- 2026-11-15: `meta/validation_monthly.csv`, `series/countries_yearly.csv`, country areas in `meta/countries.csv` and Landsat with 6 decimals in `points/` and `recent/`.
- 2026-11-15: API SP replaced by the yearly zip once FIRMS publishes the year's zip (the first will be 2026, in 2027).
