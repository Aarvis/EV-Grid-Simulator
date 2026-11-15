'use strict';
const scenarioLabels={baseline:'Baseline',charging:'EV charging only',trading:'EV trading'};
let billsRun=null, dailyBills=null, billDatesKey=null;

function renderMarket(){
  const billing=job?.billing_summary, trading=job?.market_summary;
  $('market-panel').hidden=!billing;
  if(!billing){billsRun=null;dailyBills=null;billDatesKey=null;return;}
  const id=job.id;
  const dynamic=job.assumptions?.market?.pricing_mode==='dynamic';
  const behavior=job.assumptions?.market?.behavior==='buy_sell_wait'?'Buy, sell, or wait':'Sell with automatic charging';
  const tradingOnly=job.assumptions?.trading_only_vehicle_ids;
  const multi=Boolean(job.assumptions?.travel_ids);
  const days=Object.entries(job.daily_summaries||{});
  const ready=!multi||days.length>0;
  const datesKey=id+':'+days.map(([day])=>day).join(',');
  if(billDatesKey!==datesKey){
    const day=$('bill-day'), selected=billDatesKey?.startsWith(id+':')?day.value:'';
    day.replaceChildren(new Option('Run total',''),...days.map(([date])=>new Option(date,date)));
    day.value=selected;
    const select=$('bill-scenario'), previous=select.value, cases=Object.keys(billing);
    select.replaceChildren(...cases.map(key=>new Option(scenarioLabels[key],key)));
    select.value=cases.includes(previous)?previous:cases.at(-1);
    billDatesKey=datesKey;
  }
  $('market-comparison').hidden=!trading;
  $('bill-day-field').hidden=!multi||!ready;
  $('bills-export').hidden=!ready;
  const day=$('bill-day').value, suffix=day?'?day='+day:'';
  $('bills-export').href=`/api/runs/${id}/bills${suffix}`;
  $('daily-comparison').hidden=!days.length;
  $('daily-rows').innerHTML=days.flatMap(([date, details])=>Object.entries(details.scenarios).map(([scenario, stats])=>{
    const opening=details.opening[scenario], closing=details.closing[scenario];
    const values=[stats.nuclear_mwh,stats.coal_gas_mwh,stats.solar_mwh??0,stats.generation_cost_usd,stats.ev_import_kwh,
      stats.ev_export_kwh,opening?.battery_kwh,closing?.battery_kwh,stats.ev_unmet_driving_kwh];
    const ledger=opening?`${fmt(opening.battery_value_usd,2)} → ${fmt(closing.battery_value_usd,2)}`:'—';
    const offers=opening?`${fmt(opening.offer_per_mwh,2)} → ${fmt(closing.offer_per_mwh,2)}`:'—';
    return `<tr><td>${date}${details.intervals<96?' (partial)':''}</td><th scope="row">${scenarioLabels[scenario]}</th>${values.map(value=>`<td>${fmt(value,2)}</td>`).join('')}<td>${ledger}</td><td>${offers}</td></tr>`;
  })).join('');
  $('market-summary').textContent=trading
    ?`${job.date} · ${behavior} · ${dynamic?'Dynamic price per EV':'Fixed price per interval'} · ${trading.traders} trading EVs · ${trading.passive} passive EVs${tradingOnly?` · ${tradingOnly.length} trading EVs staying home`:''} · ${fmt(trading.curtailed_export_kwh,2)} kWh of proposed exports curtailed${trading.wear_usd>0?` · $${fmt(trading.wear_usd,2)} modeled wear`:''}`
    :`${job.date} · Baseline customer bills`;
  if(trading){
    const allowViolations=job.assumptions?.market?.allow_sales_despite_grid_violations;
    $('market-grid-policy').textContent=allowViolations
      ?`Grid violations do not curtail EV sales. Trading results show ${trading.overload_intervals} intervals with equipment overloads and ${trading.voltage_violation_intervals} with voltage violations. Upstream export: ${fmt(trading.upstream_export_kwh,2)} kWh.`
      :'EV exports are screened against the charging-only case. A failed check cancels all sales for that interval. Purchases stay in place.';
    const gen=job.generation_summary;
    const cases=[['baseline',0,0,job.baseline_totals.loss_kwh,null,gen.without_ev],
      ['charging',job.totals.ev_charge_kwh,0,job.totals.loss_kwh,job.totals.ev_battery_end_kwh,gen.with_ev],
      ['trading',trading.ev_import_kwh,trading.ev_export_kwh,trading.loss_kwh,trading.ev_battery_end_kwh,gen.with_trading]];
    $('market-daily').innerHTML=cases.map(([key,buy,sell,loss,stored,generation])=>{
      const total=billing[key].net_usd;
      return `<tr><th scope="row">${scenarioLabels[key]}</th>${[buy,sell,loss,stored,
        billing[key].vehicle_net_usd,total,generation.generation_cost_usd-total].map(value=>`<td>${fmt(value,2)}</td>`).join('')}</tr>`;
    }).join('');
    $('market-intervals').innerHTML=job.series.map(row=>{
      const opening=row.trading_opening_bid_per_mwh??row.trading_locked_price_per_mwh;
      const values=[opening,row.trading_closing_bid_per_mwh??opening,row.trading_average_generation_cost_per_mwh,
        row.trading_passive_import_kwh,row.trading_ev_import_kwh-row.trading_passive_import_kwh,
        row.trading_ev_export_kwh,row.trading_curtailed_export_kwh,
        row.trading_upstream_export_kwh??(row.trading_supply_kw==null?null:Math.max(0,-row.trading_supply_kw)*.25)];
      return `<tr><td>${escapeHTML(row.timestamp.replace('T',' '))}</td>${values.map(value=>`<td>${fmt(value,3)}</td>`).join('')}</tr>`;
    }).join('');
    const latest=job.series.at(-1);
    const quotes=dynamic&&latest?.trading_opening_bid_per_mwh!=null
      ?`Opening $${fmt(latest.trading_opening_bid_per_mwh,2)} → closing $${fmt(latest.trading_closing_bid_per_mwh,2)}/MWh`
      :`Locked $${fmt(latest?.trading_locked_price_per_mwh,2)}/MWh`;
    $('market-latest').textContent=latest?`${latest.timestamp.replace('T',' ')} · ${quotes} · Realized $${fmt(latest.trading_average_generation_cost_per_mwh,2)}/MWh · ${fmt(latest.trading_ev_import_kwh,2)} kWh bought · ${fmt(latest.trading_ev_export_kwh,2)} kWh sold`:'';
  }
  const key=datesKey+':'+day;
  if(ready&&billsRun!==key)void loadBills(id,key,suffix);
  if(!ready){
    billsRun=null;dailyBills=null;$('bill-rows').replaceChildren();
    $('bill-status').textContent='Account bills will appear when the first day settles.';
  }
}

