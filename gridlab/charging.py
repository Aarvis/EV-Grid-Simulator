"""Battery energy accounting for a saved travel day with home and destination charging."""
from collections import Counter, defaultdict
import heapq
import math
from pydantic import BaseModel, ConfigDict, Field
from .fleet import rng

INTERVAL_HOURS = .25
CHARGE_CURVE = ((0., 1.), (.90, 1.), (.95, .75), (.99, .40), (1., .40))
INITIAL_PARTIAL_SOC_MIN = .30
DRIVING_CONSUMPTION = {
    "small": {"mean": .15, "std": .020},
    "compact": {"mean": .17, "std": .025},
    "long_range": {"mean": .20, "std": .035},
    "large": {"mean": .27, "std": .050},
}


class ChargingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    seed: int = Field(default=42, ge=0, le=4294967295)
    driving_kwh_per_km: float | None = Field(default=None, gt=0)
    charge_efficiency: float = Field(default=.90, gt=0, le=1)
    initial_full_pct: float = Field(default=50., ge=0, le=100)
    away_charging_enabled: bool = True
    away_coverage_pct: float = Field(default=40., ge=0, le=100)
    away_seed: int = Field(default=42, ge=0, le=4294967295)


def driving_consumption(vehicle, config):
    if config.driving_kwh_per_km is not None:
        return config.driving_kwh_per_km
    distribution = DRIVING_CONSUMPTION[vehicle["battery_category"]]
    random = rng(config.seed, f"driving-consumption|{vehicle['id']}")
    while True:
        consumption = random.gauss(distribution["mean"], distribution["std"])
        if consumption > 0:
            return consumption


def charge_energy(energy, capacity, ac_kw, hours, efficiency):
    """Integrate dE/dt across the linear SOC bands, then stop at full."""
    soc = energy / capacity
    rate = ac_kw * efficiency / capacity
    if rate <= 0 or hours <= 0 or soc >= 1:
        return energy
    for (lo, low_factor), (hi, high_factor) in zip(CHARGE_CURVE, CHARGE_CURVE[1:]):
        if soc >= hi:
            continue
        slope = (high_factor - low_factor) / (hi - lo)
        factor = low_factor + slope * (soc - lo)
        if slope:
            time_to_boundary = math.log1p(slope * (hi - soc) / factor) / (rate * slope)
        else:
            time_to_boundary = (hi - soc) / (rate * factor)
        if hours < time_to_boundary:
            if slope:
                soc += factor * math.expm1(rate * slope * hours) / slope
            else:
                soc += rate * factor * hours
            return min(hi, soc) * capacity
        soc = hi
        hours -= time_to_boundary
    return capacity


def available_discharge_kw(energy, capacity, rated_ac_kw, hours, required_drive_kwh,
                           *, enabled=False, efficiency=.90):
    """Future V2G limit for constant export, checked at the lowest SOC of the interval."""
    if not enabled or rated_ac_kw <= 0 or hours <= 0:
        return 0.
    floor = max(.20 * capacity, required_drive_kwh)
    upper = min(rated_ac_kw, max(0., energy - floor) * efficiency / hours)
    # Below 30%, the available rating falls linearly to zero at 20%.
    # Solve power <= rating * (end_SOC - .20) / .10 in addition to the energy cap.
    tapered_limit = rated_ac_kw * (energy - .20 * capacity) / (
        .10 * capacity + rated_ac_kw * hours / efficiency)
    return max(0., min(upper, tapered_limit))


def assign_destinations(network, travel, config):
    """Assign each away stop, then admit EVs to ports in arrival order."""
    classes = defaultdict(set)
    for load in network["loads"]:
        classes[load["bus"]].add(load["profile_class"])
    buses = sorted(node["id"] for node in network["nodes"]
                   if node["reachable"] and classes[node["id"]] == {"commercial profile"})
    if not buses and any(period["state"] == "parked_away" for v in travel["vehicles"] for period in v["segments"]):
        raise ValueError("No connected nonresidential buses are available for away stops")
    ranked = buses.copy()
    rng(config.away_seed, "destination-sites").shuffle(ranked)
    count = math.floor(len(buses) * config.away_coverage_pct / 100 + .5) if config.away_charging_enabled else 0
    sites = {bus:rng(config.away_seed, f"destination-ports|{bus}").randint(1, 5) for bus in ranked[:count]}
    arrivals = []
    for vehicle in travel["vehicles"]:
        vid = vehicle["vehicle_id"]
        random = rng(config.away_seed, f"destination-stops|{vid}")
        previous = None
        for index, period in enumerate(vehicle["segments"]):
            if period["state"] == "parked_away":
                choices = [bus for bus in buses if bus != previous] if len(buses) > 1 else buses
                bus = random.choice(choices)
                previous = bus
                arrivals.append((period["start_minute"], vid, index, period["end_minute"], bus))
    occupied = defaultdict(list)
    stops = defaultdict(dict)
    counts = Counter()
    for start, vid, index, end, bus in sorted(arrivals):
        if not config.away_charging_enabled:
            status = "disabled"
        elif bus not in sites:
            status = "no_charger"
        else:
            departures = occupied[bus]
            while departures and departures[0] <= start:
                heapq.heappop(departures)
            status = "site_full"
            if len(departures) < sites[bus]:
                heapq.heappush(departures, end)
                status = "plugged_in"
        counts[status] += 1
        stops[vid][index] = {"bus":bus, "start_minute":start, "end_minute":end,
                             "status":status, "grid_kwh":0.}
    return stops, sites, {"candidate_buses":len(buses), "equipped_buses":len(sites),
                          "ports":sum(sites.values()), "away_stops":len(arrivals),
                          "plugged_in_stops":counts["plugged_in"], "full_site_stops":counts["site_full"],
                          "no_charger_stops":counts["no_charger"], "disabled_stops":counts["disabled"]}


