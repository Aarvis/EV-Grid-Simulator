"""Interval EV commitments, charger settlement, and daily customer bills."""
from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import engine, generation
from .charging import charge_energy
from .fleet import rng


class MarketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    trader_pct: float = Field(default=20., ge=0, le=100)
    trading_only_pct: float = Field(default=0., ge=0, le=100)
    seed: int = Field(default=42, ge=0, le=4294967295)
    pricing_mode: Literal["fixed", "dynamic"] = "fixed"
    behavior: Literal["sell", "buy_sell_wait"] = "sell"
    discharge_efficiency: float = Field(default=.90, gt=0, le=1)
    reserve_pct: float = Field(default=20., ge=0, le=100)
    travel_buffer_pct: float = Field(default=10., ge=0, le=100)
    export_kw: float = Field(default=7.2, ge=0)
    away_export: bool = False
    allow_sales_despite_grid_violations: bool = False
    wear_usd_per_battery_kwh: float = Field(default=0., ge=0)


def choose_traders(vehicle_ids, config):
    ranked = sorted(vehicle_ids)
    rng(config.seed, "market-traders").shuffle(ranked)
    count = math.floor(len(ranked) * config.trader_pct / 100 + .5)
    return set(ranked[:count])


def choose_trading_only(vehicle_ids, config):
    traders = sorted(choose_traders(vehicle_ids, config))
    count = math.floor(len(traders) * config.trading_only_pct / 100 + .5)
    rng(config.seed, "trading-only").shuffle(traders)
    return traders[:count]


@dataclass(frozen=True)
class Window:
    hours: float
    meter: tuple[str, str] | None
    drive_kwh: float = 0.
    remaining_drive_kwh: float = 0.
    planned_drive_kwh: float = 0.
    overnight: bool = False


@dataclass(frozen=True)
class Event:
    kind: str
    battery_kwh: float
    meter_kwh: float = 0.
    meter: tuple[str, str] | None = None
    unmet_kwh: float = 0.


