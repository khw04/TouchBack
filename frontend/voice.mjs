// Browser speech is a draft input only. It never starts a run or operates a device.
export function createVoiceInput({factory,canEdit,onDraft,onStatus,onBusy,schedule=setTimeout,cancelTimer=clearTimeout}) {
  let active=null,generation=0,timer=null;
  const clear=()=>{cancelTimer(timer);timer=null;};
  function stop(notify=true) {
    const old=active;active=null;generation++;clear();
    try{old?.abort();}catch{}
    onBusy(false);
    if(notify)onStatus("음성 입력을 중단했습니다. 목표를 직접 입력할 수 있습니다.");
  }
  function start() {
    if(!factory || !canEdit())return false;
    if(active){stop();return false;}
    const token=++generation;
    let engine;
    try{engine=factory();}catch{onStatus("음성 입력을 시작하지 못했습니다. 목표를 직접 입력하세요.");return false;}
    if(!engine || typeof engine.start!=="function" || typeof engine.abort!=="function"){onStatus("이 브라우저에서 음성 입력을 사용할 수 없습니다. 직접 입력하세요.");return false;}
    active=engine;engine.lang="ko-KR";engine.continuous=false;engine.interimResults=false;engine.maxAlternatives=1;
    engine.onresult=event=>{
      if(token!==generation || active!==engine)return;
      if(!canEdit()){stop(false);return;}
      const text=String(event.results?.[0]?.[0]?.transcript||"").trim().slice(0,500);
      stop(false);
      if(!text){onStatus("음성을 인식하지 못했습니다. 다시 시도하거나 직접 입력하세요.");return;}
      onDraft(text);
      onStatus("인식된 문장을 확인·수정한 뒤 안내 시작을 누르세요. 아직 실행하지 않았습니다.");
    };
    engine.onerror=event=>{
      if(token!==generation || active!==engine)return;
      stop(false);
      onStatus(["not-allowed","service-not-allowed","audio-capture"].includes(event.error)?"마이크/음성 인식 권한을 사용할 수 없습니다. 목표를 직접 입력하세요.":"음성 인식에 실패했습니다. 다시 시도하거나 직접 입력하세요.");
    };
    engine.onend=()=>{if(token===generation && active===engine){active=null;clear();onBusy(false);onStatus("인식된 음성이 없습니다. 다시 시도하거나 직접 입력하세요.");}};
    onBusy(true);onStatus("목표를 말씀해 주세요. 인식 후 문장을 확인해야 실행할 수 있습니다.");
    try{
      engine.start();
      if(active!==engine || token!==generation)return false;
      timer=schedule(()=>{if(token===generation && active===engine){stop(false);onStatus("음성 입력 시간이 끝났습니다. 다시 시도하거나 직접 입력하세요.");}},20000);
      timer?.unref?.();
    }catch{stop(false);onStatus("음성 입력을 시작하지 못했습니다. 목표를 직접 입력하세요.");return false;}
    return true;
  }
  return {supported:Boolean(factory),start,stop};
}
