'use strict';
let travelDay=null, travelMinute=0, travelSelected=null, travelTimer=null, travelBusy=false;
let travelFleetId=null, travelEpoch=0, travelSourceReady=false, travelHomeIds=new Set();
let destinationRun=null, chargingVehicles=new Map();
let destinationKey=null, destinationTravelId=null, travelPlan=null;
const travelStateNames={home:'Home',driving:'Driving',parked_away:'Parked away'};
const travelClock=m=>`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
function activeTravel(){return travelDay&&travelDay.fleet_id===fleet?.id;}
function travelMapActive(){return activeTravel()&&$('travel-map').checked;}
function travelState(v){return travelMinute===1440?{state:'home',purpose:'Home'}:v.segments.find(s=>s.start_minute<=travelMinute&&travelMinute<s.end_minute);}
function travelHomeCount(homes){return homes.reduce((n,h)=>n+h.ev_ids.filter(id=>travelHomeIds.has(id)).length,0);}
function travelVehicleNote(id){
  if(!activeTravel())return '';
  const v=travelDay.vehicles.find(v=>v.vehicle_id===id);if(!v)return '';
  return `<p>${escapeHTML(travelDay.config.date)} ${travelClock(travelMinute)} · ${travelStateNames[travelState(v).state]} · ${fmt(v.distance_km)} km today</p>`;
}
function stopTravelPlayback(){clearInterval(travelTimer);travelTimer=null;$('travel-play').textContent='▶ Play day';}
function clearTravel(){stopTravelPlayback();travelDay=null;travelSelected=null;travelHomeIds.clear();$('travel-results').hidden=true;drawNetwork();showSelection();}
async function travelFleetChanged(){
  const id=fleet?.id;if(id===travelFleetId)return;
  travelFleetId=id;const epoch=++travelEpoch;clearTravel();
  $('travel-status').textContent='Loading travel days for this fleet…';
  try{
    const [current,history]=await Promise.all([api('/api/travel?fleet_id='+id),api('/api/travel/scenarios?fleet_id='+id)]);
    if(epoch!==travelEpoch||fleet?.id!==id)return;
    travelHistoryOptions(history,current.scenario?.id);if(current.scenario)setTravel(current.scenario);
    else $('travel-status').textContent='No travel day generated for this fleet.';
  }catch(e){if(epoch===travelEpoch){$('travel-status').textContent='Travel loading failed: '+e.message;travelFleetId=null;}}
}
function travelHistoryOptions(list,selected){$('travel-scenarios').innerHTML=list.length?list.map(t=>`<option value="${t.id}" ${t.id===selected?'selected':''}>${escapeHTML(t.config.date)} · ${t.summary.ev_count} EVs · ${fmt(t.summary.achieved.mean_km)} km mean · seed ${t.config.seed} · ${t.id}</option>`).join(''):'<option value="">No saved day for this fleet</option>';}
async function pollTravel(){
  const s=await api('/api/travel/status');travelSourceReady=s.ready;
  $('travel-source').textContent=s.error?'NHTS preparation failed: '+s.error:s.ready?`NHTS 2022 V2.1 · ${fmt(s.source.audit.accepted_vehicle_days,0)} valid national home-to-home vehicle-day diaries · not measured Greensboro EV travel`:'Preparing official NHTS data…';
  if(fleet&&travelFleetId!==fleet.id)await travelFleetChanged();
}
function setTravel(item,{updateControls=true}={}){
  if(item.fleet_id!==fleet?.id)return;
  stopTravelPlayback();travelDay=item;travelMinute=0;travelSelected=item.vehicles[0]?.vehicle_id;
  const c=item.config,s=item.summary;
  if(updateControls){
    for(const [id,key] of [['mean','mean_km'],['std','std_km'],['min','min_km'],['max','max_km'],['unused','stationary_pct'],['seed','seed'],['tolerance','match_tolerance_km']])$('travel-'+id).value=c[key];
    $('travel-month').checked=c.prefer_month;
  }
  $('travel-results').hidden=false;
  $('travel-count').textContent=`${s.ev_count} / ${s.trip_count}`;
  $('travel-return').textContent=`${s.stationary_evs} unused · ${s.all_return_home?'All return home':'Return-home check failed'}`;
  $('travel-achieved-mean').textContent=fmt(s.achieved.mean_km,2)+' km';
  $('travel-achieved-std').textContent=fmt(s.achieved.std_km,2)+' km';
  $('travel-target-mean').textContent=`Requested ${fmt(c.mean_km,2)} · sampled target ${fmt(s.target.mean_km,2)} km`;
  $('travel-target-std').textContent=`Requested ${fmt(c.std_km,2)} · sampled target ${fmt(s.target.std_km,2)} km`;
  $('travel-total').textContent=fmt(s.total_distance_km,1)+' km';
  $('travel-match').textContent=`Mean diary mismatch ${fmt(s.mean_absolute_match_error_km,2)} km`;
  const warning=$('travel-warning');warning.hidden=!s.outside_match_tolerance;
  warning.textContent=`${s.outside_match_tolerance} EVs exceed the requested matching tolerance; maximum mismatch ${fmt(s.max_match_error_km,2)} km. Inspect diagnostics or relax the settings.`;
  $('travel-status').textContent=`${c.date} · ${new Date(c.date+'T12:00:00').toLocaleDateString(undefined,{weekday:'long'})} · fleet ${item.fleet_id} · saved ${item.id}`;
  $('travel-debug').textContent=JSON.stringify({id:item.id,fleet_id:item.fleet_id,config:c,summary:s,calibration:item.calibration,pool_counts:item.pool_counts,source:item.source,assumptions:item.assumptions},null,2);
  for(const format of ['json','trips','states'])$('travel-export-'+format).href=`/api/travel/scenarios/${item.id}/export?format=${format}`;
  renderTravelTime();
  if(job)refreshDestinationView();
}
function renderTravelTime(){
  if(!activeTravel())return;
  $('travel-minute').value=travelMinute;$('travel-time').textContent=travelClock(travelMinute);
  const row=travelDay.timeline[travelMinute];
  $('travel-now').textContent=`${travelClock(travelMinute)} · Home ${row.home} · Driving ${row.driving} · Parked away ${row.parked_away}`;
  travelHomeIds=new Set(travelDay.vehicles.filter(v=>travelState(v).state==='home').map(v=>v.vehicle_id));
  renderTravelRows();renderTravelVehicle();drawTravelChart();drawNetwork();showSelection();
}
function renderTravelRows(){
  if(!activeTravel())return;
  const q=$('travel-filter').value.trim().toLowerCase();
  const list=travelDay.vehicles.filter(v=>[v.vehicle_id,v.household_id,v.home_bus].some(s=>s.toLowerCase().includes(q)));
  $('travel-rows').innerHTML=list.map(v=>`<tr><td title="${escapeHTML(v.vehicle_id)}">${escapeHTML(v.home_bus)} · ${escapeHTML(v.vehicle_id.split('::').at(-1))}</td><td>${fmt(v.distance_km,1)}</td><td>${v.trips.length}</td><td><span class="travel-state ${travelState(v).state}">${travelStateNames[travelState(v).state]}</span></td><td><button data-travel-ev="${escapeHTML(v.vehicle_id)}">Inspect</button></td></tr>`).join('')||'<tr><td colspan="5">No matching EVs.</td></tr>';
}
function renderTravelVehicle(){
  const v=travelDay?.vehicles.find(v=>v.vehicle_id===travelSelected);
  $('travel-vehicle-title').textContent=v?`${v.home_bus} · ${v.vehicle_id.split('::').at(-1)}`:'No EVs in this fleet';
  if(!v){$('travel-vehicle').innerHTML='<p class="muted">Generate a fleet containing EVs to see trips.</p>';return;}
  $('travel-vehicle').innerHTML=`<p class="muted">${escapeHTML(v.vehicle_id)}<br>Target ${fmt(v.target_km,2)} km · actual ${fmt(v.distance_km,2)} km · ${fmt(v.home_minutes/60,1)} hours home<br>First departure ${v.first_departure_minute==null?'—':travelClock(v.first_departure_minute)} · final return ${v.last_return_minute==null?'Home all day':travelClock(v.last_return_minute)}</p><div class="travel-strip" aria-label="Selected EV daily states">${v.segments.map(s=>`<span class="${s.state}" style="width:${(s.end_minute-s.start_minute)/14.4}%" title="${travelStateNames[s.state]} ${travelClock(s.start_minute)}–${travelClock(s.end_minute)}"></span>`).join('')}</div><div class="table-wrap household-scroll"><table class="fleet-table"><thead><tr><th>Depart → arrive</th><th>Journey</th><th>km</th></tr></thead><tbody>${v.trips.map(t=>`<tr><td>${travelClock(t.depart_minute)} → ${travelClock(t.arrive_minute)}</td><td>${escapeHTML(t.origin)} → ${escapeHTML(t.destination)}</td><td>${fmt(t.distance_km,1)}</td></tr>`).join('')||'<tr><td colspan="3">Unused vehicle · home all day</td></tr>'}</tbody></table></div><p class="muted">Donor diary: ${escapeHTML(v.diary_id||'Scenario unused day')} · ${escapeHTML(v.calendar_match)}</p>${destinationStopsHtml(v)}`;
}
function refreshDestinationView(){
  const summary=job?.charging_summary;
  if(!summary?.destination_summary){void loadDestinationStops(null);return;}
  const ids=job.assumptions?.travel_ids;
  const preview=travelDay?.config.date;
  const day=ids?.includes(travelDay?.id)&&(job.daily_summaries?.[preview]||summary.schedule_date===preview)
    ?preview:job.date;
  void loadDestinationStops(job.id,ids?day:null);
}
async function loadDestinationStops(runId, day=null){
  const key=runId?runId+':'+(day||'') : null;
  if(destinationKey===key)return;
  destinationKey=key;
  destinationRun=runId;
  destinationTravelId=null;
  chargingVehicles=new Map();
  renderTravelVehicle();
  if(!runId)return;
  try{
    const schedule=await api(`/api/runs/${runId}/charging${day?'?day='+day:''}`);
    if(destinationKey!==key||job?.id!==runId)return;
    destinationTravelId=schedule.travel_id||job.assumptions.ev_charging.travel_id;
    chargingVehicles=new Map(schedule.vehicles.map(vehicle=>[vehicle.id,vehicle]));
    renderBatteryTracker();
    renderTravelVehicle();
  }catch(e){
    if(destinationKey===key){destinationKey=null;error('Could not load parking assignments: '+e.message);}
  }
}
function destinationStopsHtml(vehicle){
  if(job?.id!==destinationRun||destinationTravelId!==travelDay?.id)return '';
  const charging=chargingVehicles.get(vehicle.vehicle_id);
  const tracker=batteryTrackerButton(vehicle.vehicle_id);
  const stops=charging?.away_stops;
  if(!stops?.length)return tracker;
  const labels={plugged_in:'Plugged in',site_full:'All ports occupied',no_charger:'No charger',disabled:'Away charging off'};
  const rows=stops.map(stop=>`<tr><td>${travelClock(stop.start_minute)}–${travelClock(stop.end_minute)}</td><td><button data-destination-bus="${escapeHTML(stop.bus)}">${escapeHTML(stop.bus)}</button></td><td>${labels[stop.status]}</td><td>${fmt(stop.grid_kwh,2)}</td></tr>`).join('');
  return `${tracker}<h4>Away stops in selected grid run</h4><div class="table-wrap household-scroll"><table class="fleet-table"><thead><tr><th>Parked</th><th>Bus</th><th>Charging access</th><th>kWh</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function drawTravelChart(){
  if(!activeTravel()||$('travel-results').hidden)return;
  const {ctx,w,h}=size($('travel-chart')),padding=35,plot=w-2*padding,max=Math.max(1,travelDay.summary.ev_count);
  ctx.clearRect(0,0,w,h);
  for(let i=0;i<96;i++){
    const r=travelDay.timeline[i*15];let y=h-25;
    for(const [key,color] of [['home',colors.teal],['parked_away',colors.blue],['driving',colors.amber]]){const height=r[key]/max*(h-45);ctx.fillStyle=color;ctx.fillRect(padding+i*plot/96,y-height,plot/96+.3,height);y-=height;}
  }
  ctx.strokeStyle='#fff';ctx.beginPath();const x=padding+plot*travelMinute/1440;ctx.moveTo(x,12);ctx.lineTo(x,h-24);ctx.stroke();
  ctx.fillStyle=colors.muted;ctx.font='10px Segoe UI';ctx.textAlign='center';for(let hour=0;hour<=24;hour+=6)ctx.fillText(`${String(hour).padStart(2,'0')}:00`,padding+plot*hour/24,h-7);
  ctx.textAlign='left';ctx.fillText(`${travelDay.summary.ev_count} EVs`,padding,10);
}
async function generateTravel(){
  if(travelBusy||!fleet)return;
  const ids=['mean','std','min','max','unused','seed','tolerance'];if(!validateInputs([$('date'),$('days'),...ids.map(id=>$('travel-'+id))]))return;
  const body={fleet_id:fleet.id,date:$('date').value,days:Number($('days').value),mean_km:Number($('travel-mean').value),std_km:Number($('travel-std').value),min_km:Number($('travel-min').value),max_km:Number($('travel-max').value),stationary_pct:Number($('travel-unused').value),seed:Number($('travel-seed').value),prefer_month:$('travel-month').checked,match_tolerance_km:Number($('travel-tolerance').value)};
  const key=JSON.stringify(body);
  if(travelPlan?.key===key)return travelPlan;
  const id=fleet.id,epoch=++travelEpoch;travelBusy=true;$('travel-status').textContent='Matching EVs to complete NHTS diaries…';
  try{
    const plan=await api('/api/travel/plans',{method:'POST',headers:{'Content-Type':'application/json'},body:key});
    const item=await api('/api/travel/scenarios/'+plan.plans[0].id);
    if(epoch!==travelEpoch||fleet?.id!==id)return;
    error('');setTravel(item,{updateControls:false});
    travelHistoryOptions(plan.plans,item.id);
    $('travel-status').textContent=`${plan.plans.length} dates ready · ${fmt(plan.vehicle_days,0)} EV-day travel plans`;
    travelPlan={key,date:body.date,days:body.days,ids:plan.plans.map(p=>p.id)};
    return travelPlan;
  }catch(e){if(epoch===travelEpoch){error('Travel: '+e.message);$('travel-status').textContent='Generation failed; previous travel day retained.';}}
  finally{travelBusy=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('travel-minute').oninput=()=>{stopTravelPlayback();travelMinute=Number($('travel-minute').value);renderTravelTime();};
  $('travel-map').onchange=()=>{drawNetwork();showSelection();};$('travel-filter').oninput=renderTravelRows;
  $('travel-play').onclick=()=>{if(travelTimer){stopTravelPlayback();return;}if(travelMinute===1440)travelMinute=0;$('travel-play').textContent='Pause day';travelTimer=setInterval(()=>{travelMinute=Math.min(1440,travelMinute+15);renderTravelTime();if(travelMinute===1440)stopTravelPlayback();},350);};
  $('travel-chart').onclick=e=>{stopTravelPlayback();travelMinute=Math.round(Math.max(0,Math.min(1,(e.offsetX-35)/($('travel-chart').getBoundingClientRect().width-70)))*1440);renderTravelTime();};
  $('travel-rows').onclick=e=>{const b=e.target.closest('[data-travel-ev]');if(b){travelSelected=b.dataset.travelEv;renderTravelVehicle();}};
  $('travel-vehicle').onclick=e=>{
    const battery=e.target.closest('[data-battery-ev]');
    if(battery){openBatteryTracker(battery.dataset.batteryEv);return;}
    const button=e.target.closest('[data-destination-bus]');
    if(button){selectObject(button.dataset.destinationBus,{focus:true});$('selection').scrollIntoView({block:'nearest'});}
  };
  $('travel-scenarios').onchange=async()=>{const id=$('travel-scenarios').value;if(!id)return;const epoch=++travelEpoch;try{const item=await api(`/api/travel/scenarios/${id}`);if(epoch===travelEpoch)setTravel(item,{updateControls:false});}catch(e){error(e.message);}};
  new ResizeObserver(drawTravelChart).observe($('travel-chart'));
});
