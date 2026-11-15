'use strict';
let batteryRun=null, batteryVehicleId=null, batteryData=null, batteryCase=null, batteryRequest=0, batteryDay=null;
const batteryClasses={small:'Small / short-range',compact:'Compact / standard-range',
  long_range:'Long-range / family',large:'Large SUV / pickup'};

function renderBatteryTracker(){
  const summary=job?.battery_ledger_summary;
  const days=Object.keys(job?.daily_summaries||{});
  const available=(summary||days.length)&&destinationRun===job.id&&chargingVehicles.size>0;
  $('battery-tracker').hidden=!available;
  if(!available){batteryRun=null;batteryVehicleId=null;batteryData=null;return;}
  const dateSelect=$('battery-day');
  $('battery-day-field').hidden=!days.length;
  if(days.join(',')!==Array.from(dateSelect.options,o=>o.value).join(',')){
    const selected=batteryRun===job.id?dateSelect.value:days[0];
    dateSelect.replaceChildren(...days.map(date=>new Option(date,date)));
    if(days.includes(selected))dateSelect.value=selected;
  }
  if(batteryRun===job.id)return;
  batteryRun=job.id;
  $('battery-case-field').hidden=!job.market_summary;
  $('battery-case').value=job.market_summary?'trading':'charging';
  const select=$('battery-vehicle');
  $('battery-interval').value=0;
  select.replaceChildren(...[...chargingVehicles.values()].map(vehicle=>new Option(
    `${vehicle.home_bus} · ${vehicle.household_id} · ${vehicle.id.split('::').at(-1)}`,vehicle.id)));
  void loadBatteryVehicle();
  renderTravelVehicle();
  showSelection();
}

async function loadBatteryVehicle(){
  const id=$('battery-vehicle').value;
  const run=batteryRun, vehicle=chargingVehicles.get(id), scenario=$('battery-case').value, day=$('battery-day').value;
  if(!run||!vehicle)return;
  if(id===batteryVehicleId&&scenario===batteryCase&&day===batteryDay&&(batteryData||$('battery-loading').textContent==='Loading battery record…'))return;
  const request=++batteryRequest;
  batteryVehicleId=id;
  batteryCase=scenario;
  batteryDay=day;
  batteryData=null;
  $('battery-detail').hidden=true;
  $('battery-loading').textContent='Loading battery record…';
  const spec=`${batteryClasses[vehicle.battery_category]||vehicle.battery_category||'EV'} · ${fmt(vehicle.capacity_kwh,1)} kWh battery · ${fmt(vehicle.driving_kwh_per_km,3)} kWh/km · ${fmt(vehicle.rated_ac_kw,2)} kW charger · ${fmt(job.assumptions.ev_charging.charge_efficiency*100,2)}% charging efficiency`;
  $('battery-vehicle-spec').textContent=spec;
  if(day){
    const start=job.daily_summaries[day].opening[scenario], close=job.daily_summaries[day].closing[scenario];
    $('battery-fleet-summary').textContent=`${day} · First offer $${fmt(start.offer_per_mwh,2)}/MWh · Fleet stored value $${fmt(start.battery_value_usd,2)} at opening → $${fmt(close.battery_value_usd,2)} at close`;
  }else{
    const summary=job.battery_ledger_summary, previous=summary.previous_generation;
    const proxy=previous.profile_date===previous.date?'':` (using ${previous.profile_date} profiles)`;
    const close=scenario==='trading'?job.market_summary.ev_battery_end_value_usd:summary.closing_value_usd;
    $('battery-fleet-summary').textContent=`Opening price ${previous.date}${proxy} · $${fmt(previous.average_generation_cost_per_mwh,2)}/MWh · Fleet stored value $${fmt(summary.opening_value_usd,2)} at opening → $${fmt(close,2)} at close`;
  }
  try{
    const data=await api(`/api/runs/${run}/battery?scenario=${scenario}&vehicle_id=${encodeURIComponent(id)}${day?'&day='+day:''}`);
    if(run!==batteryRun||request!==batteryRequest)return;
    batteryData=data;
    $('battery-interval').max=data.intervals.length;
    if(scenario==='trading')$('battery-vehicle-spec').textContent=`${spec} · ${data.trader?'Trading EV':'Passive EV'}`;
    $('battery-loading').textContent='';
    $('battery-detail').hidden=false;
    const last=data.intervals.at(-1);
    const charged=data.intervals.reduce((total,row)=>total+row.charging_cost_usd,0);
    const driven=data.intervals.reduce((total,row)=>total+row.driving_cost_usd,0);
    const sold=data.intervals.reduce((total,row)=>total+(row.export_battery_cost_usd||0),0);
    const credits=data.intervals.reduce((total,row)=>total+(row.export_credit_usd||0),0);
    const exportValue=scenario==='trading'?` − sold battery value $${fmt(sold,2)}`:'';
    const bill=job.billing_summary?` · Net EV bill $${fmt(charged-credits,2)}${scenario==='trading'?` after $${fmt(credits,2)} export credits`:''}`:'';
    $('battery-day-cost').textContent=`Day totals · Opening $${fmt(data.opening.value_usd,2)} + charging $${fmt(charged,2)} − driving $${fmt(driven,2)}${exportValue} = closing $${fmt(last.value_usd,2)}${bill}`;
    renderBatteryTime();
  }catch(e){
    if(run===batteryRun&&request===batteryRequest)$('battery-loading').textContent=e.message;
  }
}

function openBatteryTracker(id){
  if($('battery-tracker').hidden||!chargingVehicles.has(id))return;
  $('battery-vehicle').value=id;
  void loadBatteryVehicle();
  $('battery-tracker').scrollIntoView({block:'start'});
}

