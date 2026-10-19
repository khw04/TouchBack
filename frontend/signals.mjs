// Visualize only observations actually consumed by the server adapter.
export function signalsFor(run) {
  const sim=run?.simulation;
  const empty={beep:"—",field:"—",delta:"—",quality:"확인 전",rms:null,note:"수집된 모의 관측이 없습니다.",key:""};
  if(!sim || !sim.last_press) return empty;
  const actionId=sim.last_press.action_id;
  const items=sim.observations.filter(item=>item.action_id===actionId && (!sim.target_zone || item.device_observation.zone_id===sim.target_zone));
  if(!items.length)return {...empty,note:run.status==="observing"?"모의 센서 신호를 수집하고 있습니다.":"해당 조작의 관측이 도착하지 않았습니다."};
  const last=items.at(-1),device=last.device_observation,field=device.field_summary;
  const base=sim.baselines?.[actionId]?.device_observation?.field_summary;
  const delta=field && base && field.unit===base.unit?field.rms-base.rms:null;
  const beeps=items.every(item=>item.device_observation.beep_count!==null)?items.reduce((n,item)=>n+item.device_observation.beep_count,0):null;
  const qualities=items.map(item=>item.device_observation.quality.status);
  const quality=qualities.includes("invalid")?"불량":qualities.includes("degraded")?"저하":"정상";
  const previous=run.status==="awaiting_user" && run.instruction?.action_id!==actionId;
  return {beep:beeps===null?"확인 불가":`${beeps}회`,field:field?`${field.rms} ${field.unit}`:"관측 누락",delta:delta===null?"확인 불가":`${delta>0?"+":""}${Math.round(delta*100)/100}`,
    quality,rms:field?.rms??null,note:`${previous?"이전 조작 · ":""}수집한 모의 관측 ${items.length}건`,key:items.map(item=>item.observation_id).join(":")};
}

export function trailFor(run) {
  return (run?.events||[]).filter(item=>["instruction","tool_call","observation","recovery","verdict","error"].includes(item.kind)).slice(-3);
}

// Receipt animation is independent of the verdict and never reads simulator levels.
export function createSignalFlow() {
  let runId=null, seen=new Set(), sequence=0, view;
  const idle=()=>({phase:"idle",beep:"idle",field:"idle",sequence,
    beepLabel:"대기",fieldLabel:"대기",note:"모의 관측 수집 대기"});
  view=idle();
  function reset() { runId=null;seen.clear();sequence++;view=idle();return view; }
  function pause() { sequence++;view={...idle(),phase:"blocked",note:"상태 확인 중 · 신호 표시 보류"};return view; }
  function finish(token) {
    if(token===view.sequence && view.phase==="delivering")
      view={...view,phase:"received",beep:view.beep==="delivering"?"received":view.beep,field:view.field==="delivering"?"received":view.field,note:view.beep==="warning" || view.field==="warning"?"모의 관측 수신 · 누락/품질 이상, 성공 신호 아님":"모의 관측 전달됨 · 성공 여부는 Agent 판정 확인"};
    return view;
  }
  function update(run,{blocked=false,reducedMotion=false}={}) {
    if(runId!==run.run_id){reset();runId=run.run_id;}
    const sim=run.simulation,actionId=sim?.last_press?.action_id;
    // Only the current press's consumed Mock observations may travel to Agent.
    const items=run.source==="mock" && sim?.kind==="virtual_cooktop" && actionId
      ?sim.observations.filter(item=>item.run_id===run.run_id && item.action_id===actionId && item.device_observation.source==="mock" && (!sim.target_zone || item.device_observation.zone_id===sim.target_zone)):[];
    const ids=items.map(item=>JSON.stringify([item.action_id,item.observation_id]));
    const fresh=ids.some(id=>!seen.has(id));ids.forEach(id=>seen.add(id));
    if(["stopped","failed"].includes(run.status)) {
      view={...pause(),note:run.status==="stopped"?"실행 종료 · 신호 전달 중단":"실행 실패 · 신호 전달 중단"};return view;
    }
    if(blocked)return pause();
    if(!sim || run.source!=="mock") {view={...idle(),note:"가상 장치의 모의 관측만 표시합니다."};return view;}
    if(!items.length) {
      view={...idle(),phase:run.status==="observing"?"waiting":"idle",note:run.status==="observing"?"모의 센서 수집 중 · 아직 전달된 값 없음":"모의 관측 없음 · 성공 신호를 만들지 않습니다."};return view;
    }
    if(!fresh && view.phase==="delivering")return reducedMotion?finish(view.sequence):view;
    const signal=signalsFor(run);
    const healthy=items.every(item=>item.device_observation.quality.status==="valid");
    const beepOk=healthy && items.every(item=>item.device_observation.beep_count!==null);
    const fieldOk=healthy && items.every(item=>Boolean(item.device_observation.field_summary)) && (sim.observations.filter(item=>item.action_id===actionId).every(item=>item.device_observation.quality.status==="valid" && item.device_observation.field_summary));
    const moving=fresh && !reducedMotion && (beepOk || fieldOk);
    if(fresh)sequence++;
    view={phase:moving?"delivering":"received",sequence,
      beep:beepOk?(moving?"delivering":"received"):"warning",
      field:fieldOk?(moving?"delivering":"received"):"warning",
      beepLabel:beepOk?signal.beep:"누락·품질 확인",fieldLabel:fieldOk?(run.diagnosis?.zones?.left && run.diagnosis?.zones?.right?`좌 ${run.diagnosis.zones.left.delta} / 우 ${run.diagnosis.zones.right.delta}`:`Δ ${signal.delta}`):"누락·품질 확인",
      note:!beepOk || !fieldOk?"모의 관측 수신 · 누락/품질 이상, 성공 신호 아님":moving?"모의 관측 전달 중 · 변화 없음도 관측입니다.":"모의 관측 전달됨 · 성공 여부는 Agent 판정 확인"};
    return view;
  }
  return {update,reset,pause,finish,current:()=>view};
}
