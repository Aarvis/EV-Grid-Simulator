"""One-day EV mobility using bounded distance targets and complete NHTS diaries."""
from collections import Counter
from datetime import date as Date
import math
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import least_squares
from scipy.stats import truncnorm
from .fleet import digest, rng
from .nhts import HOME


class TravelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    date: Date = Date(2018, 11, 21)
    mean_km: float = Field(default=46, ge=0, le=1000)
    std_km: float = Field(default=22.5, ge=0, le=1000)
    min_km: float = Field(default=0, ge=0, le=1000)
    max_km: float = Field(default=120, gt=0, le=2000)
    stationary_pct: float = Field(default=0, ge=0, le=100)
    seed: int = Field(default=42, ge=0, le=4294967295)
    prefer_month: bool = True
    match_tolerance_km: float = Field(default=5, ge=0, le=100)

    @model_validator(mode="after")
    def validate_distribution(self):
        if self.date.year != 2018:
            raise ValueError("Choose a date within the electrical profile year 2018")
        calibrate(self)
        return self


def calibrate(config):
    """Moment-match a normal AFTER truncation, including the zero-distance mass.

    Not every bounded mean/SD pair is a truncated normal; explicitly fail rather
    than silently substituting a different distribution or shifting the targets.
    """
    c = config
    p = c.stationary_pct / 100
    if c.min_km >= c.max_km:
        raise ValueError("Minimum distance must be below maximum distance")
    if p == 1 or c.mean_km == 0:
        if c.mean_km != 0 or c.std_km != 0:
            raise ValueError("All-stationary days require mean and standard deviation of zero")
        return {"all_stationary":True}
    mean = c.mean_km / (1-p)
    variance = (c.std_km**2+c.mean_km**2)/(1-p)-mean**2
    if variance < -1e-8 or not c.min_km <= mean <= c.max_km:
        raise ValueError("Mean, standard deviation and unused-vehicle percentage are incompatible")
    if abs(variance) < 1e-8:
        return {"point_km":mean, "moving_mean_km":mean, "moving_std_km":0.}
    span = c.max_km-c.min_km
    m, s = (mean-c.min_km)/span, math.sqrt(variance)/span
    if not 0 < m < 1 or s*s >= m*(1-m):
        raise ValueError("Requested mean/standard deviation cannot fit the distance bounds")
    def residual(x):
        loc, scale = x[0], math.exp(x[1])
        mm, vv = truncnorm.stats(-loc/scale, (1-loc)/scale, loc=loc, scale=scale, moments="mv")
        return [float(mm)-m, math.sqrt(max(0.,float(vv)))-s]
    solution = least_squares(residual, [m, math.log(max(s,.001))],
        bounds=([-20,math.log(.0001)],[20,math.log(20)]),
        xtol=1e-11, ftol=1e-11, gtol=1e-11, max_nfev=300)
    if not solution.success or max(abs(v) for v in residual(solution.x)) > 1e-6:
        raise ValueError("Those moments cannot be matched by a truncated normal; reduce SD or adjust bounds/mean")
    return {"location_km":c.min_km+span*solution.x[0],
        "scale_km":span*math.exp(solution.x[1]), "moving_mean_km":mean,
        "moving_std_km":math.sqrt(variance)}


def distance_targets(count, config):
    params = calibrate(config)
    if not count:
        return [], params
    if params.get("all_stationary"):
        return [0.]*count, params
    unused = math.floor(count*config.stationary_pct/100+.5)
    moving = count-unused
    targets = [0.]*unused
    if moving:
        if "point_km" in params:
            targets += [params["point_km"]]*moving
        else:
            loc, scale = params["location_km"], params["scale_km"]
            q = (np.arange(moving)+.5)/moving
            targets += truncnorm.ppf(q, (config.min_km-loc)/scale,
                (config.max_km-loc)/scale, loc=loc, scale=scale).tolist()
    rng(config.seed,"travel-distances").shuffle(targets)
    return targets, params


def statistics(values):
    return {"mean_km":float(np.mean(values)) if values else 0.,
        "std_km":float(np.std(values)) if values else 0.,
        "min_km":min(values,default=0.), "max_km":max(values,default=0.)}