function batteryTrackerButton(id){
  return job?.id===batteryRun&&chargingVehicles.has(id)
    ?`<p><button data-battery-ev="${escapeHTML(id)}">Track battery and cost</button></p>`:'';
}

function renderBatteryTime(){
  if(!batteryData)return;
  const vehicle=chargingVehicles.get(batteryVehicleId);
  const slider=$('battery-interval');
  const index=Number(slider.value), clock=travelClock(index*15);
  const point=index?batteryData.intervals[index-1]:batteryData.opening;
  const average=point.stored_kwh>0?point.value_usd/point.stored_kwh:null;
  $('battery-time').textContent=clock;
  slider.setAttribute('aria-valuetext',clock);
  $('battery-state').innerHTML=prop('Battery charge',`${fmt(100*point.stored_kwh/vehicle.capacity_kwh,1)}% · ${fmt(point.stored_kwh,2)} kWh`)
    +prop('Stored electricity value',`$${fmt(point.value_usd,3)}`)
    +prop('Average cost of stored electricity',average==null?'—':`$${fmt(average,4)}/kWh`)
    +prop('Driving cost at current battery price',average==null?'—':`$${fmt(average*vehicle.driving_kwh_per_km,4)}/km`);
  const driveCost=point.driving_cost_usd_per_km;
  const offset=batteryDay?job.series.findIndex(row=>row.simulation_date===batteryDay):0;
  const source=job.series[offset+index-1];
  const prefix=batteryCase==='trading'?'trading_':'';
  const traded=point.export_kwh>0?` · Exported ${fmt(point.export_kwh,3)} kWh · Credit $${fmt(point.export_credit_usd,4)}`:'';
  const price=point.locked_price_per_mwh??source?.[prefix+'locked_price_per_mwh']??source?.average_generation_cost_per_mwh;
  const decisions={buy:'Buy',sell:'Sell',wait:'Wait',curtailed:'Export curtailed',unplugged:'Unplugged'};
  const decision=point.decision?` · ${decisions[point.decision]}`:'';
  const target=point.protected_kwh==null?'':` · ${point.overnight_target?'Tomorrow’s':'Remaining'} driving ${fmt(point.planned_drive_kwh,2)} kWh · Protected ${fmt(point.protected_kwh,2)} kWh (${fmt(100*point.protected_kwh/vehicle.capacity_kwh,1)}%) · Estimated buy cost $${fmt(point.estimated_buy_cost_per_kwh,4)}/kWh stored`;
  $('battery-flow').textContent=index
    ?`Previous 15 min · Energy price $${fmt(price,2)}/MWh · Home draw ${fmt(point.home_draw_kwh,3)} kWh · Away draw ${fmt(point.away_draw_kwh,3)} kWh · Added to battery ${fmt(point.charged_kwh,3)} kWh · Allocated grid losses ${fmt(point.distribution_loss_kwh,4)} kWh · Charging $${fmt(point.charging_cost_usd,4)} · Driving ${fmt(point.driving_kwh,3)} kWh${driveCost==null?'':` at $${fmt(driveCost,4)}/km`}${point.unmet_kwh>0?` · Driving energy shortfall ${fmt(point.unmet_kwh,3)} kWh`:''}${traded}`
    +decision+target
    :batteryDay?'Opening charge and stored value carried from the preceding day.'
    :`Opening estimate · Charger draw ${fmt(point.estimated_charger_kwh,2)} kWh · Allocated grid losses ${fmt(point.estimated_distribution_loss_kwh,3)} kWh`;
  drawBatteryCharge();
}

function drawBatteryCharge(){
  if(!batteryData||$('battery-detail').hidden||$('battery-tracker').hidden)return;
  const vehicle=chargingVehicles.get(batteryVehicleId);
  const {ctx,w,h}=size($('battery-soc-chart'));
  const left=46, right=22, top=12, bottom=22, width=w-left-right, height=h-top-bottom;
  if(width<=0||height<=0)return;
  ctx.font='10px Segoe UI';
  const y=kwh=>top+height*(1-kwh/vehicle.capacity_kwh);
  for(const pct of [0,25,50,75,100]){
    const line=y(vehicle.capacity_kwh*pct/100);
    ctx.fillStyle='#8199a9';ctx.fillText(`${pct}%`,6,line+3);
    ctx.strokeStyle='#263541';ctx.beginPath();ctx.moveTo(left,line);ctx.lineTo(w-right,line);ctx.stroke();
  }
  const x=index=>left+width*index/batteryData.intervals.length;
  ctx.strokeStyle=colors.teal;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(0),y(batteryData.opening.stored_kwh));
  batteryData.intervals.forEach((row,index)=>ctx.lineTo(x(index+1),y(row.stored_kwh)));
  ctx.stroke();
  const cursor=x(Number($('battery-interval').value));
  ctx.strokeStyle='#f0f5f8b0';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(cursor,top);ctx.lineTo(cursor,h-bottom);ctx.stroke();ctx.setLineDash([]);
  const end=travelClock(batteryData.intervals.length*15);
  ctx.fillText('00:00',left,h-5);ctx.fillText(end,w-right-ctx.measureText(end).width,h-5);
}

document.addEventListener('DOMContentLoaded',()=>{
  $('battery-vehicle').onchange=loadBatteryVehicle;
  $('battery-case').onchange=loadBatteryVehicle;
  $('battery-day').onchange=()=>{$('battery-interval').value=0;void loadBatteryVehicle();};
  $('battery-interval').oninput=renderBatteryTime;
  $('selection').addEventListener('click',event=>{
    const button=event.target.closest('[data-battery-ev]');
    if(button)openBatteryTracker(button.dataset.batteryEv);
  });
  new ResizeObserver(drawBatteryCharge).observe($('battery-soc-chart'));
});
