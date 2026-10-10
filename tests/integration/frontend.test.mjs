import test from "node:test";
import assert from "node:assert/strict";
import { createApi, ApiError, freshSpeechItems, validateRun } from "../../frontend/api.mjs";
import { createFixture, fixtureGoal } from "../../frontend/fixtures.mjs";
import { actionFor, claimableSuccess, phaseFor, unverifiedSnapshot, verdictLabel } from "../../frontend/presentation.mjs";
import { eventChange, eventLabel, eventSource } from "../../frontend/events.mjs";
import { canSendAction, connectionLabel } from "../../frontend/connection.mjs";
import { evidenceReport, evidenceFilename } from "../../frontend/evidence.mjs";
import { createReadGate, regressesTerminalState } from "../../frontend/read-gate.mjs";

test("minimum runs contract uses one request per action and carries source", async () => {
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url, options });
    const response = options.method === "GET" ? { run_id: "r1", status: "planning", execution_mode: "backend_stub",
      source: "mock", instruction: null, verdict: null, events: [] }
      : url.endsWith("/ack") ? { run_id: "r1", status: "observing", accepted: true }
      : url.endsWith("/cancel") ? { run_id: "r1", status: "stopped" }
      : { run_id: "r1", status: "planning", execution_mode: "backend_stub" };
    return { ok: true, json: async () => response };
  };
  const api = createApi("", fakeFetch);
  await api.start("오른쪽 화력 올려줘", "mock", "right");
  await api.get("r1");
  await api.ack("r1", "a1");
  await api.cancel("r1");
  assert.deepEqual(calls.map(call => [call.url, call.options.method]), [
    ["/api/runs", "POST"], ["/api/runs/r1", "GET"],
    ["/api/runs/r1/ack", "POST"], ["/api/runs/r1/cancel", "POST"],
  ]);
  assert.deepEqual(JSON.parse(calls[0].options.body), { user_input: "오른쪽 화력 올려줘", source: "mock", zone_id: "right" });
  assert.deepEqual(JSON.parse(calls[2].options.body), { action_id: "a1" });
});

test("connection and server errors remain distinct", async () => {
  await assert.rejects(createApi("", async () => { throw new Error("offline"); }).get("r1"),
    error => error instanceof ApiError && error.code === "connection");
  await assert.rejects(createApi("", async () => ({ ok: false, status: 409, json: async () => ({ code: "busy", message: "진행 중" }) })).start("a", "mock", "right"),
    error => error instanceof ApiError && error.code === "busy" && error.status === 409);
});

test("cancel response must confirm stopped before the UI trusts it", async () => {
  const api = createApi("", async () => ({ ok: true, json: async () => ({ run_id: "r1", status: "awaiting_user" }) }));
  await assert.rejects(api.cancel("r1"), error => error instanceof ApiError && error.code === "invalid_response");
});

test("invalid or mismatched API run cannot be presented as a current server state", async () => {
  const valid = { run_id: "r1", status: "awaiting_user", execution_mode: "mock_llm", source: "mock",
    instruction: { action_id: "a1", text: "한 번 누르세요" }, verdict: null, events: [] };
  assert.equal(validateRun(valid, "r1").run_id, "r1");
  for (const bad of [
    { ...valid, run_id: "other" },
    { ...valid, status: "succeeded", execution_mode: "unknown" },
    { ...valid, instruction: null },
    { ...valid, verdict: { status: "succeeded", reason_codes: [], evidence_ids: [] } },
    { ...valid, events: [{ event_id: "e1", kind: "verdict", message: "완료" }] },
  ]) {
    assert.throws(() => validateRun(bad, "r1"), error => error instanceof ApiError && error.code === "invalid_response");
  }
  await assert.rejects(createApi("", async () => ({ ok: true, json: async () => ({ ok: true }) })).start("목표", "mock", "right"),
    error => error instanceof ApiError && error.code === "invalid_response");
});

test("existing execution lookup only GETs the encoded run ID", async () => {
  const calls = [];
  const api = createApi("", async (url, options) => {
    calls.push([url, options.method]);
    return { ok: true, json: async () => ({ run_id: "run/42", status: "uncertain", execution_mode: "mock_llm",
      source: "mock", instruction: null, verdict: { status: "uncertain", reason_codes: [], evidence_ids: [] }, events: [] }) };
  });
  const run = await api.get("run/42");
  assert.equal(run.run_id, "run/42");
  assert.deepEqual(calls, [["/api/runs/run%2F42", "GET"]]);
});