def segments(trips):
    """Contiguous half-open intervals cover every minute of [00:00, 24:00)."""
    result = []
    cursor, state, purpose = 0, "home", "Home"
    for index, trip in enumerate(trips):
        if trip["depart_minute"] > cursor:
            result.append({"start_minute":cursor,"end_minute":trip["depart_minute"],
                "state":state,"purpose":purpose})
        result.append({"start_minute":trip["depart_minute"],"end_minute":trip["arrive_minute"],
            "state":"driving","purpose":trip["destination"],"trip_index":index})
        cursor = trip["arrive_minute"]
        state = "home" if trip["destination_code"] in HOME else "parked_away"
        purpose = trip["destination"]
    if cursor < 1440:
        result.append({"start_minute":cursor,"end_minute":1440,"state":state,"purpose":purpose})
    return result


def vehicle_state(vehicle, minute):
    if minute == 1440:
        return {"state":"home", "purpose":"Home"}
    return next(s for s in vehicle["segments"] if s["start_minute"] <= minute < s["end_minute"])


def keep_vehicles_home(plan, vehicle_ids):
    """Apply run-specific parking without changing the saved donor diaries."""
    if not vehicle_ids:
        return plan
    vehicles = [
        {**vehicle, "target_km":0., "distance_km":0., "diary_id":None, "survey_yyyymm":None,
         "survey_weekday":None, "calendar_match":"trading_only", "first_departure_minute":None,
         "last_return_minute":None, "home_minutes":1440, "driving_minutes":0, "parked_away_minutes":0,
         "trips":[], "segments":segments([])}
        if vehicle["vehicle_id"] in vehicle_ids else vehicle
        for vehicle in plan["vehicles"]
    ]
    return {**plan, "vehicles":vehicles}


def travel_identity(fleet_id, source, config):
    return {"algorithm":"travel-v1", "fleet_id":fleet_id, "source_digest":source["digest"],
            "config":config.model_dump(mode="json")}


