const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require(process.env.GRIDLAB_PLAYWRIGHT||'playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.GRIDLAB_CHROMIUM});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050},deviceScaleFactor:1});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://localhost:8765');
    await page.waitForFunction(()=>net!==null&&document.querySelector('#map-selection'));
    const id='Transformer.sb8_uhs19_1247_trans_2';
    await page.locator('#search').fill(id);await page.locator('#find').click();
    assert.equal(await page.locator('.selected-name').textContent(),id);
    assert.equal(await page.locator('#map-selection').isVisible(),true);
    const geometry=await page.evaluate(()=>{
      const m=transformerMarkers().find(m=>m.id===selected.id);
      const canvas=document.querySelector('#network');
      const x=Math.round(m.x),y=Math.round(m.y);
      const ring=canvas.getContext('2d').getImageData(x+11,y-1,3,3).data;
      return {x:m.x,y:m.y,w:canvas.clientWidth,h:canvas.clientHeight,scale:camera.scale,
        whitePixels:Array.from({length:ring.length/4},(_,i)=>ring[4*i]>210&&ring[4*i+1]>210&&ring[4*i+2]>210).filter(Boolean).length};
    });
    assert(geometry.scale>=5);
    assert(Math.abs(geometry.x-geometry.w/2)<1&&Math.abs(geometry.y-geometry.h/2)<1);
    assert(geometry.whitePixels>0,'Selected transformer must have a visible white selection halo');
    for(const layer of ['voltage','loading','loss','type']){
      await page.locator('#layer').selectOption(layer);
      assert((await page.locator('#map-selection').textContent()).includes(id));
    }
    await page.locator('#reset-view').click();
    const target=await page.evaluate(()=>{
      const markers=transformerMarkers();
      return markers.find(m=>m.x>25&&m.y>65&&m.x<document.querySelector('#network').clientWidth-25&&m.y<document.querySelector('#network').clientHeight-25&&markers.every(other=>other.id===m.id||Math.hypot(other.x-m.x,other.y-m.y)>12));
    });
    assert(target,'Need an independently clickable transformer symbol');
    const box=await page.locator('#network').boundingBox();
    await page.mouse.click(box.x+target.x,box.y+target.y);
    assert.equal(await page.locator('.selected-name').textContent(),target.id);
    assert.equal(await page.evaluate(()=>selected.kind),'element');
    await page.locator('#search').fill(id);await page.locator('#find').click();
    await page.locator('#network').screenshot({path:path.join(__dirname,'..','runs','transformer-highlight.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#find').click();
    assert.equal(await page.locator('#map-selection').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,transformerMarkers:await page.evaluate(()=>transformerMarkers().length),searchFocus:true,visibleHalo:true,markerClick:true,persistentAcrossLayers:true,browserErrors:errors}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});