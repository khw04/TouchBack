// Load the actual app entry point with a small DOM/transport double; no copied UI logic.
let instance = 0;
export async function loadApp(fetchImpl, search = '', {speechSupported = true} = {}) {
  const names = ['document','window','location','fetch','speechSynthesis','SpeechSynthesisUtterance','setInterval','clearInterval'];
  const previous = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const spoken = [];
  const pollers = new Map();
  let cancelled = 0;
  const synth = {cancel(){cancelled++;},speak(value){spoken.push(value.text);}};
  const Utterance = class { constructor(text){this.text=text;} };
  class Element {
    constructor(id = '') {
      Object.assign(this, {id,hidden:false,disabled:false,value:'',textContent:'',checked:false,children:[],listeners:new Map(),attributes:new Map()});
    }
    addEventListener(name, handler) { this.listeners.set(name, handler); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    focus() {}
    requestSubmit(){return this.fire("submit");}
    async fire(name, extra = {}) {
      await this.listeners.get(name)?.({preventDefault(){},currentTarget:this,...extra});
      await flush();
    }
  }
  const nodes = new Map();
  const el = id => { if (!nodes.has(id)) nodes.set(id,new Element(id)); return nodes.get(id); };
  el('user-input').value = '오른쪽 화구 화력을 한 단계 올려줘';
  el('source').value = 'mock'; el('zone').value = ''; el('auto-speech').checked = true;
  el('demo-controls').hidden = true;
  Object.assign(globalThis, {
    document:{getElementById:el,createElement:()=>new Element(),documentElement:{classList:{toggle(){return true;}}}},
    window:speechSupported ? {speechSynthesis:synth,SpeechSynthesisUtterance:Utterance} : {},location:{search},fetch:fetchImpl,
    speechSynthesis:synth, SpeechSynthesisUtterance:Utterance,
    setInterval:callback=>{const id=Symbol('poll');pollers.set(id,callback);return id;},clearInterval:id=>pollers.delete(id),
  });
  try {
    await import(`../../frontend/app.mjs?app-test=${++instance}`);
    await flush();
  } catch(error) {
    restore(); throw error;
  }
  function restore() {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis,name,descriptor);
      else delete globalThis[name];
    }
  }
  return {el,spoken,cancelCount:()=>cancelled,poll:async()=>{for(const callback of [...pollers.values()])await callback();await flush();},close:restore};
}
export async function flush() {
  for(let i=0;i<3;i++) await new Promise(resolve=>setImmediate(resolve));
}
export function ok(data) { return {ok:true,status:200,json:async()=>data}; }
export const demoMetadata = {
  enabled:true,source:'mock',execution_mode:'mock_llm',scenario:'normal',scenarios:['normal','recovery'],physical_validation_performed:false,
};
export const activeRun = {
  run_id:'run-test',status:'awaiting_user',execution_mode:'mock_llm',source:'mock',verdict:null,
  instruction:{action_id:'act-1',text:'오른쪽 화구의 + 버튼을 한 번 눌러 주세요.'},
  events:[{event_id:'evt-1',run_id:'run-test',action_id:'act-1',kind:'instruction',timestamp:'2026-10-18T00:00:00Z',source:'mock',message:'오른쪽 화구의 + 버튼을 한 번 눌러 주세요.',evidence_ids:[]}],
};
