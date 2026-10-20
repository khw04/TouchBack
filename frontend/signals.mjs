// Visualize only observations actually consumed by the server adapter.
export function signalsFor(run) {
  const sim=run?.simulation;
  const empty={beep:"—",field:"—",delta:"—",quality:"확인 전",rms:null,note:"수집된 모의 관측이 없습니다.",key:""};
  if(!sim || !sim.last_press) return empty;
  const actionId=sim.last_press.action_id;
  const items=sim.observations.filter(item=>item.action_id===actionId);
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
