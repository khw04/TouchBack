import { createApi, freshSpeechItems, TERMINAL } from "./api.mjs";
import { createFixture } from "./fixtures.mjs";

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const fixtureMode = params.get("fixture") === "1";
const api = fixtureMode ? createFixture() : createApi(params.get("api") || "");
const state = { runId: null, run: null, busy: false, seen: new Set(), lastSpoken: "", polling: null };
const statusText = {
  planning: "계획 중", awaiting_clarification: "추가 확인 필요", awaiting_user: "사용자 조작 대기",
  observing: "센서 관측 중", recovering: "복구 안내 준비 중", succeeded: "서버 판정: 목표 확인",
  uncertain: "불확실: 결과를 확인할 수 없음", stopped: "실행 중단", failed: "실행 실패",
};
const modeText = { backend_stub: "백엔드 준비 중", mock_llm: "모의 모델", real_llm: "실제 모델", ui_fixture: "UI 화면 예시" };
const sourceText = { mock: "모의 관측", replay: "녹화 재생", live: "실시간 센서" };
const speechAvailable = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;

function controls() {
  const run = state.run;
  $("start").disabled = state.busy || Boolean(run && !TERMINAL.has(run.status));
  $("ack").disabled = state.busy || !run?.instruction?.action_id || run.status !== "awaiting_user" || fixtureMode && run.status !== "awaiting_user";
  $("cancel").disabled = state.busy || !run || TERMINAL.has(run.status);
  $("retry").disabled = state.busy || !state.runId || fixtureMode;
  $("repeat").disabled = !speechAvailable || !state.lastSpoken;
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
function render(run, announce = true) {
  state.run = run;
  state.runId = run.run_id;
  const isFixture = fixtureMode || run.execution_mode === "ui_fixture";
  $("origin").textContent = isFixture ? "UI FIXTURE · 서버 판정 없음" : "서버 응답";
  $("mode").textContent = `실행 방식: ${modeText[run.execution_mode] || run.execution_mode || "확인 전"}`;
  $("run-id").textContent = `실행 ID: ${run.run_id || "없음"}`;
  const stateLabel = isFixture && run.status === "succeeded" ? "UI 화면 예시 완료 · 서버 판정 없음"
    : run.execution_mode === "backend_stub" && run.status === "succeeded" ? "백엔드 스텁 응답 오류 · 성공 근거 없음"
    : statusText[run.status] || run.status || "상태 확인 중";
  $("status").textContent = `${stateLabel} · 입력 출처: ${sourceText[run.source] || run.source || "확인 전"}`;
  $("instruction").textContent = run.instruction?.text || "현재 조작 안내가 없습니다.";
  const verdict = run.verdict;
  $("verdict").textContent = verdict
    ? `${isFixture ? "화면 예시 결과" : "서버 판정"}: ${verdict.status || "확인 필요"} · 근거 코드: ${(verdict.reason_codes || []).join(", ") || "없음"}`
    : "서버 판정을 기다리는 중입니다.";
  const items = Array.isArray(run.events) ? run.events : [];
  $("events").replaceChildren();
  if (!items.length) {
    const li = document.createElement("li"); li.textContent = "아직 기록이 없습니다."; $("events").append(li);
  }
  for (const item of items) {
    const li = document.createElement("li");
    const date = item.timestamp ? new Date(item.timestamp).toLocaleTimeString("ko-KR") : "시간 미상";
    li.textContent = `[${date}] ${item.kind || "event"} · ${item.message || "내용 없음"} · 출처 ${item.source || "미상"}${item.evidence_ids?.length ? ` · 근거 ${item.evidence_ids.join(", ")}` : ""}`;
    $("events").append(li);
  }
  const speech = freshSpeechItems(state.seen, run);
  speech.ids.forEach(id => state.seen.add(id));
  if (announce && speech.messages.length) speak(speech.messages.at(-1).message);
  if (TERMINAL.has(run.status)) stopPolling();
  controls();
}
function stopPolling() { if (state.polling) clearInterval(state.polling); state.polling = null; }
async function refresh() {
  if (!state.runId || state.busy) return;
  try { render(await api.get(state.runId)); clearError(); }
  catch (error) { stopPolling(); showError(`연결 실패: ${error.message} 다시 실행하거나 서버를 확인하세요.`); }
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
    stopPolling(); state.seen.clear(); state.lastSpoken = "";
    const run = await api.start(input, $("source").value, $("zone").value || null);
    // POST may return only an ID and status under the minimum contract.
    state.runId = run.run_id;
    if (!state.runId) throw new Error("서버가 실행 ID를 보내지 않았습니다.");
    render(await api.get(state.runId));
    startPolling();
  });
});
$("ack").addEventListener("click", () => transact(async () => {
  const actionId = state.run?.instruction?.action_id;
  if (!actionId) return;
  $("ack").disabled = true;
  await api.ack(state.runId, actionId);
  render(await api.get(state.runId));
}));
$("cancel").addEventListener("click", () => transact(async () => {
  await api.cancel(state.runId);
  render(await api.get(state.runId));
}));
$("repeat").addEventListener("click", () => speak(state.lastSpoken));
$("retry").addEventListener("click", async () => { await refresh(); if (state.run && !TERMINAL.has(state.run.status) && !state.polling) startPolling(); });
$("speech-support").textContent = speechAvailable ? "음성 안내 사용 가능. 안내 다시 듣기 버튼을 사용할 수 있습니다." : "이 브라우저는 음성 안내를 지원하지 않습니다. 화면의 안내 문구를 확인하세요.";
if (fixtureMode) $("origin").textContent = "UI FIXTURE · 서버 판정 없음";
controls();
