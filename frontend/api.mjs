export const TERMINAL = new Set(["succeeded", "uncertain", "stopped", "failed"]);
const STATUSES = new Set(["planning", "awaiting_clarification", "awaiting_user", "observing", "recovering", ...TERMINAL]);
const MODES = new Set(["backend_stub", "mock_llm", "real_llm"]);
const SOURCES = new Set(["mock", "replay", "live"]);
const EVENT_KINDS = new Set(["plan", "instruction", "tool_call", "observation", "verdict", "recovery", "error"]);

export class ApiError extends Error {
  constructor(message, code = "connection", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
const filled = value => typeof value === "string" && value.trim().length > 0;
function invalid(detail) { throw new ApiError(`API 응답 형식 오류: ${detail}`, "invalid_response"); }

function validateIdentity(data, expectedId = null) {
  if (!record(data) || !filled(data.run_id)) invalid("실행 ID가 없습니다.");
  if (expectedId !== null && data.run_id !== expectedId) invalid("요청한 실행 ID와 응답의 실행 ID가 다릅니다.");
  if (!STATUSES.has(data.status)) invalid("알 수 없는 실행 상태입니다.");
  return data;
}

export function validateStart(data) {
  validateIdentity(data);
  if (!MODES.has(data.execution_mode)) invalid("알 수 없는 실행 방식입니다.");
  return data;
}

export function validateRun(data, expectedId) {
  validateIdentity(data, expectedId);
  if (!MODES.has(data.execution_mode)) invalid("알 수 없는 실행 방식입니다.");
  if (!SOURCES.has(data.source)) invalid("입력 출처가 없거나 알 수 없습니다.");
  if (data.instruction !== null && (!record(data.instruction) || !filled(data.instruction.action_id) || !filled(data.instruction.text))) {
    invalid("조작 안내의 ID 또는 내용이 없습니다.");
  }
  if (data.status === "awaiting_user" && !data.instruction) invalid("사용자 조작 대기 상태에 안내가 없습니다.");
  if (data.verdict !== null && (!record(data.verdict) || !filled(data.verdict.status) ||
      !Array.isArray(data.verdict.reason_codes) || !Array.isArray(data.verdict.evidence_ids))) {
    invalid("판정 정보가 불완전합니다.");
  }
  if (!Array.isArray(data.events) || data.events.some(item => !record(item) || !filled(item.event_id) ||
      !filled(item.timestamp) || Number.isNaN(Date.parse(item.timestamp)) || !EVENT_KINDS.has(item.kind) ||
      !filled(item.message) || !Array.isArray(item.evidence_ids))) {
    invalid("실행 기록 형식이 다릅니다.");
  }
  return data;
}

function validateAction(data, expectedId, accepted = false) {
  validateIdentity(data, expectedId);
  if (accepted && data.accepted !== true) invalid("조작 완료 알림의 수락 여부가 없습니다.");
  return data;
}

export function createApi(baseUrl = "", fetchImpl = fetch) {
  const root = baseUrl.replace(/\/$/, "");
  async function request(path, method = "GET", body) {
    let response;
    try {
      response = await fetchImpl(`${root}${path}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store",
      });
    } catch {
      throw new ApiError("서버에 연결할 수 없습니다. 주소와 서버 실행 상태를 확인하세요.");
    }
    let data;
    try { data = await response.json(); }
    catch { throw new ApiError("서버 응답을 읽을 수 없습니다.", "invalid_response", response.status); }
    if (!response.ok) throw new ApiError(record(data) && filled(data.message) ? data.message : "요청에 실패했습니다.",
      record(data) && filled(data.code) ? data.code : "server_error", response.status);
    return data;
  }
  return {
    start: async (user_input, source, zone_id) => validateStart(await request("/api/runs", "POST", { user_input, source, zone_id })),
    get: async runId => validateRun(await request(`/api/runs/${encodeURIComponent(runId)}`), runId),
    ack: async (runId, actionId) => validateAction(await request(`/api/runs/${encodeURIComponent(runId)}/ack`, "POST", { action_id: actionId }), runId, true),
    cancel: async runId => validateAction(await request(`/api/runs/${encodeURIComponent(runId)}/cancel`, "POST"), runId),
  };
}

export function freshSpeechItems(previousIds, run, successClaimable = true) {
  const events = Array.isArray(run.events) ? run.events : [];
  const fresh = events.filter(event => event?.event_id && !previousIds.has(event.event_id));
  const messages = fresh.filter(event => ["instruction", "recovery", "verdict", "error"].includes(event.kind) && event.message &&
    !(event.kind === "instruction" && event.action_id && previousIds.has(`action:${event.action_id}`)))
    .map(event => run.status === "succeeded" && !successClaimable && event.kind === "verdict"
      ? { ...event, message: "서버의 성공 근거가 부족합니다. 인덕션 상태를 직접 확인하세요." } : event);
  // The current instruction can arrive before its event. Its action ID is the stable fallback key.
  if (run.instruction?.action_id && run.instruction.text &&
      !previousIds.has(`action:${run.instruction.action_id}`) &&
      !messages.some(event => event.action_id === run.instruction.action_id)) {
    messages.push({ event_id: `action:${run.instruction.action_id}`, message: run.instruction.text });
  }
  return { ids: fresh.map(event => event.event_id).concat(
    run.instruction?.action_id ? [`action:${run.instruction.action_id}`] : []), messages };
}
