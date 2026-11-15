"""Mobility conservation, donor integrity, distance moments and persistent API scenarios."""
import copy
import csv
import io
import json
import math
from pathlib import Path
import zipfile
import pytest
from fastapi.testclient import TestClient
from scipy.stats import truncnorm
from gridlab import fleet, nhts, server, travel


@pytest.fixture(scope="module")
def source():
    if not (nhts.FOLDER/"csv.zip").exists():
        pytest.skip("Prepare official data with python -m gridlab.nhts")
    return nhts.prepare()


def inventory(count=150):
    return {"id":"f"*16,"vehicles":[{"id":f"ev-{i:04d}","household_id":f"house-{i//2}",
        "bus":f"bus-{i//2}","is_ev":True} for i in range(count)]}


@pytest.mark.parametrize("settings",[{}, {"mean_km":35,"std_km":15},
    {"stationary_pct":10}, {"mean_km":70,"std_km":16,"min_km":10,"max_km":140}])
def test_post_truncation_moments_include_unused_days(settings):
    c=travel.TravelConfig(**settings)
    p=travel.calibrate(c)
    loc,scale=p["location_km"],p["scale_km"]
    m,v=truncnorm.stats((c.min_km-loc)/scale,(c.max_km-loc)/scale,loc=loc,scale=scale,moments="mv")
    moving=1-c.stationary_pct/100
    assert moving*m==pytest.approx(c.mean_km,abs=.0002)
    assert math.sqrt(moving*(v+m*m)-(moving*m)**2)==pytest.approx(c.std_km,abs=.0002)
    targets,_=travel.distance_targets(1000,c)
    assert sum(v==0 for v in targets)==round(c.stationary_pct*10)
    assert travel.statistics(targets)["mean_km"]==pytest.approx(c.mean_km,abs=.03)
    assert travel.statistics(targets)["std_km"]==pytest.approx(c.std_km,abs=.06)


@pytest.mark.parametrize("settings",[{"mean_km":0,"std_km":1},{"max_km":10},
    {"min_km":120},{"stationary_pct":100},{"stationary_pct":90},
    {"std_km":60},{"date":"2022-11-21"},{"std_km":float('nan')}])
def test_invalid_and_infeasible_requests_rejected(settings):
    with pytest.raises(ValueError):
        travel.TravelConfig(**settings)


def test_schedules_preserve_donors_and_conserve_all_minutes(source):
    f=inventory();original=copy.deepcopy(f)
    day=travel.build_travel(f,source,{})
    assert f==original
    assert day==travel.build_travel(f,source,{})
    donors={d["id"]:d for d in source["diaries"]}
    assert day["summary"]["all_return_home"]
    assert day["summary"]["ev_count"]==150
    for v in day["vehicles"]:
        assert v["trips"]==donors[v["diary_id"]]["trips"]
        assert v["trips"][0]["origin_code"] in nhts.HOME
        assert v["trips"][-1]["destination_code"] in nhts.HOME
        assert v["distance_km"]==pytest.approx(sum(t["distance_km"] for t in v["trips"]))
        assert v["home_minutes"]+v["driving_minutes"]+v["parked_away_minutes"]==1440
        assert v["segments"][0]["start_minute"]==0 and v["segments"][-1]["end_minute"]==1440
        assert all(a["end_minute"]==b["start_minute"] for a,b in zip(v["segments"],v["segments"][1:]))
        for trip in v["trips"]:
            assert travel.vehicle_state(v,trip["depart_minute"])["state"]=="driving"
            assert travel.vehicle_state(v,trip["arrive_minute"])["state"] in {"home","parked_away","driving"}
        if v["calendar_match"]=="month_and_weekday":
            assert v["survey_yyyymm"][-2:]=="11" and v["survey_weekday"]==4
    for minute in [0,1,480,720,1020,1439,1440]:
        states=[travel.vehicle_state(v,minute)["state"] for v in day["vehicles"]]
        row=day["timeline"][minute]
        assert {s:states.count(s) for s in ["home","driving","parked_away"]}=={k:v for k,v in row.items() if k!="minute"}
    assert all(r["home"]+r["driving"]+r["parked_away"]==150 for r in day["timeline"])
    assert day["timeline"][-1]=={"minute":1440,"home":150,"driving":0,"parked_away":0}
    assert day["summary"]["charging_added_to_grid"] is False
    changed=travel.build_travel(f,source,{"seed":43})
    assert changed["id"]!=day["id"] and changed["vehicles"]!=day["vehicles"]


