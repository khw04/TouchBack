const now = () => new Date().toISOString();
let sequence = 0;

export function createFixture() {
  const runs = new Map();
  const event = (run, kind, message, action_id = null) => ({
    event_id: `ui-${++sequence}`, run_id: run.run_id, action_id,
    timestamp: now(), kind, source: "ui_fixture", message, evidence_ids: [],
  });
  function snapshot(run) { return structuredClone(run); }
  return {
    async start(user_input, source, zone_id) {
      const run = {
        run_id: `ui-fixture-${++sequence}`, status: "awaiting_user",
        execution_mode: "ui_fixture", source, zone_id, user_input,
        instruction: { action_id: `ui-action-${sequence}`, text: "오른쪽 화구의 + 버튼을 한 번 누른 뒤 조작 완료를 누르세요." },
        verdict: null, events: [],
      };
      run.events.push(event(run, "plan", "화력 증가 요청을 화면 예시로 표시합니다."));
      run.events.push(event(run, "instruction", run.instruction.text, run.instruction.action_id));
      runs.set(run.run_id, run);
      return snapshot(run);
    },
    async get(id) { return snapshot(runs.get(id)); },
    async ack(id, actionId) {
      const run = runs.get(id);
      if (run.instruction?.action_id !== actionId) throw new Error("현재 안내와 다른 조작입니다.");
      run.status = "uncertain";
      run.instruction = null;
      run.verdict = { status: "uncertain", reason_codes: ["UI_FIXTURE_ONLY"], evidence_ids: [] };
      run.events.push(event(run, "verdict", "화면 예시가 종료되었습니다. 실제 센서 판정은 수행하지 않았습니다."));
      return snapshot(run);
    },
    async cancel(id) {
      const run = runs.get(id);
      run.status = "stopped";
      run.instruction = null;
      run.events.push(event(run, "error", "화면 예시가 취소되었습니다."));
      return snapshot(run);
    },
  };
}
