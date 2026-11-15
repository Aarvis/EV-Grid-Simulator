"""Run matched EV scenarios in lockstep, carrying their ledgers across dates."""
from datetime import date, timedelta
import hashlib
import json

from . import charging, engine, fleet, generation, market, nhts, travel
from .config import DATA, RUNS


def dates(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days)]


def carry_batteries(records):
    return {key:{"opening":{field:record["intervals"][-1][field] for field in ("stored_kwh", "value_usd")}}
            for key, record in records.items()}


def opening_totals(opening, offer):
    return {"battery_kwh":sum(record["opening"]["stored_kwh"] for record in opening.values()),
            "battery_value_usd":sum(record["opening"]["value_usd"] for record in opening.values()),
            "offer_per_mwh":offer}


def stagger_connections(diaries, new_vehicles, seed):
    result = []
    for diary in diaries:
        key = diary["vehicle_id"]
        if key not in new_vehicles:
            result.append(diary)
            continue
        departure = diary["first_departure_minute"]
        latest = (departure if departure is not None else 1439) // 15
        minute = 15 * fleet.rng(seed, "rollout-connection|" + key).randint(0, latest)
        if minute == 0:
            result.append(diary)
            continue
        first, *remaining = diary["segments"]
        if first["state"] != "home" or minute > first["end_minute"]:
            raise ValueError(f"EV {key} must enter the rollout while at home")
        if minute < first["end_minute"]:
            remaining.insert(0, {**first, "start_minute":minute})
        result.append({**diary, "segments":[{"state":"inactive", "start_minute":0, "end_minute":minute}, *remaining]})
    return result


def price_last_interval(dss, date_, nuclear, config, solar):
    day = date_.replace(year=2018).timetuple().tm_yday
    engine.solve(dss, day, 95)
    solar_mw = generation.solar_at(solar, day, 95)
    return generation.dispatch(-float(dss.Circuit.TotalPower()[0]), nuclear, config, solar_mw)["average_generation_cost_per_mwh"]


def rollout(inventory, first_plan, template, network, ratings, services, travel_source,
            settings, passive, source, nuclear, prior_offer, start, end, stop, progress, solar, solar_resource,
            trading_only):
    days = dates(start, end)
    base_travel = {**first_plan["config"]}
    base_travel.pop("date")
    seed = base_travel["seed"]
    keys = sorted(v["id"] for v in template["vehicles"])
    fleet.rng(seed, "rollout-commissioning-order").shuffle(keys)
    generation_settings = source.model_dump()
    if solar_resource:
        generation_settings["solar_profile_sha256"] = solar_resource["sha256"]
    else:
        for key in ("solar_capacity_mw", "solar_performance_factor", "solar_cost_per_mwh"):
            del generation_settings[key]
    cache_settings = {"version":1, "inventory":inventory["id"], "travel":base_travel,
                             "source":travel_source["digest"], "settings":settings.model_dump(exclude={"initial_full_pct"}),
                             "generation":generation_settings, "nuclear_mw":nuclear,
                             "start":str(start), "end":str(end), "offer":prior_offer,
                             "dataset":hashlib.sha256((DATA / "manifest.json").read_bytes()).hexdigest()}
    if trading_only:
        cache_settings["trading_only"] = sorted(trading_only)
    identity = fleet.digest(cache_settings)
    path = RUNS / "rollouts" / (identity + ".json")
    if path.exists():
        cached = json.loads(path.read_text())
        progress(len(days)*96, len(days)*96, end.isoformat(), True)
        return cached
    empty = {key:{"opening":{"stored_kwh":0., "value_usd":0.}} for key in keys}
    if not days:
        return {"opening":empty, "first_offer":prior_offer, "cache_id":None}
    progress(0, len(days)*96, start.isoformat(), False)
    grid = market.open_grid(template, start.timetuple().tm_yday, network, settings.seed)
    opening, offer = {}, prior_offer
    for offset, current in enumerate(days):
        if stop():
            return None
        first = offset * len(keys) // len(days)
        last = (offset+1) * len(keys) // len(days)
        active = set(keys[:last])
        new = set(keys[first:last])
        subset = {**inventory, "id":fleet.digest([inventory["id"], sorted(active)])[:16],
                  "vehicles":[v for v in inventory["vehicles"] if v["id"] in active]}
        if active:
            config = {**base_travel, "date":current.isoformat(),
                      "seed":fleet.rng(seed, f"rollout-travel|{current}").randrange(2**32)}
            plan = travel.keep_vehicles_home(travel.build_travel(subset, travel_source, config), trading_only)
            schedule = charging.build_schedule(subset, plan, settings, network)
            diaries = stagger_connections(plan["vehicles"], new, seed)
        else:
            schedule, diaries = {"vehicles":[]}, []
        opening.update({key:empty[key] for key in new})
        simulation = market.TradingDay(schedule, diaries, opening, settings, passive, source, nuclear,
                                       offer, current.timetuple().tm_yday, network, ratings, services, grid=grid, solar=solar)
        for index in range(96):
            if stop():
                return None
            simulation.step(index, lambda _: None)
            progress(offset*96+index+1, len(days)*96, current.isoformat(), False)
        opening, offer = carry_batteries(simulation.records), simulation.quote
    cached = {"opening":opening, "first_offer":offer, "cache_id":identity}
    nhts.atomic_json(path, cached)
    return cached