def windows_for_day(vehicle, diary, charge_efficiency, tomorrow_kwh=None):
    """Slice the same travel and port assignments into ordered interval windows."""
    stops = {stop["start_minute"]:stop for stop in vehicle["away_stops"]}
    periods = []
    for segment in diary["segments"]:
        start, end = segment["start_minute"], segment["end_minute"]
        state = segment["state"]
        meter, drive = None, 0.
        if state == "home":
            meter = ("home", vehicle["id"])
        elif state == "parked_away" and stops.get(start, {}).get("status") == "plugged_in":
            meter = ("away", stops[start]["bus"])
        elif state == "driving":
            drive = diary["trips"][segment["trip_index"]]["distance_km"] * vehicle["driving_kwh_per_km"]
        periods.append((start, end, meter, drive))
    result = [[] for _ in range(96)]
    for position, (start, end, meter, drive) in enumerate(periods):
        overnight = meter is not None and meter[0] == "home" and position == len(periods)-1
        planned_drive = (tomorrow_kwh if overnight else sum(period[3] for period in periods[position+1:])) if tomorrow_kwh is not None else 0.
        for index in range(start // 15, (end - 1) // 15 + 1):
            cutoff = (index + 1) * 15
            minutes = min(end, cutoff) - max(start, index * 15)
            remaining_drive = 0.
            if meter:
                next_stop_drive = 0.
                for _, next_end, next_meter, next_drive in periods[position+1:]:
                    # A later stop is reliable only if an assigned charger is available after this sale interval.
                    if next_meter and next_end > cutoff:
                        break
                    next_stop_drive += next_drive
                # Even a connected stop may be short. Account for conservative future
                # recharge at the minimum power fraction on the existing charge curve.
                for next_start, next_end, next_meter, next_drive in reversed(periods[position+1:]):
                    if next_meter and next_end > cutoff:
                        charging_hours = (next_end - max(next_start, cutoff)) / 60
                        remaining_drive = max(0., remaining_drive - charging_hours * vehicle["rated_ac_kw"] * charge_efficiency * .40)
                    remaining_drive += next_drive
                remaining_drive = max(next_stop_drive, remaining_drive)
            result[index].append(Window(minutes / 60, meter, drive * minutes / (end - start),
                                        remaining_drive, planned_drive, overnight))
    return result


def protected_energy(window, capacity, market):
    drive = window.planned_drive_kwh + capacity * market.travel_buffer_pct / 100
    return max(drive, capacity * market.reserve_pct / 100)


def plan_events(windows, stored, vehicle, charging, market, direction, flexible=False):
    capacity, power = vehicle["capacity_kwh"], vehicle["rated_ac_kw"]
    events = []
    for window in windows:
        if window.drive_kwh:
            used = min(stored, window.drive_kwh)
            events.append(Event("drive", -used, unmet_kwh=window.drive_kwh-used))
            stored -= used
        if not window.meter:
            continue
        if direction in ("buy", "top_up"):
            updated = charge_energy(stored, capacity, power, window.hours, charging.charge_efficiency)
            if direction == "top_up":
                updated = min(updated, protected_energy(window, capacity, market))
            gain = max(0., updated - stored)
            if gain:
                events.append(Event("buy", gain, gain / charging.charge_efficiency, window.meter))
                stored += gain
        if direction == "sell" and (window.meter[0] == "home" or market.away_export):
            reserve = max(capacity * market.reserve_pct / 100,
                          window.remaining_drive_kwh + capacity * market.travel_buffer_pct / 100)
            if flexible:
                reserve = protected_energy(window, capacity, market)
            exported = min(max(0., stored - reserve) * market.discharge_efficiency,
                           min(power, market.export_kw) * window.hours)
            if exported > 1e-10:
                used = exported / market.discharge_efficiency
                events.append(Event("sell", -used, exported, window.meter))
                stored -= used
    return events


def commit(windows, stored, value, vehicle, charging, market, trader, quote_per_mwh,
           expected_loss_cost_per_mwh=0.):
    flexible = trader and market.behavior == "buy_sell_wait"
    if flexible:
        top_up = plan_events(windows, stored, vehicle, charging, market, "top_up")
        if any(event.kind == "buy" for event in top_up):
            return top_up
    if trader and stored > 0 and quote_per_mwh * market.discharge_efficiency / 1000 > (
            value / stored + market.wear_usd_per_battery_kwh):
        events = plan_events(windows, stored, vehicle, charging, market, "sell", flexible=flexible)
        if any(event.kind == "sell" for event in events):
            return events
    if flexible and stored > 0:
        new_cost = (quote_per_mwh + expected_loss_cost_per_mwh) / (1000 * charging.charge_efficiency)
        if new_cost >= value / stored:
            return plan_events(windows, stored, vehicle, charging, market, "idle")
    return plan_events(windows, stored, vehicle, charging, market, "buy")


def meter_totals(plans):
    net, trades = defaultdict(float), {}
    for vehicle_id, events in plans.items():
        imports = exports = 0.
        for event in events:
            if event.kind == "buy":
                imports += event.meter_kwh
                net[event.meter] += event.meter_kwh
            if event.kind == "sell":
                exports += event.meter_kwh
                net[event.meter] -= event.meter_kwh
        trades[vehicle_id] = (imports, exports)
    return net, trades


def shuffled_commitments(vehicles, traders, windows, starting, charging, market,
                         previous_price, previous_net_kwh, baseline_kw, solar_mw, nuclear_mw,
                         source, day, index, expected_loss_cost_per_mwh=0.):
    """Replace the remaining share of the aggregate forecast after each trader decides."""
    plans, quotes, positions = {}, {}, {}

    def decide(key, quote):
        state = starting[key]
        return commit(windows[key][index], state["stored_kwh"], state["value_usd"],
                      vehicles[key], charging, market, key in traders, quote, expected_loss_cost_per_mwh)

    for key in vehicles:
        if key not in traders:
            plans[key] = decide(key, previous_price)
    passive_kwh = sum(event.meter_kwh for events in plans.values() for event in events if event.kind == "buy")
    order = sorted(traders)
    rng(market.seed, f"trading-order|{day}|{index}").shuffle(order)
    net_kwh = 0.
    quote = None
    for decided in range(len(order)+1):
        remaining = (len(order)-decided) / len(order)
        draw_kw = baseline_kw + (passive_kwh + net_kwh + previous_net_kwh * remaining) / generation.INTERVAL_HOURS
        quote = generation.dispatch(max(0., draw_kw), nuclear_mw, source, solar_mw)["average_generation_cost_per_mwh"]
        if quote is None:
            quote = 0.
        if decided == len(order):
            break
        key = order[decided]
        quotes[key] = quote
        positions[key] = decided + 1
        events = plans[key] = decide(key, quote)
        net_kwh += sum(event.meter_kwh if event.kind == "buy" else -event.meter_kwh
                       for event in events if event.kind in ("buy", "sell"))
    return plans, quotes, positions, quote


@dataclass
class Bill:
    import_kwh: float = 0.
    export_kwh: float = 0.
    energy_cost_usd: float = 0.
    loss_kwh: float = 0.
    loss_cost_usd: float = 0.
    export_credit_usd: float = 0.

    def accrue(self, imports, exports, quote, loss_rate, loss_price):
        self.import_kwh += imports
        self.export_kwh += exports
        self.energy_cost_usd += imports * quote
        self.loss_kwh += imports * loss_rate
        self.loss_cost_usd += imports * loss_rate * loss_price
        self.export_credit_usd += exports * quote

    def payload(self):
        return {**vars(self), "net_usd": self.energy_cost_usd + self.loss_cost_usd - self.export_credit_usd}


class BillingBook:
    def __init__(self, services, loads, vehicles=()):
        load_index = {load["id"]:index for index, load in enumerate(loads)}
        self.services = {service["id"]: service for service in services}
        self.indices = {key: [load_index[name] for name in service["load_ids"]]
                        for key, service in self.services.items()}
        self.vehicles = {vehicle["id"]:vehicle for vehicle in vehicles}
        self.service_bills = {key: Bill() for key in self.services}
        self.ev_bills = {key: Bill() for key in self.vehicles}
        self.total_bill, self.vehicle_bill = Bill(), Bill()

    def settle(self, loads, trades, quote_per_mwh, realized_per_mwh, loss_kwh, trade_quotes=None):
        demand = {key:max(0., sum(loads[index][0] for index in indices) * .25)
                  for key, indices in self.indices.items()}
        vehicle_imports = sum(buy for buy, _ in trades.values())
        exports = sum(sell for _, sell in trades.values())
        gross = sum(demand.values()) + vehicle_imports
        rate = loss_kwh / gross if gross else 0.
        quote, loss_price = quote_per_mwh / 1000, realized_per_mwh / 1000
        for key, kwh in demand.items():
            self.service_bills[key].accrue(kwh, 0., quote, rate, loss_price)
        purchase_adjustment = export_adjustment = 0.
        for key, (buy, sell) in trades.items():
            trade_quote = trade_quotes.get(key, quote_per_mwh) / 1000 if trade_quotes else quote
            self.ev_bills[key].accrue(buy, sell, trade_quote, rate, loss_price)
            purchase_adjustment += buy * (trade_quote - quote)
            export_adjustment += sell * (trade_quote - quote)
        self.total_bill.accrue(gross, exports, quote, rate, loss_price)
        self.vehicle_bill.accrue(vehicle_imports, exports, quote, rate, loss_price)
        for bill in (self.total_bill, self.vehicle_bill):
            bill.energy_cost_usd += purchase_adjustment
            bill.export_credit_usd += export_adjustment
        return rate

    def totals(self):
        return {**self.total_bill.payload(), "vehicle_net_usd":self.vehicle_bill.payload()["net_usd"]}

    def payload(self, traders=()):
        services = [{"id":key, "bus":service["bus"], "profile_class":service["profile_class"], **self.service_bills[key].payload()}
                    for key, service in self.services.items()]
        vehicles = [{"id":key, "household_id":vehicle["household_id"], "bus":vehicle["home_bus"],
                     "trader":key in traders, **self.ev_bills[key].payload()} for key, vehicle in self.vehicles.items()]
        by_house = defaultdict(list)
        for bill in vehicles:
            by_house[bill["household_id"]].append(bill)
        fields = list(Bill().payload())
        households = [{**service, **{field:service[field] + sum(v[field] for v in by_house[service["id"]]) for field in fields}}
                      for service in services if service["profile_class"] == "residential"]
        totals = {field:sum(bill[field] for bill in services + vehicles) for field in fields}
        totals["vehicle_net_usd"] = sum(bill["net_usd"] for bill in vehicles)
        return {"services":services, "vehicles":vehicles, "households":households, "totals":totals}


def replay(events, stored, value, vehicle, quote, realized, loss_rate, wear):
    """Apply settled events in chronological order to the battery's weighted-average cost."""
    row = dict.fromkeys(("home_draw_kwh", "away_draw_kwh", "distribution_loss_kwh", "charged_kwh", "driving_kwh",
                         "unmet_kwh", "charging_cost_usd", "driving_cost_usd", "export_kwh", "export_credit_usd",
                         "export_battery_cost_usd", "export_battery_kwh", "wear_usd"), 0.)
    for event in events:
        if event.kind == "buy":
            loss = event.meter_kwh * loss_rate
            cost = (event.meter_kwh * quote + loss * realized) / 1000
            row[event.meter[0] + "_draw_kwh"] += event.meter_kwh
            row["distribution_loss_kwh"] += loss
            row["charged_kwh"] += event.battery_kwh
            row["charging_cost_usd"] += cost
            value += cost
        else:
            removed = -event.battery_kwh
            cost = value * min(1., removed / stored) if stored > 0 else 0.
            value -= cost
            if event.kind == "sell":
                row["export_kwh"] += event.meter_kwh
                row["export_battery_kwh"] += removed
                row["export_credit_usd"] += event.meter_kwh * quote / 1000
                row["export_battery_cost_usd"] += cost
                row["wear_usd"] += removed * wear
            else:
                row["driving_kwh"] += removed
                row["unmet_kwh"] += event.unmet_kwh
                row["driving_cost_usd"] += cost
        stored += event.battery_kwh
    used = row["driving_kwh"]
    row.update(stored_kwh=stored, value_usd=value, average_cost_usd_per_kwh=value / stored if stored > 0 else None,
               driving_cost_usd_per_km=row["driving_cost_usd"] / used * vehicle["driving_kwh_per_km"] if used else None)
    return row


def reference_bills(rows, schedule, services, loads, first_offer, read_snapshot):
    """Bill the fixed demand cases using the same lagged-price rule as trading."""
    baseline = BillingBook(services, loads)
    charging = BillingBook(services, loads, schedule["vehicles"]) if schedule else None
    offers = []
    quote = baseline_quote = first_offer
    prefix = "baseline_" if charging else ""
    for index, row in enumerate(rows):
        snapshot = read_snapshot(index)
        baseline_snapshot = snapshot["baseline"] if charging else snapshot
        baseline_price = row[prefix + "average_generation_cost_per_mwh"]
        baseline.settle(baseline_snapshot["loads"], {}, baseline_quote, baseline_price, row[prefix + "loss_kw"] * .25)
        row[prefix + "locked_price_per_mwh"] = baseline_quote
        baseline_quote = baseline_price
        if charging:
            trades = {vehicle["id"]:(vehicle["grid_kwh"][index], 0.) for vehicle in schedule["vehicles"]}
            price = row["average_generation_cost_per_mwh"]
            loss_rate = charging.settle(snapshot["loads"], trades, quote, price, row["loss_kw"] * .25)
            row["locked_price_per_mwh"] = quote
            offers.append((quote, loss_rate))
            quote = price
    bills = {"baseline":baseline.payload()}
    if charging:
        bills["charging"] = charging.payload()
    return bills, offers


def worsens_limits(row, snapshot, reference):
    if row["supply_kw"] < 0 or not row["balance_ok"]:
        return True
    for after, before in zip(snapshot["elements"], reference["elements"], strict=True):
        loading, existing = after["loading_pct"], before["loading_pct"]
        if loading is not None and loading > max(100., existing or 0.) + .001:
            return True
    for after, before in zip(snapshot["voltages"], reference["voltages"], strict=True):
        if after is not None and before is not None and before >= .1 and after < min(.95, before) - .00001:
            return True
    for after, before in zip(snapshot["max_voltages"], reference["max_voltages"], strict=True):
        if after is not None and after > max(1.05, before or 0.) + .00001:
            return True
    return False


def open_grid(schedule, day, network, seed):
    template = {**schedule, "away_grid_kwh_by_bus":{bus:[0.]*96 for bus in schedule["destination_sites"]}}
    dss = engine.context()
    engine.solve(dss, day, 0)
    return dss, engine.install_chargers(dss, network, template, seed)


class TradingDay:
    def __init__(self, schedule, diaries, opening_ledger, charging, config, source, nuclear_mw,
                 first_offer, day, network, ratings, services, grid=None, solar=(), previous_net_kwh=0.,
                 next_distances=None, previous_loss_rate=0.):
        self.vehicles = {v["id"]:v for v in schedule["vehicles"]}
        self.traders = choose_traders(self.vehicles, config)
        self.flexible_traders = self.traders if config.behavior == "buy_sell_wait" else set()
        if self.flexible_traders and next_distances is None:
            raise ValueError("Buy, sell, or wait requires tomorrow's driving plan")
        self.windows = {}
        for diary in diaries:
            key = diary["vehicle_id"]
            vehicle = self.vehicles[key]
            tomorrow_kwh = next_distances[key] * vehicle["driving_kwh_per_km"] if key in self.flexible_traders else None
            self.windows[key] = windows_for_day(vehicle, diary, charging.charge_efficiency, tomorrow_kwh)
        self.records = {key:{"id":key, "trader":key in self.traders, "opening":record["opening"], "intervals":[]}
                        for key, record in opening_ledger.items()}
        self.grid = grid if grid is not None else open_grid(schedule, day, network, charging.seed)
        self.billing = BillingBook(services, network["loads"], self.vehicles.values())
        self.charging, self.config, self.source, self.nuclear_mw = charging, config, source, nuclear_mw
        self.day, self.network, self.ratings = day, network, ratings
        self.solar = solar
        self.quote, self.rows = first_offer, []
        self.previous_net_kwh = previous_net_kwh
        self.previous_loss_rate = previous_loss_rate

    def step(self, index, read_snapshot, baseline_kw=None):
        vehicles, traders, windows, records = self.vehicles, self.traders, self.windows, self.records
        charging, config, quote = self.charging, self.config, self.quote
        day, network, ratings = self.day, self.network, self.ratings
        dss, connections = self.grid
        starting = {key:(record["intervals"][-1] if index else record["opening"]) for key, record in records.items()}
        solar_mw = generation.solar_at(self.solar, day, index)
        expected_loss_cost = self.previous_loss_rate * quote
        trade_quotes, positions, closing_quote = {}, {}, None
        if config.pricing_mode == "dynamic" and traders:
            if baseline_kw is None:
                raise ValueError("Dynamic pricing requires the current baseline substation draw")
            plans, trade_quotes, positions, closing_quote = shuffled_commitments(
                vehicles, traders, windows, starting, charging, config, quote, self.previous_net_kwh,
                baseline_kw, solar_mw, self.nuclear_mw, self.source, day, index, expected_loss_cost)
        else:
            plans = {key:commit(windows[key][index], starting[key]["stored_kwh"], starting[key]["value_usd"],
                                vehicle, charging, config, key in traders, quote, expected_loss_cost)
                     for key, vehicle in vehicles.items()}
        meters, trades = meter_totals(plans)
        proposed_sellers = {key for key, (_, sell) in trades.items() if sell}
        proposed = sum(sell for _, sell in trades.values())
        allow_violations = config.allow_sales_despite_grid_violations
        screen_exports = bool(proposed) and not allow_violations
        reference = read_snapshot(index) if screen_exports else None
        try:
            engine.set_charger_loads(dss, connections, index, meters)
            engine.solve(dss, day, index)
            row, snapshot = engine.state(dss, network, ratings, day, index, connections)
            curtail = screen_exports and worsens_limits(row, snapshot, reference)
        except RuntimeError:
            if not screen_exports:
                raise
            curtail = True
        if curtail:
            for key, (_, sell) in trades.items():
                if sell:
                    plans[key] = plan_events(windows[key][index], starting[key]["stored_kwh"],
                                             vehicles[key], charging, config, "idle")
            meters, trades = meter_totals(plans)
            engine.set_charger_loads(dss, connections, index, meters)
            engine.solve(dss, day, index)
            row, snapshot = engine.state(dss, network, ratings, day, index, connections)
        if allow_violations and not row["balance_ok"]:
            raise RuntimeError(
                f"Power balance check failed at {row['timestamp']} (interval {index + 1}): "
                f"{abs(row['balance_residual_kw']):.6f} kW mismatch exceeds "
                f"{row['balance_tolerance_kw']:.6f} kW tolerance"
            )
        imports = sum(buy for buy, _ in trades.values())
        exports = sum(sell for _, sell in trades.values())
        if not math.isclose(row["ev_charge_kw"] * .25, imports - exports, abs_tol=1e-5):
            raise RuntimeError("Solved net EV power differs from the metered commitments")
        row.update(generation.dispatch(row["supply_kw"], self.nuclear_mw, self.source, solar_mw))
        price = row["average_generation_cost_per_mwh"]
        loss_rate = self.billing.settle(snapshot["loads"], trades, quote, price, row["loss_kw"] * .25, trade_quotes)
        for key, events in plans.items():
            state = starting[key]
            trade_quote = trade_quotes.get(key, quote)
            ledger = replay(events, state["stored_kwh"], state["value_usd"], vehicles[key],
                            trade_quote, price, loss_rate, config.wear_usd_per_battery_kwh)
            if key in positions:
                ledger.update(bid_position=positions[key], locked_price_per_mwh=trade_quote)
            if key in self.flexible_traders:
                connected = [window for window in windows[key][index] if window.meter]
                if connected:
                    vehicle = vehicles[key]
                    capacity = vehicle["capacity_kwh"]
                    window = max(connected, key=lambda item: protected_energy(item, capacity, config))
                    action = "buy" if trades[key][0] else "sell" if trades[key][1] else "wait"
                    if curtail and key in proposed_sellers:
                        action = "curtailed"
                    ledger.update(decision=action, planned_drive_kwh=window.planned_drive_kwh,
                                  protected_kwh=protected_energy(window, capacity, config),
                                  overnight_target=window.overnight, locked_price_per_mwh=trade_quote,
                                  estimated_buy_cost_per_kwh=(trade_quote+expected_loss_cost)/(1000*charging.charge_efficiency))
                else:
                    ledger["decision"] = "unplugged"
            records[key]["intervals"].append(ledger)
        if trade_quotes:
            row.update(opening_bid_per_mwh=next(iter(trade_quotes.values())), closing_bid_per_mwh=closing_quote,
                       forecast_trading_net_kwh=self.previous_net_kwh)
        self.previous_net_kwh = sum(trades[key][0]-trades[key][1] for key in traders)
        self.previous_loss_rate = loss_rate
        row.update(locked_price_per_mwh=quote, ev_import_kwh=imports, ev_export_kwh=exports,
                   upstream_export_kwh=max(0., -row["supply_kw"]) * generation.INTERVAL_HOURS,
                   passive_import_kwh=sum(buy for key, (buy, _) in trades.items() if key not in traders),
                   curtailed_export_kwh=proposed-exports,
                   ev_battery_end_kwh=sum(record["intervals"][-1]["stored_kwh"] for record in records.values()))
        self.rows.append(row)
        self.quote = price
        return row, snapshot

    def result(self):
        records, traders, rows = self.records, self.traders, self.rows
        details = self.billing.payload(traders)
        summary = {**generation.totals(rows, "", self.source), **engine.totals(rows),
                   "traders":len(traders), "passive":len(self.vehicles)-len(traders),
                   "ev_battery_end_kwh":rows[-1]["ev_battery_end_kwh"],
                   "ev_battery_end_value_usd":sum(record["intervals"][-1]["value_usd"] for record in records.values())}
        for field in ("ev_import_kwh", "ev_export_kwh", "passive_import_kwh", "curtailed_export_kwh", "upstream_export_kwh"):
            summary[field] = sum(row[field] for row in rows)
        summary["ev_unmet_driving_kwh"] = sum(row["unmet_kwh"] for record in records.values() for row in record["intervals"])
        summary["wear_usd"] = sum(row["wear_usd"] for record in records.values() for row in record["intervals"])
        return rows, records, details, summary


def run_trading(schedule, diaries, opening_ledger, charging, config, source, nuclear_mw, first_offer,
                day, network, ratings, services, read_snapshot, write_snapshot, stop, progress, solar=(),
                baseline_kw=None, next_distances=None):
    simulation = TradingDay(schedule, diaries, opening_ledger, charging, config, source, nuclear_mw,
                            first_offer, day, network, ratings, services, solar=solar, next_distances=next_distances)
    for index in range(96):
        if stop():
            return None
        _, snapshot = simulation.step(index, read_snapshot, baseline_kw[index] if baseline_kw is not None else None)
        write_snapshot(index, snapshot)
        progress(index + 1)
    return simulation.result()