def test_unused_empty_and_weekend_fleets(source):
    unused=travel.build_travel(inventory(),source,{"stationary_pct":10})
    assert unused["summary"]["stationary_evs"]==15
    assert all(v["home_minutes"]==1440 for v in unused["vehicles"] if not v["trips"])
    empty=travel.build_travel(inventory(0),source,{})
    assert empty["summary"]["ev_count"]==0 and len(empty["timeline"])==1441
    allhome=travel.build_travel(inventory(),source,{"mean_km":0,"std_km":0,"stationary_pct":100})
    assert allhome["summary"]["stationary_evs"]==150
    weekend=travel.build_travel(inventory(),source,{"date":"2018-11-24","prefer_month":False})
    assert all(v["survey_weekday"] in {1,7} for v in weekend["vehicles"])


def test_parser_rejects_whole_bad_vehicle_day_and_deduplicates_passengers(tmp_path):
    household={"HOUSEID":"1","FLAG100":"1","TDAYDATE":"202211","TRAVDAY":"04"}
    def leg(vehicle,start,end,origin,destination,seq,**kw):
        return {"HOUSEID":"1","VEHID":str(vehicle),"DRVR_FLG":"01","PERSONID":"1",
            "SEQ_TRIPID":str(seq),"TRIPID":str(seq),"TRPHHVEH":"01","TRPTRANS":"01",
            "STRTTIME":str(start),"ENDTIME":str(end),"TRPMILES":"5","TRVLCMIN":"30",
            "WHYFROM":str(origin),"WHYTO":str(destination),"TDAYDATE":"202211","TRAVDAY":"04",**kw}
    rows=[]
    for i in [1,2,3]:
        rows += [leg(i,800,830,1,3,1),leg(i,1700,1730,3,1,2)]
    rows.append(leg(1,800,830,1,3,1,DRVR_FLG="02",PERSONID="2"))
    rows.append(leg(2,900,930,3,3,3,TRPMILES="-9"))
    rows[5]["ENDTIME"]="1650" # Invalid return leg must not be dropped.
    archive=tmp_path/"fixture.zip"
    with zipfile.ZipFile(archive,"w") as z:
        def write(name,records):
            stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records);z.writestr(name,stream.getvalue())
        write('hhv2pub.csv',[household])
        write('vehv2pub.csv',[{"HOUSEID":"1","VEHID":str(i),"VEHTYPE":"01","WTHHFIN":"10"} for i in [1,2,3]])
        write('tripv2pub.csv',rows)
    parsed=nhts.parse_archive(archive)
    assert len(parsed["diaries"])==1
    assert len(parsed["diaries"][0]["trips"])==2
    assert parsed["audit"]["driver_vehicle_days"]==3


def test_api_persistence_fleet_binding_and_exports(source,monkeypatch,tmp_path):
    f=inventory(8)
    other={**inventory(0),"id":"e"*16}
    state={**server.STATE,"ready":True,"service_registry":{},"fleets":{f["id"]:f,other["id"]:other},"current_fleet":f["id"]}
    monkeypatch.setattr(server,"STATE",state)
    monkeypatch.setattr(server,"TRAVEL_STATE",{"ready":True,"preparing":False,"error":None,"source":source})
    monkeypatch.setattr(server,"TRAVEL_DIR",tmp_path)
    monkeypatch.setattr(server,"LOG",tmp_path/"events.jsonl")
    client=TestClient(server.app)
    assert client.get('/api/travel').json()=={"scenario":None}
    assert client.get('/api/travel/status').json()["ready"]
    result=client.post('/api/travel/scenarios',json={"fleet_id":f["id"]})
    assert result.status_code==201,result.text
    item=result.json();base='/api/travel/scenarios/'+item["id"]
    assert client.get('/api/travel').json()["scenario"]==item
    assert client.post('/api/travel/scenarios',json={"fleet_id":f["id"]}).json()==item
    assert client.get(base+'/export').json()==item
    assert client.get(base+'/export').headers['cache-control']=='no-store'
    trips=list(csv.DictReader(io.StringIO(client.get(base+'/export?format=trips').text)))
    states=list(csv.DictReader(io.StringIO(client.get(base+'/export?format=states').text)))
    assert len(trips)==item['summary']['trip_count']
    assert sum(int(s['end_minute'])-int(s['start_minute']) for s in states)==8*1440
    assert client.get(base+'/export?format=wrong').status_code==422
    assert client.get('/api/travel/scenarios/does-not-exist').status_code==404
    assert client.post('/api/travel/scenarios',json={"mean_km":0}).status_code==422
    assert client.post('/api/travel/scenarios',json={"nonsense":1}).status_code==422
    state['current_fleet']=other['id']
    assert client.get('/api/travel').json()=={"scenario":None}
    assert client.get('/api/travel/scenarios').json()==[]
    # Existing scenarios remain bound to their own inventory when selected.
    assert client.post(base+'/select').status_code==200
    assert client.get('/api/travel').json()=={"scenario":None}
    state['current_fleet']=f['id']
    assert client.get('/api/travel').json()["scenario"]==item
    assert len(client.get('/api/travel/scenarios').json())==1
