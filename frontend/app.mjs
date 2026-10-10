import { createApi, freshSpeechItems, TERMINAL } from "./api.mjs?v=13";
import { createFixture } from "./fixtures.mjs?v=9";
import { actionFor, claimableSuccess, phaseFor, unverifiedSnapshot, verdictLabel } from "./presentation.mjs?v=13";
import { eventChange, eventLabel, eventSource } from "./events.mjs";
import { canSendAction, connectionLabel } from "./connection.mjs";
import { evidenceReport, evidenceFilename } from "./evidence.mjs";
import { createReadGate, regressesTerminalState } from "./read-gate.mjs";

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const fixtureMode = params.get("fixture") === "1";
const api = fixtureMode ? createFixture() : createApi(params.get("api") || "");
const state = { runId: null, run: null, busy: false, connection: "unknown", lastConfirmedAt: null,
  startUnknown: false, seen: new Set(), renderedEventIds: [], lastSpoken: "", polling: null, submittedRequest: null };
const reads = createReadGate(() => state.runId);
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
  const writable = canSendAction(fixtureMode ? "fixture" : "server", state.connection);
  $("start").disabled = state.busy || state.startUnknown || ["offline", "invalid", "unverified"].includes(state.connection) || Boolean(run && !TERMINAL.has(run.status));
  $("ack").disabled = state.busy || !writable || !run?.instruction?.action_id || run.status !== "awaiting_user";
  $("cancel").disabled = state.busy || !writable || !run || TERMINAL.has(run.status);
  $("retry").disabled = state.busy || !state.runId || fixtureMode;
  $("repeat").disabled = state.busy || !speechAvailable || !state.lastSpoken || (!fixtureMode && ["offline", "invalid", "unverified"].includes(state.connection));
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
  if (fixtureMode || !state.runId) return;
  if (speechAvailable) speechSynthesis.cancel();
  const snapshot = unverifiedSnapshot();
  $("status").textContent = snapshot.status;
  $("phase").textContent = snapshot.phase;
  $("phase-detail").textContent = snapshot.detail;
  $("instruction").textContent = snapshot.action;
  $("verdict").textContent = snapshot.verdict;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
  controls();
}
function clearError() { $("error").hidden = true; $("error").textContent = ""; }
function speak(message) {
  if (!speechAvailable || !message) return;
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
function render(run, announce = true) {
  const previousAction = state.run?.status === "awaiting_user" ? state.run.instruction?.action_id : null;
  const currentAction = run.status === "awaiting_user" ? run.instruction?.action_id : null;
  if (previousAction && previousAction !== currentAction) {
    if (speechAvailable) speechSynthesis.cancel();
    state.lastSpoken = "";
  }
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
  const nextStatus = `${stateLabel} · 입력 출처: ${sourceText[run.source] || run.source || "확인 전"}`;
  // Repeated polls should not retrigger the screen reader's live region.
  if ($("status").textContent !== nextStatus) $("status").textContent = nextStatus;
  const [phase, detail] = phaseFor(run);
  $("phase").textContent = phase;
  $("phase-detail").textContent = detail;
  $("instruction").textContent = actionFor(run);
  $("verdict").textContent = verdictLabel(run);
  const items = Array.isArray(run.events) ? run.events : [];
  renderEvents(items);
  const speech = freshSpeechItems(state.seen, run, claimableSuccess(run));
  speech.ids.forEach(id => state.seen.add(id));
  if (speech.messages.length) {
    state.lastSpoken = speech.messages.at(-1).message;
    if (announce && $("auto-speech").checked) speak(state.lastSpoken);
  }
  if (TERMINAL.has(run.status)) stopPolling();
  controls();
}
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
    clearError();
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
async function transact(task) {
  if (state.busy) return;
  state.busy = true; controls(); clearError();
  try { await task(); }
  catch (error) { showError(error.message || "요청에 실패했습니다."); }
  finally { state.busy = false; controls(); }
}
$("goal-form").addEventListener("submit", event => {
  event.preventDefault();
  if (state.run && !TERMINAL.has(state.run.status)) return;
  transact(async () => {
    const input = $("user-input").value.trim();
    if (!input) throw new Error("목표를 입력하세요.");
    reads.invalidate();
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
$("resume-form").addEventListener("submit", event => {
  event.preventDefault();
  if (fixtureMode || state.run && !TERMINAL.has(state.run.status)) return;
  transact(async () => {
    const runId = $("resume-id").value.trim();
    if (!runId) {
      $("resume-id").setAttribute("aria-invalid", "true");
      $("resume-id").focus();
      throw new Error("조회할 실행 ID를 입력하세요.");
    }
    $("resume-id").removeAttribute("aria-invalid");
    reads.invalidate();
    let run;
    try { run = await api.get(runId); }
    catch (error) {
      if (error.code === "connection") markOffline(`실행 ID 조회 실패: ${error.message}`);
      else if (error.code === "invalid_response") markInvalid(`실행 ID 조회 실패: ${error.message}`);
      else showError(`실행 ID를 조회할 수 없습니다: ${error.message}`);
      return;
    }
    stopPolling();
    state.seen.clear(); state.renderedEventIds = []; state.lastSpoken = "";
    state.submittedRequest = null;
    state.startUnknown = false;
    render(run, false);
    clearError();
    if (!TERMINAL.has(run.status)) startPolling();
  });
});
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
  reads.invalidate();
  let canceled;
  try { canceled = await api.cancel(state.runId); }
  catch (error) {
    markUnverified(`취소 결과를 확인할 수 없습니다. 상태 다시 확인을 누르세요. ${error.message}`);
    return;
  }
  if (canceled.status === "stopped" && state.run?.run_id === state.runId) {
    render({ ...state.run, status: "stopped", instruction: null, verdict: null }, false);
  }
  if (!await refresh(true)) return;
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
  if (recovered && state.run && !TERMINAL.has(state.run.status) && !state.polling) startPolling();
});
$("speech-support").textContent = speechAvailable ? "음성 안내 사용 가능. 안내 다시 듣기 버튼을 사용할 수 있습니다." : "이 브라우저는 음성 안내를 지원하지 않습니다. 화면의 안내 문구를 확인하세요.";
if (!speechAvailable) { $("auto-speech").checked = false; $("auto-speech").disabled = true; }
if (fixtureMode) $("origin").textContent = "UI FIXTURE · 서버 판정 없음";
showConnection();
controls();
