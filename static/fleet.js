'use strict';
let fleet=null, serviceRegistry=null, fleetByBus=new Map(), fleetVehicles=new Map(), fleetBusy=false, fleetFormDirty=false, fleetSyncEpoch=0;
function fleetVisible(){return fleet&&($('fleet-overlay').checked||$('layer').value==='fleet');}
function fleetBusColor(id){const homes=fleetByBus.get(id);return homes?homes.some(h=>h.ev_count>0)?colors.teal:colors.blue:'#45606e';}
function fleetMarkers(){
  if(!fleetVisible())return [];
  return [...fleetByBus.entries()].flatMap(([bus,homes])=>{
    const count=travelMapActive()?travelHomeCount(homes):homes.reduce((n,h)=>n+h.ev_count,0),p=positions.get(bus);
    return count&&p?[{bus,x:p.x,y:p.y,count}]:[];
  });
}
function drawFleetOverlay(ctx,w,h){
  for(const marker of fleetMarkers()){
    if(marker.x< -10||marker.y< -10||marker.x>w+10||marker.y>h+10)continue;
    ctx.save();ctx.setLineDash([]);ctx.globalAlpha=1;ctx.fillStyle='#133e37';ctx.strokeStyle=colors.teal;ctx.lineWidth=1.5;
    ctx.beginPath();ctx.arc(marker.x,marker.y,7.5,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.fillStyle='#b9ffed';ctx.font='bold 9px Segoe UI';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(String(marker.count),marker.x,marker.y);ctx.restore();
  }
}
function pickFleetMarker(x,y){let found=null,best=9;for(const m of fleetMarkers()){const distance=Math.hypot(m.x-x,m.y-y);if(distance<best){found=m.bus;best=distance;}}return found;}
function fleetInspectorHtml(id,kind){
  if(!fleet)return '';
  const load=kind==='load'?net.loads.find(l=>l.id===id):null;
  const bus=kind==='bus'?id:load?.bus;
  const homes=bus?fleetByBus.get(bus):null;
  if(!homes?.length){
    const review=serviceRegistry?.needs_review.find(s=>s.bus===bus);
    return review?`<h4 class="fleet-inspector-title">EV allocation pending review</h4><p class="muted">Residential-profile 480 V service. Dwelling count and home-charger supply need mapping.</p>`:'';
  }
  return homes.map(home=>{
    const evs=home.ev_ids.map(id=>fleetVehicles.get(id));
    return `<h4 class="fleet-inspector-title">Household vehicle inventory</h4>`+prop('Household service',home.id)+prop('All vehicles',home.vehicle_count)+prop('EVs / other vehicles',`${home.ev_count} / ${home.vehicle_count-home.ev_count}`)+prop('Nominal battery capacity',fmt(home.battery_kwh,1)+' kWh')+prop('Home charging potential',fmt(home.potential_home_charging_kw,2)+' kW')+evs.map(v=>`<div class="vehicle-card"><h5>${escapeHTML(v.id.split('::').at(-1))} · ${escapeHTML(v.battery_category.replaceAll('_',' '))}</h5><p>${fmt(v.battery_kwh)} kWh battery · ${fmt(v.onboard_ac_kw,2)} kW assumed onboard AC limit</p><p>${v.home_access?`${v.charger_level==='level1'?'Level 1 · 120 V':'Level 2 · 240 V'} · ${fmt(v.potential_home_charging_kw,2)} kW home potential`:'No home-charging access assigned'}</p>${travelVehicleNote(v.id)}${batteryTrackerButton(v.id)}</div>`).join('');
  }).join('');
}
function updateFleetMode(){const fixed=$('fleet-mode').value==='fixed';$('fixed-field').hidden=!fixed;$('adoption-field').hidden=fixed;}
function setFleet(item,{updateControls=true}={}){
  fleetSyncEpoch++;
  fleet=item;fleetByBus=new Map();fleetVehicles=new Map(item.vehicles.map(v=>[v.id,v]));
  for(const h of item.households){if(!fleetByBus.has(h.bus))fleetByBus.set(h.bus,[]);fleetByBus.get(h.bus).push(h);}
  const c=item.config;
  if(updateControls){
  $('fleet-seed').value=c.seed;$('fleet-mode').value=c.mode;$('fleet-adoption').value=c.ev_adoption_pct;$('fleet-count').value=c.fixed_ev_count;
  $('fleet-home-access').value=c.home_access_pct;$('fleet-level1').value=c.level1_share_pct;updateFleetMode();
  }
  const s=item.summary,r=item.registry_summary;
  $('fleet-households').textContent=fmt(s.household_services,0);
  $('fleet-vehicles').textContent=fmt(s.all_vehicles,0);
  $('fleet-ownership').textContent=`${fmt(s.average_vehicles_per_household,3)} vehicles per household service`;
  $('fleet-evs').textContent=fmt(s.ev_count,0);
  $('fleet-adoption-result').textContent=`${fmt(s.actual_ev_adoption_pct,2)}% of vehicles · ${s.households_with_evs} services with EVs`;
  $('fleet-storage').textContent=fmt(s.nominal_battery_kwh,1)+' kWh';
  $('fleet-storage-detail').textContent=`${fmt(s.nominal_battery_kwh/1000,3)} MWh nominal · available energy unassigned`;
  $('fleet-power').textContent=fmt(s.potential_home_charging_kw,2)+' kW';
  $('fleet-power-detail').textContent=`${s.home_access_count} EVs with home access · nameplate sum`;
  $('fleet-audit').textContent=`${r.unique_services} unique services from ${r.dss_load_objects} load objects. Fleet uses ${r.eligible_household_services} residential 120/240 V household proxies; ${r.residential_services_for_review} residential-profile 480 V services need review. Commercial-profile services are excluded.`;
  $('fleet-categories').innerHTML=item.categories.map(b=>`<tr><td>${escapeHTML(b.name)}</td><td>${fmt(b.battery_kwh)} kWh</td><td>${b.count}</td><td>${fmt(b.realized_share_pct,1)}% / ${fmt(b.share*100,0)}%</td><td>${fmt(b.total_battery_kwh)} kWh</td></tr>`).join('');
  $('fleet-chargers').textContent=`Home charging: ${s.level1_count} Level 1 · ${s.level2_count} Level 2 · ${s.no_home_charging_count} without home access. Battery capacity assigned to EVs with home access: ${fmt(s.home_access_nominal_battery_kwh)} kWh; this is not plugged-in or usable energy.`;
  const link=$('fleet-export');link.href=`/api/fleets/${item.id}/export`;link.classList.remove('disabled');link.setAttribute('aria-disabled','false');
  $('fleet-status').textContent=`Fleet ${item.id} · seed ${c.seed} · inventory saved`;
  const review=serviceRegistry?.needs_review||[];
  $('fleet-review').innerHTML=`<h4>Service reconciliation</h4><p>${r.split_phase_pairs} matched split-phase pairs across all customer types. Residential services: ${r.residential_profile_services}; commercial-profile services: ${r.commercial_profile_services}.</p><div class="fleet-ownership-bins">${s.ownership_counts.map((count,i)=>`<span>${i} vehicles: ${count} homes</span>`).join('')}</div><h4>Residential services excluded pending review (${review.length})</h4><div class="fleet-review-list">${review.map(s=>`<div class="exception" data-fleet-bus="${escapeHTML(s.bus)}"><span>${escapeHTML(s.bus)}</span><span>480 V service</span></div>`).join('')}</div>`;
  $('fleet-debug').textContent=JSON.stringify({id:item.id,registry_digest:item.registry_digest,config:c,summary:s,assumptions:item.assumptions},null,2);
  renderFleetHouseholds();drawNetwork();showSelection();
  void travelFleetChanged();
}
function updateFleetMapStatus(){
  const label=$('fleet-map-status');if(!label)return;
  if(!fleet){label.textContent='EV inventory is loading…';return;}
  const mobile=travelMapActive(),count=mobile?travelHomeIds.size:fleet.summary.ev_count, buses=[...fleetByBus.values()].filter(homes=>mobile?travelHomeCount(homes)>0:homes.some(h=>h.ev_count>0)).length;
  label.textContent=`${fleetVisible()?'Map fleet':'EV overlay hidden'}: ${count} EVs${mobile?' at home at '+travelClock(travelMinute):''} · ${buses} bus badges · seed ${fleet.config.seed} · ${fleet.id}`;
}
function showFleetOnMap(){
  $('layer').value='fleet';$('fleet-overlay').checked=true;fit();
}
async function syncFleetInventory(id){
  if(fleetBusy||!id||fleet?.id===id)return;
  const epoch=fleetSyncEpoch;
  const item=await api('/api/fleets/'+id);
  if(fleetBusy||epoch!==fleetSyncEpoch)return;
  setFleet(item,{updateControls:!fleetFormDirty});
  if(fleetFormDirty)$('fleet-status').textContent+=' · unsaved form settings retained';
  await refreshFleetHistory();
}
function renderFleetHouseholds(){
  if(!fleet)return;
  const needle=$('fleet-filter').value.trim().toLowerCase(),only=$('fleet-only-ev').checked;
  const homes=fleet.households.filter(h=>(!only||h.ev_count>0)&&(!needle||h.id.toLowerCase().includes(needle)||h.bus.toLowerCase().includes(needle)));
  $('fleet-household-rows').innerHTML=homes.map(h=>`<tr><td title="${escapeHTML(h.id)}">${escapeHTML(h.bus)}</td><td>${h.vehicle_count} / ${h.ev_count}</td><td>${fmt(h.battery_kwh)}</td><td>${fmt(h.potential_home_charging_kw,2)}</td><td><button data-fleet-bus="${escapeHTML(h.bus)}">Inspect</button></td></tr>`).join('')||'<tr><td colspan="5">No household services match this filter.</td></tr>';
  $('fleet-household-count').textContent=`Showing ${homes.length} of ${fleet.households.length} household services. Vehicles / EVs are counts; home kW is potential charging, not simulated demand.`;
}
async function refreshFleetHistory(){
  const list=await api('/api/fleets');
  $('fleet-scenarios').innerHTML=list.map(f=>`<option value="${f.id}" ${f.id===fleet?.id?'selected':''}>${f.summary.ev_count} EVs · ${fmt(f.summary.nominal_battery_kwh)} kWh · seed ${f.config.seed} · ${f.id}</option>`).join('');
}
async function loadFleetInventory(){
  const [item,registry]=await Promise.all([api('/api/fleet'),api('/api/households')]);
  serviceRegistry=registry;setFleet(item);await refreshFleetHistory();$('fleet-generate').disabled=false;
}
async function generateFleet(){
  if(fleetBusy)return;
  const inputs=['fleet-seed','fleet-mode','fleet-home-access','fleet-level1',$('fleet-mode').value==='fixed'?'fleet-count':'fleet-adoption'];
  if(inputs.some(id=>!$(id).reportValidity()))return;
  fleetSyncEpoch++;fleetBusy=true;$('fleet-generate').disabled=true;$('fleet-status').textContent='Generating vehicle inventory…';
  try{
    const item=await api('/api/fleets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seed:Number($('fleet-seed').value),mode:$('fleet-mode').value,ev_adoption_pct:Number($('fleet-adoption').value),fixed_ev_count:Number($('fleet-count').value),home_access_pct:Number($('fleet-home-access').value),level1_share_pct:Number($('fleet-level1').value)})});
    error('');fleetFormDirty=false;setFleet(item);showFleetOnMap();
    if(item.summary.ev_count)await generateTravel();
    await refreshFleetHistory();
  }catch(e){error('Fleet: '+e.message);$('fleet-status').textContent='Inventory generation failed; previous inventory retained.';}
  finally{fleetBusy=false;$('fleet-generate').disabled=!ready;}
}
document.addEventListener('DOMContentLoaded',()=>{
  $('fleet-mode').onchange=updateFleetMode;
  for(const id of ['fleet-seed','fleet-mode','fleet-adoption','fleet-count','fleet-home-access','fleet-level1'])$(id).addEventListener('input',()=>{fleetFormDirty=true;});
  $('fleet-generate').onclick=generateFleet;
  $('fleet-filter').oninput=renderFleetHouseholds;$('fleet-only-ev').onchange=renderFleetHouseholds;
  $('fleet-overlay').onchange=drawNetwork;
  $('fleet-scenarios').onchange=async()=>{fleetSyncEpoch++;fleetBusy=true;try{const item=await api(`/api/fleets/${$('fleet-scenarios').value}/select`,{method:'POST'});fleetFormDirty=false;setFleet(item);showFleetOnMap();}catch(e){error(e.message);}finally{fleetBusy=false;}};
  $('fleet-panel').addEventListener('click',e=>{const button=e.target.closest('[data-fleet-bus]');if(button){selectObject(button.dataset.fleetBus,{focus:true});document.querySelector('.workspace').scrollIntoView({behavior:'smooth',block:'start'});}});
});
