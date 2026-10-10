const now = () => new Date().toISOString();
let sequence = 0;

export const FIXTURE_SCENARIOS = new Set(["normal", "recovery", "sensor_error"]);

export function fixtureGoal(userInput, selectedZone) {
  const mentionsRight = /오른쪽|우측/.test(userInput);
  const mentionsLeft = /왼쪽|좌측/.test(userInput);
  if (!/올려|높여|증가|\+/.test(userInput) || /내려|낮춰|감소|꺼/.test(userInput)) {
    return { zone: null, message: "화력을 한 단계 올리는 요청만 화면 연습할 수 있습니다. 목표를 다시 입력하세요." };
  }
  if (mentionsRight && mentionsLeft) return { zone: null, message: "요청에 양쪽 화구가 모두 들어 있습니다. 화구를 하나만 지정하세요." };
  const mentionedZone = mentionsRight ? "right" : mentionsLeft ? "left" : null;
  if (selectedZone && mentionedZone && selectedZone !== mentionedZone) {
    return { zone: null, message: "입력한 화구와 선택한 화구가 다릅니다. 두 값을 일치시키세요." };
  }
  const zone = selectedZone || mentionedZone;
  if (!zone) return { zone: null, message: "화구를 확인할 수 없습니다. 오른쪽 또는 왼쪽을 지정하세요." };
  return { zone, message: null };
}

export function createFixture() {
  const runs = new Map();
  const event = (run, kind, message, action_id = null) => ({
    event_id: `ui-${++sequence}`, run_id: run.run_id, action_id,
    timestamp: now(), kind, source: "ui_fixture", message, evidence_ids: [],
  });
  function snapshot(run) { return structuredClone(run); }
  return {
    async start(user_input, source, zone_id, scenario = "normal") {
      if (!FIXTURE_SCENARIOS.has(scenario)) throw new Error("알 수 없는 화면 예시입니다.");
      const goal = fixtureGoal(user_input, zone_id);
      const zoneText = goal.zone === "left" ? "왼쪽" : "오른쪽";
      const run = {
        run_id: `ui-fixture-${++sequence}`, status: goal.message ? "awaiting_clarification" : "awaiting_user",
        execution_mode: "ui_fixture", source, zone_id: goal.zone, user_input, scenario, fixture_step: 0,
        clarification: goal.message,
        instruction: goal.message ? null : { action_id: `ui-action-${sequence}`, text: `${zoneText} 화구의 + 버튼을 한 번 누른 뒤 조작 완료를 누르세요.` },
        verdict: null, events: [],
      };
      run.events.push(event(run, "plan", goal.message || "화력 증가 요청을 화면 예시로 표시합니다."));
      if (run.instruction) run.events.push(event(run, "instruction", run.instruction.text, run.instruction.action_id));
      runs.set(run.run_id, run);
      return snapshot(run);
    },
    async get(id) { return snapshot(runs.get(id)); },
    async ack(id, actionId) {
      const run = runs.get(id);
      if (run.instruction?.action_id !== actionId) throw new Error("현재 안내와 다른 조작입니다.");
      if (run.scenario === "recovery" && run.fixture_step === 0) {
        run.fixture_step = 1;
        run.events.push(event(run, "observation", "화면 예시: 첫 조작이 인식되지 않은 상황을 재현했습니다.", actionId));
        const zoneText = run.zone_id === "left" ? "왼쪽" : "오른쪽";
        run.instruction = { action_id: `ui-action-${++sequence}`, text: `화면 예시의 새 안내입니다. ${zoneText} 화구 상태를 확인한 뒤 + 버튼을 한 번만 다시 누르세요.` };
        run.events.push(event(run, "recovery", run.instruction.text, run.instruction.action_id));
        return snapshot(run);
      }
      run.status = "uncertain";
      run.instruction = null;
      const reason = run.scenario === "sensor_error" ? "UI_FIXTURE_SENSOR_ERROR" : "UI_FIXTURE_ONLY";
      run.verdict = { status: "uncertain", reason_codes: [reason], evidence_ids: [] };
      if (run.scenario === "sensor_error") {
        run.events.push(event(run, "error", "화면 예시: 센서 오류 상황을 재현했습니다. 실제 센서 상태를 읽지 않았습니다."));
      } else {
        run.events.push(event(run, "observation", "화면 예시의 가상 관측 단계입니다. 실제 센서 값은 없습니다."));
      }
      run.events.push(event(run, "verdict", "화면 예시가 종료되었습니다. 실제 센서·서버 판정은 수행하지 않았습니다."));
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
