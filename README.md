# EV Grid Simulator Lab

EV Grid Simulator Lab simulates how household EV charging and bidirectional energy trading affect a synthetic electricity distribution network in Greensboro, North Carolina. Compare **no EVs**, **charging only**, and **EV trading** on the same network. The dashboard shows feeder demand, generation costs, customer bills, battery state, and voltage and equipment loading at each 15-minute interval.

The model uses the SMART-DS **uhs19_1247** substation and the 2018 demand calendar. A 69 kV source feeds two feeders with **2,087 buses, 1,755 lines, 335 transformers, and 1,329 electrical load objects**. Reconciling those load objects gives **732 customer connections** (458 residential-profile and 274 commercial-profile). The 447 residential 120/240 V split-phase connections serve as household proxies for fleet allocation. The profiles do not identify a separate set of verified industrial customers.

The [research report](Greensboro%20EV%20Trading%20Research%20Report.md) documents five 150-day experiments, their assumptions, and results. Open the HTML copy beside it in a browser to read the same report with its figures. In those experiments, trading reduced coal and gas generation relative to charging only. Grid violations, household bills, and the battery energy remaining at the end of each run are reported alongside those savings.

## Set up a fresh clone

Use **Linux or Ubuntu in WSL** with Python **3.11 or later**, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/). Run the following commands from the root of the clone, where `pyproject.toml` and `uv.lock` live. On Windows, use the Ubuntu terminal for these commands. If Python and pip are already available but uv is missing, install it with `python3 -m pip install --user uv` where user installs are supported, or use the linked installer.

```bash
uv sync --locked --dev
uv run --locked python -m gridlab.download
uv run --locked python -m gridlab.nhts
uv run --locked python -m gridlab
```

