import { signalsFor, trailFor, createSignalFlow } from "./signals.mjs?v=21";
import { screenSummary } from "./screen.mjs?v=20";
import { demoLabel, demoControlsDisabled } from "./demo.mjs?v=22";
import { createApi, freshSpeechItems, TERMINAL } from "./api.mjs?v=22";
import { createFixture } from "./fixtures.mjs?v=20";
import { actionFor, claimableSuccess, phaseFor, unverifiedSnapshot, verdictLabel } from "./presentation.mjs?v=22";
import { eventChange, eventLabel, eventSource } from "./events.mjs?v=20";
import { canSendAction, connectionLabel } from "./connection.mjs?v=20";
import { evidenceReport, evidenceFilename } from "./evidence.mjs?v=20";
import { createReadGate, regressesTerminalState } from "./read-gate.mjs?v=20";

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const fixtureMode = params.get("fixture") === "1";
const api = fixtureMode ? createFixture() : createApi(params.get("api") || "");
const state = { runId: null, run: null, busy: false, connection: "unknown", lastConfirmedAt: null,
  startUnknown: false, seen: new Set(), renderedEventIds: [], lastSpoken: "", polling: null, submittedRequest: null };
const observationRequests = new Map();
const MAX_OBSERVATION_ATTEMPTS = 2; // Initial delivery + one explicit, idempotent retry.
state.cancelRequestedRunId = null;
const cancellingCurrentRun = () => Boolean(state.runId && state.cancelRequestedRunId === state.runId);
state.demo = null;
state.demoInfo = null;
state.demoUnverified = !fixtureMode;
state.demoLoading = false;
state.recoverRunId = null;
const reads = createReadGate(() => state.runId);
const signalFlow=createSignalFlow();
// Capture nodes so a finishing animation never depends on a later global page.
const flowNodes={rail:$("signal-flow"),beep:$("flow-beep-label"),field:$("flow-field-label"),status:$("flow-status")};
let flowTimer=null,flowSequence=null;
function paintFlow(view) {
  for(const name of ["phase","beep","field"])flowNodes.rail.setAttribute("data-"+name,view[name]);
  flowNodes.rail.setAttribute("data-sequence",String(view.sequence%2));
  flowNodes.beep.textContent=view.beepLabel;flowNodes.field.textContent=view.fieldLabel;
  if(flowNodes.status.textContent!==view.note)flowNodes.status.textContent=view.note;
  if(view.phase!=="delivering" || flowSequence!==view.sequence) {
    clearTimeout(flowTimer);flowTimer=null;flowSequence=view.sequence;
    if(view.phase==="delivering") {
      const token=view.sequence;
      flowTimer=setTimeout(()=>paintFlow(signalFlow.finish(token)),1800);
      flowTimer.unref?.();
    }
  }
}
function renderFlow(run,blocked) {
  paintFlow(signalFlow.update(run,{blocked,reducedMotion:window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false}));
}

const statusText = {
  planning: "계획 중", awaiting_clarification: "추가 확인 필요", awaiting_user: "사용자 조작 대기",
  observing: "센서 관측 중", recovering: "복구 안내 준비 중", succeeded: "서버 판정: 목표 확인",
  uncertain: "불확실: 결과를 확인할 수 없음", stopped: "실행 중단", failed: "실행 실패",
};
const modeText = { backend_stub: "백엔드 준비 중", mock_llm: "모의 모델", real_llm: "실제 모델", ui_fixture: "UI 화면 예시" };
const sourceText = { mock: "모의 관측", replay: "녹화 재생", live: "실시간 센서" };
const fixtureScenarioText = { normal: "정상 흐름", recovery: "미인식 후 복구", sensor_error: "센서 오류" };
const speechAvailable = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
$("fixture-options").hidden = !fixtureMode;
$("resume-form").hidden = fixtureMode;