async function loadBills(id,key,suffix){
  billsRun=key;dailyBills=null;
  $('bill-rows').replaceChildren();
  $('bill-status').textContent='Loading daily account bills…';
  try{
    const data=await api(`/api/runs/${id}/bills${suffix}`);
    if(billsRun!==key||job?.id!==id)return;
    dailyBills=data;renderBills();
  }catch(e){if(billsRun===key)$('bill-status').textContent=e.message;}
}

function renderBills(){
  if(!dailyBills)return;
  const kind=$('bill-kind').value, scenario=$('bill-scenario').value;
  const query=$('bill-filter').value.trim().toLowerCase();
  const rows=dailyBills[scenario][kind].filter(row=>[row.id,row.bus,row.household_id].some(value=>value?.toLowerCase().includes(query)));
  const fields=['import_kwh','export_kwh','energy_cost_usd','loss_kwh','loss_cost_usd','export_credit_usd','net_usd'];
  $('bill-status').textContent=`${rows.length} accounts · ${$('bill-day').value||'Total for available days'} · After loss charges and export credits`;
  $('bill-rows').innerHTML=rows.map(row=>`<tr><th scope="row">${escapeHTML(row.id)}${kind==='vehicles'?` · ${row.trader?'Trading':'Passive'}`:''}</th>${fields.map(key=>`<td>${fmt(row[key],3)}</td>`).join('')}</tr>`).join('')||'<tr><td colspan="8">No matching accounts.</td></tr>';
}

document.addEventListener('DOMContentLoaded',()=>{
  $('bill-scenario').onchange=renderBills;
  $('bill-day').onchange=renderMarket;
  $('bill-kind').onchange=renderBills;
  $('bill-filter').oninput=renderBills;
});
