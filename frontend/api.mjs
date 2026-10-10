export const TERMINAL = new Set(["succeeded", "uncertain", "stopped", "failed"]);

export class ApiError extends Error {
  constructor(message, code = "connection", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
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
    if (!response.ok) throw new ApiError(data.message || "요청에 실패했습니다.", data.code || "server_error", response.status);
    return data;
  }
  return {
    start: (user_input, source, zone_id) => request("/api/runs", "POST", { user_input, source, zone_id }),
    get: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`),
    ack: (runId, actionId) => request(`/api/runs/${encodeURIComponent(runId)}/ack`, "POST", { action_id: actionId }),
    cancel: (runId) => request(`/api/runs/${encodeURIComponent(runId)}/cancel`, "POST"),
  };
}

export function freshSpeechItems(previousIds, run) {
  const events = Array.isArray(run.events) ? run.events : [];
  const fresh = events.filter(event => event?.event_id && !previousIds.has(event.event_id));
  const messages = fresh.filter(event => ["instruction", "recovery", "verdict", "error"].includes(event.kind) && event.message &&
    !(event.kind === "instruction" && event.action_id && previousIds.has(`action:${event.action_id}`)));
  // The current instruction can arrive before its event. Its action ID is the stable fallback key.
  if (run.instruction?.action_id && run.instruction.text &&
      !previousIds.has(`action:${run.instruction.action_id}`) &&
      !messages.some(event => event.action_id === run.instruction.action_id)) {
    messages.push({ event_id: `action:${run.instruction.action_id}`, message: run.instruction.text });
  }
  return { ids: fresh.map(event => event.event_id).concat(
    run.instruction?.action_id ? [`action:${run.instruction.action_id}`] : []), messages };
}
