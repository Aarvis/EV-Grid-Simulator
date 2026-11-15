"""Audit the annual peak at the selected substation's existing 69 kV boundary."""
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
import numpy as np
from gridlab import engine
from gridlab.config import DATA, PREFIX, ROOT


def main():
    out=ROOT/'runs'/'annual-peak-2018'
    out.mkdir(exist_ok=True)
    metadata=list(csv.DictReader((DATA/PREFIX/'scenarios/base_timeseries/metrics.csv').open()))
    feeders=[r for r in metadata if r['Substation Name']=='uhs19_1247']
    planning_kw=sum(float(r['Total Peak Planning Load (MW)'])*1000 for r in feeders)
    print(json.dumps({'planning_peak_mw':planning_kw/1000,'feeders':[r['Feeder Name'] for r in feeders]}),flush=True)
    started=time.monotonic()
    d=engine.context()
    grouped={}
    for name in d.Loads.AllNames():
        d.Loads.Name(name)
        shape=d.Loads.Yearly()
        grouped[shape]=grouped.get(shape,0)+d.Loads.kW()
    requested=np.zeros(35040)
    for shape,kw in grouped.items():
        values=np.loadtxt(DATA/PREFIX/'profiles'/(shape+'.csv'),delimiter=',')
        requested+=kw*values
    demand_index=int(np.argmax(requested))
    print(json.dumps({'profile_peak_requested_mw':float(requested[demand_index])/1000,
                      'interval_end':(datetime(2018,1,1)+timedelta(minutes=15*(demand_index+1))).isoformat()}),flush=True)
    max_supply=(-float('inf'),None)
    failures=[]
    peak=None
    with (out/'annual_supply.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['interval','timestamp','requested_load_kw','supply_kw','network_loss_kw','converged','controls_settled'])
        writer.writeheader()
        for index in range(35040):
            d.Solution.DblHour((index+1)*.25)
            d.Solution.SolveSnap()
            converged=bool(d.Solution.Converged())
            settled=bool(d.Solution.ControlActionsDone())
            supply=-float(d.Circuit.TotalPower()[0])
            losses=float(d.Circuit.Losses()[0])/1000
            timestamp=(datetime(2018,1,1)+timedelta(minutes=15*(index+1))).isoformat(timespec='minutes')
            writer.writerow({'interval':index,'timestamp':timestamp,'requested_load_kw':requested[index],
                             'supply_kw':supply,'network_loss_kw':losses,'converged':converged,'controls_settled':settled})
            if not converged or not settled:
                failures.append({'interval':index,'timestamp':timestamp,'converged':converged,'controls_settled':settled})
            elif supply>max_supply[0]:
                max_supply=(supply,index)
                actual_load=0.
                for name in d.Loads.AllNames():
                    d.Loads.Name(name)
                    actual_load+=d.CktElement.TotalPowers()[0]
                peak={'interval':index,'interval_end':timestamp,'supply_mw':supply/1000,
                      'actual_customer_load_mw':actual_load/1000,'requested_customer_load_mw':float(requested[index])/1000,
                      'network_losses_mw':losses/1000,'balance_residual_kw':supply-actual_load-losses}
            if (index+1)%1000==0 or index==35039:
                print(json.dumps({'completed':index+1,'total':35040,'elapsed_s':round(time.monotonic()-started,1),
                                  'max_supply_mw':max_supply[0]/1000,'failed_intervals':len(failures)}),flush=True)
    result={'station':'uhs19_1247','year':2018,'interval_minutes':15,'intervals_checked':35040,
            'planning_peak_mw':planning_kw/1000,'peak_supply':peak,
            'profile_peak_requested_mw':float(requested[demand_index])/1000,
            'profile_peak_interval_end':(datetime(2018,1,1)+timedelta(minutes=15*(demand_index+1))).isoformat(),
            'failures':failures,'elapsed_s':round(time.monotonic()-started,2),
            'boundary':'69 kV external-grid input; includes modeled distribution losses, excludes upstream losses',
            'dataset':'SMART-DS v1.0 2018 base_timeseries; synthetic demand',
            'engine':d.Basic.Version()}
    (out/'summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':
    main()