Open [http://localhost:8765](http://localhost:8765) and wait for the grid to report that it is ready. The travel panel separately reports when its diary library is ready. The server binds to `127.0.0.1`. Press **Ctrl+C** in the terminal to stop it. If the port is occupied, use `uv run --locked python -m gridlab --port 8766` and open [http://localhost:8766](http://localhost:8766).

### Downloaded inputs

| Input | How to get it | Where it goes |
|---|---|---|
| SMART-DS v1.0 Greensboro model and demand profiles | `uv run --locked python -m gridlab.download` | `data/SMART-DS/` and `data/manifest.json` |
| FHWA 2022 National Household Travel Survey (NHTS) V2.1 | `uv run --locked python -m gridlab.nhts` | `data/nhts/2022-v2.1/` |
| Greensboro solar irradiance | The prepared 2018 profile ships with the repo | `gridlab/resources/greensboro-2018-gti.json` |

- The SMART-DS downloader uses the public `oedi-data-lake` S3 bucket and fetches only the selected substation, its reference files, and its 444 referenced active/reactive power profiles (about 301 MB). It needs no AWS credentials. Rerun it after an interruption to fetch missing files. It generates `Baseline.dss` for interval-by-interval simulation and records source keys, sizes, and SHA-256 hashes in `data/manifest.json`.
- The NHTS command downloads the [CSV archive](https://nhts.ornl.gov/media/2022/download/csv.zip) and [codebook](https://nhts.ornl.gov/media/2022/doc/codebook.xlsx), then builds the local diary cache. If the automatic transfer fails, save the official files as `csv.zip` and `codebook.xlsx` in `data/nhts/2022-v2.1/` and rerun the command. The server also attempts this preparation at startup. A baseline can run while the travel library is preparing.
- To regenerate solar data from the public NSRDB archive, run the optional command below from the repo root. It reads the required site through HTTP range requests and rewrites the bundled JSON. Routine simulations use the bundled file directly.

```bash
uv run --locked --with fsspec --with h5py --with pvlib python scripts/prepare_solar.py
```

Allow room for the environment and downloaded data plus the output for each run. A long EV run saves thousands of snapshots and per-vehicle interval records and can use several GB.

## Run a simulation

1. Set **Start date** and **Simulation days**. A run may cover **1–150 days** entirely within **2018**, with **96 intervals per day**. The dashboard opens with **April 1, 2018** and **150 days** selected. For a first run, change the duration to **1 day**.
2. Click **Run baseline** to solve ordinary demand and generation without an EV fleet. For a full comparison, set the fleet and travel controls, click **Generate EV fleet**, and wait for the daily plans to be ready. Then click **Run EV simulation**. This runs all three cases, including the baseline.
3. For a quick EV trial, set **EV rollout start** to the same day as **Start date**. This skips the warm-up and starts the batteries empty. Leave the rollout date blank for a regular experiment. The default is 31 days before the simulation, bounded by January 1, 2018.
4. Watch the progress indicator. **Stop run** requests cancellation and preserves completed intervals. Use **Run history** to reopen a saved run and **Export CSV** for its interval series.

One simulation can run at a time. Save the scenario settings or retain its run folder to reproduce a comparison. The [local API docs](http://localhost:8765/docs) describe the request schema and routes. API field defaults can differ from the dashboard defaults listed below.

## Dashboard controls

### Fleet and travel

| Control | What it does | Dashboard default |
|---|---|---|
| **Random seed** | Reproduces ownership assignment, EV selection, and fleet equipment choices. | 32 |
| **Fleet size setting** | Choose a percentage of all household vehicles or a fixed EV count. A fixed count cannot exceed available vehicle slots. | 50% adoption |
| **Home charger access in fleet inventory** | Changes access displayed in the saved inventory. The EV simulation equips every EV with a home charger. | 100% |
| **Level 1 among home chargers** | Sets the share targeted for Level 1 instead of Level 2. | 20% |
| **Generate EV fleet**, **Saved fleet**, **Fleet JSON** | Build or restore an inventory and export its assignments. Generating a fleet prepares travel plans for the selected dates. | |
| **Mean distance**, **Standard deviation**, **Travel seed** | Set daily distance targets in km and reproduce diary matching. The mean and standard deviation include unused vehicles. Actual distances follow sampled diaries. | 46 km/day, 22.5 km, seed 42 |
| **Minimum moving distance**, **Maximum distance**, **Unused EVs** | Bound distance targets and set the share that makes no trips in the diary plans. | 0–120 km, 0% unused |
| **Diary distance tolerance**, **Prefer the same month** | Set the matching band and prefer matching month and weekday before broadening the search. | 5 km, on |
| **Saved travel day** | Inspect a saved daily plan for the selected fleet. Use its travel clock and export buttons for schedules, trips, and state intervals. | |

Greensboro American Community Survey 2020–2024 estimates give vehicle ownership shares of 8.2%, 40.1%, 34.2%, and 17.5% for households with zero, one, two, and three or more vehicles. The model splits the last group into 12%, 4%, and 1.5% for three, four, and five vehicles. That allocates 751 vehicles to 447 household proxies, or 1.68 vehicles per household. The default 50% setting selects 376 EVs. Battery and charger assignments are seeded scenario inputs.

| EV class | Target share | Battery | Level 2 rating | Mean driving use |
|---|---:|---:|---:|---:|
| Small | 5% | 35 kWh | 3.84 kW | 0.15 kWh/km |
| Compact | 20% | 55 kWh | 7.68 kW | 0.17 kWh/km |
| Long-range / family | 60% | 77.5 kWh | 9.60 kW | 0.20 kWh/km |
| Large SUV / pickup | 15% | 110 kWh | 11.52 kW | 0.27 kWh/km |

Level 1 charging is rated at 1.44 kW. The travel library filters national NHTS driver records into complete home-to-home vehicle-day diaries. Sampling preserves their trip times and distances. During rollout, EVs enter gradually with empty batteries, drive and charge without selling, and carry their battery energy and its stored cost into the comparison dates.

### Charging and trading

| Control | What it does | Dashboard default |
|---|---|---|
| **EV rollout start** | Sets the first warm-up day. The warm-up ends the day before the comparison. | 31 days earlier, bounded by Jan 1 |
| **Battery and driving seed** | Reproduces battery and per-vehicle driving consumption inputs. The rollout determines the opening battery charge for the dashboard comparison. | 42 |
| **Charging efficiency** and **Discharge efficiency** | Convert charger input to stored energy and battery output to metered exports. At 90% each way, round-trip efficiency is 81% before feeder losses. | 90% each |
| **Allow charging away from home**, **Nonresidential buses equipped**, **Destination seed** | Assign away stops to eligible commercial-profile buses. Equipped buses have 1–5 ports, assigned on arrival when one is free and occupied until departure. | On, 100%, seed 42 |
| **Trading EV behavior** | **Buy, sell, or wait** uses the battery and price rules below. **Sell with automatic charging** checks for a sale and otherwise charges when possible. EVs outside the trading share charge automatically when connected. | Buy, sell, or wait |
| **Trading price mode** | **Fixed price per interval** uses the previous realized price as the offer for that interval. **Dynamic price per EV** recomputes quotes as traders commit. | Dynamic |
| **Trading EVs**, **Trading EVs that stay home**, **Trading seed** | Set the share eligible to trade, the share of those traders kept home for every day of the run, and the trader selection and ordering seed. Charging-only uses the same trip restrictions. | 100% eligible, 0% stay home, seed 42 |
| **Minimum battery charge**, **Travel reserve buffer** | Protect a minimum state of charge for exports and add a buffer to planned driving energy. | 10%, 5% |
| **Export limit per EV** | Caps exports at the lower of this power and the modeled charging power. | 7.2 kW |
| **Wear cost** | Sets a trading cost per battery kWh discharged. Battery capacity stays fixed throughout the simulation. | $0/kWh |
| **Allow export at equipped away chargers** | Enables sales when a vehicle occupies an away port. | On |
| **Allow EV sales despite grid violations** | On allows sales from a valid solve even if screens flag a violation. Off cancels the interval’s proposed sales when the screening check fails, then reruns power flow with purchases retained. | On |

### Generation costs and dispatch

| Control | What it does | Dashboard default |
|---|---|---|
| **Generation sizing** | **Use baseline minimum** sets nuclear from the lowest baseline draw minus the coal/gas minimum, floored at zero. **Set nuclear output manually** uses the entered nuclear MW. **Set total generation minimum** uses the entered total minus the coal/gas minimum. | Manual |
| **Fixed nuclear generation** or **Minimum total generation** | Sets the value required by the selected sizing method. The total minimum refers to nuclear plus the coal/gas floor. | 3.696 MW nuclear or 4.296 MW total |
| **Minimum coal + natural gas generation** | Sets the floor for the flexible source. Above that floor, it covers demand remaining after fixed nuclear and solar. | 0.60 MW |
| **Nuclear cost**, **Coal + gas cost**, **Solar generation cost** | Set generation costs per MWh of output. The average cost used for pricing is total generation cost divided by total generated energy. | $9.16, $31.065, and $0 per MWh |
| **Solar rated capacity**, **Solar performance factor** | Scale modeled solar output from the irradiance profile. Set capacity to 0 MW to disable solar. | 8 MW, 0.85 |

## How trades clear

1. The simulator sets an opening quote from the generation mix. In dynamic mode it estimates demand from the current baseline draw, passive charging, and the previous interval’s net trading energy.
2. Each 15-minute interval uses a fresh seeded shuffle of trading EVs. As each EV commits, its buy adds demand or its sell subtracts demand. These commitments replace the previous net estimate in proportion to how many traders have decided. The simulator updates the quote for the next EV.
3. In **Buy, sell, or wait** mode, a connected EV below its protected energy target first tries to charge to that target. The target is the greater of the minimum battery charge and planned driving energy plus the buffer. After the final home arrival it uses the next day's plan. An EV with spare energy can sell when its quote covers the stored energy cost, discharge losses, and configured wear cost. When it does not sell, it may buy if charging would lower its average stored energy cost after expected losses. Otherwise it waits.
4. OpenDSS solves the resulting power flow. Cleared trades settle at their accepted quotes, while the realized generation price is recomputed from solved substation draw. Buyers pay an allocated share of distribution losses. The simulator carries battery energy and its weighted-average cost forward and records excess generation as surplus.

Power flow is quasi-static, with a solved state for each interval and no upstream transmission model. The voltage screen is 0.95–1.05 p.u. Equipment loading is checked against line and transformer ratings. These are research settings. Coal and natural gas are combined into one dispatchable source. Input profiles and EV characteristics represent modeled scenarios, with no claim about actual local utility bills, equipment limits, or regulatory compliance.

## Inspect and export results

| Dashboard section | What to look for |
|---|---|
| **Power through time** | Compare baseline, charging-only, and trading power. Click a plot or scrub/play the electrical timeline to select a 15-minute interval. **Follow live** moves the selection as a run progresses. |
| **Generation and cost** | Compare nuclear, coal/gas, solar, surplus, and generation cost. Expand interval trading prices and quantities, daily totals, and midnight carryover. Use **Billing day**, **Bill scenario**, **Account type**, and the search box to find household, individual EV, and grid meter accounts. |
| **Household vehicles & EV storage** | See ownership, battery categories, nominal capacity, and installed charging potential. Filter by household or bus, toggle **Homes with EVs**, or select **Inspect**. |
| **EV travel through the day** | Use the separate travel clock and **Play day** to inspect trip and parking states. The optional map badges show vehicles currently at home. Select a vehicle or download **Travel JSON**, **Trips CSV**, or **State intervals CSV**. |
| **Electrical network** | Drag to pan, scroll to zoom, use **Fit** to reset, and switch between voltage, loading, loss, equipment type, and EV fleet layers. Click an element or use **Find** to open its details. |
| **EV charging comparison** | Inspect charging changes and equipment limits by interval. Use the battery tracker to select scenario, date, vehicle, and time. Download results as a 15-minute CSV or battery schedule. |
| **Diagnostics**, **Event log**, **Run history** | Review solver and limit checks, filter logs to the selected run, and return to completed, cancelled, or interrupted jobs. |

Results are written to `runs/<id>/`, including `job.json`, interval snapshots, and `timeseries.csv`. EV comparison runs also save schedules, battery and market ledgers, and bills. `runs/fleets/` and `runs/travel/` store reproducible inputs, `runs/rollouts/` caches warm-up state, and `runs/server.jsonl` holds server events. Keep any run folders you want to archive outside Git.

## Checks and Git

Run the tests after preparing the data. Tests that require external inputs can skip when those inputs are missing.

```bash
uv run --locked pytest -q
```

Keep source, tests, `pyproject.toml`, `uv.lock`, the bundled solar profile, and report assets in Git. `.gitignore` excludes downloaded `data/`, generated `runs/`, virtual environments, and local caches. Review the staged contents before the first push:

```bash
# Use git init -b main first if this directory has no existing Git repository.
git status --short --ignored
git add .
git diff --cached --stat
```

If data or run output was already tracked in an existing history, Git ignore rules apply only to untracked files. To stop tracking those paths while keeping the files on disk, use `git rm -r --cached --ignore-unmatch data runs .venv` before committing. This removes them from the next tree only. Previously committed copies remain in earlier commits.

For a fresh repository on the `main` branch, commit after reviewing the staged files, then push to an empty remote. Replace the example URL with your repository URL.

```bash
git commit -m "Add EV Grid Simulator Lab"
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPO.git
git push -u origin main
```

Data sources include [SMART-DS v1.0 (CC BY 4.0)](https://data.openei.org/submissions/2981), [Greensboro ACS 2020–2024 housing estimates](https://data.census.gov/table/ACSDP5Y2024.DP04?g=160XX00US3728000), [FHWA 2022 NHTS](https://nhts.ornl.gov/downloads), and [NSRDB GOES CONUS](https://developer.nlr.gov/docs/solar/nsrdb/nsrdb-GOES-conus-v4-0-0-download/).