def build_schedule(inventory, travel, config, network=None):
    evs = {vehicle["id"]: vehicle for vehicle in inventory["vehicles"] if vehicle["is_ev"]}
    if not evs:
        raise ValueError("Generate a fleet with EVs before running home charging")
    if len(travel["vehicles"]) != len(evs) or {v["vehicle_id"] for v in travel["vehicles"]} != evs.keys():
        raise ValueError("Travel day and charger inventory contain different EVs")
    full_count = math.floor(len(evs) * config.initial_full_pct / 100 + .5)
    full = set(rng(config.seed, "initial-full").sample(sorted(evs), full_count))
    stops, sites, destination_summary = assign_destinations(network, travel, config) if network is not None else ({}, {}, {})
    away_by_bus = defaultdict(lambda: [0.] * 96)
    intervals = [{"grid_kwh":0., "battery_kwh":0., "drive_kwh":0., "unmet_kwh":0.,
                  "home_grid_kwh":0., "away_grid_kwh":0., "charging_evs":0,
                  "battery_end_kwh":0.} for _ in range(96)]
    schedules = []
    for diary in travel["vehicles"]:
        vid = diary["vehicle_id"]
        vehicle = evs[vid]
        if (diary["home_bus"], diary["household_id"]) != (vehicle["bus"], vehicle["household_id"]):
            raise ValueError(f"Home in travel schedule differs from fleet for {vid}")
        capacity = vehicle["battery_kwh"]
        ac_kw = vehicle["potential_home_charging_kw"]
        kwh_per_km = driving_consumption(vehicle, config)
        if not vehicle["home_access"] or ac_kw <= 0:
            raise ValueError(f"Home charger missing for {vid}")
        soc = 1. if vid in full else min(
            rng(config.seed, f"initial-soc|{vid}").uniform(INITIAL_PARTIAL_SOC_MIN, 1.), math.nextafter(1., 0.))
        energy = capacity * soc
        end_soc, grid_kwh, home_grid_kwh, energy_events = [], [], [], []
        away_stops = stops.get(vid, {})
        segment_index = 0
        for index, row in enumerate(intervals):
            cursor = index * 15
            end = cursor + 15
            home_energy = away_energy = 0.
            events = []
            while cursor < end:
                period = diary["segments"][segment_index]
                stop = min(end, period["end_minute"])
                minutes = stop - cursor
                away_stop = away_stops.get(segment_index)
                if period["state"] == "home" or (away_stop and away_stop["status"] == "plugged_in"):
                    updated = charge_energy(energy, capacity, ac_kw, minutes/60, config.charge_efficiency)
                    gain = updated - energy
                    row["battery_kwh"] += gain
                    draw = gain / config.charge_efficiency
                    if draw > 0:
                        events.append((gain, draw, 0.))
                    if away_stop:
                        away_energy += draw
                        away_stop["grid_kwh"] += draw
                        if draw > 0:
                            away_by_bus[away_stop["bus"]][index] += draw
                    else:
                        home_energy += draw
                    energy = updated
                elif period["state"] == "driving":
                    trip = diary["trips"][period["trip_index"]]
                    required = trip["distance_km"] * kwh_per_km * minutes / (
                        trip["arrive_minute"] - trip["depart_minute"])
                    used = min(energy, required)
                    row["drive_kwh"] += used
                    row["unmet_kwh"] += required - used
                    events.append((-used, 0., required - used))
                    energy -= used
                cursor = stop
                if cursor == period["end_minute"]:
                    segment_index += 1
            grid_energy = home_energy + away_energy
            row["grid_kwh"] += grid_energy
            row["home_grid_kwh"] += home_energy
            row["away_grid_kwh"] += away_energy
            row["charging_evs"] += int(grid_energy > 0)
            row["battery_end_kwh"] += energy
            grid_kwh.append(grid_energy)
            home_grid_kwh.append(home_energy)
            end_soc.append(energy / capacity)
            energy_events.append(events)
        schedules.append({"id":vid, "home_bus":vehicle["bus"], "household_id":vehicle["household_id"],
                          "voltage_v":vehicle["charger_voltage_v"], "rated_ac_kw":ac_kw,
                          "capacity_kwh":capacity, "battery_category":vehicle.get("battery_category"),
                          "driving_kwh_per_km":kwh_per_km, "initial_soc":soc, "end_soc":end_soc,
                          "grid_kwh":grid_kwh, "home_grid_kwh":home_grid_kwh,
                          "energy_events":energy_events,
                          "away_stops":[away_stops[index] for index in sorted(away_stops)]})
    return {"intervals":intervals, "vehicles":schedules,
            "destination_sites":sites, "destination_summary":destination_summary,
            "away_grid_kwh_by_bus":dict(away_by_bus),
            "initial_full_count":full_count, "ev_count":len(evs),
            "initial_battery_kwh":sum(v["initial_soc"]*v["capacity_kwh"] for v in schedules),
            "capacity_kwh":sum(v["capacity_kwh"] for v in schedules),
            "nameplate_kw":sum(v["rated_ac_kw"] for v in schedules)}
