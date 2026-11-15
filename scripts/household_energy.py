"""Household profile averages plus an explicitly assumed daily EV energy budget."""
import json
from urllib.request import urlopen
import numpy as np
from gridlab.config import DATA,PREFIX,RUNS
from gridlab.fleet import FleetConfig,build_fleet
registry=json.load(urlopen('http://localhost:8765/api/households'))
current=json.load(urlopen('http://localhost:8765/api/fleet'))
config={**current['config'],'mode':'adoption','ev_adoption_pct':30.0}
fleet=build_fleet(registry,FleetConfig.model_validate(config))
cache={}
house_energy={}
for house in registry['households']:
    shape=house['profile']
    if shape not in cache:
        cache[shape]=float(np.loadtxt(DATA/PREFIX/'profiles'/(shape+'.csv'),delimiter=',').sum()*.25)
    house_energy[house['id']]=house['rated_kw']*cache[shape]/365
n=len(house_energy)
base_daily=sum(house_energy.values())/n
# These are illustrative assumptions, not measured vehicle behaviour or a charging simulation.
km_per_day=30.
kwh_per_km=.18
charge_efficiency=.90
per_ev_grid_kwh=km_per_day*kwh_per_km/charge_efficiency
evs=[v for v in fleet['vehicles'] if v['is_ev']]
home_evs=[v for v in evs if v['home_access']]
ev_home_ids={v['household_id'] for v in home_evs}
non_ev_home_ids={h['id'] for h in fleet['households'] if h['ev_count']==0}
result={'eligible_households':n,'config':config,'fleet_summary':fleet['summary'],
        'baseline':{'mean_kwh_per_household_day':base_daily,'mean_kw_per_household':base_daily/24,
                    'method':'2018 residential split-phase profile integration; requested customer energy; excludes network losses'},
        'ev_assumptions':{'km_per_ev_day':km_per_day,'battery_kwh_per_km':kwh_per_km,'charging_efficiency':charge_efficiency,
                          'grid_kwh_per_ev_day':per_ev_grid_kwh,'home_energy_fraction_for_home_access_evs':1.0,
                          'note':'Home-access EVs assumed to replenish all daily driving energy at home; no-home-access EVs charge outside this modeled household network.'},
        'with_30pct_adoption':{'home_charging_evs':len(home_evs),'households_with_home_charging_evs':len(ev_home_ids),
                              'added_home_charging_kwh_day':len(home_evs)*per_ev_grid_kwh,
                              'added_kwh_per_household_day':len(home_evs)*per_ev_grid_kwh/n,
                              'mean_kwh_per_household_day':base_daily+len(home_evs)*per_ev_grid_kwh/n,
                              'mean_kw_per_household':(base_daily+len(home_evs)*per_ev_grid_kwh/n)/24,
                              'increase_pct':(len(home_evs)*per_ev_grid_kwh/n)/base_daily*100,
                              'if_all_evs_charged_at_home_mean_kwh_per_household_day':base_daily+len(evs)*per_ev_grid_kwh/n,
                              'home_charging_households_baseline_kwh_day':sum(house_energy[h] for h in ev_home_ids)/len(ev_home_ids),
                              'home_charging_households_with_ev_kwh_day':(sum(house_energy[h] for h in ev_home_ids)+len(home_evs)*per_ev_grid_kwh)/len(ev_home_ids),
                              'zero_ev_households_baseline_kwh_day':sum(house_energy[h] for h in non_ev_home_ids)/len(non_ev_home_ids)}}
(RUNS/'household-energy-30pct.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))