test("same event and action are spoken only once across polls", () => {
  const run = { status: "awaiting_user", instruction: { action_id: "a1", text: "버튼을 누르세요" }, events: [
    { event_id: "e1", action_id: "a1", kind: "instruction", message: "버튼을 누르세요" },
  ] };
  const seen = new Set();
  const first = freshSpeechItems(seen, run);
  assert.equal(first.messages.length, 1);
  first.ids.forEach(id => seen.add(id));
  assert.equal(freshSpeechItems(seen, run).messages.length, 0);
  run.events.push({ event_id: "e1-late", action_id: "a1", kind: "instruction", message: "버튼을 누르세요" });
  assert.equal(freshSpeechItems(seen, run).messages.length, 0);
  run.events.push({ event_id: "e2", action_id: "a2", kind: "recovery", message: "상태를 확인하세요" });
  run.instruction = { action_id: "a2", text: "상태를 확인하세요" };
  assert.deepEqual(freshSpeechItems(seen, run).messages.map(item => item.message), ["상태를 확인하세요"]);
});

test("old instruction is neither shown nor spoken after leaving the action state", () => {
  const instruction = { action_id: "a1", text: "오른쪽 + 버튼을 누르세요" };
  for (const status of ["observing", "recovering", "stopped", "uncertain", "failed"]) {
    const run = { status, instruction, events: [
      { event_id: "e1", action_id: "a1", kind: "instruction", message: instruction.text },
    ] };
    assert.doesNotMatch(actionFor(run), /\+ 버튼을 누르세요/);
    assert.deepEqual(freshSpeechItems(new Set(), run).messages, []);
  }
});

test("only the current action can be spoken after recovery", () => {
  const run = { status: "awaiting_user", instruction: { action_id: "a2", text: "새 안내" }, events: [
    { event_id: "e1", action_id: "a1", kind: "instruction", message: "지난 버튼을 누르세요" },
    { event_id: "e2", action_id: "a2", kind: "recovery", message: "새 안내" },
  ] };
  assert.deepEqual(freshSpeechItems(new Set(), run).messages.map(item => item.message), ["새 안내"]);
});

test("UI fixture reports uncertain and never server success", async () => {
  const fixture = createFixture();
  const started = await fixture.start("오른쪽 화력 올려줘", "mock", "right");
  assert.equal(started.execution_mode, "ui_fixture");
  assert.equal(started.status, "awaiting_user");
  await fixture.ack(started.run_id, started.instruction.action_id);
  const result = await fixture.get(started.run_id);
  assert.equal(result.status, "uncertain");
  assert.equal(result.verdict.reason_codes[0], "UI_FIXTURE_ONLY");
  assert.equal(result.events.at(-1).source, "ui_fixture");
});

test("fixture recovery issues a new action before its unverified ending", async () => {
  const fixture = createFixture();
  const first = await fixture.start("오른쪽 화력 올려줘", "mock", "right", "recovery");
  const recovered = await fixture.ack(first.run_id, first.instruction.action_id);
  assert.equal(recovered.status, "awaiting_user");
  assert.notEqual(recovered.instruction.action_id, first.instruction.action_id);
  assert.equal(recovered.events.at(-1).kind, "recovery");
  assert.equal(recovered.events.at(-1).source, "ui_fixture");
  const ended = await fixture.ack(first.run_id, recovered.instruction.action_id);
  assert.equal(ended.status, "uncertain");
  assert.equal(ended.verdict.reason_codes[0], "UI_FIXTURE_ONLY");
});

test("fixture sensor error remains uncertain without fabricated sensor evidence", async () => {
  const fixture = createFixture();
  const first = await fixture.start("오른쪽 화력 올려줘", "live", "right", "sensor_error");
  const ended = await fixture.ack(first.run_id, first.instruction.action_id);
  assert.equal(ended.status, "uncertain");
  assert.deepEqual(ended.verdict.evidence_ids, []);
  assert.equal(ended.verdict.reason_codes[0], "UI_FIXTURE_SENSOR_ERROR");
  assert.equal(ended.events.at(-2).kind, "error");
  assert.equal(ended.events.at(-2).source, "ui_fixture");
});

