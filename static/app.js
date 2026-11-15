'use strict';
const $ = id => document.getElementById(id);
const colors = {teal:'#53dbbd', blue:'#79adf4', amber:'#efbf70', red:'#fa817e', muted:'#526e7f'};
let net=null, job=null, snap=null, diagnostic=null, selected=null, ready=false;
let camera={scale:1, x:0, y:0}, positions=new Map(), basePositions=new Map();
let selectedInterval=0, follow=true, loadingSnapshot=0, playing=null, drag=null, lastLogPayload=null;
let savedRuns=[];
const requestedRun=new URLSearchParams(window.location.search).get('run');
let preparingEV=false, gridScenario='charging';
const powerPlotPadding={left:64,right:24,top:18,bottom:28};
const fmt=(v,n=1)=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:n});
const escapeHTML=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function error(message){ $('error').textContent=message; $('error').hidden=!message; }
function validateInputs(fields){
  for(const field of fields){
    if(field.validity.valid)continue;
    for(let parent=field.closest('details');parent;parent=parent.parentElement.closest('details'))parent.open=true;
    field.reportValidity();return false;
  }
  return true;
}
async function api(path, options={}) {
  const response=await fetch(path, options);
  if(!response.ok){let body=await response.json().catch(()=>({})); const detail=body.detail; throw new Error(typeof detail==='string'?detail:Array.isArray(detail)?[...new Set(detail.map(e=>String(e.msg||'Invalid setting').replace(/^Value error, /,'')))].join('; '):response.statusText);}
  return response.json();
}
function size(canvas){const rect=canvas.getBoundingClientRect(), ratio=devicePixelRatio||1; canvas.width=Math.round(rect.width*ratio); canvas.height=Math.round(rect.height*ratio); const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);return {ctx,w:rect.width,h:rect.height};}
function fit(){camera={scale:1,x:0,y:0};drawNetwork();}
function computePositions(w,h){
  if(!net)return;
  const valid=net.nodes.filter(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)&&n.x!==0&&n.y!==0);
  const xs=valid.map(n=>n.x), ys=valid.map(n=>n.y);
  const loX=Math.min(...xs),hiX=Math.max(...xs),loY=Math.min(...ys),hiY=Math.max(...ys);
  const lat=Math.cos((loY+hiY)/2*Math.PI/180);
  const scale=Math.min((w-90)/((hiX-loX)*lat||1),(h-90)/(hiY-loY||1));
  basePositions=new Map(valid.map(n=>[n.id,{x:w/2+(n.x-(loX+hiX)/2)*lat*scale,y:h/2-(n.y-(loY+hiY)/2)*scale}]));
  positions=new Map([...basePositions].map(([id,p])=>[id,{x:(p.x-w/2)*camera.scale+w/2+camera.x,y:(p.y-h/2)*camera.scale+h/2+camera.y}]));
}
function voltageColor(v){return v==null?colors.muted:v<.1?'#56616c':v<.95||v>1.05?colors.red:v<.97||v>1.03?colors.amber:colors.teal;}
function loadingColor(v){return v==null?colors.muted:v>100?colors.red:v>80?colors.amber:colors.teal;}
function transformerMarkers(){
  if(!net)return [];
  return net.elements.filter(e=>e.kind==='transformer').flatMap(e=>{
    const points=e.buses.map(bus=>positions.get(bus)).filter(Boolean);
    if(!points.length)return [];
    return [{id:e.id,x:points.reduce((n,p)=>n+p.x,0)/points.length,
      y:points.reduce((n,p)=>n+p.y,0)/points.length,element:e}];
  });
}
function focusSelection(){
  const w=$('network').clientWidth,h=$('network').clientHeight;
  computePositions(w,h);
  const element=net.elements.find(e=>e.id===selected?.id);
  const load=net.loads.find(e=>e.id===selected?.id);
  const buses=element?element.buses:[load?load.bus:selected.id];
  const points=buses.map(bus=>basePositions.get(bus)).filter(Boolean);
  if(!points.length)return;
  const x=points.reduce((n,p)=>n+p.x,0)/points.length;
  const y=points.reduce((n,p)=>n+p.y,0)/points.length;
  camera.scale=Math.max(camera.scale,element?.kind==='transformer'?5:2);
  camera.x=-(x-w/2)*camera.scale;
  camera.y=-(y-h/2)*camera.scale;
}
function drawSelection(ctx){
  if(!selected)return;
  const element=net.elements.find(e=>e.id===selected.id);
  const load=net.loads.find(e=>e.id===selected.id);
  const points=(element?element.buses:[load?load.bus:selected.id]).map(bus=>positions.get(bus)).filter(Boolean);
  if(!points.length)return;
  ctx.save();ctx.setLineDash([]);ctx.globalAlpha=1;
  // Render after the network so a selected branch cannot be hidden by bus dots.
  if(element){
    ctx.strokeStyle='#fff';ctx.lineWidth=5;ctx.shadowColor=colors.teal;ctx.shadowBlur=12;
    for(const point of points.slice(1)){ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);ctx.lineTo(point.x,point.y);ctx.stroke();}
  }
  const x=points.reduce((n,p)=>n+p.x,0)/points.length,y=points.reduce((n,p)=>n+p.y,0)/points.length;
  ctx.shadowBlur=0;ctx.fillStyle='#0c1117';ctx.strokeStyle='#fff';ctx.lineWidth=2;
  ctx.beginPath();ctx.arc(x,y,12,0,Math.PI*2);ctx.fill();ctx.stroke();
  ctx.fillStyle=element?.kind==='transformer'?colors.amber:colors.teal;
  ctx.beginPath();ctx.moveTo(x,y-7);ctx.lineTo(x+7,y);ctx.lineTo(x,y+7);ctx.lineTo(x-7,y);ctx.closePath();ctx.fill();
  ctx.restore();
}
function drawNetwork(){
  const {ctx,w,h}=size($('network'));ctx.clearRect(0,0,w,h);if(!net)return;
  computePositions(w,h);
  const layer=$('layer').value, elementIndex=new Map(net.elements.map((e,i)=>[e.id,i]));
  for(const edge of net.edges){
    const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;
    const state=snap?.elements[elementIndex.get(edge.id)];
    ctx.strokeStyle=layer==='loading'?loadingColor(state?.loading_pct):layer==='loss'?(state?.loss_kw>1?colors.amber:'#315963'):edge.kind==='transformer'?'#cfa567':'#367b7c';
    ctx.globalAlpha=(edge.closed&&edge.enabled) ? .85 : .3;ctx.lineWidth=selected?.id===edge.id?3:edge.kind==='transformer'?1.6:1;
    ctx.setLineDash(edge.closed?[]:[3,3]);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
  }
  ctx.setLineDash([]);ctx.globalAlpha=1;
  net.nodes.forEach((node,i)=>{const p=positions.get(node.id);if(!p)return;ctx.fillStyle=layer==='fleet'?fleetBusColor(node.id):layer==='voltage'?voltageColor(snap?.voltages[i]):node.customers.length?colors.blue:'#63989e';ctx.beginPath();ctx.arc(p.x,p.y,node.id===net.source_bus?6:selected?.id===node.id?5:node.customers.length?2.1:1.2,0,Math.PI*2);ctx.fill();});
  const root=positions.get(net.source_bus);if(root){ctx.strokeStyle=colors.teal;ctx.lineWidth=1;ctx.strokeRect(root.x-9,root.y-9,18,18);ctx.fillStyle='#d7eceb';ctx.font='11px Segoe UI';ctx.fillText('69 kV supply',root.x+13,root.y-10);}
  for(const marker of transformerMarkers()){
    if(marker.x< -8||marker.y< -8||marker.x>w+8||marker.y>h+8)continue;
    ctx.fillStyle=colors.amber;ctx.strokeStyle='#302a20';ctx.lineWidth=1;
    const r=3.5;ctx.beginPath();ctx.moveTo(marker.x,marker.y-r);ctx.lineTo(marker.x+r,marker.y);ctx.lineTo(marker.x,marker.y+r);ctx.lineTo(marker.x-r,marker.y);ctx.closePath();ctx.fill();ctx.stroke();
  }
  drawFleetOverlay(ctx,w,h);updateFleetMapStatus();
  drawSelection(ctx);
  const selectedLabel=$('map-selection');
  selectedLabel.hidden=!selected;
  if(selected){selectedLabel.textContent='Selected '+selected.id;selectedLabel.title=selected.id;}

  let legend=layer==='voltage'?[[colors.teal,'0.97–1.03 pu'],[colors.amber,'Near limits'],[colors.red,'Outside 0.95–1.05'],['#56616c','De-energized']]:layer==='loading'?[[colors.teal,'≤80%'],[colors.amber,'80–100%'],[colors.red,'>100%']]:layer==='loss'?[[colors.amber,'>1 kW'],['#315963','≤1 kW']]:[['#367b7c','Line'],['#cfa567','Transformer']];
  if(layer==='fleet')legend=[[colors.teal,'Household with EVs'],[colors.blue,'Household without EVs'],[colors.muted,'Other bus']];
  if(!snap&&layer!=='type'&&layer!=='fleet')legend=[[colors.muted,'Run a simulation to show electrical state']];
  if(layer!=='type')legend.push([colors.amber,'◇ Transformer']);
  if(fleetVisible())legend.push([colors.teal,travelMapActive()?'Numbered circles: EVs at home':'Numbered circles: EV count']);
  $('legend').innerHTML=legend.map(([c,t])=>`<span><i style="background:${c}"></i>${t}</span>`).join('');
}
function prop(label,value){return `<div class="property"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>`;}
function showSelection(){
  if(!selected||!net)return;
  const {id,kind}=selected;let html='';
  if(kind==='bus'){
    const i=net.nodes.findIndex(n=>n.id===id),n=net.nodes[i],v=snap?.voltages[i];
    html+=prop('Type','Bus / connection point')+prop('Min phase voltage',v==null?'No interval selected':fmt(v,4)+' pu')+prop('Base voltage (L–N)',fmt(n.base_kv_ln,3)+' kV')+prop('Nodes / phases',n.phases.join(', '))+prop('Supply path',n.reachable?'Connected':'Disconnected')+prop('Load objects',n.customers.length);
    if(n.customers.length){const demand=n.customers.map(name=>net.loads.findIndex(l=>l.id===name)).reduce((a,i)=>a+(snap?.loads[i]?.[0]||0),0);html+=prop('Solved demand',snap?fmt(demand,2)+' kW':'—');html+=`<details><summary>Load objects (${n.customers.length})</summary><pre>${escapeHTML(n.customers.join('\n'))}</pre></details>`;}
    if(snap?.baseline){
      const indices=n.customers.map(name=>net.loads.findIndex(l=>l.id===name));
      const before=indices.reduce((sum,i)=>sum+(snap.baseline.loads[i]?.[0]||0),0);
      const home=indices.reduce((sum,i)=>sum+(snap.loads[i]?.[0]||0),0);
      const ev=snap.ev_kw_by_bus[id]||0;
      const baselineVoltage=snap.baseline.voltages[i];
      html+=prop('Baseline bus demand',fmt(before,3)+' kW')+prop('EV charging at bus',fmt(ev,3)+' kW')+prop('Bus demand with EVs',fmt(home+ev,3)+' kW');
      if(baselineVoltage!=null&&v!=null)html+=prop('Baseline min phase voltage',fmt(baselineVoltage,5)+' p.u.')+prop('Change in min phase voltage',fmt(v-baselineVoltage,5)+' p.u.');
    }else if(snap?.ev_kw_by_bus)html+=prop('Net EV power at bus',fmt(snap.ev_kw_by_bus[id]||0,3)+' kW');
    const ports=job?.charging_summary?.destination_sites?.[id];
    if(ports)html+=prop('Destination charging ports',ports);
  }else if(kind==='load'){
    const i=net.loads.findIndex(l=>l.id===id),l=net.loads[i];
    html+=prop('Bus',l.bus)+prop('Profile class',l.profile_class)+prop('Profile',l.profile)+prop('Annual max setting',fmt(l.rated_kw,2)+' kW')+prop('Solved active power',snap?fmt(snap.loads[i][0],3)+' kW':'—')+prop('Solved reactive power',snap?fmt(snap.loads[i][1],3)+' kvar':'—');
  }else{
    const i=net.elements.findIndex(e=>e.id===id),e=net.elements[i],s=snap?.elements[i];
    html+=prop('Type',e.kind)+prop('Phases',e.phases)+prop('Loading',s?fmt(s.loading_pct,1)+'%':'—')+prop('Real-power loss',s?fmt(s.loss_kw,4)+' kW':'—')+prop('State',e.enabled&&e.closed?'Enabled / closed':'Disabled or open');
    const baseline=snap?.baseline?.elements[i];
    if(baseline){
      if(baseline.loading_pct!=null&&s.loading_pct!=null)html+=prop('Baseline loading',fmt(baseline.loading_pct,2)+'%')+prop('Change in loading',fmt(s.loading_pct-baseline.loading_pct,2)+' percentage points');
      if(baseline.loss_kw!=null&&s.loss_kw!=null)html+=prop('Baseline loss',fmt(baseline.loss_kw,4)+' kW')+prop('Change in loss',fmt(s.loss_kw-baseline.loss_kw,4)+' kW');
    }
    if(e.kind==='line')html+=prop('Length',fmt(e.length,5)+' '+({0:'unspecified units',1:'mi',2:'kft',3:'km',4:'m',5:'ft',6:'in',7:'cm',8:'mm'}[e.units]||e.units))+prop('Normal current rating',fmt(e.normal_amps)+' A')+prop('Linecode',e.linecode);
    else e.windings.forEach((w,i)=>{html+=prop('Winding '+(i+1),fmt(w.kv,3)+' kV / '+fmt(w.kva)+' kVA');});
    html+=`<details><summary>Terminal buses</summary><pre>${escapeHTML(e.buses.join('\n'))}</pre></details>`;
  }
  html+=fleetInspectorHtml(id,kind);
  $('selection').innerHTML=`<div class="selected-name">${escapeHTML(id)}</div><div class="properties">${html}</div>`;
}
function selectObject(id,{focus=false}={}){
  if(net.nodes.some(n=>n.id===id))selected={id,kind:'bus'};
  else if(net.elements.some(e=>e.id===id))selected={id,kind:'element'};
  else if(net.loads.some(l=>l.id===id))selected={id,kind:'load'};
  else{error('No matching bus, element or load object.');return;}
  error('');$('search').value=id;if(focus)focusSelection();showSelection();drawNetwork();
}
function drawPowerPlot(canvas, rows, prefix, max, includeEv, showDate){
  const {ctx,w,h}=size(canvas);
  const {left,right,top,bottom}=powerPlotPadding;
  const ww=w-left-right, hh=h-top-bottom;
  ctx.clearRect(0,0,w,h);
  ctx.font='10px Segoe UI';ctx.fillStyle='#8199a9';
  if(!rows.length){ctx.fillText(job?'Waiting for the first interval.':'Run a simulation to see power through time.',12,h/2);return;}
  for(let i=0;i<=4;i++){let y=top+hh*i/4;ctx.strokeStyle='#263541';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();ctx.fillStyle='#8199a9';ctx.fillText(fmt(max*(1-i/4)/1000,2)+' MW',8,y+3);}
  const curves=[['supply_kw',colors.teal],['load_kw',colors.blue],['loss_kw',colors.amber]];
  if(prefix==='trading_')curves.push(['ev_import_kwh','#d3a5f3',4],['ev_export_kwh','#ef96c5',4]);
  else if(includeEv)curves.push(['ev_charge_kw','#d3a5f3']);
  const xAt=index=>left+index/Math.max(1,rows.length-1)*ww;
  for(const [key,color,factor=1] of curves){
    ctx.strokeStyle=color;ctx.lineWidth=key==='loss_kw'?1.5:2;ctx.beginPath();
    rows.forEach((row,index)=>{
      const x=xAt(index), y=top+hh-row[prefix+key]*factor/max*hh;
      if(index===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  const cursor=xAt(Math.min(selectedInterval,rows.length-1));
  ctx.strokeStyle='#dde9ed66';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(cursor,top);ctx.lineTo(cursor,top+hh);ctx.stroke();ctx.setLineDash([]);
  const timeLabel=row=>job?.days>1?row.timestamp.slice(5,16).replace('T',' '):showDate&&ww>=220?row.timestamp.replace('T',' '):row.timestamp.slice(11,16);
  ctx.fillStyle='#8199a9';ctx.fillText(timeLabel(rows[0]),left,h-10);
  if(rows.length>1){
    const last=timeLabel(rows.at(-1));ctx.fillText(last,w-right-ctx.measureText(last).width,h-10);
  }
}

function drawChart(){
  const rows=job?.series||[];
  const paired=Boolean(job?.assumptions?.ev_charging);
  const trading=Boolean(job?.market_summary);
  $('charging-power-plot').hidden=!paired;
  $('trading-power-plot').hidden=!trading;
  $('power-plots').classList.toggle('paired',paired);
  $('power-plots').classList.toggle('triple',trading);
  $('charging-chart-keys').hidden=!paired;
  $('trading-chart-key').hidden=!trading;
  const current=job?.series.at(-1)?.timestamp.replace('T',' ');
  $('power-chart-context').textContent=job
    ? `${job.date} · ${current?'Settled through '+current:'Waiting for intervals'} · 15-minute intervals${paired?' · shared axes':''}`
    : '15-minute intervals · click a plot to inspect a time';
  const max=Math.max(1,...rows.map(r=>Math.max(r.supply_kw,r.load_kw,r.baseline_supply_kw||0,r.baseline_load_kw||0,
    r.trading_supply_kw||0,r.trading_load_kw||0,4*(r.trading_ev_import_kwh||0),4*(r.trading_ev_export_kwh||0))))*1.12;
  drawPowerPlot($('chart'),rows,paired?'baseline_':'',max,false,!paired);
  if(paired)drawPowerPlot($('chart-with-ev'),rows,'',max,true,false);
  if(trading)drawPowerPlot($('chart-with-trading'),rows,'trading_',max,true,false);
  drawGenerationCharts();
}

function drawGenerationPlot(canvas, rows, prefix, max, solarOnly=false){
  const {ctx,w,h}=size(canvas);
  const {left,right,top,bottom}=powerPlotPadding;
  const height=h-top-bottom, step=(w-left-right)/rows.length;
  if(step<=0||height<=0)return;
  const y=mw=>top+height*(1-mw/max);
  ctx.font='10px Segoe UI';
  for(let i=0;i<=4;i++){
    const value=max*i/4;
    ctx.strokeStyle='#263541';ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(w-right,y(value));ctx.stroke();
    ctx.fillStyle='#8199a9';ctx.fillText(`${fmt(value,2)} MW`,8,y(value)+3);
  }
  rows.forEach((row,index)=>{
    const x=left+index*step, solar=row[prefix+'solar_mw']??0;
    let base=0;
    if(!solarOnly){
      const nuclear=row[prefix+'nuclear_mw'];
      base=nuclear+row[prefix+'coal_gas_mw'];
      ctx.fillStyle='#4f7eb8';ctx.fillRect(x,y(nuclear),step+.25,y(0)-y(nuclear));
      ctx.fillStyle='#ad803e';ctx.fillRect(x,y(base),step+.25,y(nuclear)-y(base));
    }
    ctx.fillStyle='#53dbbd';ctx.fillRect(x,y(base+solar),step+.25,y(base)-y(base+solar));
  });
  if(!solarOnly){
    ctx.strokeStyle='#f0f5f8';ctx.lineWidth=1.25;ctx.beginPath();
    rows.forEach((row,index)=>{
      const x=left+index*step, draw=y(row[prefix+'supply_kw']/1000);
      if(index===0)ctx.moveTo(x,draw);else ctx.lineTo(x,draw);
      ctx.lineTo(x+step,draw);
    });
    ctx.stroke();
  }
  const cursor=left+(Math.min(selectedInterval,rows.length-1)+.5)*step;
  ctx.strokeStyle='#f0f5f8b0';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(cursor,top);ctx.lineTo(cursor,y(0));ctx.stroke();ctx.setLineDash([]);
  const stamp=row=>job.days===1?row.timestamp.slice(11,16):row.timestamp.slice(5,16).replace('T',' ');
  const last=stamp(rows.at(-1));
  ctx.fillStyle='#8199a9';ctx.fillText(stamp(rows[0]),left,h-10);ctx.fillText(last,w-right-ctx.measureText(last).width,h-10);
}

function drawGenerationCharts(){
  if(!job?.generation_summary)return;
  const rows=job.series, paired=Boolean(job.generation_summary.with_ev), trading=Boolean(job.market_summary);
  if(!rows.length)return;
  const max=Math.max(1,...rows.map(row=>(row.solar_mw??0)+Math.max(row.nuclear_mw+row.coal_gas_mw,
    (row.baseline_nuclear_mw||0)+(row.baseline_coal_gas_mw||0),
    (row.trading_nuclear_mw||0)+(row.trading_coal_gas_mw||0))))*1.10;
  drawGenerationPlot($('generation-baseline'),rows,paired?'baseline_':'',max);
  if(paired)drawGenerationPlot($('generation-ev'),rows,'',max);
  if(trading)drawGenerationPlot($('generation-trading'),rows,'trading_',max);
  if(!$('solar-output').hidden){
    const solarMax=Math.max(.1,...rows.map(row=>row.solar_mw??0))*1.10;
    drawGenerationPlot($('generation-solar'),rows,'',solarMax,true);
  }
  const row=rows[Math.min(selectedInterval,rows.length-1)];
  const cost=prefix=>{
    const value=row[prefix+'average_generation_cost_per_mwh'];
    return value==null?'—':`$${fmt(value,2)}/MWh`;
  };
  const prices=paired?`${cost('baseline_')} without EV charging · ${cost('')} with EV charging`:cost('');
  $('generation-interval').textContent=`${row.timestamp.replace('T',' ')} · Solar ${fmt(row.solar_mw??0,3)} MW · Next offer · ${prices}${trading?` · ${cost('trading_')} with EV trading`:''}`;
}

function renderGeneration(){
  const settings=job.assumptions?.generation, summary=job.generation_summary;
  $('generation-results').hidden=!summary;
  if(!summary){
    $('generation-status').textContent=!settings?'Run a new baseline or EV charging comparison to see generation and cost.'
      :job.status==='running'?'Generation and cost will appear as intervals settle.'
      :'Generation and cost require a completed run.';
    return;
  }
  const mode=settings.nuclear_mw==null?'automatic':'manual';
  const minimumGeneration=summary.nuclear_mw+settings.coal_gas_min_mw;
  const resource=job.assumptions.solar_resource;
  $('solar-output').hidden=!resource;
  if(resource)$('solar-profile-label').textContent=`${resource.source} · Greensboro ${resource.year} · ${fmt(settings.solar_capacity_mw,3)} MW rated · ${fmt(settings.solar_performance_factor,3)} performance factor · $${fmt(settings.solar_cost_per_mwh,3)}/MWh · Same output in all scenarios`;
  $('generation-status').textContent=`${job.date} · Baseline minimum ${fmt(summary.baseline_min_mw,4)} MW · Minimum generation ${fmt(minimumGeneration,4)} MW · Fixed nuclear ${fmt(summary.nuclear_mw,4)} MW (${mode}) · Coal + gas minimum ${fmt(settings.coal_gas_min_mw,3)} MW · Nuclear $${fmt(settings.nuclear_cost_per_mwh,3)}/MWh · Coal + gas $${fmt(settings.coal_gas_cost_per_mwh,3)}/MWh`;
  const paired=Boolean(summary.with_ev);
  const trading=Boolean(summary.with_trading);
  $('generation-ev-plot').hidden=!paired;
  $('generation-trading-plot').hidden=!trading;
  $('generation-plots').classList.toggle('paired',paired);
  $('generation-plots').classList.toggle('triple',trading);
  const cases=[['Baseline',paired?'baseline_':'',summary.without_ev]];
  if(paired)cases.push(['EV charging only','',summary.with_ev]);
  if(trading)cases.push(['EV trading','trading_',summary.with_trading]);
  const totals=['nuclear_mwh','nuclear_cost_usd','coal_gas_mwh','coal_gas_cost_usd','solar_mwh','solar_cost_usd','total_mwh','surplus_mwh',
    'generation_cost_usd','average_generation_cost_per_mwh'];
  const fields=['nuclear_mw','coal_gas_mw','solar_mw','surplus_mw','generation_cost_usd','average_generation_cost_per_mwh'];
  $('generation-totals').innerHTML=cases.map(([label,,total])=>`<tr><th scope="row">${label}</th>${totals.map(key=>`<td>${fmt(key==='total_mwh'?total.nuclear_mwh+total.coal_gas_mwh+(total.solar_mwh??0):total[key]??0,2)}</td>`).join('')}</tr>`).join('');
  $('generation-rows').innerHTML=job.series.flatMap(row=>cases.map(([label,prefix])=>
    `<tr><td>${escapeHTML(row.timestamp.replace('T',' '))}</td><td>${label}</td><td>${fmt(row[prefix+'supply_kw']/1000,3)}</td>${fields.map((key,index)=>`<td>${fmt(key==='solar_mw'?row[prefix+key]??0:row[prefix+key],index<4?3:2)}</td>`).join('')}</tr>`)).join('');
}

function updateComparisonButton(){
  const button=$('open-charging-comparison');
  const matching=job?.days===1&&!job.assumptions?.ev_charging
    ? savedRuns.find(run=>run.date===job.date&&run.assumptions?.ev_charging&&run.status==='completed')
    : null;
  button.hidden=!matching;
  button.dataset.run=matching?.id||'';
}
async function loadSnapshot(index){
  if(!job||!job.completed)return;selectedInterval=Math.max(0,Math.min(index,job.completed-1));const request=++loadingSnapshot;
  $('follow-live').checked=follow;
  const id=job.id, scenario=gridScenario;
  try{const data=await api(`/api/runs/${id}/snapshot/${selectedInterval}?scenario=${scenario}`);if(request!==loadingSnapshot||job?.id!==id)return;snap=scenario==='baseline'&&data.baseline?data.baseline:data;$('interval').value=selectedInterval;$('timestamp').textContent=`${scenarioLabels[scenario]} · ${job.series[selectedInterval]?.timestamp.replace('T',' ')||''}`;drawNetwork();drawChart();showSelection();renderDiagnostics();renderChargingInterval();}catch(e){error(e.message);}
}
function renderJob(){
  if(!job)return;const t=job.totals||{};
  $('supplied').textContent=fmt(t.supply_kwh);$('consumption').textContent=fmt(t.load_kwh);$('loss').textContent=fmt(t.loss_kwh);$('loss-detail').textContent=`kWh · ${fmt(t.loss_pct,2)}% of supply`;
  $('progress').max=job.intervals;$('progress').value=job.completed;
  const trading=job.status==='running'&&job.phase==='trading';
  const settling=job.status==='running'&&job.completed===job.intervals;
  const rollout=job.status==='running'&&job.phase==='rollout';
  const runningSequence=job.status==='running'&&job.assumptions?.travel_ids;
  if(trading)$('progress').value=job.trading_completed;
  if(rollout){$('progress').max=job.rollout_intervals||1;$('progress').value=job.rollout_completed;}
  $('progress-label').textContent=rollout
    ?`Rollout · ${job.rollout_date} · ${job.rollout_completed}/${job.rollout_intervals} intervals · ${fmt(job.elapsed_s)} s`
    :runningSequence&&job.phase==='sizing'?'Sizing generation from the baseline demand'
    :runningSequence?`${job.comparison_date||job.date} · ${job.day_completed||0}/96 today · ${job.completed}/${job.intervals} settled`
    :`${trading?'EV trading':settling?'Valuing energy and losses':job.status} · ${trading?job.trading_completed:job.completed}/${job.intervals} intervals · ${fmt(job.elapsed_s)} s`;
  $('follow-live').checked=follow;
  $('run-id').textContent=job.id;$('interval').disabled=!job.completed;$('interval').max=Math.max(0,job.completed-1);$('play').disabled=!job.completed;
  const exporting=job.status!=='running'&&job.completed>0;$('export').classList.toggle('disabled',!exporting);$('export').setAttribute('aria-disabled',String(!exporting));$('export').href=exporting?`/api/runs/${job.id}/export`:'#';
  if(job.error)error(job.error);
  renderGeneration();drawNetwork();drawChart();showSelection();renderDiagnostics();renderChargingComparison();renderMarket();updateComparisonButton();
}

function renderChargingComparison(){
  const settings=job?.assumptions?.ev_charging;
  $('charging-panel').hidden=!settings;
  const destinations=job.charging_summary?.destination_summary;
  refreshDestinationView();
  renderBatteryTracker();
  if(!settings)return;
  const t=job.totals, base=job.baseline_totals, fleetSummary=job.charging_summary;
  const startFull=settings.initial_full_pct==null?'':` (${settings.initial_full_pct}% target)`;
  const opening=job.assumptions.travel_ids
    ?`Rollout from ${job.assumptions.rollout_start} · ${fmt(job.opening_batteries?.battery_kwh,1)} kWh at opening`
    :`${fleetSummary?.initial_full_count} EVs start full${startFull}`;
  $('charging-scenario').textContent=fleetSummary?`${job.date} · ${fleetSummary.ev_count} home chargers · ${opening} · ${fmt(settings.charge_efficiency*100,2)}% charging efficiency`:job.date;
  $('charging-draw').textContent=fmt(t.ev_charge_kwh,2);
  $('charging-energy-split').textContent=t.ev_away_charge_kwh==null?'kWh drawn at home buses'
    :`${fmt(t.ev_home_charge_kwh,2)} kWh home · ${fmt(t.ev_away_charge_kwh,2)} kWh away`;
  $('charging-destinations').hidden=!destinations;
  if(destinations){
    $('charging-destinations').textContent=settings.away_charging_enabled
      ? `${destinations.equipped_buses} of ${destinations.candidate_buses} destination buses equipped · ${destinations.ports} ports · ${destinations.plugged_in_stops} stops plugged in · ${destinations.full_site_stops} arrivals at full sites · seed ${settings.away_seed}`
      : 'Away charging off';
  }
  $('charging-peak').textContent=fmt(t.peak_ev_charge_kw,2);
  $('charging-supply').textContent=fmt(t.delta_supply_kwh,2);
  $('charging-loss').textContent=fmt(t.delta_loss_kwh,2);
  $('charging-totals').textContent=base?`Peak supply ${fmt(base.peak_supply_kw,1)} → ${fmt(t.peak_supply_kw,1)} kW · Overload intervals ${base.overload_intervals} → ${t.overload_intervals} · Voltage violation intervals ${base.voltage_violation_intervals} → ${t.voltage_violation_intervals}`:'';
  $('charging-unmet').textContent=t.ev_unmet_driving_kwh>1e-6?`Scheduled trips required ${fmt(t.ev_unmet_driving_kwh,2)} kWh more than the batteries could provide.`:'';
  $('charging-battery').hidden=!fleetSummary;
  if(fleetSummary)$('charging-battery').href=`/api/runs/${job.id}/charging`;
  $('charging-csv').hidden=job.status==='running'||!job.completed;
  $('charging-csv').href=`/api/runs/${job.id}/export`;
  $('charging-rows').innerHTML=job.series.map(r=>`<tr><td><button data-charge-index="${r.index}">${escapeHTML(r.timestamp.replace('T',' '))}</button></td><td>${fmt(r.baseline_load_kw,1)}</td><td>${fmt(r.load_kw,1)}</td><td>${fmt(r.ev_charge_kwh,2)}</td><td>${fmt(r.delta_supply_kwh,2)}</td><td>${fmt(r.baseline_max_line_loading_pct,1)} → ${fmt(r.max_line_loading_pct,1)}%</td><td>${fmt(r.baseline_max_transformer_loading_pct,1)} → ${fmt(r.max_transformer_loading_pct,1)}%</td><td>${fmt(r.baseline_min_voltage_pu,4)} → ${fmt(r.min_voltage_pu,4)}</td></tr>`).join('');
  renderChargingInterval();
}

function renderChargingInterval(){
  if(!job?.assumptions?.ev_charging)return;
  const row=job.series[snap?.index ?? 0];
  if(!row){$('charging-interval').textContent='Waiting for the first interval.';return;}
  const asset=(id,label)=>id?`<button data-charge-object="${escapeHTML(id)}">${escapeHTML(label)} · ${escapeHTML(id)}</button>`:'';
  const split=row.ev_away_charge_kwh==null?'':` · ${fmt(row.ev_home_charge_kwh,2)} kWh home · ${fmt(row.ev_away_charge_kwh,2)} kWh away`;
  $('charging-interval').innerHTML=`<p>${escapeHTML(row.timestamp.replace('T',' '))} · ${row.evs_charging} EVs charged · ${fmt(row.ev_charge_kwh,2)} kWh EV draw${split}</p><div class="travel-exports">${asset(row.line_max_increase_id,`Line +${fmt(row.line_max_increase_pp,2)} pp`)}${asset(row.transformer_max_increase_id,`Transformer +${fmt(row.transformer_max_increase_pp,2)} pp`)}${asset(row.max_bus_voltage_drop_id,`Bus −${fmt(row.max_bus_voltage_drop_pu,5)} p.u.`)}</div>`;
  for(const button of $('charging-rows').querySelectorAll('button')){
    button.setAttribute('aria-current',String(Number(button.dataset.chargeIndex)===row.index));
  }
}
function renderDiagnostics(){
  const v=diagnostic?.profile_validation||{}, item=job?.series[selectedInterval];
  const prefix=gridScenario==='trading'?'trading_':gridScenario==='baseline'&&job?.baseline_totals?'baseline_':'';
  const row=prefix&&item?Object.fromEntries(Object.entries(item).filter(([key])=>key.startsWith(prefix)).map(([key,value])=>[key.slice(prefix.length),value])):item;
  const totals=gridScenario==='trading'?job?.market_summary:gridScenario==='baseline'?(job?.baseline_totals||job?.totals):job?.totals;
  const checks=[['Annual P/Q profiles',v.passed?`${v.checked} / ${v.expected} valid`:'Checking…',v.passed?'good':'warn'],['Power balance',totals?`${fmt(totals.max_balance_residual_kw,6)} kW max residual`:'Run required',totals?.balance_failures?'bad':totals?'good':'warn'],['Topology',diagnostic?`${diagnostic.unreachable_buses.length} disconnected buses / ${diagnostic.unreachable_loads.length} load objects`:'Checking…',diagnostic?.unreachable_loads.length?'bad':diagnostic?.unreachable_buses.length?'warn':'good'],['Voltage & loading',row?`${row.low_voltage_buses+row.high_voltage_buses} voltage flags · ${fmt(Math.max(row.max_line_loading_pct,row.max_transformer_loading_pct))}% max loading`:'Run required',row?(row.low_voltage_buses||row.high_voltage_buses||row.max_line_loading_pct>100||row.max_transformer_loading_pct>100?'warn':'good'):'warn']];
  $('checks').innerHTML=checks.map(([name,value,c])=>`<div class="check">${name}<strong class="${c}">${escapeHTML(value)}</strong></div>`).join('');
  if(snap&&net){
    const exceptions=[];
    net.nodes.forEach((n,i)=>{const v=snap.voltages[i];if(v!=null&&(v<.95||v>1.05))exceptions.push({id:n.id,text:v<.1?'De-energized':fmt(v,4)+' pu',value:v<.1?0:1});});
    net.elements.forEach((e,i)=>{const s=snap.elements[i];if(s.loading_pct>100)exceptions.push({id:e.id,text:fmt(s.loading_pct,1)+'% loading',value:2});});
    exceptions.sort((a,b)=>b.value-a.value);
    $('exceptions').innerHTML=exceptions.length?exceptions.map(e=>`<div class="exception" data-object="${escapeHTML(e.id)}"><span>${escapeHTML(e.id)}</span><span class="${e.value?'warn':'muted'}">${escapeHTML(e.text)}</span></div>`).join(''):'No voltage or normal-rating exceptions at this interval.';
  }
  $('debug').textContent=JSON.stringify({engine:diagnostic?.engine,profile_validation:v,assumptions:job?.assumptions,selected_interval:row,topology:{unreachable_buses:diagnostic?.unreachable_buses,unreachable_loads:diagnostic?.unreachable_loads},notes:diagnostic?.notes,error:job?.error,traceback:job?.traceback},null,2);
}
async function refreshLogs(){
  try {
    const suffix=$('only-run').checked&&job?`?run_id=${encodeURIComponent(job.id)}`:'';
    const entries=await api('/api/logs'+suffix), encoded=JSON.stringify(entries);
    if(encoded===lastLogPayload)return;
    lastLogPayload=encoded;
    const opened=new Set([...$('log-lines').querySelectorAll('details[open]')].map(e=>e.closest('[data-key]').dataset.key));
    $('log-lines').innerHTML=entries.map(e=>{
      const {time,level,message,...payload}=e;
      const key=encodeURIComponent(`${time}|${level}|${message}|${payload.run_id||''}`);
      return `<div class="log-entry" data-key="${key}"><time>${escapeHTML(time)}</time> <span class="${level==='ERROR'?'bad':level==='WARNING'?'warn':'good'}">${escapeHTML(level)}</span> ${escapeHTML(message)}${Object.keys(payload).length?`<details ${opened.has(key)?'open':''}><summary>Details</summary><pre>${escapeHTML(JSON.stringify(payload,null,2))}</pre></details>`:''}</div>`;
    }).join('')||'<span class="muted">No events yet.</span>';
  }catch(e){error(e.message);}
}
async function refreshHistory(){
  savedRuns=await api('/api/runs');
  $('history').innerHTML=savedRuns.map(run=>{
    const type=run.assumptions?.market?'EV trading comparison':run.assumptions?.ev_charging?'EV charging comparison':'Baseline';
    const pricing=run.assumptions?.market?.pricing_mode;
    const mode=pricing?` · ${pricing==='dynamic'?'Dynamic price':'Fixed price'}`:'';
    return `<div class="history-row"><span>${escapeHTML(run.date)} · ${type}${mode} · ${run.days} day(s)</span><small>${escapeHTML(run.status)} · ${run.completed}/${run.intervals}</small><small>${escapeHTML(run.id)}</small><button data-run="${escapeHTML(run.id)}">Inspect</button></div>`;
  }).join('')||'<p class="muted">Completed and interrupted runs will appear here.</p>';
  updateComparisonButton();
  return savedRuns;
}
async function chooseRun(id,{updateControls=true}={}){
  stopPlayback();job=await api('/api/runs/'+id);follow=job.status==='running';
  gridScenario=job.assumptions?.ev_charging?'charging':'baseline';
  if(updateControls){
    $('date').value=job.date;$('days').value=job.days;
    $('rollout-start').value=job.assumptions?.rollout_start??'';
  }
  snap=null;selectedInterval=0;renderJob();
  if(job.completed)await loadSnapshot(follow?job.completed-1:0);
}
async function poll(){
  try{
    const state=await api('/api/status');ready=state.ready;
    $('server-status').textContent=state.error?'Preparation failed':ready?'Engine ready':'Validating dataset…';$('status-dot').className='dot '+(state.error?'failed':ready?'ready':'');
    $('run').disabled=!ready||!!state.active||preparingEV;$('cancel').disabled=!state.active||job?.id!==state.active;
    if(state.error)error(state.error);
    if(ready&&!net){net=await api('/api/network');diagnostic=await api('/api/diagnostics');$('customers').textContent=fmt(net.customer_count_metadata,0);$('objects').textContent=`${fmt(net.load_objects,0)} load objects · ${net.feeder_count} feeders`;$('network-meta').textContent=`${fmt(net.bus_count,0)} buses · ${fmt(net.elements.length,0)} lines & transformers`;$('objects-list').innerHTML=[...net.nodes.map(n=>n.id),...net.elements.map(e=>e.id),...net.loads.map(l=>l.id)].map(id=>`<option value="${escapeHTML(id)}"></option>`).join('');drawNetwork();renderDiagnostics();const history=await refreshHistory();const linked=history.find(run=>run.id===requestedRun);if(history.length&&!job)await chooseRun(linked?.id||state.active||(history.find(r=>r.status==='completed')||history.find(r=>r.completed>0)||history[0]).id,{updateControls:Boolean(linked||state.active)});if(!job)$('progress-label').textContent='Ready for first baseline run';}
    if(ready&&net&&!fleet)await loadFleetInventory();
    else if(ready&&fleet)await syncFleetInventory(state.current_fleet);
    if(ready&&fleet)await pollTravel();
    $('run-ev').disabled=!ready||!!state.active||preparingEV||fleetBusy||travelBusy||!travelSourceReady||!fleet?.summary.ev_count;
    if(job?.status==='running'){
      const id=job.id, previous=job.completed;
      const updated=await api('/api/runs/'+id);
      if(job.id===id){
        job=updated;renderJob();
        if(follow&&job.completed>previous)await loadSnapshot(job.completed-1);
        if(job.status!=='running')await refreshHistory();
      }
    }
    await refreshLogs();
  }catch(e){error('Server connection: '+e.message);}finally{setTimeout(poll,2000);}
}
async function startGridRun(body){
  try{
    const mode=$('nuclear-mode').value;
    const fields={coal_gas_min_mw:$('coal-gas-min'),nuclear_cost_per_mwh:$('nuclear-cost'),
      coal_gas_cost_per_mwh:$('coal-gas-cost'),solar_capacity_mw:$('solar-capacity'),
      solar_performance_factor:$('solar-performance'),solar_cost_per_mwh:$('solar-cost')};
    if(mode==='manual')fields.nuclear_mw=$('nuclear-mw');
    if(!validateInputs(Object.values(fields)))return;
    body.generation=Object.fromEntries(Object.entries(fields).map(([key,field])=>[key,Number(field.value)]));
    if(mode==='floor'){
      const floor=$('generation-floor');
      const nuclear=floor.valueAsNumber-body.generation.coal_gas_min_mw;
      floor.setCustomValidity(nuclear<0?'Minimum total generation must be at least the coal + gas minimum.':'');
      if(!validateInputs([floor]))return;
      body.generation.nuclear_mw=nuclear;
    }
    error('');stopPlayback();
    const result=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    follow=true;snap=null;selectedInterval=0;job=await api('/api/runs/'+result.id);
    gridScenario=body.travel_id||body.travel_ids?'charging':'baseline';
    $('date').value=job.date;$('days').value=job.days;
    renderJob();$('run').disabled=true;$('run-ev').disabled=true;$('cancel').disabled=false;
    await refreshHistory();
  }catch(e){error(e.message);}
}
$('nuclear-mode').onchange=()=>{
  const mode=$('nuclear-mode').value;
  const manual=mode==='manual';
  $('nuclear-manual').hidden=!manual;
  $('nuclear-mw').disabled=!manual;
  $('generation-floor-field').hidden=mode!=='floor';
  $('generation-floor').disabled=mode!=='floor';
  if(manual&&!$('nuclear-mw').value&&job?.generation_summary){
    $('nuclear-mw').value=job.generation_summary.nuclear_mw;
  }
};
$('generation-floor').oninput=$('coal-gas-min').oninput=()=>$('generation-floor').setCustomValidity('');
$('run').onclick=()=>{if(validateInputs([$('date'),$('days')]))void startGridRun({date:$('date').value,days:Number($('days').value)});};
$('away-charging').onchange=()=>{$('away-charging-options').hidden=!$('away-charging').checked;};
$('market-behavior').onchange=()=>{
  const flexible=$('market-behavior').value==='buy_sell_wait';
  if($('market-reserve').value===(flexible?'20':'10'))$('market-reserve').value=flexible?'10':'20';
  if($('market-buffer').value===(flexible?'10':'5'))$('market-buffer').value=flexible?'5':'10';
};
$('run-ev').onclick=async()=>{
  if(preparingEV||!fleet?.summary.ev_count)return;
  if(fleetFormDirty){error('Generate the EV fleet to apply your fleet changes.');return;}
  const rollout=$('rollout-start'), efficiency=$('charge-efficiency'), batterySeed=$('battery-seed');
  const marketFields={trader_pct:$('market-share'),trading_only_pct:$('market-trading-only'),
    seed:$('market-seed'),discharge_efficiency:$('market-efficiency'),
    reserve_pct:$('market-reserve'),travel_buffer_pct:$('market-buffer'),export_kw:$('market-power'),
    wear_usd_per_battery_kwh:$('market-wear')};
  if(!validateInputs([rollout,efficiency,batterySeed,...Object.values(marketFields)]))return;
  if(Number(efficiency.value)<=0||Number(marketFields.discharge_efficiency.value)<=0){error('Charging and discharge efficiency must be greater than 0%.');return;}
  const enabled=$('away-charging').checked;
  const coverage=$('away-coverage'), seed=$('away-seed');
  if(enabled&&!validateInputs([coverage,seed]))return;
  const charging={seed:Number(batterySeed.value),charge_efficiency:Number(efficiency.value)/100,away_charging_enabled:enabled};
  if(coverage.validity.valid)charging.away_coverage_pct=Number(coverage.value);
  if(seed.validity.valid)charging.away_seed=Number(seed.value);
  const market=Object.fromEntries(Object.entries(marketFields).map(([key,field])=>[key,Number(field.value)]));
  market.discharge_efficiency/=100;
  market.pricing_mode=$('market-pricing').value;
  market.behavior=$('market-behavior').value;
  market.away_export=$('market-away').checked;
  market.allow_sales_despite_grid_violations=$('market-allow-grid-violations').checked;
  preparingEV=true;$('run-ev').disabled=true;$('run').disabled=true;
  $('progress-label').textContent='Preparing EV travel for the selected dates…';
  try{
    const plan=await generateTravel();
    if(plan)await startGridRun({date:plan.date,days:plan.days,travel_ids:plan.ids,
                               rollout_start:rollout.value||null,charging,market});
    if(job?.status!=='running')$('progress-label').textContent='Ready';
  }finally{preparingEV=false;}
};
$('charging-panel').onclick=async event=>{
  const interval=event.target.closest('[data-charge-index]');
  const asset=event.target.closest('[data-charge-object]');
  if(interval){follow=false;stopPlayback();await loadSnapshot(Number(interval.dataset.chargeIndex));}
  if(asset){selectObject(asset.dataset.chargeObject,{focus:true});$('selection').scrollIntoView({block:'nearest'});}
};
$('cancel').onclick=async()=>{try{await api('/api/runs/'+job.id+'/cancel',{method:'POST'});$('cancel').disabled=true;}catch(e){error(e.message);}};
$('interval').oninput=()=>{follow=false;loadSnapshot(Number($('interval').value));};
$('follow-live').onchange=()=>{follow=$('follow-live').checked;if(follow){stopPlayback();void loadSnapshot(job?.completed-1);}};
$('layer').onchange=drawNetwork;$('reset-view').onclick=fit;
$('find').onclick=()=>selectObject($('search').value.trim(),{focus:true});$('search').onkeydown=e=>{if(e.key==='Enter')$('find').click();};
function stopPlayback(){clearInterval(playing);playing=null;$('play').textContent='▶';}
$('play').onclick=()=>{if(playing){stopPlayback();return;}if(!job?.completed)return;follow=false;$('play').textContent='Ⅱ';playing=setInterval(()=>{if(selectedInterval>=job.completed-1){stopPlayback();return;}loadSnapshot(selectedInterval+1);},600);};
$('refresh-logs').onclick=refreshLogs;$('only-run').onchange=refreshLogs;
for(const tab of document.querySelectorAll('.tab'))tab.onclick=()=>{for(const b of document.querySelectorAll('.tab')){b.classList.toggle('active',b===tab);b.setAttribute('aria-selected',String(b===tab));$(b.dataset.tab).hidden=b!==tab;}if(tab.dataset.tab==='runs')refreshHistory().catch(e=>error(e.message));if(tab.dataset.tab==='logs')refreshLogs();};
$('history').onclick=e=>{const button=e.target.closest('[data-run]');if(button)chooseRun(button.dataset.run).catch(e=>error(e.message));};
$('exceptions').onclick=e=>{const item=e.target.closest('[data-object]');if(item)selectObject(item.dataset.object,{focus:true});};
const canvas=$('network');
canvas.onpointerdown=e=>{drag={x:e.offsetX,y:e.offsetY,originalX:camera.x,originalY:camera.y,moved:false};canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;const dx=e.offsetX-drag.x,dy=e.offsetY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4)drag.moved=true;camera.x=drag.originalX+dx;camera.y=drag.originalY+dy;drawNetwork();};
canvas.onpointerup=e=>{if(!drag)return;const wasDrag=drag.moved;drag=null;if(wasDrag||!net)return;const fleetBus=pickFleetMarker(e.offsetX,e.offsetY);if(fleetBus){selectObject(fleetBus);return;}let closest=null,best=8;for(const marker of transformerMarkers()){const dist=Math.hypot(marker.x-e.offsetX,marker.y-e.offsetY);if(dist<best){closest=marker.id;best=dist;}}if(closest){selectObject(closest);return;}best=9;for(const[id,p]of positions){const dist=Math.hypot(p.x-e.offsetX,p.y-e.offsetY);if(dist<best){closest=id;best=dist;}}if(!closest){best=5;for(const edge of net.edges){const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;const dx=b.x-a.x,dy=b.y-a.y,t=Math.max(0,Math.min(1,((e.offsetX-a.x)*dx+(e.offsetY-a.y)*dy)/(dx*dx+dy*dy||1)));const dist=Math.hypot(e.offsetX-a.x-t*dx,e.offsetY-a.y-t*dy);if(dist<best){closest=edge.id;best=dist;}}}if(closest)selectObject(closest);};
canvas.onwheel=e=>{e.preventDefault();const w=canvas.clientWidth,h=canvas.clientHeight,factor=Math.exp(-e.deltaY*.001),next=Math.max(.5,Math.min(30,camera.scale*factor)),ratio=next/camera.scale;camera.x=e.offsetX-w/2-(e.offsetX-w/2-camera.x)*ratio;camera.y=e.offsetY-h/2-(e.offsetY-h/2-camera.y)*ratio;camera.scale=next;drawNetwork();};
canvas.onkeydown=e=>{if(e.key==='0')fit();if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();camera.x+=e.key==='ArrowLeft'?25:e.key==='ArrowRight'?-25:0;camera.y+=e.key==='ArrowUp'?25:e.key==='ArrowDown'?-25:0;drawNetwork();}};
$('open-charging-comparison').onclick=async()=>{
  try{
    await chooseRun($('open-charging-comparison').dataset.run);
    $('power-panel').scrollIntoView({block:'nearest'});
  }catch(e){error(e.message);}
};
function selectPowerInterval(event){
  if(!job?.completed)return;
  const {left,right}=powerPlotPadding;
  const fraction=(event.offsetX-left)/(event.currentTarget.clientWidth-left-right);
  follow=false;
  stopPlayback();
  gridScenario=plotScenario(event.currentTarget.id);
  void loadSnapshot(Math.round(Math.max(0,Math.min(1,fraction))*(job.completed-1)));
}
$('chart').onclick=selectPowerInterval;
$('chart-with-ev').onclick=selectPowerInterval;
$('chart-with-trading').onclick=selectPowerInterval;
function plotScenario(id){return id.includes('trading')?'trading':['chart','generation-baseline'].includes(id)?'baseline':'charging';}
for(const id of ['generation-baseline','generation-ev','generation-trading','generation-solar'])$(id).onclick=event=>{
  if(!job?.generation_summary)return;
  const {left,right}=powerPlotPadding;
  const fraction=(event.offsetX-left)/(event.currentTarget.clientWidth-left-right);
  follow=false;
  stopPlayback();
  if(id!=='generation-solar')gridScenario=plotScenario(id);
  void loadSnapshot(Math.floor(fraction*job.completed));
};
new ResizeObserver(()=>{drawNetwork();drawChart();}).observe($('network').parentElement);
new ResizeObserver(drawChart).observe($('power-plots'));
new ResizeObserver(drawGenerationCharts).observe($('generation-plots'));
poll();