function controls() {
  const run = state.run;
  const cannotEdit = state.busy || state.startUnknown || Boolean(state.recoverRunId) || Boolean(run && !TERMINAL.has(run.status));
  $("recover-run").hidden = !state.recoverRunId;
  $("recover-run").disabled = state.busy || state.demoLoading;
  $("cancel").textContent = run?.status === "awaiting_clarification" ? "입력 수정하기" : "실행 취소";
  $("restart").disabled = state.busy || state.startUnknown || Boolean(state.recoverRunId) || state.demoUnverified || !run || !TERMINAL.has(run.status) || (!fixtureMode && state.connection!=="online");
  $("preset-right").disabled = cannotEdit;
  $("preset-left").disabled = cannotEdit;
  $("demo-apply").disabled = !state.demoInfo || demoControlsDisabled(state);
  $("demo-execution-mode").disabled = !state.demoInfo || demoControlsDisabled(state);
  $("demo-scenario").disabled = !state.demoInfo || state.demoUnverified || demoControlsDisabled(state);
  $("demo-reload").disabled = state.busy || state.demoLoading;
  const virtual = Boolean(state.demo?.virtual_cooktop || run?.simulation?.kind === "virtual_cooktop");
  $("ack").hidden = virtual;
  for (const zone of ["left","right"]) for (const operation of ["increase","decrease"]) {
    $("virtual-"+zone+"-"+operation).disabled = !virtual || !run?.simulation || state.busy || cancellingCurrentRun() || state.demoUnverified || state.connection !== "online" || run.status !== "awaiting_user";
  }
  const writable = canSendAction(fixtureMode ? "fixture" : "server", state.connection);
  $("start").disabled = Boolean(state.recoverRunId) || Boolean(state.demoInfo?.manageable && (!state.demo || !state.demo.virtual_cooktop || state.demoInfo.execution_mode === "real_llm" && state.demoInfo.api_key_configured === false)) || state.busy || state.startUnknown || state.demoUnverified || ["offline", "invalid", "unverified"].includes(state.connection) || Boolean(run && !TERMINAL.has(run.status));
  $("ack").disabled = state.busy || cancellingCurrentRun() || state.demoUnverified || !writable || !run?.instruction?.action_id || run.status !== "awaiting_user";
  $("cancel").disabled = state.busy || !writable || !run || TERMINAL.has(run.status);
  $("retry").disabled = state.busy || !state.runId || fixtureMode;
  $("repeat").disabled = state.busy || cancellingCurrentRun() || state.demoUnverified || !speechAvailable || !state.lastSpoken || (!fixtureMode && ["offline", "invalid", "unverified"].includes(state.connection));
  $("resume").disabled = fixtureMode || state.busy || Boolean(run && !TERMINAL.has(run.status));
  $("export-evidence").setAttribute("aria-disabled", String(!run?.run_id));
  if (run?.run_id) $("export-evidence").setAttribute("href", "#");
  else $("export-evidence").removeAttribute("href");
}

function showConnection() {
  const label = connectionLabel(fixtureMode ? "fixture" : "server", state.connection);
  if ($("connection").textContent !== label) $("connection").textContent = label;
  $("last-confirmed").textContent = fixtureMode ? "마지막 서버 확인: 해당 없음" : state.lastConfirmedAt
    ? `마지막 상태 확인: ${new Date(state.lastConfirmedAt).toLocaleTimeString("ko-KR")}` : "마지막 상태 확인: 없음";
  controls();
}

function markOffline(message) {
  state.connection = "offline";
  showConnection();
  maskUnverifiedSnapshot();
  showError(message);
}

function markInvalid(message) {
  state.connection = "invalid";
  showConnection();
  maskUnverifiedSnapshot();
  showError(message);
}

function markUnverified(message) {
  state.connection = "unverified";
  showConnection();
  maskUnverifiedSnapshot();
  showError(message);
}