test("fixture guides the selected zone and pauses on conflicting or unsupported goals", async () => {
  const fixture = createFixture();
  const left = await fixture.start("왼쪽 화구 화력을 한 단계 올려줘", "mock", "left");
  assert.match(left.instruction.text, /왼쪽 화구/);
  const recovered = await fixture.start("왼쪽 화구 화력을 올려줘", "mock", "left", "recovery");
  const next = await fixture.ack(recovered.run_id, recovered.instruction.action_id);
  assert.match(next.instruction.text, /왼쪽 화구/);
  const mismatch = await fixture.start("왼쪽 화구 화력을 올려줘", "mock", "right");
  assert.equal(mismatch.status, "awaiting_clarification");
  assert.equal(mismatch.instruction, null);
  assert.match(mismatch.events[0].message, /다릅니다/);
  const unsupported = await fixture.start("오른쪽 화구를 꺼줘", "mock", "right");
  assert.equal(unsupported.status, "awaiting_clarification");
  assert.equal(unsupported.instruction, null);
  assert.equal(fixtureGoal("화력 올려줘", null).zone, null);
});

test("guided phase names actions without treating uncertain as progress success", () => {
  assert.deepEqual(phaseFor({ status: "awaiting_user" }), ["2/3 · 사용자 조작", "현재 안내를 듣고 직접 조작한 뒤 완료 알림을 누르세요."]);
  assert.match(phaseFor({ status: "recovering" })[1], /다시 조작하지 마세요/);
  assert.match(phaseFor({ status: "uncertain" })[0], /확인 불가/);
  assert.equal(actionFor({ status: "uncertain" }), "인덕션 상태를 직접 확인하세요.");
});

test("fixture and backend stub cannot claim success in verdict panel", () => {
  for (const execution_mode of ["ui_fixture", "backend_stub"]) {
    const run = { status: "succeeded", execution_mode, verdict: { status: "success", verification_level: "exact_step",
      goal_satisfied: true, evidence_ids: ["obs-1"], reason_codes: [] } };
    assert.equal(claimableSuccess(run), false);
    assert.match(phaseFor(run)[0], /성공 근거 부족/);
    assert.match(verdictLabel(run), /성공 근거 부족/);
  }
  assert.equal(claimableSuccess({ status: "succeeded", execution_mode: "real_llm", verdict: {
    status: "succeeded", verification_level: "exact_step", goal_satisfied: true, evidence_ids: ["obs-1"],
  } }), false);
});

test("late and overlapping reads cannot undo a newer run state", async () => {
  let runId = "run-1";
  const gate = createReadGate(() => runId);
  let finishOld;
  const oldResponse = new Promise(resolve => { finishOld = resolve; });
  const old = gate.fetch(runId, () => oldResponse);
  assert.equal((await gate.fetch(runId, async () => ({ status: "awaiting_user" }))).kind, "overlap");

  gate.invalidate(); // cancel or ack started after the GET
  const current = await gate.fetch(runId, async () => ({ status: "stopped" }));
  assert.equal(current.kind, "current");
  assert.equal(current.value.status, "stopped");
  finishOld({ status: "awaiting_user" });
  assert.equal((await old).kind, "stale");

  let finishAnother;
  const another = gate.fetch(runId, () => new Promise(resolve => { finishAnother = resolve; }));
  runId = "run-2";
  gate.invalidate();
  assert.equal((await gate.fetch(runId, async () => ({ status: "planning" }))).kind, "current");
  finishAnother({ status: "awaiting_user" });
  assert.equal((await another).kind, "stale");
  assert.equal(regressesTerminalState({ run_id: "run-1", status: "stopped" },
    { run_id: "run-1", status: "awaiting_user" }), true);
  assert.equal(regressesTerminalState({ run_id: "run-1", status: "stopped" },
    { run_id: "run-2", status: "awaiting_user" }), false);
});

test("server success needs exact step, goal match and evidence before being announced", () => {
  const run = { status: "succeeded", execution_mode: "mock_llm", verdict: {
    status: "success", verification_level: "exact_step", goal_satisfied: true,
    evidence_ids: ["obs-1"], reason_codes: [],
  }, events: [{ event_id: "e1", kind: "verdict", message: "목표를 확인했습니다" }] };
  assert.equal(claimableSuccess(run), true);
  assert.match(verdictLabel(run), /목표 확인 보고/);
  const weak = { ...run, verdict: { ...run.verdict, evidence_ids: [] } };
  assert.equal(claimableSuccess(weak), false);
  assert.match(phaseFor(weak)[0], /성공 근거 부족/);
  assert.match(verdictLabel(weak), /성공 근거 부족/);
  assert.match(freshSpeechItems(new Set(), weak, claimableSuccess(weak)).messages[0].message, /성공 근거가 부족/);
});

