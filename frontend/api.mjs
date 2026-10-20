export const TERMINAL = new Set(["succeeded", "uncertain", "stopped", "failed"]);
const STATUSES = new Set(["planning", "awaiting_clarification", "awaiting_user", "observing", "recovering", ...TERMINAL]);
const MODES = new Set(["backend_stub", "mock_llm", "real_llm"]);
const SOURCES = new Set(["mock", "replay", "live"]);
const EVENT_KINDS = new Set(["plan", "instruction", "tool_call", "observation", "verdict", "recovery", "error"]);
const VERDICT_STATUSES = new Set(["success", "mismatch", "uncertain"]);

export class ApiError extends Error {
  constructor(message, code = "connection", status = 0, activeRunId = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.activeRunId = activeRunId;
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
  if (data.verdict !== null && (!record(data.verdict) || !VERDICT_STATUSES.has(data.verdict.status) ||
      !Array.isArray(data.verdict.reason_codes) || !Array.isArray(data.verdict.evidence_ids))) {
    invalid("판정 정보가 불완전합니다.");
  }
  if (!Array.isArray(data.events) || data.events.some(item => !record(item) || !filled(item.event_id) ||
      !filled(item.timestamp) || Number.isNaN(Date.parse(item.timestamp)) || !EVENT_KINDS.has(item.kind) ||
      !filled(item.message) || !Array.isArray(item.evidence_ids))) {
    invalid("실행 기록 형식이 다릅니다.");
  }
  if(data.diagnosis!=null && (!record(data.diagnosis) || data.source!=="mock" || data.diagnosis.source!=="mock" || !filled(data.diagnosis.code) || !filled(data.diagnosis.message) || !filled(data.diagnosis.next_action) || !Array.isArray(data.diagnosis.evidence_ids) || !record(data.diagnosis.zones)))invalid("관측 진단 형식이 다릅니다.");
  if (data.simulation != null) {
    const sim=data.simulation;
    if(data.source!=="mock" || !record(sim) || sim.kind!=="virtual_cooktop" || !record(sim.levels) ||
       ![sim.levels.left,sim.levels.right].every(v=>Number.isInteger(v)&&v>=0&&v<=9) || !Array.isArray(sim.observations)) invalid("가상 인덕션 상태가 유효하지 않습니다.");
  }
  if(data.simulation != null) {
    for(const item of data.simulation.observations) {
      const d=item?.device_observation,f=d?.field_summary;
      if(!record(item)||!filled(item.observation_id)||item.run_id!==data.run_id||!filled(item.action_id)||!record(d)||d.source!=="mock"||
         !["left","right"].includes(d.zone_id)||!(d.beep_count===null||Number.isInteger(d.beep_count)&&d.beep_count>=0)||
         !record(d.quality)||!["valid","degraded","invalid"].includes(d.quality.status)||
         !(f===null||record(f)&&Number.isFinite(f.rms)&&f.rms>=0&&["adc_count","millivolt"].includes(f.unit))) invalid("모의 관측 표시 정보가 유효하지 않습니다.");
    }
    if(data.simulation.last_press!=null && (!record(data.simulation.last_press)||!filled(data.simulation.last_press.action_id))) invalid("가상 입력 기록이 유효하지 않습니다.");
    if(data.simulation.baselines!=null) {
      if(!record(data.simulation.baselines)) invalid("기준 관측 정보가 유효하지 않습니다.");
      for(const baseline of Object.values(data.simulation.baselines)) {
        const f=baseline?.device_observation?.field_summary;
        if(baseline!==null && (!record(baseline)||!filled(baseline.observation_id)||!record(f)||!Number.isFinite(f.rms)||f.rms<0||!["adc_count","millivolt"].includes(f.unit))) invalid("기준 관측 수치가 유효하지 않습니다.");
      }
    }
  }
  return data;
}

export function validateDemoMetadata(data) {
  if (!record(data) || typeof data.enabled !== "boolean") invalid("모의 입력 설정 형식이 다릅니다.");
  if (data.active_run_id != null && !filled(data.active_run_id)) invalid("진행 중인 실행 ID가 유효하지 않습니다.");
  if (!data.enabled) {
    if (data.manageable !== true) return { enabled: false };
    if (typeof data.api_key_configured !== "boolean" || typeof data.model !== "string" ||
        !MODES.has(data.execution_mode) || !Array.isArray(data.scenarios) || !data.scenarios.includes(data.scenario)) invalid("시연 준비 정보가 유효하지 않습니다.");
    return data;
  }
  const allowed = new Set(["normal", "recovery", "sensor_error", "missing", "delayed", "no_change", "duplicate", "recovery_exhausted", "double_input"]);
  if (data.source !== "mock" || !["mock_llm", "real_llm"].includes(data.execution_mode) ||
      data.physical_validation_performed !== false || !Array.isArray(data.scenarios) ||
      !data.scenarios.length || data.scenarios.some(name => !allowed.has(name)) ||
      new Set(data.scenarios).size !== data.scenarios.length || !data.scenarios.includes(data.scenario)) {
    invalid("모의 입력 출처·실행 방식·시나리오가 유효하지 않습니다.");
  }
  return data;
}

function validateAction(data, expectedId, accepted = false) {
  validateIdentity(data, expectedId);
  if (accepted && data.accepted !== true) invalid("조작 완료 알림의 수락 여부가 없습니다.");
  return data;
}

export function createApi(baseUrl = "", fetchImpl = fetch, { timeoutMs = 15000 } = {}) {
  const root = baseUrl.replace(/\/$/, "");
  async function request(path, method = "GET", body) {
    const controller = new AbortController();
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new ApiError("서버 요청 시간이 초과됐습니다. 상태를 다시 확인하세요."));
      }, timeoutMs);
    });
    const operation = async () => {
      let response;
      try {
        response = await fetchImpl(`${root}${path}`, {
          method, signal: controller.signal,
          headers: body ? { "Content-Type": "application/json" } : {},
          body: body ? JSON.stringify(body) : undefined, cache: "no-store",
        });
      } catch { throw new ApiError("서버에 연결할 수 없습니다. 주소와 서버 실행 상태를 확인하세요."); }
      let data;
      try { data = await response.json(); }
      catch { throw new ApiError("서버 응답을 읽을 수 없습니다.", "invalid_response", response.status); }
      if (!response.ok) throw new ApiError(record(data) && filled(data.message) ? data.message : "요청에 실패했습니다.",
        record(data) && filled(data.code) ? data.code : "server_error", response.status,
        response.status === 409 && data?.code === "busy" && filled(data.active_run_id) ? data.active_run_id : null);
      return data;
    };
    try { return await Promise.race([operation(), timeout]); }
    finally { clearTimeout(timer); }
  }
  return {
    demoInfo: async () => validateDemoMetadata(await request("/api/demo")),
    configureDemo: async scenario => validateDemoMetadata(await request("/api/demo/scenario", "POST", { scenario })),
    configureDemoSettings: async (execution_mode, scenario) => validateDemoMetadata(await request("/api/demo/settings", "POST", {execution_mode, scenario})),
    start: async (user_input, source, zone_id) => validateStart(await request("/api/runs", "POST", { user_input, source, zone_id })),
    get: async runId => validateRun(await request(`/api/runs/${encodeURIComponent(runId)}`), runId),
    ack: async (runId, actionId) => validateAction(await request(`/api/runs/${encodeURIComponent(runId)}/ack`, "POST", { action_id: actionId }), runId, true),
    step: async (runId, actionId) => validateRun(await request(`/api/runs/${encodeURIComponent(runId)}/step`, "POST", { action_id: actionId }), runId),
    press: async (runId,action_id,zone_id,operation) => validateRun(await request(`/api/demo/runs/${encodeURIComponent(runId)}/press`, "POST", {action_id,zone_id,operation}),runId),
    cancel: async runId => {
      const response = validateAction(await request(`/api/runs/${encodeURIComponent(runId)}/cancel`, "POST"), runId);
      if (!TERMINAL.has(response.status)) invalid("취소 요청 뒤 종료 상태가 아닙니다.");
      return response;
    },
  };
}

