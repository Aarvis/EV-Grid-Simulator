"""Deterministic household vehicle inventory; no electrical injections or SOC model."""
from collections import Counter, defaultdict
import hashlib
import json
import math
import random
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

OWNERSHIP = [0.082, 0.401, 0.342, 0.12, 0.04, 0.015]
BATTERIES = [
    {"id":"small", "name":"Small / short-range", "share":0.05, "battery_kwh":35.0, "level2_kw":3.84, "onboard_ac_kw":3.84},
    {"id":"compact", "name":"Compact / standard-range", "share":0.20, "battery_kwh":55.0, "level2_kw":7.68, "onboard_ac_kw":7.68},
    {"id":"long_range", "name":"Long-range / family", "share":0.60, "battery_kwh":77.5, "level2_kw":9.60, "onboard_ac_kw":9.60},
    {"id":"large", "name":"Large SUV / pickup", "share":0.15, "battery_kwh":110.0, "level2_kw":11.52, "onboard_ac_kw":11.52},
]

class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    seed: int = Field(default=42, ge=0, le=4294967295)
    mode: Literal["adoption", "fixed"] = "adoption"
    ev_adoption_pct: float = Field(default=10.0, ge=0, le=100)
    fixed_ev_count: int = Field(default=500, ge=0, le=100000)
    home_access_pct: float = Field(default=75.0, ge=0, le=100)
    level1_share_pct: float = Field(default=20.0, ge=0, le=100)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def rng(seed, label):
    # Independent streams keep ownership and EV identities stable when charger settings change.
    return random.Random(int(hashlib.sha256(f"fleet-v1|{seed}|{label}".encode()).hexdigest(),16))


def apportion(total, weights):
    if total < 0 or not math.isclose(sum(weights),1.0,abs_tol=1e-9):
        raise ValueError("Invalid allocation weights")
    raw=[total*w for w in weights]
    counts=[math.floor(v) for v in raw]
    ranked=sorted(range(len(weights)),key=lambda i:(-(raw[i]-counts[i]),i))
    for i in ranked[:total-sum(counts)]:
        counts[i]+=1
    return counts


def service_registry(network):
    """Only merge explicitly matched _1/_2 halves; retain their original IDs."""
    by_name={load["id"]:load for load in network["loads"]}
    if len(by_name)!=len(network["loads"]):
        raise ValueError("Duplicate DSS load IDs")
    visited=set()
    services=[]
    split_pairs=0
    for name in sorted(by_name):
        if name in visited:
            continue
        item=by_name[name]
        match=re.fullmatch(r"(.+)_([12])",name)
        pair=[]
        if match:
            first,second=match[1]+"_1",match[1]+"_2"
            if first not in by_name or second not in by_name:
                raise ValueError(f"Unpaired split-phase load: {name}")
            pair=[by_name[first],by_name[second]]
            a,b=pair
            same=all(a[key]==b[key] for key in ("bus","profile","profile_class"))
            same=same and all(math.isclose(a[key],b[key],rel_tol=1e-9,abs_tol=1e-8) for key in ("rated_kw","rated_kvar"))
            if not same:
                raise ValueError(f"Inconsistent split-phase pair: {match[1]}")
            if any(p.get("phases")!=1 or not math.isclose(p.get("connection_kv",0),.12,abs_tol=.001) or p.get("is_delta") for p in pair):
                raise ValueError(f"Unverified 120/240 V split-phase pair: {match[1]}")
            terminal_nodes={tuple(p.get("terminal_bus","").split('.')[1:]) for p in pair}
            if terminal_nodes not in ({('1','0'),('2','0')}, {('1',),('2',)}):
                raise ValueError(f"Unexpected split-phase terminals: {match[1]} {terminal_nodes}")
            source_id=match[1]
            split_pairs+=1
        else:
            pair=[item]
            source_id=name
        visited.update(p["id"] for p in pair)
        residential=item["profile_class"]=="residential"
        eligible=residential and len(pair)==2
        reason=None if eligible else "480 V or other non-split residential service requires dwelling/charger mapping" if residential else "Non-residential demand profile"
        services.append({"id":source_id,"bus":item["bus"],"load_ids":[p["id"] for p in pair],
                         "profile":item["profile"],"profile_class":item["profile_class"],
                         "rated_kw":sum(p["rated_kw"] for p in pair),"split_phase":len(pair)==2,
                         "service_voltage_ll_kv":.24 if len(pair)==2 else item.get("connection_kv"),
                         "eligible_household":eligible,"exclusion_reason":reason})
    if len(services)!=network["customer_count_metadata"]:
        raise ValueError(f"Customer reconciliation failed: {len(services)} services vs {network['customer_count_metadata']} metadata customers")
    households=[s for s in services if s["eligible_household"]]
    unresolved=[s for s in services if s["profile_class"]=="residential" and not s["eligible_household"]]
    summary={"unique_services":len(services),"dss_load_objects":len(by_name),"split_phase_pairs":split_pairs,
             "residential_profile_services":sum(s["profile_class"]=="residential" for s in services),
             "commercial_profile_services":sum(s["profile_class"]=="commercial profile" for s in services),
             "eligible_household_services":len(households),"residential_services_for_review":len(unresolved),
             "policy":"One household proxy per residential-profile 120/240 V split-phase service. 480 V residential-profile services excluded pending dwelling mapping."}
    return {"digest":digest(services),"summary":summary,"services":services,"households":households,"needs_review":unresolved}