test("uncertain verdict is described in Korean", () => {
  assert.match(verdictLabel({ execution_mode: "ui_fixture", verdict: { status: "uncertain", reason_codes: [] } }), /확인 불가/);
});

test("event list appends new records without replacing opened details", () => {
  const first = [{ event_id: "e1" }, { event_id: "e2" }];
  assert.deepEqual(eventChange([], first), { reset: false, ids: ["e1", "e2"], from: 0 });
  assert.deepEqual(eventChange(["e1", "e2"], first), { reset: false, ids: ["e1", "e2"], from: 2 });
  assert.deepEqual(eventChange(["e1", "e2"], [...first, { event_id: "e3" }]),
    { reset: false, ids: ["e1", "e2", "e3"], from: 2 });
  assert.equal(eventChange(["e1", "e2"], [{ event_id: "new" }]).reset, true);
});

test("event labels and input sources are readable without technical codes", () => {
  assert.equal(eventLabel("tool_call"), "도구 호출");
  assert.equal(eventLabel("verdict"), "판정");
  assert.equal(eventSource("ui_fixture"), "화면 예시");
});

test("lost or unverified connection cannot submit a physical action", () => {
  assert.equal(canSendAction("server", "online"), true);
  assert.equal(canSendAction("server", "offline"), false);
  assert.equal(canSendAction("server", "invalid"), false);
  assert.equal(canSendAction("server", "unverified"), false);
  assert.equal(canSendAction("fixture", "unknown"), true);
  assert.match(connectionLabel("server", "offline"), /마지막 확인 값/);
  assert.match(connectionLabel("server", "invalid"), /응답 형식 오류/);
  assert.match(connectionLabel("server", "unverified"), /다시 확인/);
});

test("unverified server snapshot hides the previous action and verdict", () => {
  const display = unverifiedSnapshot();
  assert.match(display.status, /현재 서버 상태를 확인할 수 없습니다/);
  assert.match(display.action, /추가 조작을 멈추고/);
  assert.match(display.verdict, /현재 상태를 보증하지 않습니다/);
  assert.doesNotMatch(Object.values(display).join(" "), /목표 확인 보고|화력이 한 단계 올라갔습니다/);
});

test("downloaded fixture record cannot claim server success", async () => {
  const fixture = createFixture();
  const started = await fixture.start("화력 올려줘", "live", "right", "sensor_error");
  const ended = await fixture.ack(started.run_id, started.instruction.action_id);
  const report = evidenceReport(ended, { fixtureMode: true, connection: "online", submittedRequest: {
    user_input: "화력 올려줘", source: "live", zone_id: "right",
  }, capturedAt: "2026-10-10T00:00:00.000Z" });
  assert.equal(report.classification, "ui_fixture");
  assert.equal(report.snapshot_freshness, "not_applicable");
  assert.equal(report.last_confirmed_at, null);
  assert.equal(report.run.status, "uncertain");
  assert.deepEqual(report.run.verdict.evidence_ids, []);
  assert.equal(report.run.events.at(-2).source, "ui_fixture");
  assert.equal(report.submitted_request.source, "live");
  assert.match(evidenceFilename(report), /^touchback-ui_fixture-/);
});

test("downloaded server record distinguishes stale snapshot and stub", () => {
  const run = { run_id: "r/1", status: "succeeded", execution_mode: "mock_llm", source: "mock",
    events: [{ event_id: "e1", kind: "verdict", message: "확인", evidence_ids: ["obs-1"], secret: "excluded" }] };
  const report = evidenceReport(run, { connection: "offline", lastConfirmedAt: Date.parse("2026-10-10T01:00:00Z") });
  assert.equal(report.classification, "server_response");
  assert.equal(report.snapshot_freshness, "stale_or_unverified_snapshot");
  assert.equal(report.last_confirmed_at, "2026-10-10T01:00:00.000Z");
  assert.equal(report.run.events[0].secret, undefined);
  assert.equal(evidenceFilename(report), "touchback-server_response-r_1.json");
  assert.equal(evidenceReport({ ...run, execution_mode: "backend_stub" }).classification, "backend_stub");
});
