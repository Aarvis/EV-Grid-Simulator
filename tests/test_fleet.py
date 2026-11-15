"""Fleet allocation invariants and API persistence against the real service registry."""
import copy
import json
import pytest
from fastapi.testclient import TestClient
from gridlab import engine, fleet, server
from gridlab.config import MODEL


@pytest.fixture(scope="module")
def registry():
    if not MODEL.exists():
        pytest.skip("Run python -m gridlab.download first")
    d=engine.context()
    engine.solve(d,1,0)
    network,_=engine.topology(d)
    return fleet.service_registry(network)


def test_service_identity_reconciles_all_customers_and_excludes_480v(registry):
    s=registry["summary"]
    assert (s["unique_services"],s["dss_load_objects"],s["split_phase_pairs"])==(732,1329,597)
    assert (s["residential_profile_services"],s["commercial_profile_services"])==(458,274)
    assert len(registry["households"])==447
    assert len(registry["needs_review"])==11
    load_ids=[name for service in registry["services"] for name in service["load_ids"]]
    assert len(load_ids)==len(set(load_ids))==1329
    assert all(len(h["load_ids"])==2 and h["service_voltage_ll_kv"]==.24 for h in registry["households"])
    assert all(h["service_voltage_ll_kv"]==.48 for h in registry["needs_review"])
    assert not ({h["id"] for h in registry["households"]}&{h["id"] for h in registry["needs_review"]})


def test_default_population_reproducible_with_exact_vehicle_caps(registry):
    a=fleet.build_fleet(registry,{})
    b=fleet.build_fleet(registry,{})
    assert a==b
    assert a["summary"]["all_vehicles"]==751
    assert a["summary"]["ev_count"]==75
    assert a["summary"]["ownership_counts"]==[37,179,153,53,18,7]
    assert a["summary"]["nominal_battery_kwh"]==5695
    assert a["summary"]["potential_home_charging_kw"]==pytest.approx(398.4)
    assert sum(a["summary"]["ownership_counts"])==447
    assert sum(a["summary"]["ev_counts_per_household"].values())==447
    vehicles={v["id"]:v for v in a["vehicles"]}
    assert len(vehicles)==751
    for home in a["households"]:
        assert 0<=home["ev_count"]<=home["vehicle_count"]<=5
        assert len(home["vehicle_ids"])==home["vehicle_count"]
        assert set(home["ev_ids"])<=set(home["vehicle_ids"])
        assert all(vehicles[name]["household_id"]==home["id"] for name in home["vehicle_ids"])
        assert home["battery_kwh"]==sum(vehicles[name]["battery_kwh"] for name in home["ev_ids"])
    changed=fleet.build_fleet(registry,{"seed":43})
    assert a["id"]!=changed["id"]
    assert [h["vehicle_count"] for h in a["households"]]!=[h["vehicle_count"] for h in changed["households"]]
    assert a["summary"]["ownership_counts"]==changed["summary"]["ownership_counts"]


@pytest.mark.parametrize("percentage,expected",[(0,0),(10,75),(20,150),(100,751)])
def test_adoption_count_and_nested_population(registry,percentage,expected):
    result=fleet.build_fleet(registry,{"ev_adoption_pct":percentage})
    assert result["summary"]["ev_count"]==expected
    default=fleet.build_fleet(registry,{})
    if percentage>=10:
        old={v["id"]:v for v in default["vehicles"] if v["is_ev"]}
        new={v["id"]:v for v in result["vehicles"] if v["is_ev"]}
        assert set(old)<=set(new)
        assert all(old[name]==new[name] for name in old)
    assert result["summary"]["available_storage_kwh"] is None
    assert result["summary"]["charging_added_to_grid"] is False


def test_fixed_count_and_oversubscribed_fleet(registry):
    result=fleet.build_fleet(registry,{"mode":"fixed","fixed_ev_count":500})
    assert result["summary"]["ev_count"]==500
    assert result["summary"]["actual_ev_adoption_pct"]==pytest.approx(100*500/751)
    with pytest.raises(ValueError,match="only 751 vehicle slots"):
        fleet.build_fleet(registry,{"mode":"fixed","fixed_ev_count":752})


@pytest.mark.parametrize("access,level1",[(0,20),(100,100),(100,0),(75,20)])
def test_charger_power_and_storage_accounting(registry,access,level1):
    result=fleet.build_fleet(registry,{"home_access_pct":access,"level1_share_pct":level1})
    evs=[v for v in result["vehicles"] if v["is_ev"]]
    s=result["summary"]
    assert s["nominal_battery_kwh"]==sum(v["battery_kwh"] for v in evs)
    assert s["nominal_battery_kwh"]==sum(c["total_battery_kwh"] for c in result["categories"])
    assert s["potential_home_charging_kw"]==pytest.approx(sum(v["potential_home_charging_kw"] for v in evs))
    assert s["level1_count"]+s["level2_count"]+s["no_home_charging_count"]==len(evs)
    for v in evs:
        assert v["potential_home_charging_kw"]==min(v["installed_charger_kw"],v["onboard_ac_kw"])
        assert v["battery_kwh"] in {35,55,77.5,110}
        assert v["v2g_capable"] is False and v["soc"] is None
        if not v["home_access"]:
            assert v["installed_charger_kw"]==0
    if access==0:
        assert s["potential_home_charging_kw"]==0 and s["home_access_nominal_battery_kwh"]==0
    elif level1==100:
        assert s["level1_count"]==75 and s["potential_home_charging_kw"]==pytest.approx(75*1.44)
    elif level1==0:
        assert s["level2_count"]==75


def test_api_validation_export_and_restart_restore(registry,monkeypatch,tmp_path):
    state={**server.STATE,"ready":True,"service_registry":None,"fleets":{},"current_fleet":None}
    monkeypatch.setattr(server,"STATE",state)
    monkeypatch.setattr(server,"FLEET_DIR",tmp_path)
    monkeypatch.setattr(server,"LOG",tmp_path/"server.jsonl")
    server.initialize_fleets(registry)
    client=TestClient(server.app)
    default=client.get('/api/fleet').json()
    assert default["summary"]["ev_count"]==75
    assert client.get('/api/households').json()["summary"]["eligible_household_services"]==447
    result=client.post('/api/fleets',json={"mode":"fixed","fixed_ev_count":500})
    assert result.status_code==201
    selected=result.json()
    assert selected["summary"]["ev_count"]==500
    assert client.post('/api/fleets',json={"mode":"fixed","fixed_ev_count":752}).status_code==422
    assert client.post('/api/fleets',json={"ev_adoption_pct":101}).status_code==422
    assert client.post('/api/fleets',json={"seed":-1}).status_code==422
    assert client.post('/api/fleets',json={"mode":"something"}).status_code==422
    assert client.post('/api/fleets',json={"unknown":1}).status_code==422
    with pytest.raises(ValueError):
        fleet.FleetConfig(ev_adoption_pct=float("inf"))
    assert client.get('/api/fleets/does-not-exist').status_code==404
    exported=client.get('/api/fleets/'+selected["id"]+'/export')
    assert exported.status_code==200 and exported.json()==selected
    assert client.get('/api/fleet').json()["id"]==selected["id"]
    state["fleets"]={}
    state["current_fleet"]=None
    server.initialize_fleets(registry)
    assert client.get('/api/fleet').json()==selected
    chosen=client.post('/api/fleets/'+default["id"]+'/select')
    assert chosen.status_code==200 and chosen.json()==default
    assert client.get('/api/fleet').json()==default
    assert len(client.get('/api/fleets').json())==2