def build_fleet(registry, config):
    config=config if isinstance(config,FleetConfig) else FleetConfig.model_validate(config)
    houses=registry["households"]
    counts=apportion(len(houses),OWNERSHIP)
    ownership=[number for number,count in enumerate(counts) for _ in range(count)]
    rng(config.seed,"ownership").shuffle(ownership)
    households=[]
    vehicles=[]
    for home,number in zip(houses,ownership):
        ids=[f"{home['id']}::vehicle-{i+1}" for i in range(number)]
        households.append({"id":home["id"],"bus":home["bus"],"load_ids":home["load_ids"],
                           "vehicle_ids":ids,"vehicle_count":number,"ev_ids":[],"ev_count":0,
                           "battery_kwh":0.,"home_access_battery_kwh":0.,"potential_home_charging_kw":0.})
        vehicles.extend({"id":vehicle_id,"household_id":home["id"],"bus":home["bus"],"is_ev":False} for vehicle_id in ids)
    n=len(vehicles)
    ev_count=math.floor(n*config.ev_adoption_pct/100+.5) if config.mode=="adoption" else config.fixed_ev_count
    if ev_count>n:
        raise ValueError(f"Requested {ev_count} EVs but only {n} vehicle slots exist in {len(houses)} eligible household services")
    # A single shuffled ranking produces nested EV populations for adoption sweeps.
    ranking=list(range(n))
    rng(config.seed,"adoption").shuffle(ranking)
    selected=set(ranking[:ev_count])
    by_home={h["id"]:h for h in households}
    for i,vehicle in enumerate(vehicles):
        if i not in selected:
            continue
        battery=rng(config.seed,"battery|"+vehicle["id"]).choices(BATTERIES,weights=[b["share"] for b in BATTERIES],k=1)[0]
        home_access=rng(config.seed,"access|"+vehicle["id"]).random()<config.home_access_pct/100
        level1=rng(config.seed,"charger|"+vehicle["id"]).random()<config.level1_share_pct/100
        charger="level1" if home_access and level1 else "level2" if home_access else "none"
        installed_kw=1.44 if charger=="level1" else battery["level2_kw"] if charger=="level2" else 0.
        effective=min(installed_kw,battery["onboard_ac_kw"])
        vehicle.update(is_ev=True,battery_category=battery["id"],battery_kwh=battery["battery_kwh"],
                       onboard_ac_kw=battery["onboard_ac_kw"],home_access=home_access,charger_level=charger,
                       installed_charger_kw=installed_kw,potential_home_charging_kw=effective,
                       charger_voltage_v=120 if charger=="level1" else 240 if charger=="level2" else None,
                       v2g_capable=False,soc=None,available_energy_kwh=None)
        h=by_home[vehicle["household_id"]]
        h["ev_ids"].append(vehicle["id"])
        h["ev_count"]+=1
        h["battery_kwh"]+=vehicle["battery_kwh"]
        h["potential_home_charging_kw"]+=effective
        if home_access:
            h["home_access_battery_kwh"]+=vehicle["battery_kwh"]
    evs=[v for v in vehicles if v["is_ev"]]
    categories=[]
    for b in BATTERIES:
        group=[v for v in evs if v["battery_category"]==b["id"]]
        categories.append({**b,"count":len(group),"realized_share_pct":100*len(group)/ev_count if ev_count else 0.,
                           "total_battery_kwh":sum(v["battery_kwh"] for v in group),
                           "potential_home_charging_kw":round(sum(v["potential_home_charging_kw"] for v in group),6)})
    summary={"household_services":len(houses),"all_vehicles":n,"ev_count":ev_count,"ice_count":n-ev_count,
             "actual_ev_adoption_pct":100*ev_count/n if n else 0.,"average_vehicles_per_household":n/len(houses) if houses else 0.,
             "households_with_evs":sum(h["ev_count"]>0 for h in households),"ownership_counts":counts,
             "ev_counts_per_household":{str(i):sum(h["ev_count"]==i for h in households) for i in range(6)},
             "nominal_battery_kwh":sum(v["battery_kwh"] for v in evs),
             "home_access_nominal_battery_kwh":sum(v["battery_kwh"] for v in evs if v["home_access"]),
             "potential_home_charging_kw":round(sum(v["potential_home_charging_kw"] for v in evs),6),
             "home_access_count":sum(v["home_access"] for v in evs),
             "level1_count":sum(v["charger_level"]=="level1" for v in evs),
             "level2_count":sum(v["charger_level"]=="level2" for v in evs),
             "no_home_charging_count":sum(not v["home_access"] for v in evs),
             "available_storage_kwh":None,"charging_added_to_grid":False}
    identity={"algorithm":"fleet-v1","registry_digest":registry["digest"],"config":config.model_dump()}
    return {"id":digest(identity)[:16],"schema_version":1,**identity,"summary":summary,
            "registry_summary":registry["summary"],"categories":categories,"households":households,"vehicles":vehicles,
            "assumptions":{"vehicle_ownership_probabilities":OWNERSHIP,"battery_classes":BATTERIES,
                "ownership_allocation":"Largest remainder counts, seeded permutation across household services",
                "ev_selection":"Exact rounded count, uniform sampling of vehicle slots without replacement",
                "equipment_assignment":"Independent seeded categorical battery draw and home-access / L1 draws; realized shares vary",
                "onboard_charger_limits":"Provisional category-specific limits equal to selected L2 ratings",
                "household_identity":"Eligible electrical services are household proxies, not verified individual dwellings",
                "battery_capacity":"Nominal inventory; SOC, mobility reserves and plugged-in availability are not assigned",
                "charging_power":"Sum of AC nameplates limited by assumed onboard chargers, not grid-hosting capacity",
                "evidence":"Ownership split, EV share, battery mix and charging mix are user-selected scenario settings; not measured Greensboro EV data",
                "electrical_effect":"Inventory only; no EV loads, storage injections or trading added to OpenDSS"}}