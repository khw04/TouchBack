import { TERMINAL } from "./api.mjs?v=20";
const now = () => new Date().toISOString();
let sequence = 0;

export const FIXTURE_SCENARIOS = new Set(["normal", "recovery", "sensor_error"]);

export function fixtureGoal(userInput, selectedZone) {
  // Same conservative full-request gate as backend contracts/goal_policy.py.
  // Shared regression vectors guard Python/JavaScript parity.
  const match = /^(?:인덕션\s*)?(?:(오른쪽|우측|왼쪽|좌측)\s*(?:화구(?:의)?\s*)?)?(?:화력|출력)(?:을|를)?\s*(?:좀\s*)?(?:(?:한\s*단계|한\s*단|1\s*단계|1\s*단)\s*)?(?:올려(?:줘|주세요)?|높여(?:줘|주세요)?|증가(?:시켜(?:줘|주세요)?|시켜\s*주세요|해(?:줘|주세요)?)?|키워(?:줘|주세요)?|내려(?:줘|주세요)?|낮춰(?:줘|주세요)?|줄여(?:줘|주세요)?|감소시켜(?:줘|주세요)?)[.!]?$/.exec(userInput.trim());
  if (!match) return { zone: null, message: "지원하지 않거나 준비되지 않은 목표입니다. 화력을 한 단계 올리거나 내리는 단일 요청인지 확인해 주세요." };
  const mentionedZone = ["오른쪽", "우측"].includes(match[1]) ? "right" : match[1] ? "left" : null;
  if (selectedZone && mentionedZone && selectedZone !== mentionedZone) {
    return { zone: null, message: "입력한 화구와 선택한 화구가 다릅니다. 화구를 확인해 주세요." };
  }
  const zone = selectedZone || mentionedZone;
  if (!zone) return { zone: null, message: "화구가 지정되지 않았습니다. 조작할 화구를 선택해 주세요." };
  return { zone, direction:/내려|낮춰|줄여|감소/.test(userInput)?"decrease":"increase", message: null };
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
        execution_mode: "ui_fixture", source, zone_id: goal.zone, user_input, scenario, direction:goal.direction, fixture_step: 0,
        clarification: goal.message,
        instruction: goal.message ? null : { action_id: `ui-action-${sequence}`, text: `${zoneText} 화구의 ${goal.direction==="decrease"?"−":"+"} 버튼을 한 번 누른 뒤 조작 완료를 누르세요.` },
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
        run.instruction = { action_id: `ui-action-${++sequence}`, text: `화면 예시의 새 안내입니다. ${zoneText} 화구 상태를 확인한 뒤 ${run.direction==="decrease"?"−":"+"} 버튼을 한 번만 다시 누르세요.` };
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
      if (TERMINAL.has(run.status)) return snapshot(run);
      run.status = "stopped";
      run.instruction = null;
      run.events.push(event(run, "error", "화면 예시가 취소되었습니다."));
      return snapshot(run);
    },
  };
}