function maskUnverifiedSnapshot() {
  paintFlow(signalFlow.pause());
  if (fixtureMode || !state.runId) return;
  if (speechAvailable) speechSynthesis.cancel();
  const snapshot = unverifiedSnapshot();
  $("status").textContent = snapshot.status;
  $("phase").textContent = snapshot.phase;
  $("phase-detail").textContent = snapshot.detail;
  $("instruction").textContent = snapshot.action;
  $("verdict").textContent = snapshot.verdict;
  $("agent-stage").textContent = "현재 상태 확인 필요";
  $("sensor-stage").textContent = "현재 관측 확인 불가";
  $("agent-stage-detail").textContent = "상태 다시 확인이 성공할 때까지 이전 결과를 현재 결과로 사용하지 마세요.";
  $("result-summary").textContent = snapshot.verdict;
  $("signal-note").textContent="현재 수치는 마지막 서버 응답이며 최신 상태가 아닐 수 있습니다.";
  $("field-meter").hidden=true;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
  controls();
}
function clearError() { $("error").hidden = true; $("error").textContent = ""; }
function speak(message) {
  if (!speechAvailable || !message || state.demoUnverified || cancellingCurrentRun()) return;
  if(state.run?.source === "mock" && !message.startsWith("모의 시연.")) message = `모의 시연. ${message}`;
  state.lastSpoken = message;
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(message);
  utterance.lang = "ko-KR";
  speechSynthesis.speak(utterance);
  controls();
}
function renderEvents(items) {
  const change = eventChange(state.renderedEventIds, items);
  const list = $("events");
  if (change.reset || !state.renderedEventIds.length) list.replaceChildren();
  $("event-count").textContent = `${items.length}건`;
  if (!items.length) {
    const li = document.createElement("li"); li.textContent = "아직 기록이 없습니다.";
    list.replaceChildren(li);
  }
  for (const item of items.slice(change.from)) {
    const li = document.createElement("li");
    li.className = "event-card";
    const heading = document.createElement("div"); heading.className = "event-heading";
    const label = document.createElement("strong"); label.textContent = eventLabel(item.kind);
    const time = document.createElement("time");
    const parsed = Date.parse(item.timestamp || "");
    time.textContent = Number.isNaN(parsed) ? "시간 미상" : new Date(parsed).toLocaleTimeString("ko-KR");
    if (!Number.isNaN(parsed)) time.dateTime = item.timestamp;
    heading.append(label, time);
    const message = document.createElement("p"); message.textContent = item.message || "내용 없음";
    const detail = document.createElement("details");
    const summary = document.createElement("summary"); summary.textContent = "근거 자세히 보기";
    const metadata = document.createElement("p");
    metadata.textContent = `입력 출처: ${eventSource(item.source)} · 이벤트 ID: ${item.event_id || "없음"} · 조작 ID: ${item.action_id || "없음"} · 근거 ID: ${(item.evidence_ids || []).join(", ") || "없음"}`;
    detail.append(summary, metadata);
    li.append(heading, message, detail);
    list.append(li);
  }
  state.renderedEventIds = change.ids;
}
function renderScreen(run) {
  const signal=signalsFor(run);
  for(const name of ["beep","field","delta","quality","note"]) $("signal-"+name).textContent=signal[name];
  $("field-meter").hidden=signal.rms===null;
  if(signal.rms!==null) $("field-meter").value=signal.rms;
  $("signal-note").setAttribute("data-state",run.status);
  $("agent-trail").replaceChildren(...trailFor(run).map(item=>{const li=document.createElement("li");li.textContent=`${eventLabel(item.kind)} · ${item.message}`;return li;}));
  const sim=run.simulation;
  for(const zone of ["left","right"]) $("level-"+zone).textContent=sim?String(sim.levels[zone]):"—";
  $("cooktop-note").textContent=sim?"가상 장치 내부 화력 · Agent는 이 숫자를 읽지 않고 모의 센서 관측으로 판정합니다.":"화면에서 시연을 활성화하면 가상 버튼을 조작할 수 있습니다.";
  const summary = screenSummary(run);
  const blocked = state.demoUnverified || cancellingCurrentRun() && !TERMINAL.has(run.status);
  renderFlow(run,blocked || cancellingCurrentRun() || state.connection!=="online");
  if(blocked) {$("signal-note").textContent="설정·취소 결과 확인 중 · 표시된 수치는 마지막 관측입니다.";$("field-meter").hidden=true;}
  $("agent-stage").textContent = blocked ? "조작 안내 보류 중" : summary.agent;
  $("sensor-stage").textContent = blocked ? "상태 확인 대기" : summary.sensor;
  $("agent-stage-detail").textContent = summary.detail;
  $("result-summary").textContent = blocked ? "현재 설정·취소 결과를 확인한 뒤 판정을 확인하세요." : summary.result;
  $("screen-mode").textContent = modeText[run.execution_mode] || "모델 확인 전";
  $("screen-source").textContent = run.execution_mode === "ui_fixture" ? "화면 연습 · 서버 판정 없음" : `${sourceText[run.source] || "입력 미확인"} · 실물 미검증`;
}
function renderAction(run) {
  renderScreen(run);
  if (cancellingCurrentRun()) {
    state.lastSpoken = "";
    if (speechAvailable) speechSynthesis.cancel();
    renderScreen(run);
    if (!TERMINAL.has(run.status)) {
      $("phase").textContent = "취소 요청 확인 중";
      $("phase-detail").textContent = "추가 조작을 멈추세요. 서버의 취소 결과를 확인하고 있습니다.";
      $("instruction").textContent = "취소 확인 전까지 새 조작 안내를 보류합니다.";
      return;
    }
  }
  if (state.demoUnverified) {
    $("phase").textContent = "실행 설정 확인 대기";
    $("phase-detail").textContent = "설정 확인 전에는 실물을 조작하지 마세요. 시연 설정 다시 확인을 누르세요.";
    $("instruction").textContent = "실행 설정을 확인할 때까지 조작 안내를 보류합니다.";
    state.lastSpoken = "";
    if (speechAvailable) speechSynthesis.cancel();
    return;
  }
  const [phase, detail] = phaseFor(run);
  $("phase").textContent = phase;
  $("phase-detail").textContent = state.demo && run.source === "mock" && run.status === "awaiting_user"
    ? run.simulation ? "가상 인덕션에서 안내한 화구의 버튼을 누르세요. 모의 센서 관측과 판정이 이어집니다." : "실물은 조작하지 말고, 안내를 확인한 뒤 완료 알림을 눌러 모의 관측을 진행하세요." : detail;
  $("instruction").textContent = actionFor(run);
  if (state.demo && run.source === "mock" && TERMINAL.has(run.status)) {
    $("instruction").textContent = screenSummary(run).result;
    $("phase-detail").textContent = "모의 입력 시연의 실행 결과입니다. 실제 인덕션 성능을 검증한 결과는 아닙니다.";
  }
}
function render(run, announce = true) {
  const previousAction = state.run?.status === "awaiting_user" ? state.run.instruction?.action_id : null;
  const currentAction = run.status === "awaiting_user" ? run.instruction?.action_id : null;
  if (previousAction && previousAction !== currentAction) {
    if (speechAvailable) speechSynthesis.cancel();
    state.lastSpoken = "";
  }
  if(state.recoverRunId===run.run_id)state.recoverRunId=null;
  state.run = run;
  state.runId = run.run_id;
  if (!fixtureMode) { state.connection = "online"; state.lastConfirmedAt = Date.now(); }
  showConnection();
  const isFixture = fixtureMode || run.execution_mode === "ui_fixture";
  $("origin").textContent = isFixture ? "UI FIXTURE · 서버 판정 없음" : "서버 응답";
  $("mode").textContent = `실행 방식: ${modeText[run.execution_mode] || run.execution_mode || "확인 전"}`;
  $("run-id").textContent = `실행 ID: ${run.run_id || "없음"}`;
  $("fixture-active").hidden = !isFixture || !run.scenario;
  if (isFixture && run.scenario) $("fixture-active").textContent = `화면 연습: ${fixtureScenarioText[run.scenario] || run.scenario}`;
  const stateLabel = run.status === "succeeded" && !claimableSuccess(run) ? "서버 응답: 성공 근거 부족"
    : statusText[run.status] || run.status || "상태 확인 중";
  const nextStatus = cancellingCurrentRun() && !TERMINAL.has(run.status)
    ? "취소 요청 확인 중 · 추가 조작 보류" : `${stateLabel} · 입력 출처: ${sourceText[run.source] || run.source || "확인 전"}`;
  // Repeated polls should not retrigger the screen reader's live region.
  if ($("status").textContent !== nextStatus) $("status").textContent = nextStatus;
  renderAction(run);
  $("verdict").textContent = verdictLabel(run);
  const items = Array.isArray(run.events) ? run.events : [];
  renderEvents(items);
  renderScreen(run);
  const speech = state.demoUnverified || cancellingCurrentRun() ? { ids: [], messages: [] } : freshSpeechItems(state.seen, run, claimableSuccess(run));
  speech.ids.forEach(id => state.seen.add(id));
  if (speech.messages.length) {
    state.lastSpoken = speech.messages.at(-1).message;
    if (announce && $("auto-speech").checked) speak(state.lastSpoken);
  }
  if (TERMINAL.has(run.status)) stopPolling();
  else if (!fixtureMode && run.status === "observing") observe(run);
  controls();
}
function observe(run, retry = false) {
  if (cancellingCurrentRun()) return;
  const actionId = run.instruction?.action_id;
  if (!actionId) { markInvalid("관측할 조작 ID가 없습니다. 상태를 다시 확인하세요."); return; }
  const key = `${run.run_id}:${actionId}`;
  const request = observationRequests.get(key) || { pending: false, completed: false, attempts: 0 };
  if (request.pending || request.completed) return;
  if (request.attempts >= MAX_OBSERVATION_ATTEMPTS) {
    if (retry) showError("이 조작의 관측 전송 재시도 한도에 도달했습니다. 상태 조회로 서버의 종료 결과를 확인하거나 실행을 취소하세요.");
    return;
  }
  if (request.attempts && !retry) return;
  request.pending = true;
  request.attempts += 1;
  observationRequests.set(key, request);
  clearError();
  api.step(run.run_id, actionId).then(result => {
    request.completed = true;
    if (state.runId !== run.run_id || state.run?.status !== "observing" ||
        state.run.instruction?.action_id !== actionId) return;
    reads.invalidate();
    render(result);
    clearError();
  }).catch(error => {
    if (state.runId === run.run_id && state.run?.status === "observing" && !cancellingCurrentRun()) {
      markUnverified(`관측 요청 결과를 확인할 수 없습니다. 상태 다시 확인을 누르면 현재 조작을 확인한 뒤 전송을 한 번 재시도합니다. ${error.message}`);
    }
  }).finally(() => { request.pending = false; });
}
function observationWarning(run) {
  const request = observationRequests.get(`${run.run_id}:${run.instruction?.action_id}`);
  if (run.status !== "observing" || !request?.attempts || request.pending || request.completed || cancellingCurrentRun()) return "";
  return request.attempts >= MAX_OBSERVATION_ATTEMPTS
    ? "관측 전송 재시도 한도에 도달했습니다. 서버 종료 결과를 확인하거나 실행을 취소하세요."
    : "관측 전송 결과를 확인하지 못했습니다. 상태 다시 확인을 누르면 같은 조작의 전송을 한 번 재시도합니다.";
}
function renderDemo(info) {
  state.demoInfo = info;
  if(Object.hasOwn(info,"active_run_id")) {
    state.recoverRunId=info.active_run_id && info.active_run_id!==state.runId?info.active_run_id:null;
    if(state.recoverRunId) {
      $("resume-id").value=state.recoverRunId;
      $("status").textContent="진행 중인 실행이 있습니다. 불러온 뒤 종료하거나 이어서 진행하세요.";
    }
  }
  $("demo-execution-mode").value = ["mock_llm", "real_llm"].includes(info.execution_mode) ? info.execution_mode : "mock_llm";
  $("real-mode-option").disabled = info.api_key_configured === false;
  $("api-readiness").textContent = info.api_key_configured ? `API 키 설정됨 · 모델 ${info.model || "서버 설정"}. 실제 호출 성공은 실행 기록에서 확인하세요.` : "실제 AI 키 미설정 · 서버 .env에 키를 넣고 재시작하세요. 지금은 Mock LLM으로 연습할 수 있습니다.";
  if (info.manageable) {
    $("source").value = "mock"; $("source").disabled = true;
    $("demo-scenario").replaceChildren(...info.scenarios.map(name => {const option=document.createElement("option"); option.value=name; option.textContent=demoLabel(name); return option;}));
    $("demo-scenario").value = info.scenario;
  }
  state.demo = info.enabled ? info : null;
  state.demoUnverified = false;
  $("screen-mode").textContent = info.enabled ? modeText[info.execution_mode] : "시연 모드 꺼짐";
  $("screen-source").textContent = info.enabled ? "모의 센서 · 실물 미검증" : "입력 출처는 시연 설정에서 확인";
  $("demo-controls").hidden = !info.enabled && !info.manageable;
  if (info.enabled) {
    $("page-title").textContent = "센서 없는 Agent 시연";
    $("page-description").textContent = "Mock 관측을 실제 실행 API와 검증·복구 Skill로 처리합니다. 실제 센서 측정이나 실물 성능 검증이 아닙니다.";
    $("ack").textContent = "모의 조작 확인";
    $("demo-mode").textContent = `서버 설정: ${modeText[info.execution_mode]} · 입력: 모의 관측 · 실물 미검증`;
    $("source").value = "mock";
    $("source").disabled = true;
    $("observation-hint").textContent = info.virtual_cooktop ? "가상 버튼 클릭 후 모의 관측을 확인합니다. 실제 가열은 없습니다." : "완료 알림은 합성 관측을 진행합니다. Mock 판정은 실제 인덕션 성공이 아닙니다.";
    $("demo-scenario").replaceChildren(...info.scenarios.map(name => {
      const option = document.createElement("option"); option.value = name; option.textContent = demoLabel(name); return option;
    }));
    $("demo-scenario").value = info.scenario;
    $("demo-status").textContent = `적용된 모의 입력: ${demoLabel(info.scenario)}. 실행 중에는 변경할 수 없습니다.`;
  } else if (info.manageable) {
    $("settings-panel").open = true;
    $("demo-status").textContent = "AI 실행 방식을 선택하고 시연 설정 적용을 누르세요. 모의 센서를 사용합니다.";
    $("demo-mode").textContent = "로컬 시연 준비 · 아직 활성화하지 않았습니다.";
    $("page-title").textContent = "시연을 준비하세요";
    $("page-description").textContent = "시연 설정에서 실행 방식을 선택하면 모의 센서로 시작할 수 있습니다.";
  } else {
    $("source").disabled = false;
    $("ack").textContent = "조작 완료 알림";
    $("page-title").textContent = "조작한 뒤, 실제 결과를 확인하세요";
    $("page-description").textContent = "사용자가 직접 인덕션을 조작합니다. 화면은 서버가 보고한 안내와 관측 결과를 보여줍니다.";
    $("observation-hint").textContent = "조작 완료 알림은 성공 판정이 아닙니다. 센서 관측과 서버 판정을 기다리세요.";
  }
  if (info.manageable && info.enabled && (!info.virtual_cooktop || info.execution_mode === "real_llm" && !info.api_key_configured)) {
    $("settings-panel").open = true;
    $("demo-status").textContent = "가상 인덕션 시연을 사용할 AI 방식을 선택하고 시연 설정 적용을 누르세요. 실제 AI는 서버 API 키가 필요합니다.";
  }
  if (state.run && state.connection === "online") renderAction(state.run);
  controls();
}
async function loadDemo() {
  if (state.demoLoading) return;
  state.demoLoading = true;
  state.demoUnverified = true;
  $("demo-controls").hidden = false;
  $("demo-status").textContent = "실행 설정을 확인하는 중입니다. 확인 전에는 실물을 조작하지 마세요.";
  if (!state.demo) {
    $("demo-mode").textContent = "모델·입력 방식: 확인 전";
    $("page-title").textContent = "실행 설정을 확인하세요";
    $("page-description").textContent = "모델과 입력 방식을 확인한 뒤 실행할 수 있습니다. 확인 전에는 실물을 조작하지 마세요.";
  }
  if (state.run && state.connection === "online") renderAction(state.run);
  controls();
  try { renderDemo(await api.demoInfo()); }
  catch {
    $("demo-controls").hidden = false;
    $("settings-panel").open = true;
    $("screen-mode").textContent = "설정 확인 실패";
    $("demo-status").textContent = "시연 설정을 확인하지 못했습니다. 시연 설정 다시 확인을 누른 뒤 실행하세요.";
  } finally {
    state.demoLoading = false;
    controls();
  }
}
$("demo-apply").addEventListener("click", () => {
  if (!state.demoInfo || demoControlsDisabled(state)) return;
  transact(async () => {
    try { renderDemo(await api.configureDemoSettings($("demo-execution-mode").value, $("demo-scenario").value)); $("settings-panel").open = false; }
    catch (error) {
      registerBusy(error);
      state.demoUnverified = state.demoUnverified || error.code === "connection" || error.code === "invalid_response" || error.status >= 500;
      $("demo-scenario").value = state.demoInfo.scenario;
      $("demo-status").textContent = `모의 입력 변경을 확인하지 못했습니다. 시연 설정 다시 확인을 누르세요. ${error.message}`;
    }
  });
});
$("restart").addEventListener("click", () => {
  if(state.busy || state.startUnknown || state.demoUnverified || !state.run || !TERMINAL.has(state.run.status) || !fixtureMode && state.connection!=="online") return;
  $("goal-form").requestSubmit();
});
$("demo-scenario").addEventListener("change", () => {
  if (!state.demo || demoControlsDisabled(state)) return;
  transact(async () => {
    try { renderDemo(await api.configureDemo($("demo-scenario").value)); }
    catch (error) {
      registerBusy(error);
      state.demoUnverified = true;
      $("demo-status").textContent = `선택 결과를 확인하지 못했습니다. 시연 설정 다시 확인을 누르세요. ${error.message}`;
    }
  });
});
for (const zone of ["right", "left"]) {
  $("preset-" + zone).addEventListener("click", () => {
    if (state.busy || state.startUnknown || state.run && !TERMINAL.has(state.run.status)) return;
    $("zone").value = zone;
    $("user-input").value = `${zone === "right" ? "오른쪽" : "왼쪽"} 화구 화력을 한 단계 올려줘`;
    $("user-input").focus();
  });
}
$("settings-panel").addEventListener("toggle", () => { if ($("settings-panel").open) $("records-panel").open = false; });
$("records-panel").addEventListener("toggle", () => { if ($("records-panel").open) $("settings-panel").open = false; });
for(const panel of ["settings","records"]) $(panel+"-panel").addEventListener("keydown",event=>{
  if(event.key==="Escape") {$(panel+"-panel").open=false;$(panel+"-toggle").focus();}
});
$("close-settings").addEventListener("click", () => { $("settings-panel").open = false; $("settings-toggle").focus(); });
$("close-records").addEventListener("click", () => { $("records-panel").open = false; $("records-toggle").focus(); });
$("demo-reload").addEventListener("click", () => transact(loadDemo));
function stopPolling() { if (state.polling) clearInterval(state.polling); state.polling = null; }
async function refresh(allowBusy = false) {
  if (!state.runId || state.busy && !allowBusy) return false;
  const outcome = await reads.fetch(state.runId, id => api.get(id));
  if (outcome.kind === "current") {
    if (regressesTerminalState(state.run, outcome.value)) {
      markUnverified("서버가 종료된 실행의 이전 상태를 반환했습니다. 상태를 다시 확인하세요.");
      stopPolling();
      return false;
    }
    render(outcome.value);
    const warning = observationWarning(outcome.value);
    if (warning) showError(warning); else clearError();
    return true;
  }
  if (outcome.kind === "error") {
    const { error } = outcome;
    stopPolling();
    if (error.code === "connection") {
      markOffline(`상태 조회 실패: ${error.message} 마지막 화면은 현재 상태가 아닐 수 있습니다.`);
    } else if (error.code === "invalid_response") {
      markInvalid(`API 응답을 읽을 수 없습니다. 서버의 /api/runs 구현과 응답 형식을 확인하세요. ${error.message}`);
    } else {
      markUnverified(`상태 확인 실패: ${error.message} 서버의 실행 ID를 확인하세요.`);
    }
  }
  return false;
}
function startPolling() {
  stopPolling();
  if (!fixtureMode) state.polling = setInterval(refresh, 1500);
}
function registerBusy(error) {
  if(error.code==="busy" && error.activeRunId) {
    state.recoverRunId=error.activeRunId;
    $("resume-id").value=error.activeRunId;
    $("settings-panel").open=false;
    $("phase").textContent="진행 중 실행 확인 필요";
    $("instruction").textContent="진행 중 실행 불러오기를 눌러 기존 요청을 확인하세요.";
    $("phase-detail").textContent="새 실행은 생성되지 않았습니다. 기존 실행을 불러온 뒤 종료하거나 이어서 진행하세요.";
    state.lastSpoken="";
    if(speechAvailable)speechSynthesis.cancel();
    paintFlow(signalFlow.pause());
    return true;
  }
  return false;
}
async function transact(task) {
  if (state.busy) return;
  state.busy = true; controls(); clearError();
  try { await task(); }
  catch (error) { showError(registerBusy(error)?"진행 중인 실행이 있습니다. 진행 중 실행 불러오기를 누르세요.":error.message || "요청에 실패했습니다."); }
  finally { state.busy = false; controls(); }
}
$("goal-form").addEventListener("submit", event => {
  event.preventDefault();
  if (state.demoUnverified || state.demoInfo?.manageable && (!state.demo || !state.demo.virtual_cooktop || state.demoInfo.execution_mode === "real_llm" && state.demoInfo.api_key_configured === false)) { showError("실행 설정을 확인한 뒤 시작하세요."); return; }
  if (state.recoverRunId || state.run && !TERMINAL.has(state.run.status)) return;
  transact(async () => {
    const input = $("user-input").value.trim();
    if (!input) throw new Error("목표를 입력하세요.");
    reads.invalidate();
    paintFlow(signalFlow.reset());
    for(const zone of ["left","right"]) $("level-"+zone).textContent="—";
    for(const name of ["beep","field","delta"]) $("signal-"+name).textContent="—";
    $("signal-quality").textContent="확인 전";
    $("signal-note").textContent="새 실행의 관측을 기다립니다.";
    $("field-meter").hidden=true;
    $("agent-trail").replaceChildren();
    $("result-summary").textContent="새 실행의 응답을 기다립니다.";
    $("instruction").textContent="서버의 새 조작 안내를 기다리세요.";
    $("phase").textContent="새 실행 준비 중";
    if(speechAvailable) speechSynthesis.cancel();
    stopPolling(); state.seen.clear(); state.renderedEventIds = []; state.lastSpoken = "";
    let run;
    const request = { user_input: input, source: $("source").value, zone_id: $("zone").value || null };
    try { run = await api.start(request.user_input, request.source, request.zone_id,
      fixtureMode ? $("fixture-scenario").value : undefined); }
    catch (error) {
      if (error.code === "connection" || error.code === "invalid_response") {
        state.startUnknown = true;
        const message = "실행 생성 결과를 확인할 수 없습니다. 중복 실행을 피하려면 서버의 활성 실행 여부를 확인한 뒤 화면을 새로고침하세요.";
        if (error.code === "connection") markOffline(message);
        else markInvalid(message);
        return;
      }
      throw error;
    }
    // POST may return only an ID and status under the minimum contract.
    state.runId = run.run_id;
    state.cancelRequestedRunId = null;
    observationRequests.clear();
    if (!state.runId) throw new Error("서버가 실행 ID를 보내지 않았습니다.");
    state.run = null;
    state.submittedRequest = request;
    $("origin").textContent = fixtureMode ? "UI FIXTURE · 서버 판정 없음" : "서버 상태 조회 중";
    $("mode").textContent = "실행 방식: 확인 전";
    $("run-id").textContent = `실행 ID: ${state.runId}`;
    $("fixture-active").hidden = true;
    $("status").textContent = "새 실행 ID를 받았습니다. 현재 상태를 확인하는 중입니다.";
    $("phase").textContent = "상태 조회 중";
    $("instruction").textContent = "서버의 안내를 기다리세요.";
    $("phase-detail").textContent = "조회가 실패하면 상태 다시 확인을 누르세요.";
    $("verdict").textContent = "서버 판정을 기다리는 중입니다.";
    $("sensor-stage").textContent = "관측 대기";
    $("agent-stage").textContent = "서버 응답 확인 중";
    $("result-summary").textContent = "새 실행의 판정을 기다립니다.";
    $("evidence-preview").hidden = true;
    $("evidence-preview").open = false;
    $("evidence-json").textContent = "";
    $("export-note").textContent = "";
    renderEvents([]);
    controls();
    try { render(await api.get(state.runId)); }
    catch (error) {
      const message = `실행 ID ${state.runId}를 받았지만 상태 조회에 실패했습니다. 상태 다시 확인을 누르세요. ${error.message}`;
      if (error.code === "connection") markOffline(message);
      else if (error.code === "invalid_response") markInvalid(message);
      else markUnverified(message);
      return;
    }
    startPolling();
  });
});
async function resumeRun(runId) {
    $("resume-id").removeAttribute("aria-invalid");
    reads.invalidate();
    let run;
    try { run = await api.get(runId); }
    catch (error) {
      if (error.code === "connection") markOffline(`실행 ID 조회 실패: ${error.message}`);
      else if (error.code === "invalid_response") markInvalid(`실행 ID 조회 실패: ${error.message}`);
      else {
        if(error.code==="unknown_run" && state.recoverRunId===runId) {
          state.recoverRunId=null;
          await loadDemo();
        }
        showError(`실행 ID를 조회할 수 없습니다: ${error.message}`);
      }
      return;
    }
    stopPolling();
    state.seen.clear(); state.renderedEventIds = []; state.lastSpoken = "";
    state.submittedRequest = null;
    state.startUnknown = false;
    if(run.status==="awaiting_clarification") {
      // The reloaded page's default goal is not the recovered run's submitted input.
      $("user-input").value="";
      $("user-input").placeholder="입력 수정하기를 누른 뒤 목표를 다시 입력하세요.";
    }
    render(run, false);
    clearError();
    if (!TERMINAL.has(run.status)) startPolling();
  return true;
}
$("resume-form").addEventListener("submit", event => {
  event.preventDefault();
  if (fixtureMode || state.run && !TERMINAL.has(state.run.status)) return;
  transact(async () => {
    const runId=$("resume-id").value.trim();
    if(!runId){$("resume-id").setAttribute("aria-invalid","true");$("resume-id").focus();throw new Error("조회할 실행 ID를 입력하세요.");}
    await resumeRun(runId);
  });
});
$("recover-run").addEventListener("click",()=>{
  if(fixtureMode || !state.recoverRunId || state.busy)return;
  transact(async()=>{if(await resumeRun(state.recoverRunId))$("settings-panel").open=false;});
});
for(const zone of ["left","right"]) for(const operation of ["increase","decrease"]) {
  $("virtual-"+zone+"-"+operation).addEventListener("click",()=>{
    if (!state.run?.simulation || state.busy || cancellingCurrentRun() || state.demoUnverified || state.connection!=="online" || state.run.status!=="awaiting_user") return;
    transact(async()=>{
      const runId=state.runId,actionId=state.run.instruction?.action_id;
      reads.invalidate();
      try {
        const run=await api.press(runId,actionId,zone,operation);
        if(state.runId===runId && !cancellingCurrentRun()) render(run);
      }catch(error){markUnverified(`가상 조작 결과를 확인하지 못했습니다. 다시 누르지 말고 상태 다시 확인을 누르세요. ${error.message}`);}
    });
  });
}
$("ack").addEventListener("click", () => transact(async () => {
  const actionId = state.run?.instruction?.action_id;
  if (!actionId) return;
  reads.invalidate();
  $("ack").disabled = true;
  try { await api.ack(state.runId, actionId); }
  catch (error) {
    markUnverified(`조작 완료 알림 결과를 확인할 수 없습니다. 상태 다시 확인을 누르세요. ${error.message}`);
    return;
  }
  if (!await refresh(true)) return;
}));
$("cancel").addEventListener("click", () => transact(async () => {
  const runId = state.runId;
  const editing = state.run?.status === "awaiting_clarification";
  if (!runId) return;
  state.cancelRequestedRunId = runId;
  reads.invalidate();
  if (state.run) renderAction(state.run);
  $("status").textContent = "취소 요청 확인 중 · 추가 조작을 멈춰 주세요.";
  controls();
  let canceled;
  try { canceled = await api.cancel(runId); }
  catch (error) {
    markUnverified(`취소 결과를 확인할 수 없습니다. 상태 다시 확인을 누르세요. ${error.message}`);
    return;
  }
  if (state.runId !== runId) return;
  if (canceled.status === "stopped" && state.run?.run_id === runId) {
    render({ ...state.run, status: "stopped", instruction: null, verdict: null }, false);
  }
  if (!await refresh(true)) return;
  if(editing && state.run?.status==="stopped") {
    $("status").textContent="이전 요청을 종료했습니다. 목표를 수정하고 안내 시작을 누르세요.";
    $("user-input").focus();
  }
}));
$("repeat").addEventListener("click", () => speak(state.lastSpoken));
$("export-evidence").addEventListener("click", event => {
  if (!state.run?.run_id) { event.preventDefault(); return; }
  const report = evidenceReport(state.run, { fixtureMode, connection: state.connection,
    lastConfirmedAt: state.lastConfirmedAt, submittedRequest: state.submittedRequest });
  const json = JSON.stringify(report, null, 2);
  $("evidence-json").textContent = json;
  $("evidence-preview").hidden = false;
  $("evidence-preview").open = true;
  $("export-note").textContent = "기록 파일 저장을 요청했습니다. 브라우저에서 저장이 제한되면 아래 JSON 내용을 선택해 복사하세요.";
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  event.currentTarget.href = url;
  event.currentTarget.download = evidenceFilename(report);
  setTimeout(() => URL.revokeObjectURL(url), 30000);
});
$("auto-speech").addEventListener("change", () => {
  if (!$("auto-speech").checked && speechAvailable) speechSynthesis.cancel();
});
$("large-text").addEventListener("click", () => {
  const enabled = document.documentElement.classList.toggle("large-text");
  $("large-text").setAttribute("aria-pressed", String(enabled));
  $("large-text").textContent = enabled ? "큰 글씨 끄기" : "큰 글씨 켜기";
});
$("retry").addEventListener("click", async () => {
  const recovered = await refresh();
  if (recovered && state.run?.status === "observing") observe(state.run, true);
  if (recovered && state.run && !TERMINAL.has(state.run.status) && !state.polling) startPolling();
});
$("speech-support").textContent = speechAvailable ? "음성 안내 사용 가능. 안내 다시 듣기 버튼을 사용할 수 있습니다." : "이 브라우저는 음성 안내를 지원하지 않습니다. 화면의 안내 문구를 확인하세요.";
if (!speechAvailable) { $("auto-speech").checked = false; $("auto-speech").disabled = true; }
if (fixtureMode) {
  $("demo-scenario").hidden = true;
  $("origin").textContent = "UI FIXTURE · 서버 판정 없음";
  $("screen-mode").textContent = "UI 화면 예시";
  $("screen-source").textContent = "화면 연습 · 서버 판정 없음";
  $("page-title").textContent = "화면 연습";
}
showConnection();
controls();

if (!fixtureMode) loadDemo();
