const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require(process.env.GRIDLAB_PLAYWRIGHT||'playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.GRIDLAB_CHROMIUM});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1150}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://localhost:8765');
    await page.getByText('Engine ready',{exact:true}).waitFor({timeout:90000});
    await page.waitForFunction(()=>job?.status==='completed'&&snap?.index===0);
    assert.equal(await page.locator('#date').inputValue(),await page.evaluate(()=>job.date));
    const savedId=await page.evaluate(()=>job.id);
    await page.locator('[data-tab="logs"]').click();
    await page.locator('#log-lines details').first().locator('summary').click();
    await page.evaluate(async()=>{lastLogPayload=null;await refreshLogs();});
    assert(await page.locator('#log-lines details').first().getAttribute('open')!==null);
    await page.locator('[data-tab="diagnostics"]').click();
    await page.locator('#interval').fill('48');
    await page.waitForFunction(()=>snap?.index===48);
    await page.reload();
    await page.waitForFunction(()=>job?.status==='completed'&&snap?.index===0);
    assert.equal(await page.evaluate(()=>job.id),savedId);
    const download=await page.request.get('http://localhost:8765/api/runs/'+savedId+'/export');
    assert.equal(download.status(),200);
    assert.equal((await download.text()).trim().split('\n').length,97);
    await page.screenshot({path:path.join(__dirname,'..','runs','dashboard-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('#network').width<=400*(devicePixelRatio||1));
    await page.screenshot({path:path.join(__dirname,'..','runs','dashboard-mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    assert.equal(await page.locator('#error').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,restoredRun:savedId,exportRows:96,logsRemainExpanded:true,browserErrors:errors}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});