def build_travel(fleet, source, config):
    config = config if isinstance(config,TravelConfig) else TravelConfig.model_validate(config)
    evs = sorted((v for v in fleet["vehicles"] if v["is_ev"]), key=lambda v:v["id"])
    targets, calibration = distance_targets(len(evs),config)
    # Python Monday=0, NHTS Sunday=1.
    weekday = (config.date.weekday()+1)%7+1
    weekend = weekday in {1,7}
    eligible = [d for d in source["diaries"] if config.min_km <= d["total_km"] <= config.max_km]
    pools = []
    if config.prefer_month:
        pools.append(("month_and_weekday",[d for d in eligible if d["month"]==config.date.month and d["weekday"]==weekday]))
    pools.extend([("weekday",[d for d in eligible if d["weekday"]==weekday]),
        ("weekday_or_weekend",[d for d in eligible if (d["weekday"] in {1,7})==weekend])])
    vehicles = []
    for ev, target in zip(evs,targets):
        diary, match = None, "stationary_scenario"
        if target > 0:
            candidates = []
            for label, pool in pools:
                if not pool:
                    continue
                nearest = min(abs(d["total_km"]-target) for d in pool)
                candidates.append((nearest,label,pool))
                if nearest <= config.match_tolerance_km:
                    break
            if not candidates:
                raise ValueError("No suitable NHTS diaries within the distance bounds and weekday/weekend category")
            # Prefer the most specific calendar pool that meets distance tolerance.
            nearest, match, pool = candidates[-1] if candidates[-1][0] <= config.match_tolerance_km else min(candidates,key=lambda c:c[0])
            limit = nearest+min(1.,config.match_tolerance_km)
            if nearest <= config.match_tolerance_km:
                limit = min(limit,config.match_tolerance_km)
            close = [d for d in pool if abs(d["total_km"]-target) <= limit+1e-10]
            diary = rng(config.seed,"travel-diary|"+ev["id"]).choices(close,weights=[d["weight"] for d in close],k=1)[0]
        trips = [dict(t) for t in diary["trips"]] if diary else []
        periods = segments(trips)
        vehicles.append({"vehicle_id":ev["id"],"household_id":ev["household_id"],"home_bus":ev["bus"],
            "target_km":target,"distance_km":sum(t["distance_km"] for t in trips),
            "diary_id":diary["id"] if diary else None,
            "survey_yyyymm":diary["survey_yyyymm"] if diary else None,
            "survey_weekday":diary["weekday"] if diary else None,"calendar_match":match,
            "first_departure_minute":trips[0]["depart_minute"] if trips else None,
            "last_return_minute":trips[-1]["arrive_minute"] if trips else None,
            "home_minutes":sum(p["end_minute"]-p["start_minute"] for p in periods if p["state"]=="home"),
            "driving_minutes":sum(p["end_minute"]-p["start_minute"] for p in periods if p["state"]=="driving"),
            "parked_away_minutes":sum(p["end_minute"]-p["start_minute"] for p in periods if p["state"]=="parked_away"),
            "trips":trips,"segments":periods})
    # Store minute counts, not a 1440-element state array for every individual EV.
    differences = {s:np.zeros(1441,dtype=int) for s in ["home","driving","parked_away"]}
    for vehicle in vehicles:
        for segment in vehicle["segments"]:
            arr = differences[segment["state"]]
            arr[segment["start_minute"]] += 1
            arr[segment["end_minute"]] -= 1
    counts = {s:np.cumsum(v) for s,v in differences.items()}
    timeline = [{"minute":m, **{s:int(v[m]) if m<1440 else len(vehicles) if s=="home" else 0 for s,v in counts.items()}} for m in range(1441)]
    actual = [v["distance_km"] for v in vehicles]
    errors = [abs(v["target_km"]-v["distance_km"]) for v in vehicles]
    ids = [v["diary_id"] for v in vehicles if v["diary_id"]]
    summary = {"ev_count":len(vehicles),"moving_evs":len(ids),"stationary_evs":len(vehicles)-len(ids),
        "trip_count":sum(len(v["trips"]) for v in vehicles),"total_distance_km":sum(actual),
        "target":statistics(targets),"achieved":statistics(actual),
        "mean_absolute_match_error_km":float(np.mean(errors)) if errors else 0.,
        "max_match_error_km":max(errors,default=0.),
        "outside_match_tolerance":sum(e>config.match_tolerance_km for e in errors),
        "calendar_matches":dict(Counter(v["calendar_match"] for v in vehicles)),
        "unique_source_diaries":len(set(ids)),"diary_reuses":len(ids)-len(set(ids)),
        "all_return_home":all(not v["trips"] or v["trips"][-1]["destination_code"] in HOME for v in vehicles),
        "charging_added_to_grid":False}
    identity = travel_identity(fleet["id"], source, config)
    return {"id":digest(identity)[:16],"schema_version":1,**identity,"summary":summary,
        "calibration":calibration,"source":{k:source[k] for k in ["source_url","citation","source_scope","filtering","weighting","audit"]},
        "pool_counts":{name:len(pool) for name,pool in pools},"vehicles":vehicles,"timeline":timeline,
        "assumptions":{"scope":"EVs only; existing fleet inventory unchanged",
            "distance_distribution":"Calibrated truncated normal plus explicit unused-day mass; mean and population SD include all EVs. Midpoint quantiles shuffled by seed; small fleets differ from population moments.",
            "diary_matching":"National vehicle-day donor sampling with replacement, using WTHHFIN within near-distance matches. Prefer month+weekday, then weekday, then weekday/weekend. Requested and achieved moments are reported separately.",
            "chronology":"Original local minute times and distances retained; no rescaling and no invented return legs. All EVs start and end at their own home.",
            "location":"Trip purposes are abstract destinations, possibly outside the small electrical model. No road routes or destination bus assignments; purpose continuity cannot establish exact source locations.",
            "calendar":"2018 simulation date borrows representative behaviour from 2022/23 survey diaries; no exact-date, holiday or weather matching.",
            "electrical_effect":"No charging, discharging, SOC, driving-energy or degradation calculation. Home state is location, not proof of being plugged in. Existing home-charger access assignments remain unchanged.",
            "time":"Local wall-clock minutes [0,1440]; half-open state intervals; 1440 is the next midnight."}}