def charging_interval(simulation):
    records = [record["intervals"][-1] for record in simulation.records.values()]
    return {"home_grid_kwh":sum(row["home_draw_kwh"] for row in records),
            "away_grid_kwh":sum(row["away_draw_kwh"] for row in records),
            "grid_kwh":simulation.rows[-1]["ev_import_kwh"],
            "battery_kwh":sum(row["charged_kwh"] for row in records),
            "drive_kwh":sum(row["driving_kwh"] for row in records),
            "unmet_kwh":sum(row["unmet_kwh"] for row in records),
            "charging_evs":sum(row["charged_kwh"] > 0 for row in records),
            "battery_end_kwh":simulation.rows[-1]["ev_battery_end_kwh"]}


def save_schedule(folder, label, plan, schedule):
    vehicles = []
    for v in schedule["vehicles"]:
        vehicle = {key:value for key, value in v.items() if key not in
                   {"energy_events", "end_soc", "initial_soc", "grid_kwh", "home_grid_kwh", "away_stops"}}
        vehicle["away_stops"] = [{key:value for key, value in stop.items() if key != "grid_kwh"}
                                 for stop in v["away_stops"]]
        vehicles.append(vehicle)
    nhts.atomic_json(folder / f"charging-{label}.json", {
        "date":label, "travel_id":plan["id"], "vehicles":vehicles, "destination_sites":schedule["destination_sites"]})


def cumulative_bills(bill_days):
    fields = market.Bill().payload().keys()
    combined = {}
    for day in bill_days:
        for case, account in day.items():
            target = combined.setdefault(case, {kind:{} for kind in ("services", "vehicles", "households", "totals")})
            for kind in ("services", "vehicles", "households"):
                for item in account[kind]:
                    if item["id"] not in target[kind]:
                        target[kind][item["id"]] = dict(item)
                    else:
                        for field in fields:
                            target[kind][item["id"]][field] += item[field]
            for field, value in account["totals"].items():
                target["totals"][field] = target["totals"].get(field, 0.) + value
    return {case:{**account, **{kind:list(account[kind].values()) for kind in ("services", "vehicles", "households")}}
            for case, account in combined.items()}


def totals_so_far(baseline_rows, charging_rows, trading_rows, source, minimum, nuclear, ledger, counts):
    comparison = {"baseline_min_mw":minimum / 1000, "nuclear_mw":nuclear,
                  **{key:generation.totals(rows, "", source) for key, rows in (
                      ("without_ev", baseline_rows), ("with_ev", charging_rows), ("with_trading", trading_rows))}}
    charging_totals = engine.totals(charging_rows)
    charging_totals.update({key:sum(row[key] for row in charging_rows) for key in (
        "ev_charge_kwh", "ev_home_charge_kwh", "ev_away_charge_kwh", "ev_battery_gain_kwh",
        "ev_driving_kwh", "ev_unmet_driving_kwh", "delta_load_kwh", "delta_supply_kwh", "delta_loss_kwh")})
    charging_totals["peak_ev_charge_kw"] = max(row["ev_charge_kw"] for row in charging_rows)
    charging_totals["ev_battery_end_kwh"] = charging_rows[-1]["ev_battery_end_kwh"]
    trade_totals = {**comparison["with_trading"], **engine.totals(trading_rows), **counts}
    for key in ("ev_import_kwh", "ev_export_kwh", "passive_import_kwh", "curtailed_export_kwh",
                "upstream_export_kwh", "ev_unmet_driving_kwh", "wear_usd"):
        trade_totals[key] = sum(row[key] for row in trading_rows)
    trade_totals["ev_battery_end_kwh"] = trading_rows[-1]["ev_battery_end_kwh"]
    trade_totals["ev_battery_end_value_usd"] = ledger
    return {"generation_summary":comparison, "totals":charging_totals, "baseline_totals":engine.totals(baseline_rows),
            "market_summary":trade_totals}