export function freshSpeechItems(previousIds, run, successClaimable = true) {
  const events = Array.isArray(run.events) ? run.events : [];
  const fresh = events.filter(event => event?.event_id && !previousIds.has(event.event_id));
  const currentActionId = run.status === "awaiting_user" ? run.instruction?.action_id : null;
  const messages = [];
  const seenAction = new Set(previousIds);
  for (const event of fresh) {
    if (!event || !event.message) continue;
    if (["instruction", "recovery"].includes(event.kind)) {
      if (currentActionId && event.action_id === currentActionId && !seenAction.has(`action:${currentActionId}`)) {
        messages.push(event);
        seenAction.add(`action:${currentActionId}`);
      }
    } else if (["verdict", "error"].includes(event.kind)) {
      messages.push(run.status === "succeeded" && !successClaimable && event.kind === "verdict"
        ? { ...event, message: "서버의 성공 근거가 부족합니다. 인덕션 상태를 직접 확인하세요." } : event);
    }
  }
  // The current instruction can arrive before its event. Its action ID is the stable fallback key.
  if (currentActionId && run.instruction?.text &&
      !seenAction.has(`action:${currentActionId}`)) {
    messages.push({ event_id: `action:${currentActionId}`, message: run.instruction.text });
    seenAction.add(`action:${currentActionId}`);
  }
  return { ids: fresh.map(event => event.event_id).concat(
    currentActionId ? [`action:${currentActionId}`] : []), messages };
}
