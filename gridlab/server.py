"""Loopback-only HTTP API and persistent simulation job manager."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta
import csv
import json
import logging
import threading
import time
import traceback
import uuid
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import ROOT, RUNS, DATA, MAX_SIMULATION_DAYS
from . import battery, charging, engine, fleet, generation, market, nhts, sequence, travel
from .charging import ChargingConfig
from .generation import GenerationConfig
from .market import MarketConfig
from .solar import load_solar

LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gridlab")
STATE = {"ready": False, "preparing": True, "error": None, "network": None,
         "profile_validation": None, "jobs": {}, "active": None,
         "service_registry": None, "fleets": {}, "current_fleet": None}
CANCEL = threading.Event()
LOG = RUNS / "server.jsonl"
TRAVEL_LOCK = threading.RLock()
TRAVEL_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="travel")
TRAVEL_DIR = RUNS / "travel"
TRAVEL_DIR.mkdir(exist_ok=True)
TRAVEL_STATE = {"ready":False,"preparing":True,"error":None,"source":None}


def prepare_travel():
    try:
        log("INFO","Preparing NHTS home-to-home vehicle-day diary library")
        source=nhts.prepare()
        with TRAVEL_LOCK:
            TRAVEL_STATE.update(ready=True,preparing=False,error=None,source=source)
        log("INFO","NHTS travel library ready",audit=source["audit"],source_digest=source["digest"])
    except Exception as exc:
        with TRAVEL_LOCK:
            TRAVEL_STATE.update(ready=False,preparing=False,error=str(exc))
        log("ERROR","NHTS travel preparation failed",error=str(exc),traceback=traceback.format_exc())


def log(level, message, **fields):
    entry = {"time": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"), "level": level,
             "message": message, **fields}
    with LOCK:
        with LOG.open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
    print(f"[{level}] {message}", flush=True)


def persist(job):
    folder = RUNS / job["id"]
    folder.mkdir(exist_ok=True)
    temp = folder / "job.tmp"
    temp.write_text(json.dumps(job, allow_nan=False))
    temp.replace(folder / "job.json")


def prepare():
    try:
        log("INFO", "Loading SMART-DS uhs19_1247 and validating annual profiles")
        validation = engine.validate_profiles()
        if not validation["passed"]:
            raise ValueError(f"Profile validation failed: {validation['errors'][:5]}")
        d = engine.context()
        engine.solve(d, 1, 0)
        network, ratings = engine.topology(d)
        network["engine_version"] = d.Basic.Version()
        baseline, _ = engine.state(d, network, ratings, 1, 0)
        network["initial_diagnostics"] = baseline
        registry = fleet.service_registry(network)
        network["service_registry_summary"] = registry["summary"]
        initialize_fleets(registry)
        (RUNS / "network.json").write_text(json.dumps(network, allow_nan=False))
        with LOCK:
            STATE.update(ready=True, preparing=False, network=network, profile_validation=validation)
        log("INFO", f"Ready: {network['bus_count']} buses, {network['load_objects']} load objects; {validation['checked']} profiles checked")
        if network["unreachable_buses"]:
            log("WARNING", f"{network['unreachable_buses']} buses are disconnected in the static topology; inspect before interpretation")
    except Exception as exc:
        with LOCK:
            STATE.update(preparing=False, error=str(exc))
        log("ERROR", "Dataset preparation failed", error=str(exc), traceback=traceback.format_exc())


def add_comparison(row, snapshot, baseline, reference, network, interval):
    for key in ("supply_kw", "load_kw", "loss_kw", "line_loss_kw", "transformer_loss_kw",
                "max_line_loading_pct", "max_transformer_loading_pct", "min_voltage_pu",
                "max_voltage_pu", "low_voltage_buses", "high_voltage_buses"):
        row[f"baseline_{key}"] = baseline[key]
    for key in ("supply_kw", "load_kw", "loss_kw"):
        row[f"delta_{key}"] = row[key] - baseline[key]
        row[f"delta_{key.removesuffix('_kw')}_kwh"] = row[f"delta_{key}"] * .25
    row.update(ev_charge_kwh=row["ev_charge_kw"]*.25,
               ev_home_charge_kwh=interval["home_grid_kwh"], ev_away_charge_kwh=interval["away_grid_kwh"],
               ev_battery_gain_kwh=interval["battery_kwh"], ev_driving_kwh=interval["drive_kwh"],
               ev_unmet_driving_kwh=interval["unmet_kwh"], evs_charging=interval["charging_evs"],
               ev_battery_end_kwh=interval["battery_end_kwh"])
    if abs(row["ev_charge_kwh"] - interval["grid_kwh"]) > 1e-5:
        raise RuntimeError("Solved charger draw differs from the battery energy schedule")
    for kind in ("line", "transformer"):
        changes = [(after["loading_pct"] - before["loading_pct"], element["id"])
                   for element, after, before in zip(network["elements"], snapshot["elements"], reference["elements"])
                   if element["kind"] == kind and after["loading_pct"] is not None and before["loading_pct"] is not None]
        increase, name = max(changes, default=(0., None))
        row[f"{kind}_max_increase_pp"] = max(0., increase)
        row[f"{kind}_max_increase_id"] = name if increase > 1e-6 else None
    drops = [(before-after, node["id"])
             for node, before, after in zip(network["nodes"], reference["voltages"], snapshot["voltages"])
             if before is not None and after is not None and before >= .1]
    drop, bus = max(drops, default=(0., None))
    row["max_bus_voltage_drop_pu"] = max(0., drop)
    row["max_bus_voltage_drop_id"] = bus if drop > 1e-6 else None
    snapshot["baseline"] = reference


def previous_generation(date, nuclear_mw, config, solar=()):
    previous = Date.fromisoformat(date) - timedelta(days=1)
    profile_date = previous.replace(year=2018)
    day = profile_date.timetuple().tm_yday
    d = engine.context()
    rows = []
    for index in range(96):
        if CANCEL.is_set():
            return None
        engine.solve(d, day, index)
        solar_mw = generation.solar_at(solar, day, index)
        rows.append(generation.dispatch(-float(d.Circuit.TotalPower()[0]), nuclear_mw, config, solar_mw))
    return {"date": previous.isoformat(), "profile_date": profile_date.isoformat(),
            "last_interval_price_per_mwh": rows[-1]["average_generation_cost_per_mwh"],
            **generation.totals(rows, "", config)}


def run_job(job, charging_inputs=None, solar=()):
    folder = RUNS / job["id"]
    series = []
    baseline_series = []
    started = time.monotonic()
    try:
        log("INFO", f"Starting {job['days']} day(s) from {job['date']}", run_id=job["id"])
        d = engine.context()
        first_day = Date.fromisoformat(job["date"]).timetuple().tm_yday
        # Establish the same topology ordering as the published network payload.
        engine.solve(d, first_day, 0)
        network, ratings = engine.topology(d)
        chargers = ()
        if charging_inputs:
            inventory, travel_day, settings = charging_inputs
            trading_only = set(job["assumptions"].get("trading_only_vehicle_ids", ()))
            travel_day = travel.keep_vehicles_home(travel_day, trading_only)
            schedule = charging.build_schedule(inventory, travel_day, settings, network)
            nhts.atomic_json(folder / "charging.json", {**schedule, "vehicles": [
                {key: value for key, value in vehicle.items() if key != "energy_events"}
                for vehicle in schedule["vehicles"]]})
            with LOCK:
                job["charging_summary"] = {k:v for k,v in schedule.items() if k not in {
                    "intervals", "vehicles", "away_grid_kwh_by_bus"}}
            reference_context = engine.context()
            engine.solve(reference_context, first_day, 0)
            chargers = engine.install_chargers(d, network, schedule, settings.seed)
        for index in range(job["intervals"]):
            if CANCEL.is_set():
                status = "cancelled"
                log("WARNING", "Run cancelled; completed intervals retained", run_id=job["id"])
                break
            if charging_inputs:
                engine.solve(reference_context, first_day, index)
                baseline, reference = engine.state(reference_context, network, ratings, first_day, index)
                engine.set_charger_loads(d, chargers, index)
            engine.solve(d, first_day, index)
            row, snapshot = engine.state(d, network, ratings, first_day, index, chargers)
            if charging_inputs:
                add_comparison(row, snapshot, baseline, reference, network, schedule["intervals"][index])
                baseline_series.append(baseline)
            (folder / f"snapshot-{index:05d}.json").write_text(json.dumps(snapshot, allow_nan=False))
            series.append(row)
            with LOCK:
                job.update(completed=len(series), series=list(series), totals=engine.totals(series),
                           elapsed_s=round(time.monotonic()-started, 2))
                if charging_inputs:
                    job["baseline_totals"] = engine.totals(baseline_series)
                    job["totals"].update({key:sum(r[key] for r in series) for key in (
                        "ev_charge_kwh", "ev_home_charge_kwh", "ev_away_charge_kwh",
                        "ev_battery_gain_kwh", "ev_driving_kwh", "ev_unmet_driving_kwh",
                        "delta_load_kwh", "delta_supply_kwh", "delta_loss_kwh")})
                    job["totals"]["peak_ev_charge_kw"] = max(r["ev_charge_kw"] for r in series)
                    job["totals"]["ev_battery_end_kwh"] = series[-1]["ev_battery_end_kwh"]
                persist(job)
            if index % 16 == 0 or index == job["intervals"]-1:
                log("INFO", f"Solved interval {index+1}/{job['intervals']}", run_id=job["id"],
                    min_voltage_pu=row["min_voltage_pu"], balance_residual_kw=row["balance_residual_kw"])
            if not row["balance_ok"]:
                log("WARNING", "Power balance residual exceeds numerical tolerance", run_id=job["id"], interval=index,
                    balance_residual_kw=row["balance_residual_kw"])
        else:
            status = "completed"
        generation_summary = ledger_summary = market_summary = bills = None
        if status == "completed":
            generation_config = GenerationConfig.model_validate(job["assumptions"]["generation"])
            series, generation_summary = generation.allocate(series, generation_config, paired=bool(charging_inputs),
                                                            solar=solar, first_day=first_day)
            # Keep incomplete or invalid dispatch data out of the persisted job.
            json.dumps(generation_summary, allow_nan=False)
            market_settings = job["assumptions"].get("market")
            if charging_inputs or job["days"] == 1:
                log("INFO", "Valuing the opening EV batteries from the prior-day generation mix", run_id=job["id"])
                previous = previous_generation(job["date"], generation_summary["nuclear_mw"], generation_config, solar)
                if previous is None or CANCEL.is_set():
                    status = "cancelled"
                else:
                    first_offer = previous["last_interval_price_per_mwh"]

                    def read_snapshot(index):
                        return json.loads((folder / f"snapshot-{index:05d}.json").read_text())

                    offers = None
                    if market_settings is not None or not charging_inputs:
                        services = ready_registry()["services"]
                        bills, offers = market.reference_bills(series, schedule if charging_inputs else None,
                                                               services, network["loads"], first_offer, read_snapshot)
                    if charging_inputs:
                        ledger, ledger_summary, interval_costs = battery.build_ledger(
                            schedule, series, previous["average_generation_cost_per_mwh"], settings.charge_efficiency, offers)
                        ledger_summary["previous_generation"] = previous
                        nhts.atomic_json(folder / "battery-ledger.json", ledger)
                        for row, cost in zip(series, interval_costs, strict=True):
                            row.update(cost)
                    if market_settings is not None:
                        log("INFO", "Solving EV trading commitments", run_id=job["id"])
                        with LOCK:
                            job.update(phase="trading", trading_completed=0)

                        def trading_progress(count):
                            with LOCK:
                                job.update(trading_completed=count, elapsed_s=round(time.monotonic()-started, 2))
                            if count % 16 == 0:
                                log("INFO", f"Settled trading interval {count}/96", run_id=job["id"])

                        def save_trading_snapshot(index, snapshot):
                            (folder / f"market-{index:05d}.json").write_text(json.dumps(snapshot, allow_nan=False))

                        next_plan_id = job["assumptions"].get("next_travel_id")
                        next_distances = sequence.planned_distances(read_travel(next_plan_id), trading_only) if next_plan_id else None
                        result = market.run_trading(schedule, travel_day["vehicles"], ledger, settings,
                            MarketConfig.model_validate(market_settings), generation_config, generation_summary["nuclear_mw"],
                            first_offer, first_day, network, ratings, services, read_snapshot, save_trading_snapshot,
                            CANCEL.is_set, trading_progress, solar=solar,
                            baseline_kw=[row["baseline_supply_kw"] for row in series], next_distances=next_distances)
                        if result is None:
                            status = "cancelled"
                        else:
                            traded, records, bills["trading"], market_summary = result
                            nhts.atomic_json(folder / "market-ledger.json", records)
                            generation_summary["with_trading"] = generation.totals(traded, "", generation_config)
                            for row, trading_row in zip(series, traded, strict=True):
                                row.update({"trading_" + key:value for key, value in trading_row.items()})
            if bills is not None:
                nhts.atomic_json(folder / "daily-bills.json", bills)
        if series:
            with (folder / "timeseries.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(series[0]))
                writer.writeheader()
                writer.writerows(series)
        with LOCK:
            job.update(status=status, series=series)
            if generation_summary is not None:
                job["generation_summary"] = generation_summary
            if ledger_summary is not None:
                job["battery_ledger_summary"] = ledger_summary
            if market_summary is not None:
                job["market_summary"] = market_summary
            if bills is not None:
                job["billing_summary"] = {case:bill["totals"] for case, bill in bills.items()}
        log("INFO", f"Run {status}", run_id=job["id"], totals=job["totals"])
    except Exception as exc:
        job.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        log("ERROR", "Simulation failed", run_id=job["id"], error=str(exc), traceback=job["traceback"])
    finally:
        with LOCK:
            job["elapsed_s"] = round(time.monotonic()-started, 2)
            persist(job)
            STATE["active"] = None


def run_sequence_job(job, inventory, plans, settings, travel_source, solar=()):
    started = time.monotonic()
    folder = RUNS / job["id"]

    def publish(save=True, **changes):
        with LOCK:
            job.update(elapsed_s=round(time.monotonic()-started, 2), **changes)
            if save:
                persist(job)

    try:
        log("INFO", f"Starting {job['days']} day EV comparison from {job['date']}", run_id=job["id"])
        next_plan_id = job["assumptions"].get("next_travel_id")
        next_plan = read_travel(next_plan_id) if next_plan_id else None
        sequence.run_sequence(job, inventory, plans, settings, travel_source, ready_registry(),
                              publish, CANCEL.is_set, add_comparison, solar=solar, next_plan=next_plan)
        status = "completed" if job["completed"] == job["intervals"] else "cancelled"
        if job["series"]:
            with (folder / "timeseries.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(job["series"][0]))
                writer.writeheader()
                writer.writerows(job["series"])
        publish(status=status)
        log("INFO", f"EV comparison {status}", run_id=job["id"], intervals=job["completed"])
    except Exception as exc:
        publish(status="failed", error=str(exc), traceback=traceback.format_exc())
        log("ERROR", "EV comparison failed", run_id=job["id"], error=str(exc), traceback=job["traceback"])
    finally:
        with LOCK:
            STATE["active"] = None


@asynccontextmanager
async def lifespan(app):
    # Restore finished runs; interrupted processes are never reported as still running.
    for path in sorted(RUNS.glob("*/job.json")):
        try:
            job = json.loads(path.read_text())
            if job["status"] == "running":
                job.update(status="interrupted", error="Server stopped before this run finished")
                persist(job)
            STATE["jobs"][job["id"]] = job
        except Exception as exc:
            log("WARNING", f"Could not restore {path.name}", error=str(exc))
    POOL.submit(prepare)
    TRAVEL_POOL.submit(prepare_travel)
    yield
    CANCEL.set()
    POOL.shutdown(wait=True)
    TRAVEL_POOL.shutdown(wait=True)


app = FastAPI(title="Greensboro Grid Lab", lifespan=lifespan)


@app.middleware("http")
async def local_requests(request: Request, call_next):
    # Block browser requests from unrelated origins and DNS-rebinding hostnames.
    host = request.url.hostname
    if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return JSONResponse({"detail": "Local hosts only"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
    try:
        response = await call_next(request)
        if request.url.path.startswith('/api/') or request.url.path in {'/', '/index.html', '/app.js', '/battery.js', '/fleet.js', '/travel.js', '/market.js', '/style.css'}:
            response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception as exc:
        log("ERROR", "HTTP request failed", path=request.url.path, error=str(exc), traceback=traceback.format_exc())
        return JSONResponse({"detail": str(exc)}, status_code=500)


@app.get("/api/status")
def status():
    with LOCK:
        return {"ready": STATE["ready"], "preparing": STATE["preparing"], "error": STATE["error"],
                "active": STATE["active"], "profile_validation": STATE["profile_validation"],
                "current_fleet": STATE["current_fleet"]}


@app.get("/api/network")
def network():
    if not STATE["ready"]:
        raise HTTPException(503, STATE["error"] or "Preparing dataset")
    return STATE["network"]


class RunRequest(BaseModel):
    date: Date = Date(2018, 1, 1)
    days: int = Field(default=1, ge=1, le=MAX_SIMULATION_DAYS)
    travel_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{16}$")
    travel_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_SIMULATION_DAYS)
    rollout_start: Date | None = None
    charging: ChargingConfig | None = None
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    market: MarketConfig | None = None


def following_travel_plan(inventory, first_plan, next_date):
    initial = travel.TravelConfig.model_validate(first_plan["config"])
    seed = fleet.rng(initial.seed, f"travel-plan|{next_date}").randrange(2**32)
    # Lookahead may cross into 2019 even though electrical simulation stops at the end of 2018.
    settings = initial.model_copy(update={"date":next_date, "seed":seed})
    with TRAVEL_LOCK:
        if not TRAVEL_STATE["ready"]:
            raise HTTPException(503, "The travel library is still preparing")
        try:
            return get_or_create_travel(inventory, settings)
        except ValueError as exc:
            raise HTTPException(422, f"Could not prepare the next day's driving plan: {exc}") from exc


@app.post("/api/runs", status_code=202)
def start_run(config: RunRequest):
    if config.date.year != 2018 or config.date.toordinal()+config.days > Date(2019, 1, 1).toordinal():
        raise HTTPException(422, "Choose a date range entirely within 2018")
    with LOCK:
        if not STATE["ready"]:
            raise HTTPException(503, "Dataset is not ready")
        if STATE["active"]:
            raise HTTPException(409, "A simulation is already running")
        try:
            solar, solar_resource = load_solar(config.generation)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, f"Greensboro solar profile unavailable: {exc}") from exc
        charging_inputs = None
        plans = None
        lookahead = None
        if config.travel_id and config.travel_ids:
            raise HTTPException(422, "Submit a travel day or a list of daily plans")
        if config.rollout_start is not None and not config.travel_ids:
            raise HTTPException(422, "A rollout requires daily EV travel plans")
        if config.travel_id or config.travel_ids:
            if config.travel_ids:
                plans = [read_travel(item) for item in config.travel_ids]
                expected = sequence.dates(config.date, config.date+timedelta(days=config.days))
                if [plan["config"]["date"] for plan in plans] != [str(day) for day in expected]:
                    raise HTTPException(422, "Provide one EV travel plan for each selected date in order")
                if len({plan["fleet_id"] for plan in plans}) != 1:
                    raise HTTPException(422, "Every travel plan must belong to the same fleet")
                if not TRAVEL_STATE["ready"] or any(plan["source_digest"] != TRAVEL_STATE["source"]["digest"] for plan in plans):
                    raise HTTPException(422, "Generate the daily plans from the current travel library")
                settings_without_dates = {fleet.digest({k:v for k,v in plan["config"].items() if k not in {"date", "seed"}})
                                          for plan in plans}
                if len(settings_without_dates) != 1:
                    raise HTTPException(422, "Use consistent travel settings across the selected days")
                rollout_start = config.rollout_start or max(Date(2018, 1, 1), config.date-timedelta(days=31))
                if not (Date(2018, 1, 1) <= rollout_start <= config.date):
                    raise HTTPException(422, "Rollout must start in 2018 on or before the simulation start")
                travel_day = plans[0]
            else:
                travel_day = read_travel(config.travel_id)
                if config.days != 1 or travel_day["config"]["date"] != config.date.isoformat():
                    raise HTTPException(422, "Use the saved EV travel date and a duration of one day")
            original = get_fleet(travel_day["fleet_id"])
            if not original["summary"]["ev_count"]:
                raise HTTPException(422, "Generate a fleet with EVs before running home charging")
            inventory = fleet.build_fleet(ready_registry(), {**original["config"], "home_access_pct":100.})
            charging_inputs = (inventory, travel_day, config.charging or charging.ChargingConfig())
            if config.market and config.market.behavior == "buy_sell_wait" and config.market.trader_pct > 0:
                lookahead = following_travel_plan(original, travel_day, config.date+timedelta(days=config.days))
        elif config.charging is not None or config.market is not None:
            raise HTTPException(422, "Choose a saved travel day for EV charging")
        run_id = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = {"id": run_id, "date": config.date.isoformat(), "days": config.days,
               "intervals": config.days*96, "completed": 0, "status": "running", "series": [],
               "totals": {}, "error": None, "elapsed_s": 0,
               "assumptions": {"station": "uhs19_1247", "dataset": "SMART-DS v1.0 / 2018 / base_timeseries",
                   "interval_minutes": 15, "sampling": "end of interval, DSS yearly loadshapes",
                   "voltage_screen_pu": [.95, 1.05], "source": "69 kV external-grid equivalent",
                   "generation": config.generation.model_dump(),
                   "controls": "static settling at each interval; fresh context per run",
                   "line_loading": "max phase current at terminal 1 / normal ampacity",
                   "transformer_loading": "maximum phase current / rated winding current across windings"}}
        if solar_resource:
            job["assumptions"]["solar_resource"] = solar_resource
        if lookahead:
            job["assumptions"]["next_travel_id"] = lookahead["id"]
            job["assumptions"]["next_travel_date"] = lookahead["config"]["date"]
        if charging_inputs:
            inventory, travel_day, settings = charging_inputs
            job["assumptions"]["ev_charging"] = {
                **settings.model_dump(), "fleet_id":travel_day["fleet_id"], "travel_id":travel_day["id"],
                "charger_inventory_id":inventory["id"], "home_access_pct":100,
                "level1_count":inventory["summary"]["level1_count"], "level2_count":inventory["summary"]["level2_count"],
                "initial_soc":"Configured share starts full at midnight, rounded to the nearest EV with ties rounded up. Other EVs start at a random charge from 30% up to but below 100%.",
                "capacity":"Fleet nameplate capacity treated as usable capacity",
                "driving_consumption_by_class":charging.DRIVING_CONSUMPTION,
                "battery_valuation":"Opening uses previous-day non-EV generation at the current nuclear output, with today's incremental loss per charger kWh as a proxy. In-day charging uses the with-EV generation cost and proportional net distribution losses. Driving consumes energy at the battery's moving average cost.",
                "charge_curve":charging.CHARGE_CURVE, "v2g_enabled":False,
                "load_model":"Home chargers use 120 V line-neutral or 240 V line-line. Destination loads use the site's supply voltage and balanced connection, with ideal onsite conversion. Unity power factor throughout.",
                "away_ports":"1–5 ports per equipped bus, uniformly sampled. Arrival-only allocation; ports stay occupied until departure. Destination power uses each EV's home charging limit.",
                "timing":"Charge energy follows exact parking windows and SOC taper, then averages over 15 minutes. Driving energy spread uniformly across each trip."}
        if config.market is not None or plans:
            market_config = config.market or MarketConfig()
            job["assumptions"]["market"] = market_config.model_dump()
            if market_config.trading_only_pct:
                job["assumptions"]["trading_only_vehicle_ids"] = market.choose_trading_only(
                    (vehicle["id"] for vehicle in inventory["vehicles"] if vehicle["is_ev"]), market_config)
            job["assumptions"]["ev_charging"]["v2g_enabled"] = True
            job["assumptions"]["ev_charging"]["battery_valuation"] = "Both cases share opening value. Purchases use the locked price plus a share of realized distribution losses. Exports settle at the locked price. Battery capacity stays fixed."
            if job["assumptions"]["market"]["pricing_mode"] == "dynamic":
                job["assumptions"]["market_pricing"] = (
                    "Each interval shuffles the trading EVs using the seed, day and interval. Quotes use current "
                    "baseline draw including baseline losses, passive charging and the previous net trading energy "
                    "forecast, progressively replaced by commitments. Each trader settles at its own accepted "
                    "quote. Passive charging and customer demand use the previous realized price.")
        if plans:
            job.update(phase="sizing", daily_summaries={})
            job["assumptions"].update(rollout_start=rollout_start.isoformat(),
                                      travel_ids=[plan["id"] for plan in plans])
            job["assumptions"]["ev_charging"]["initial_soc"] = "New EVs enter empty during rollout. Charge, value and prices carry forward between dates."
        STATE["jobs"][run_id] = job
        STATE["active"] = run_id
        CANCEL.clear()
        persist(job)
        if plans:
            POOL.submit(run_sequence_job, job, inventory, plans, settings, TRAVEL_STATE["source"], solar)
        else:
            POOL.submit(run_job, job, charging_inputs, solar)
        return {"id": run_id}


@app.get("/api/runs")
def runs():
    with LOCK:
        return [{k:v for k,v in job.items() if k not in ("series", "traceback")}
                for job in reversed(list(STATE["jobs"].values()))]


def get_job(run_id):
    if run_id not in STATE["jobs"]:
        raise HTTPException(404, "Run not found")
    return STATE["jobs"][run_id]


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    with LOCK:
        return dict(get_job(run_id))


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str):
    with LOCK:
        job = get_job(run_id)
        if job["status"] != "running":
            raise HTTPException(409, "Run is not running")
        CANCEL.set()
        return {"cancellation_requested": True}


@app.get("/api/runs/{run_id}/snapshot/{index}")
def snapshot(run_id: str, index: int, scenario: str = "charging"):
    job = get_job(run_id)
    if index < 0 or index >= job["completed"]:
        raise HTTPException(404, "Interval is not available")
    if scenario == "trading" and not job.get("market_summary"):
        raise HTTPException(404, "Trading results are unavailable for this run")
    name = "market" if scenario == "trading" else "snapshot"
    return FileResponse(RUNS / run_id / f"{name}-{index:05d}.json", media_type="application/json")


@app.get("/api/runs/{run_id}/export")
def export(run_id: str):
    job = get_job(run_id)
    path = RUNS / run_id / "timeseries.csv"
    if job["status"] == "running" or not path.exists():
        raise HTTPException(409, "CSV is available after the run stops")
    return FileResponse(path, media_type="text/csv", filename=f"{run_id}.csv")


@app.get("/api/runs/{run_id}/charging")
def charging_details(run_id: str, day: Date | None = None):
    job = get_job(run_id)
    name = f"charging-{day or job['date']}.json" if job["assumptions"].get("travel_ids") else "charging.json"
    path = RUNS / run_id / name
    if not job.get("charging_summary") or not path.exists():
        raise HTTPException(404, "Battery schedules are unavailable for this run")
    return FileResponse(path, media_type="application/json", filename=f"{run_id}-battery-schedules.json")


@app.get("/api/runs/{run_id}/battery")
def battery_details(run_id: str, vehicle_id: str, scenario: str = "charging", day: Date | None = None):
    job = get_job(run_id)
    stem = "market-ledger" if scenario == "trading" else "battery-ledger"
    suffix = f"-{day or job['date']}" if job["assumptions"].get("travel_ids") else ""
    path = RUNS / run_id / f"{stem}{suffix}.json"
    if not path.exists():
        raise HTTPException(404, "Battery records are unavailable for this day")
    ledger = json.loads(path.read_text())
    if vehicle_id not in ledger:
        raise HTTPException(404, "EV not found in this run")
    return ledger[vehicle_id]


@app.get("/api/runs/{run_id}/bills")
def daily_bills(run_id: str, day: Date | None = None):
    job = get_job(run_id)
    name = f"bills-{day}.json" if day and job["assumptions"].get("travel_ids") else "daily-bills.json"
    path = RUNS / run_id / name
    if not path.exists():
        raise HTTPException(404, "Daily bills are unavailable for this run")
    return FileResponse(path, media_type="application/json")


@app.get("/api/logs")
def logs(run_id: str | None = None, limit: int = 150):
    limit = max(1, min(limit, 500))
    if not LOG.exists():
        return []
    # Bounded reverse scan so long-running servers don't read the entire log into memory.
    with LOG.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        chunks = []
        while position > 0 and sum(c.count(b"\n") for c in chunks) < 5000:
            size = min(65536, position)
            position -= size
            stream.seek(position)
            chunks.insert(0, stream.read(size))
    lines = b"".join(chunks).decode(errors="replace").splitlines()
    result = []
    for line in lines:
        try:
            item = json.loads(line)
            if run_id is None or item.get("run_id") == run_id:
                result.append(item)
        except ValueError:
            pass
    return result[-limit:]


@app.get("/api/diagnostics")
def diagnostics():
    n = STATE["network"] or {}
    return {"profile_validation": STATE["profile_validation"], "engine": n.get("engine_version"),
            "unreachable_buses": [b["id"] for b in n.get("nodes", []) if not b["reachable"]],
            "unreachable_loads": [load["id"] for load in n.get("loads", [])
                                   if load["bus"] in {b["id"] for b in n.get("nodes", []) if not b["reachable"]}],
            "initial_snapshot": n.get("initial_diagnostics"), "notes": n.get("notes", []),
            "service_registry": n.get("service_registry_summary")}


FLEET_DIR = RUNS / "fleets"
FLEET_DIR.mkdir(exist_ok=True)


def persist_fleet(item):
    """Immutable inventories plus an atomic pointer to the current selection."""
    path = FLEET_DIR / (item["id"] + ".json")
    if not path.exists():
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(item,allow_nan=False))
        temp.replace(path)
    pointer=FLEET_DIR / "current.tmp"
    pointer.write_text(json.dumps({"id":item["id"]}))
    pointer.replace(FLEET_DIR / "current.json")
    STATE["fleets"][item["id"]]=item
    STATE["current_fleet"]=item["id"]


def initialize_fleets(registry):
    with LOCK:
        STATE["service_registry"]=registry
        for path in sorted(FLEET_DIR.glob("*.json"),key=lambda p:p.stat().st_mtime):
            if path.name=="current.json":
                continue
            try:
                item=json.loads(path.read_text())
                if item.get("registry_digest")==registry["digest"] and item.get("schema_version")==1:
                    # Rebuild from stored config to validate consistency with current algorithm.
                    rebuilt=fleet.build_fleet(registry,fleet.FleetConfig.model_validate(item["config"]))
                    if rebuilt!=item:
                        raise ValueError("Stored inventory does not match its deterministic configuration")
                    STATE["fleets"][item["id"]]=item
            except Exception as exc:
                log("WARNING","Could not restore fleet inventory",file=path.name,error=str(exc))
        pointer=FLEET_DIR / "current.json"
        selected=json.loads(pointer.read_text()).get("id") if pointer.exists() else None
        if selected not in STATE["fleets"]:
            default=fleet.build_fleet(registry,fleet.FleetConfig())
            persist_fleet(default)
        else:
            STATE["current_fleet"]=selected
    summary=registry["summary"]
    log("INFO","Customer services reconciled for EV inventory",**summary)
    log("INFO","Fleet inventory ready",fleet_id=STATE["current_fleet"],summary=STATE["fleets"][STATE["current_fleet"]]["summary"])


def ready_registry():
    if not STATE["ready"] or STATE["service_registry"] is None:
        raise HTTPException(503,"Household registry is not ready")
    return STATE["service_registry"]


@app.get("/api/households")
def household_registry():
    return ready_registry()


@app.get("/api/fleet")
def current_fleet():
    ready_registry()
    with LOCK:
        return STATE["fleets"][STATE["current_fleet"]]


@app.get("/api/fleets")
def fleet_history():
    ready_registry()
    with LOCK:
        return [{"id":item["id"],"config":item["config"],"summary":item["summary"],
                 "selected":item["id"]==STATE["current_fleet"]}
                for item in reversed(list(STATE["fleets"].values()))]


@app.post("/api/fleets",status_code=201)
def create_fleet(config: fleet.FleetConfig):
    registry=ready_registry()
    try:
        item=fleet.build_fleet(registry,config)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    with LOCK:
        persist_fleet(item)
    log("INFO","Fleet inventory generated",fleet_id=item["id"],config=item["config"],summary=item["summary"])
    return item


def get_fleet(fleet_id):
    ready_registry()
    if fleet_id not in STATE["fleets"]:
        raise HTTPException(404,"Fleet not found")
    return STATE["fleets"][fleet_id]


@app.get("/api/fleets/{fleet_id}")
def fleet_details(fleet_id: str):
    return get_fleet(fleet_id)


@app.post("/api/fleets/{fleet_id}/select")
def select_fleet(fleet_id: str):
    with LOCK:
        item=get_fleet(fleet_id)
        persist_fleet(item)
    log("INFO","Selected saved fleet inventory",fleet_id=fleet_id)
    return item


@app.get("/api/fleets/{fleet_id}/export")
def fleet_export(fleet_id: str):
    get_fleet(fleet_id)
    return FileResponse(FLEET_DIR / (fleet_id+".json"),media_type="application/json",filename=f"fleet-{fleet_id}.json")


@app.get("/api/travel/status")
def travel_status():
    with TRAVEL_LOCK:
        source=TRAVEL_STATE["source"]
        return {k:v for k,v in TRAVEL_STATE.items() if k!="source"} | {
            "source":{k:source[k] for k in ["source_url","citation","source_scope","digest","audit"]} if source else None}


def selected_fleet_id(fleet_id=None):
    with LOCK:
        item=get_fleet(fleet_id or STATE["current_fleet"])
        return item["id"]


def read_travel(scenario_id):
    import re
    if not re.fullmatch(r"[a-f0-9]{16}",scenario_id):
        raise HTTPException(404,"Travel scenario not found")
    path=TRAVEL_DIR/(scenario_id+".json")
    if not path.exists():
        raise HTTPException(404,"Travel scenario not found")
    return json.loads(path.read_text())


def travel_pointer(item):
    nhts.atomic_json(TRAVEL_DIR/("current-"+item["fleet_id"]+".json"),{"id":item["id"]})


@app.get("/api/travel")
def current_travel(fleet_id: str | None = None):
    fleet_id=selected_fleet_id(fleet_id)
    with TRAVEL_LOCK:
        path=TRAVEL_DIR/("current-"+fleet_id+".json")
        item=read_travel(json.loads(path.read_text())["id"]) if path.exists() else None
        return {"scenario":item}


@app.get("/api/travel/scenarios")
def travel_history(fleet_id: str | None = None):
    fleet_id=selected_fleet_id(fleet_id)
    result=[]
    with TRAVEL_LOCK:
        for path in sorted(TRAVEL_DIR.glob("*.json"),key=lambda p:p.stat().st_mtime,reverse=True):
            if path.name.startswith("current-"):
                continue
            item=json.loads(path.read_text())
            if item["fleet_id"]==fleet_id:
                result.append({k:item[k] for k in ["id","fleet_id","config","summary"]})
    return result


class TravelRequest(travel.TravelConfig):
    fleet_id: str | None = Field(default=None,pattern=r"^[a-f0-9]{16}$")


class TravelPlanRequest(TravelRequest):
    days: int = Field(default=1, ge=1, le=MAX_SIMULATION_DAYS)


def get_or_create_travel(item, settings):
    source = TRAVEL_STATE["source"]
    identity = travel.travel_identity(item["id"], source, settings)
    path = TRAVEL_DIR / (fleet.digest(identity)[:16] + ".json")
    if path.exists():
        return json.loads(path.read_text())
    result = travel.build_travel(item, source, settings)
    nhts.atomic_json(path, result)
    return result


@app.post("/api/travel/scenarios",status_code=201)
def generate_travel(config: TravelRequest):
    with LOCK:
        item=get_fleet(config.fleet_id or STATE["current_fleet"])
    with TRAVEL_LOCK:
        if not TRAVEL_STATE["ready"]:
            raise HTTPException(503,TRAVEL_STATE["error"] or "NHTS data are still preparing")
        try:
            scenario=get_or_create_travel(item, travel.TravelConfig.model_validate(config.model_dump(exclude={"fleet_id"})))
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        travel_pointer(scenario)
    log("INFO","EV day travel generated",travel_id=scenario["id"],fleet_id=item["id"],summary=scenario["summary"])
    return scenario


@app.post("/api/travel/plans", status_code=201)
def generate_travel_plans(config: TravelPlanRequest):
    end = config.date + timedelta(days=config.days)
    if end > Date(2019, 1, 1):
        raise HTTPException(422, "Choose a date range entirely within 2018")
    with LOCK:
        item = get_fleet(config.fleet_id or STATE["current_fleet"])
    if not item["summary"]["ev_count"]:
        raise HTTPException(422, "Generate a fleet with EVs before preparing travel plans")
    with TRAVEL_LOCK:
        if not TRAVEL_STATE["ready"]:
            raise HTTPException(503, TRAVEL_STATE["error"] or "NHTS data are still preparing")
        plans = []
        try:
            values = config.model_dump(exclude={"fleet_id", "days"})
            for day in sequence.dates(config.date, end):
                seed = config.seed if day == config.date else fleet.rng(config.seed, f"travel-plan|{day}").randrange(2**32)
                settings = travel.TravelConfig.model_validate({**values, "date":day, "seed":seed})
                plans.append(get_or_create_travel(item, settings))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        travel_pointer(plans[0])
    return {"plans":[{key:plan[key] for key in ("id", "config", "summary")} for plan in plans],
            "fleet_id":item["id"], "vehicle_days":sum(plan["summary"]["ev_count"] for plan in plans)}


@app.get("/api/travel/scenarios/{scenario_id}")
def travel_details(scenario_id: str):
    return read_travel(scenario_id)


@app.post("/api/travel/scenarios/{scenario_id}/select")
def select_travel(scenario_id: str):
    with TRAVEL_LOCK:
        item=read_travel(scenario_id)
        travel_pointer(item)
    return item


@app.get("/api/travel/scenarios/{scenario_id}/export")
def travel_export(scenario_id: str, format: str = "json"):
    import io
    from fastapi.responses import Response
    item=read_travel(scenario_id)
    if format=="json":
        return FileResponse(TRAVEL_DIR/(scenario_id+".json"),media_type="application/json",filename=f"travel-{scenario_id}.json")
    if format not in {"trips","states"}:
        raise HTTPException(422,"Choose json, trips or states")
    stream=io.StringIO(newline="")
    fields=["vehicle_id","household_id","home_bus"]
    fields += ["trip_index","depart_minute","arrive_minute","distance_km","origin","destination","diary_id"] if format=="trips" else ["start_minute","end_minute","state","purpose"]
    writer=csv.DictWriter(stream,fieldnames=fields)
    writer.writeheader()
    for vehicle in item["vehicles"]:
        base={k:vehicle[k] for k in ["vehicle_id","household_id","home_bus"]}
        for index,record in enumerate(vehicle["trips"] if format=="trips" else vehicle["segments"]):
            data={**base,**record,"trip_index":index,"diary_id":vehicle["diary_id"]}
            writer.writerow({k:data[k] for k in fields})
    return Response(stream.getvalue(),media_type="text/csv",headers={"Content-Disposition":f'attachment; filename="travel-{scenario_id}-{format}.csv"'})


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="dashboard")