def planned_distances(plan, trading_only=()):
    return {vehicle["vehicle_id"]:0. if vehicle["vehicle_id"] in trading_only else vehicle["distance_km"]
            for vehicle in plan["vehicles"]}


def run_sequence(job, inventory, plans, settings, travel_source, registry, publish, stop, add_comparison,
                 solar=(), next_plan=None):
    folder = RUNS / job["id"]
    start = date.fromisoformat(job["date"])
    warm_start = date.fromisoformat(job["assumptions"]["rollout_start"])
    source = generation.GenerationConfig.model_validate(job["assumptions"]["generation"])
    config = market.MarketConfig.model_validate(job["assumptions"]["market"])
    trading_only = set(job["assumptions"].get("trading_only_vehicle_ids", ()))
    passive = config.model_copy(update={"trader_pct":0., "allow_sales_despite_grid_violations":False})
    services = registry["services"]
    day = start.timetuple().tm_yday
    baseline_grid = engine.context()
    engine.solve(baseline_grid, day, 0)
    network, ratings = engine.topology(baseline_grid)
    template = charging.build_schedule(inventory, travel.keep_vehicles_home(plans[0], trading_only), settings, network)
    publish(phase="sizing")
    minimum = float("inf")
    for index in range(job["intervals"]):
        if stop():
            return
        engine.solve(baseline_grid, day, index)
        minimum = min(minimum, -float(baseline_grid.Circuit.TotalPower()[0]))
    nuclear = source.nuclear_mw if source.nuclear_mw is not None else max(0., minimum/1000-source.coal_gas_min_mw)
    prior_offer = price_last_interval(baseline_grid, warm_start-timedelta(days=1), nuclear, source, solar)
    del baseline_grid

    def rollout_progress(completed, intervals, current, cached):
        publish(save=completed in (0, intervals) or completed % 96 == 0, phase="rollout",
                rollout_completed=completed, rollout_intervals=intervals, rollout_date=current, rollout_cached=cached)

    initial = rollout(inventory, plans[0], template, network, ratings, services, travel_source,
                      settings, passive, source, nuclear, prior_offer, warm_start, start, stop, rollout_progress,
                      solar, job["assumptions"].get("solar_resource"), trading_only)
    if initial is None or stop():
        return
    openings = {case:initial["opening"] for case in ("charging", "trading")}
    quotes = {case:initial["first_offer"] for case in ("baseline", "charging", "trading")}
    previous_trading_net_kwh = 0.
    previous_trading_loss_rate = 0.
    publish(phase="comparison", opening_batteries=opening_totals(initial["opening"], initial["first_offer"]),
            rollout_cache_id=initial["cache_id"])
    baseline_grid = engine.context()
    grids = {case:market.open_grid(template, day, network, settings.seed) for case in openings}
    all_baseline, all_charging, all_trading, combined = [], [], [], []
    days, bill_days = {}, []
    billed = {case:{} for case in quotes}
    for offset, plan in enumerate(plans):
        if stop():
            break
        current = start + timedelta(days=offset)
        label = current.isoformat()
        current_day = current.timetuple().tm_yday
        plan = travel.keep_vehicles_home(plan, trading_only)
        schedule = template if offset == 0 else charging.build_schedule(inventory, plan, settings, network)
        tomorrow = plans[offset+1] if offset+1 < len(plans) else next_plan
        next_distances = planned_distances(tomorrow, trading_only) if tomorrow else None
        save_schedule(folder, label, plan, schedule)
        publish(charging_summary={"destination_sites":schedule["destination_sites"],
                                  "destination_summary":schedule["destination_summary"],
                                  "ev_count":schedule["ev_count"], "schedule_date":label})
        cases = {case:market.TradingDay(schedule, plan["vehicles"], openings[case], settings, mode, source,
                                       nuclear, quotes[case], current_day, network, ratings, services, grids[case], solar=solar,
                                       previous_net_kwh=previous_trading_net_kwh if case == "trading" else 0.,
                                       next_distances=next_distances,
                                       previous_loss_rate=previous_trading_loss_rate if case == "trading" else 0.)
                 for case, mode in (("charging", passive), ("trading", config))}
        reference_book = market.BillingBook(services, network["loads"])
        start_state = {case:opening_totals(openings[case], quotes[case]) for case in cases}
        count = 0
        for index in range(96):
            if stop():
                break
            absolute = offset*96+index
            engine.solve(baseline_grid, current_day, index)
            base, base_snapshot = engine.state(baseline_grid, network, ratings, current_day, index)
            solar_mw = generation.solar_at(solar, current_day, index)
            base.update(generation.dispatch(base["supply_kw"], nuclear, source, solar_mw))
            base["locked_price_per_mwh"] = quotes["baseline"]
            reference_book.settle(base_snapshot["loads"], {}, quotes["baseline"],
                                  base["average_generation_cost_per_mwh"], base["loss_kw"]*.25)
            quotes["baseline"] = base["average_generation_cost_per_mwh"]
            charged, charge_snapshot = cases["charging"].step(index, lambda _: None)
            traded, trade_snapshot = cases["trading"].step(index, lambda _: charge_snapshot, base["supply_kw"])
            add_comparison(charged, charge_snapshot, base, base_snapshot, network, charging_interval(cases["charging"]))
            records = [r["intervals"][-1] for r in cases["trading"].records.values()]
            traded.update(ev_unmet_driving_kwh=sum(r["unmet_kwh"] for r in records), wear_usd=sum(r["wear_usd"] for r in records))
            stored_value = sum(r["value_usd"] for r in records)
            for snapshot in (base_snapshot, charge_snapshot, trade_snapshot):
                snapshot["index"] = absolute
            nhts.atomic_json(folder / f"snapshot-{absolute:05d}.json", charge_snapshot)
            nhts.atomic_json(folder / f"market-{absolute:05d}.json", trade_snapshot)
            all_baseline.append(base)
            all_charging.append(charged)
            all_trading.append(traded)
            combined.append({**charged, "index":absolute, "simulation_date":label,
                             **{"baseline_"+key:value for key, value in base.items()},
                             **{"trading_"+key:value for key, value in traded.items()}})
            counts = {"traders":len(cases["trading"].traders), "passive":schedule["ev_count"]-len(cases["trading"].traders)}
            live_bills = {case:book.totals() for case, book in (
                ("baseline", reference_book), ("charging", cases["charging"].billing), ("trading", cases["trading"].billing))}
            accumulated = {case:{key:value+billed[case].get(key, 0.) for key, value in account.items()}
                           for case, account in live_bills.items()}
            count = index + 1
            # Keep live results at 15-minute resolution while limiting full-history writes on longer runs.
            publish(save=job["days"] <= 7 or count % 8 == 0, completed=absolute+1, series=list(combined),
                    comparison_date=label, day_completed=count,
                    billing_summary=accumulated, **totals_so_far(all_baseline, all_charging, all_trading,
                                                                 source, minimum, nuclear, stored_value, counts))
        if not count:
            break
        previous_trading_net_kwh = cases["trading"].previous_net_kwh
        previous_trading_loss_rate = cases["trading"].previous_loss_rate
        billing = {"baseline":reference_book.payload()}
        summaries = {"baseline":{**generation.totals(all_baseline[-count:], "", source), **engine.totals(all_baseline[-count:])}}
        for case, simulation in cases.items():
            _, records, billing[case], summaries[case] = simulation.result()
            openings[case], quotes[case] = carry_batteries(records), simulation.quote
            stem = "market-ledger" if case == "trading" else "battery-ledger"
            nhts.atomic_json(folder / f"{stem}-{label}.json", records)
        for case, account in billing.items():
            billed[case] = {key:value+billed[case].get(key, 0.) for key, value in account["totals"].items()}
        bill_days.append(billing)
        nhts.atomic_json(folder / f"bills-{label}.json", billing)
        nhts.atomic_json(folder / "daily-bills.json", cumulative_bills(bill_days))
        days[label] = {"intervals":count, "opening":start_state,
                       "closing":{case:opening_totals(openings[case], quotes[case]) for case in cases},
                       "scenarios":summaries, "billing":{case:value["totals"] for case, value in billing.items()}}
        publish(daily_summaries=dict(days))
