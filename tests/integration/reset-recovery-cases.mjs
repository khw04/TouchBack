import test from 'node:test';
import assert from 'node:assert/strict';
import {loadApp,ok,demoMetadata,activeRun} from './app-harness.mjs';
const metadata={...demoMetadata,virtual_cooktop:true,active_run_id:null,virtual_levels:{left:6,right:7}};
for(const terminal of [false,true])for(const levels of [{left:5,right:5},{left:6,right:7}])test(`lost reset reconciles confirmed idle metadata, terminal=${terminal}, left=${levels.left}`,async()=>{
 let reads=0,starts=0,resets=0;
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...metadata,virtual_levels:++reads===1?metadata.virtual_levels:levels});
  if(url==='/api/demo/device/reset'){resets++;throw new Error('lost reset response');}
  if(url==='/api/runs'){starts++;return ok({...activeRun,status:'uncertain',instruction:null});}
  return ok({...activeRun,status:'uncertain',instruction:null});
 });
 try{
  if(terminal)await app.el('goal-form').fire('submit');
  await app.el('reset-device').fire('click');assert.equal(app.el('start').disabled,true);
  await app.el('demo-reload').fire('click');
  assert.equal(app.el('start').disabled,false);assert.equal(resets,1);
  assert.equal(app.el('level-left').textContent,String(levels.left));assert.equal(app.el('level-right').textContent,String(levels.right));
  assert.equal(app.el('repeat').disabled,true);assert.equal(app.el('export-evidence').attributes.get('aria-disabled'),'true');
  assert.doesNotMatch(app.el('status').textContent,/초기화 완료/);
  await app.el('goal-form').fire('submit');assert.equal(starts,terminal?2:1);
 }finally{app.close();}
});
for(const kind of ['active','missing-id','missing-levels','invalid-levels','network'])test(`lost reset stays blocked after ${kind} metadata`,async()=>{
 let reads=0;
 const app=await loadApp(async(url)=>{
  if(url!=='/api/demo')throw new Error('lost');
  if(++reads===1)return ok(metadata);
  if(kind==='network')throw new Error('offline');
  const result={...metadata};
  if(kind==='active')result.active_run_id='run-other';
  if(kind==='missing-id')delete result.active_run_id;
  if(kind==='missing-levels')delete result.virtual_levels;
  if(kind==='invalid-levels')result.virtual_levels={left:99,right:5};
  return ok(result);
 });
 try{
  await app.el('reset-device').fire('click');await app.el('demo-reload').fire('click');
  assert.equal(app.el('start').disabled,true);assert.equal(app.el('repeat').disabled,true);
  if(kind==='active'){assert.equal(app.el('recover-run').hidden,false);assert.equal(app.el('resume-id').value,'run-other');}
 }finally{app.close();}
});
test('settings recheck cannot clear unrelated uncertain virtual input',async()=>{
 const run={...activeRun,simulation:{kind:'virtual_cooktop',levels:{left:5,right:5}}};
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok(metadata);
  if(url.endsWith('/press'))throw new Error('lost press response');
  return ok(run);
 });
 try{
  await app.el('goal-form').fire('submit');await app.el('virtual-right-increase').fire('click');
  await app.el('demo-reload').fire('click');
  assert.equal(app.el('virtual-right-increase').disabled,true);assert.equal(app.el('start').disabled,true);assert.equal(app.el('repeat').disabled,true);
 }finally{app.close();}
});
