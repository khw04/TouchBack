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

test("freshSpeechItems selects user-facing instruction sentence from recovery response and ignores internal plan description", () => {
  const run = {
    status: "awaiting_user",
    instruction: { action_id: "act-rec-2", text: "오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요." },
    events: [
      { event_id: "e1", action_id: "act-1", kind: "instruction", message: "오른쪽 화구의 + 버튼을 한 번 눌러 주세요." },
      { event_id: "e2", action_id: "act-1", kind: "observation", message: "관측 처리 종료. 판정 수준과 근거를 확인해 주세요." },
      { event_id: "e3", action_id: "act-rec-2", kind: "plan", message: "LLM 재조작 안내 수립 완료" },
      { event_id: "e4", action_id: "act-rec-2", kind: "recovery", message: "오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요." },
      { event_id: "e5", action_id: "act-rec-2", kind: "instruction", message: "오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요." },
      { event_id: "e6", action_id: "act-rec-2", kind: "plan", message: "LLM 복구 토큰 메트릭: model: mock, tokens: 45" },
    ],
  };
  const speech = freshSpeechItems(new Set(), run);
  const messages = speech.messages.map(item => item.message);
  assert.deepEqual(messages, ["오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요."]);
  assert(!messages.includes("LLM 재조작 안내 수립 완료"));
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

test("fixture follows the shared safe-goal regression vectors", async () => {
  const { readFile } = await import("node:fs/promises");
  const cases = JSON.parse(await readFile(new URL("./goal-cases.json", import.meta.url), "utf8"));
  for (const entry of cases) {
    const goal = fixtureGoal(entry.text, entry.selected);
    assert.equal(goal.zone, entry.zone, entry.text);
    const run = await createFixture().start(entry.text, "mock", entry.selected);
    assert.equal(run.status, entry.zone ? "awaiting_user" : "awaiting_clarification", entry.text);
    assert.equal(Boolean(run.instruction), Boolean(entry.zone), entry.text);
  }
});

test("observation step carries the acknowledged action and validates run response", async () => {
  const calls = [];
  const run = { run_id: "r1", status: "uncertain", execution_mode: "backend_stub", source: "mock",
    instruction: null, verdict: { status: "uncertain", reason_codes: ["sensor_adapter_unconfigured"], evidence_ids: [] }, events: [] };
  const api = createApi("", async (url, options) => { calls.push([url, options]); return { ok: true, json: async () => run }; });
  assert.deepEqual(await api.step("r1", "a1"), run);
  assert.equal(calls[0][0], "/api/runs/r1/step");
  assert.equal(calls[0][1].method, "POST");
  assert.deepEqual(JSON.parse(calls[0][1].body), { action_id: "a1" });
});

test("cancel accepts every preserved terminal state but rejects nonterminal replies", async () => {
  for (const status of ["succeeded", "uncertain", "failed", "stopped"]) {
    const api = createApi("", async () => ({ ok: true, json: async () => ({ run_id: "r1", status }) }));
    assert.equal((await api.cancel("r1")).status, status);
  }
});

test("requests and response bodies have a deadline even if transport ignores abort", async () => {
  for (const transport of [
    async () => new Promise(() => {}),
    async () => ({ ok: true, json: async () => new Promise(() => {}) }),
  ]) {
    let signal;
    const api = createApi("", (url, options) => { signal = options.signal; return transport(); }, { timeoutMs: 10 });
    await assert.rejects(api.step("r1", "a1"), error => error.code === "connection");
    assert.equal(signal.aborted, true);
  }
});

test("fixture cancellation preserves its completed verdict and repeated cancellation", async () => {
  const fixture = createFixture();
  const run = await fixture.start("화력 올려줘", "mock", "right");
  const final = await fixture.ack(run.run_id, run.instruction.action_id);
  assert.equal(final.status, "uncertain");
  assert.deepEqual(await fixture.cancel(run.run_id), final);
  assert.deepEqual(await fixture.cancel(run.run_id), final);
  const active = await fixture.start("화력 올려줘", "mock", "right");
  const stopped = await fixture.cancel(active.run_id);
  assert.equal(stopped.status, "stopped");
  assert.deepEqual(await fixture.cancel(active.run_id), stopped);
});

test("operator demo configuration carries no scenario label in the Agent start request", async () => {
  const metadata = { enabled: true, source: "mock", execution_mode: "mock_llm", scenario: "recovery",
    scenarios: ["normal", "recovery"], physical_validation_performed: false };
  const calls=[];
  const api=createApi("",async (url,options) => {
    calls.push([url,options]);
    return {ok:true,json:async()=>url==="/api/runs"?{run_id:"r1",status:"planning",execution_mode:"mock_llm"}:metadata};
  });
  assert.deepEqual(await api.demoInfo(),metadata);
  assert.deepEqual(await api.configureDemo("recovery"),metadata);
  await api.start("화력 올려줘","mock","right");
  assert.deepEqual(JSON.parse(calls[1][1].body),{scenario:"recovery"});
  assert.deepEqual(JSON.parse(calls[2][1].body),{user_input:"화력 올려줘",source:"mock",zone_id:"right"});
});

test("operator controls reject malformed or non-Mock metadata", async () => {
  const valid={enabled:true,source:"mock",execution_mode:"mock_llm",scenario:"normal",scenarios:["normal"],physical_validation_performed:false};
  const {validateDemoMetadata}=await import("../../frontend/api.mjs");
  assert.deepEqual(validateDemoMetadata({enabled:false}),{enabled:false});
  for(const bad of [null,{}, {...valid,source:"live"},{...valid,execution_mode:"backend_stub"},
    {...valid,scenario:"unknown"},{...valid,scenarios:["normal","normal"]},{...valid,physical_validation_performed:true}]) {
    assert.throws(()=>validateDemoMetadata(bad),error=>error.code==="invalid_response");
  }
});

test("demo settings cannot change during planning, observation or uncertain start", async () => {
  const {demoControlsDisabled,demoLabel}=await import("../../frontend/demo.mjs");
  for(const status of ["planning","awaiting_clarification","awaiting_user","observing","recovering"])
    assert.equal(demoControlsDisabled({run:{status}}),true);
  for(const status of ["succeeded","uncertain","stopped","failed"])
    assert.equal(demoControlsDisabled({run:{status}}),false);
  assert.equal(demoControlsDisabled({busy:true}),true);
  assert.equal(demoControlsDisabled({startUnknown:true}),true);
  assert.equal(demoLabel("recovery"),"미인식 후 한 번 복구");
});

test("actual app blocks form submission and speech while initial demo lookup is pending", async () => {
  const {loadApp,ok,demoMetadata,activeRun,flush}=await import('./app-harness.mjs');
  let release,starts=0;
  const pending=new Promise(resolve=>{release=resolve;});
  const app=await loadApp(async (url,options)=>{
    if(url==='/api/demo')return pending;
    if(options.method==='POST'){starts++;return ok(activeRun);}
    return ok(activeRun);
  });
  try {
    assert.equal(app.el('start').disabled,true);
    assert.equal(app.el('demo-controls').hidden,false);
    await app.el('goal-form').fire('submit');
    assert.equal(starts,0);assert.deepEqual(app.spoken,[]);
    release(ok(demoMetadata));await flush();
    assert.equal(app.el('start').disabled,false);
    await app.el('goal-form').fire('submit');
    assert.equal(starts,1);
    assert.match(app.el('phase-detail').textContent,/실물은 조작하지 말고/);
  } finally { release(ok(demoMetadata));await flush();app.close(); }
});

test("actual app keeps failed initial lookup locked and exposes successful retry", async () => {
  const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');
  let requests=0,starts=0;
  const app=await loadApp(async (url)=>{
    if(url==='/api/demo'){if(++requests===1)throw new Error('offline');return ok(demoMetadata);}
    starts++;throw new Error('must not start');
  });
  try {
    assert.equal(app.el('start').disabled,true);
    assert.equal(app.el('demo-controls').hidden,false);
    assert.equal(app.el('demo-reload').disabled,false);
    assert.match(app.el('demo-status').textContent,/확인하지 못|확인할 수 없/);
    await app.el('goal-form').fire('submit');assert.equal(starts,0);assert.deepEqual(app.spoken,[]);
    await app.el('demo-reload').fire('click');
    assert.equal(app.el('start').disabled,false);
    assert.equal(app.el('demo-scenario').value,'normal');
  } finally { app.close(); }
});

test("actual app preserves unknown selection after a subsequent 409 until confirmed lookup", async () => {
  const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');
  let selected='normal',changes=0;
  const app=await loadApp(async (url,options)=>{
    if(options.method==='GET')return ok({...demoMetadata,scenario:selected});
    if(++changes===1){selected='recovery';throw new Error('response lost');}
    return {ok:false,status:409,json:async()=>({code:'busy',message:'other tab active'})};
  });
  try {
    app.el('demo-scenario').value='recovery';await app.el('demo-apply').fire('click');
    assert.equal(app.el('start').disabled,true);
    await app.el('demo-apply').fire('click');
    assert.equal(app.el('start').disabled,true);
    await app.el('demo-reload').fire('click');
    assert.equal(app.el('start').disabled,false);assert.equal(app.el('demo-scenario').value,'recovery');
  } finally { app.close(); }
});

test("actual app distinguishes confirmed disabled metadata and fixture from unknown", async () => {
  const {loadApp,ok}=await import('./app-harness.mjs');
  let calls=0;
  const app=await loadApp(async()=>{calls++;return ok({enabled:false});});
  try {assert.equal(app.el('start').disabled,false);assert.equal(app.el('demo-controls').hidden,true);}
  finally {app.close();}
  const fixture=await loadApp(async()=>{throw new Error('fixture must not fetch');},'?fixture=1');
  try {assert.equal(fixture.el('start').disabled,false);assert.equal(fixture.el('demo-controls').hidden,true);assert.equal(calls,1);}
  finally {fixture.close();}
});

test("actual app hides resumed instructions and speech until demo settings are confirmed", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let reads=0;
  const app=await loadApp(async url=>{
    if(url==='/api/demo'){if(++reads===1)throw new Error('offline');return ok(demoMetadata);}
    return ok(activeRun);
  });
  try {
    app.el('resume-id').value='run-test';await app.el('resume-form').fire('submit');
    assert.equal(app.el('ack').disabled,true);assert.equal(app.el('repeat').disabled,true);
    assert.match(app.el('instruction').textContent,/보류/);assert.deepEqual(app.spoken,[]);
    await app.el('demo-reload').fire('click');
    assert.equal(app.el('ack').disabled,false);
    assert.match(app.el('phase-detail').textContent,/실물은 조작하지 말고/);
  } finally {app.close();}
});

test("actual app treats malformed initial metadata as unknown rather than disabled", async () => {
  const {loadApp,ok}=await import('./app-harness.mjs');
  let reads=0;
  const app=await loadApp(async()=>ok(++reads===1?{}:{enabled:false}));
  try {
    assert.equal(app.el('start').disabled,true);assert.equal(app.el('demo-controls').hidden,false);
    await app.el('demo-reload').fire('click');
    assert.equal(app.el('start').disabled,false);assert.equal(app.el('demo-controls').hidden,true);
  } finally {app.close();}
});

test("metadata retry cannot unmask a run whose server snapshot is unverified", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let runReads=0;
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(options.method==='GET' && ++runReads>1)throw new Error('run connection lost');
    return ok(activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');
    await app.el('retry').fire('click');
    const masked=app.el('instruction').textContent;
    assert.doesNotMatch(masked,/\+ 버튼/);
    await app.el('demo-reload').fire('click');
    assert.equal(app.el('instruction').textContent,masked);
    assert.equal(app.el('ack').disabled,true);
  } finally {app.close();}
});

test("actual app speech harness exercises both supported and unsupported browsers", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  for(const speechSupported of [true,false]){
    const app=await loadApp(async url=>ok(url==='/api/demo'?demoMetadata:activeRun),'',{speechSupported});
    try {
      await app.el('goal-form').fire('submit');
      assert.equal(app.el('auto-speech').disabled,speechSupported?false:true);
      assert.deepEqual(app.spoken,speechSupported?[`모의 시연. ${activeRun.instruction.text}`]:[]);
    } finally {app.close();}
  }
});

for(const ending of ['stopped','succeeded','unknown'])test(`cancel intent suppresses recovery and terminal speech before ${ending} response`, async () => {
  const {loadApp,ok,demoMetadata,activeRun,flush}=await import('./app-harness.mjs');
  let releaseStep,releaseCancel,rejectCancel,ack=false,confirmed=false;
  const step=new Promise(resolve=>{releaseStep=resolve;});
  const cancel=new Promise((resolve,reject)=>{releaseCancel=resolve;rejectCancel=reject;});
  const retryText='오른쪽 화구의 + 버튼을 다시 한 번 눌러 주세요.';
  const recovery={...activeRun,instruction:{action_id:'act-2',text:retryText},events:[...activeRun.events,
    {event_id:'evt-2',run_id:'run-test',action_id:'act-2',kind:'recovery',timestamp:'2026-10-18T00:00:01Z',source:'mock',message:retryText,evidence_ids:[]}]};
  const terminal={...activeRun,status:ending==='succeeded'?'succeeded':'stopped',instruction:null,
    verdict:ending==='succeeded'?{status:'success',verification_level:'exact_step',goal_satisfied:true,reason_codes:['requested_step_verified'],evidence_ids:['mock-evidence']}:null};
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/ack')){ack=true;return ok({run_id:'run-test',status:'observing',accepted:true});}
    if(url.endsWith('/step'))return step;
    if(url.endsWith('/cancel'))return cancel;
    return ok(confirmed?terminal:ack?{...activeRun,status:'observing'}:activeRun);
  });
  let cancellation;
  try {
    await app.el('goal-form').fire('submit');await app.el('ack').fire('click');
    const before=app.spoken.length,cancelCount=app.cancelCount();
    cancellation=app.el('cancel').fire('click');await flush();
    assert.ok(app.cancelCount()>cancelCount);
    assert.doesNotMatch(app.el('instruction').textContent,/\+ 버튼/);
    releaseStep(ok(recovery));await flush();
    assert.doesNotMatch(app.el('instruction').textContent,/\+ 버튼/);
    assert.equal(app.el('ack').disabled,true);assert.equal(app.el('repeat').disabled,true);
    assert.deepEqual(app.spoken.slice(before),[]);
    if(ending==='unknown'){
      rejectCancel(new Error('cancel response lost'));await cancellation;
      await app.el('retry').fire('click');
      assert.doesNotMatch(app.el('instruction').textContent,/\+ 버튼/);
      assert.equal(app.el('ack').disabled,true);assert.equal(app.el('repeat').disabled,true);
    }else{
      confirmed=true;releaseCancel(ok({run_id:'run-test',status:ending}));await cancellation;
      assert.match(app.el('status').textContent,ending==='stopped'?/실행 중단/:/목표 확인/);
      assert.deepEqual(app.spoken.slice(before),[]);
      assert.equal(app.el('start').disabled,false);
    }
  } finally {
    releaseStep(ok(recovery));confirmed=true;releaseCancel(ok({run_id:'run-test',status:'stopped'}));
    if(cancellation)await cancellation;app.close();
  }
});

test("manual confirmed lookup retries a lost step once with the same action", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let ack=false,steps=[];
  const terminal={...activeRun,status:'uncertain',instruction:null,verdict:{status:'uncertain',verification_level:'none',goal_satisfied:null,reason_codes:['synthetic-check'],evidence_ids:[]}};
  let finished=false;
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/ack')){ack=true;return ok({run_id:'run-test',status:'observing',accepted:true});}
    if(url.endsWith('/step')){
      steps.push(JSON.parse(options.body));if(steps.length===1)throw new Error('request lost before server');
      finished=true;return ok(terminal);
    }
    return ok(finished?terminal:ack?{...activeRun,status:'observing'}:activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');await app.el('ack').fire('click');
    assert.equal(steps.length,1);
    await app.el('retry').fire('click');
    assert.deepEqual(steps,[{action_id:'act-1'},{action_id:'act-1'}]);
    assert.match(app.el('status').textContent,/불확실/);
    await app.el('retry').fire('click');assert.equal(steps.length,2);
  } finally {app.close();}
});

test("polls do not duplicate an inflight step or automatically repeat failed POSTs", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let ack=false,steps=0;
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/ack')){ack=true;return ok({run_id:'run-test',status:'observing',accepted:true});}
    if(url.endsWith('/step')){steps++;throw new Error('offline');}
    return ok(ack?{...activeRun,status:'observing'}:activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');await app.el('ack').fire('click');
    for(let i=0;i<3;i++)await app.el('retry').fire('click');
    assert.equal(steps,2);
    assert.match(app.el('error').textContent,/한도|재시도/);
  } finally {app.close();}
});

test("automatic polls and manual lookup cannot duplicate a pending observation POST", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let ack=false,steps=0,release;
  const pending=new Promise(resolve=>{release=resolve;});
  const terminal={...activeRun,status:'uncertain',instruction:null,verdict:{status:'uncertain',verification_level:'none',goal_satisfied:null,reason_codes:['synthetic-check'],evidence_ids:[]}};
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/ack')){ack=true;return ok({run_id:'run-test',status:'observing',accepted:true});}
    if(url.endsWith('/step')){steps++;return pending;}
    return ok(ack?{...activeRun,status:'observing'}:activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');await app.el('ack').fire('click');
    for(let i=0;i<3;i++)await app.poll();
    await app.el('retry').fire('click');assert.equal(steps,1);
    release(ok(terminal));
  } finally {release(ok(terminal));await (await import('./app-harness.mjs')).flush();app.close();}
});

test("automatic polls after failed delivery do not silently resend an observation", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let ack=false,steps=0;
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/ack')){ack=true;return ok({run_id:'run-test',status:'observing',accepted:true});}
    if(url.endsWith('/step')){steps++;throw new Error('request never reached server');}
    return ok(ack?{...activeRun,status:'observing'}:activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');await app.el('ack').fire('click');
    for(let i=0;i<3;i++)await app.poll();assert.equal(steps,1);
    assert.equal(app.el('error').hidden,false);assert.match(app.el('error').textContent,/상태 다시 확인/);
    await app.el('retry').fire('click');assert.equal(steps,2);
    for(let i=0;i<3;i++)await app.poll();assert.equal(steps,2);
  } finally {app.close();}
});

test("a new goal after cancellation can speak its own instruction", async () => {
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let created=0,cancelled=false;
  const next={...activeRun,run_id:'run-next',instruction:{action_id:'act-next',text:'왼쪽 화구의 + 버튼을 한 번 눌러 주세요.'},events:[]};
  const app=await loadApp(async(url,options)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url==='/api/runs'&&options.method==='POST')return ok(++created===1?activeRun:next);
    if(url.endsWith('/cancel')){cancelled=true;return ok({run_id:'run-test',status:'stopped'});}
    return ok(url.includes('run-next')?next:cancelled?{...activeRun,status:'stopped',instruction:null}:activeRun);
  });
  try {
    await app.el('goal-form').fire('submit');await app.el('cancel').fire('click');
    const before=app.spoken.length;
    app.el('user-input').value='왼쪽 화력 올려줘';app.el('zone').value='left';
    await app.el('goal-form').fire('submit');
    assert.deepEqual(app.spoken.slice(before),[`모의 시연. ${next.instruction.text}`]);
    assert.equal(app.el('ack').disabled,false);assert.equal(app.el('repeat').disabled,false);
  } finally {app.close();}
});


test("single-screen summary reports server evidence without inventing sensor readings", async () => {
  const {screenSummary}=await import('../../frontend/screen.mjs');
  assert.match(screenSummary(null).result,/아직/);
  const run={status:'succeeded',source:'mock',execution_mode:'mock_llm',verdict:{status:'success',verification_level:'exact_step',goal_satisfied:true,evidence_ids:['e1'],reason_codes:[]}};
  const summary=screenSummary(run);
  assert.match(summary.result,/모의 관측에서 한 단계 증가/);
  assert.match(summary.sensor,/1건/);
  assert.match(summary.detail,/실제 AI 호출 없음/);
  assert.doesNotMatch(JSON.stringify(summary),/120|비프음.*1회/);
  for(const mode of ['ui_fixture','backend_stub']) assert.doesNotMatch(screenSummary({...run,execution_mode:mode}).result,/증가를 확인/);
  assert.doesNotMatch(screenSummary({...run,verdict:{...run.verdict,evidence_ids:[]}}).result,/증가를 확인/);
});

test("single-screen uncertain reasons distinguish unconfigured input and processing errors", async()=>{
  const {screenSummary}=await import('../../frontend/screen.mjs');
  const run={status:'uncertain',execution_mode:'backend_stub',source:'mock',verdict:{reason_codes:['sensor_adapter_unconfigured'],evidence_ids:[]}};
  assert.match(screenSummary(run).result,/설정되지 않아/);
  assert.match(screenSummary({...run,verdict:{...run.verdict,reason_codes:['observation_timeout']}}).result,/제한 시간/);
  assert.match(screenSummary({...run,verdict:{...run.verdict,reason_codes:['unknown_reason']}}).result,/확인하지 못/);
});

test("goal presets set matching input and zone without starting a run", async()=>{
  const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');
  const calls=[];
  const app=await loadApp(async(url,options)=>{calls.push([url,options.method]);return ok(demoMetadata);});
  try{
    await app.el('preset-left').fire('click');
    assert.equal(app.el('zone').value,'left');assert.match(app.el('user-input').value,/왼쪽/);
    await app.el('preset-right').fire('click');
    assert.equal(app.el('zone').value,'right');assert.match(app.el('user-input').value,/오른쪽/);
    assert.deepEqual(calls,[['/api/demo','GET']]);
    assert.match(app.el('screen-mode').textContent,/모의 모델/);
    assert.match(app.el('screen-source').textContent,/모의 센서/);
  } finally{app.close();}
});

test("active run prevents preset edits and cancellation masks the compact summary",async()=>{
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let cancelResolve;
  const app=await loadApp(async(url)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/cancel'))return new Promise(resolve=>{cancelResolve=resolve;});
    return ok(activeRun);
  });
  try{
    await app.el('goal-form').fire('submit');
    assert.equal(app.el('preset-left').disabled,true);
    await app.el('preset-left').fire('click');assert.equal(app.el('zone').value,'');
    assert.match(app.el('agent-stage').textContent,/조작 안내/);
    const pending=app.el('cancel').fire('click');
    assert.match(app.el('result-summary').textContent,/취소 결과/);
    assert.match(app.el('agent-stage').textContent,/보류/);
    cancelResolve(ok({run_id:activeRun.run_id,status:'stopped'}));await pending;
  }finally{app.close();}
});

test("failed state lookup masks compact summary as well as the main instruction",async()=>{
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let broken=false;
  const app=await loadApp(async(url)=>{if(url==='/api/demo')return ok(demoMetadata);if(broken)throw new Error('offline');return ok(activeRun);});
  try{
    await app.el('goal-form').fire('submit');broken=true;
    await app.el('retry').fire('click');
    assert.match(app.el('agent-stage').textContent,/현재 상태 확인 필요/);
    assert.match(app.el('sensor-stage').textContent,/확인 불가/);
    assert.match(app.el('result-summary').textContent,/현재 상태를 보증하지 않/);
  }finally{app.close();}
});

test("demo metadata failure exposes settings recovery and fixture is explicitly labeled",async()=>{
  const {loadApp}=await import('./app-harness.mjs');
  const app=await loadApp(async()=>{throw new Error('offline');});
  try{assert.equal(app.el('settings-panel').open,true);assert.equal(app.el('start').disabled,true);assert.match(app.el('screen-mode').textContent,/확인 실패/);}finally{app.close();}
  const fixture=await loadApp(async()=>{throw new Error('must not fetch');},'?fixture=1');
  try{assert.match(fixture.el('screen-source').textContent,/서버 판정 없음/);}finally{fixture.close();}
});


test("confirmed cancellation replaces compact pending text with the stopped result",async()=>{
  const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
  let cancelled=false;
  const app=await loadApp(async(url)=>{
    if(url==='/api/demo')return ok(demoMetadata);
    if(url.endsWith('/cancel')){cancelled=true;return ok({run_id:activeRun.run_id,status:'stopped'});}
    return ok(cancelled?{...activeRun,status:'stopped',instruction:null}:activeRun);
  });
  try{
    await app.el('goal-form').fire('submit');await app.el('cancel').fire('click');
    assert.match(app.el('result-summary').textContent,/취소|종료/);
    assert.doesNotMatch(app.el('result-summary').textContent,/취소 결과를 확인/);
    assert.equal(app.el('start').disabled,false);
  }finally{app.close();}
});


test("local disabled demo requires explicit screen activation without a model fallback",async()=>{
 const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');
 const disabled={enabled:false,manageable:true,api_key_configured:false,model:'configured-model',execution_mode:'backend_stub',scenario:'normal',scenarios:['normal','recovery']};
 const calls=[];
 const app=await loadApp(async(url,options)=>{calls.push([url,options.body]);return ok(url==='/api/demo'?disabled:{...demoMetadata,manageable:true,api_key_configured:false,virtual_cooktop:true});});
 try{
  assert.equal(app.el('start').disabled,true);assert.equal(app.el('settings-panel').open,true);
  assert.equal(app.el('real-mode-option').disabled,true);
  await app.el('demo-apply').fire('click');
  assert.deepEqual(JSON.parse(calls.at(-1)[1]),{execution_mode:'mock_llm',scenario:'normal'});
  assert.equal(app.el('start').disabled,false);
 }finally{app.close();}
});

test("scenario selection applies once and lost response locks starting until confirmed",async()=>{
 const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');let changed=false;const posts=[];
 const app=await loadApp(async(url,options)=>{
  if(options.method==='POST'){posts.push(url);changed=true;throw new Error('response lost');}
  return ok({...demoMetadata,scenario:changed?'recovery':'normal'});
 });
 try{
  app.el('demo-scenario').value='recovery';await app.el('demo-scenario').fire('change');
  assert.deepEqual(posts,['/api/demo/scenario']);assert.equal(app.el('start').disabled,true);
  await app.el('demo-reload').fire('click');assert.equal(app.el('start').disabled,false);
  assert.equal(app.el('demo-scenario').value,'recovery');
 }finally{app.close();}
});


test("virtual button performs one press and one observation without an extra ack",async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');const posts=[];
 const sim={kind:'virtual_cooktop',levels:{left:5,right:5},last_press:null,observations:[]};
 const initial={...activeRun,simulation:sim};
 const ended={...initial,status:'succeeded',instruction:null,verdict:{status:'success',verification_level:'exact_step',goal_satisfied:true,reason_codes:[],evidence_ids:['e1']},simulation:{...sim,levels:{left:5,right:6}}};
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(options.method==='POST')posts.push(url);
  if(url.endsWith('/press'))return ok({...initial,status:'observing',simulation:ended.simulation});
  if(url.endsWith('/step'))return ok(ended);
  return ok(initial);
 });
 try{
  await app.el('goal-form').fire('submit');assert.equal(app.el('ack').hidden,true);
  await app.el('virtual-right-increase').fire('click');
  assert.equal(posts.filter(p=>p.endsWith('/press')).length,1);assert.equal(posts.filter(p=>p.endsWith('/step')).length,1);
  assert.equal(posts.filter(p=>p.endsWith('/ack')).length,0);assert.equal(app.el('level-right').textContent,'6');
  assert.equal(app.el('virtual-right-increase').disabled,true);assert.match(app.el('result-summary').textContent,/모의 관측에서/);
 }finally{app.close();}
});

test("lost virtual press response blocks further presses until status is rechecked",async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let presses=0;
 const run={...activeRun,simulation:{kind:'virtual_cooktop',levels:{left:5,right:5},last_press:null,observations:[]}};
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(url.endsWith('/press')){presses++;throw new Error('lost');}
  return ok(run);
 });
 try{
  await app.el('goal-form').fire('submit');await app.el('virtual-right-increase').fire('click');
  assert.equal(app.el('virtual-right-increase').disabled,true);
  await app.el('virtual-right-increase').fire('click');assert.equal(presses,1);
  await app.el('retry').fire('click');assert.equal(app.el('virtual-right-increase').disabled,false);assert.equal(presses,1);
 }finally{app.close();}
});


test("sensor visualization uses only consumed current press evidence and preserves missing values",async()=>{
 const {signalsFor,trailFor}=await import('../../frontend/signals.mjs');
 const observation={observation_id:'obs',action_id:'a1',device_observation:{beep_count:1,field_summary:{rms:120,unit:'adc_count'},quality:{status:'valid'}}};
 const sim={kind:'virtual_cooktop',levels:{left:5,right:6},last_press:{action_id:'a1'},baselines:{a1:{device_observation:{field_summary:{rms:100,unit:'adc_count'}}}},observations:[observation]};
 assert.equal(signalsFor({simulation:{...sim,observations:[]},status:'observing'}).rms,null);
 const signal=signalsFor({simulation:sim,status:'succeeded'});
 assert.equal(signal.beep,'1회');assert.equal(signal.field,'120 adc_count');assert.equal(signal.delta,'+20');assert.equal(signal.quality,'정상');
 const retry=signalsFor({simulation:sim,status:'awaiting_user',instruction:{action_id:'a2'}});assert.match(retry.note,/이전 조작/);
 const broken=signalsFor({simulation:{...sim,observations:[{...observation,device_observation:{beep_count:null,field_summary:null,quality:{status:'invalid'}}}]},status:'uncertain'});
 assert.equal(broken.rms,null);assert.equal(broken.beep,'확인 불가');assert.equal(broken.quality,'불량');
 assert.deepEqual(trailFor({events:[{kind:'plan'},{kind:'tool_call'},{kind:'instruction'},{kind:'observation'},{kind:'verdict'}]}).map(e=>e.kind),['instruction','observation','verdict']);
});

test("malformed simulation signal data cannot be accepted as a server snapshot",()=>{
 const base={run_id:'r',status:'uncertain',execution_mode:'mock_llm',source:'mock',instruction:null,verdict:null,events:[],simulation:{kind:'virtual_cooktop',levels:{left:5,right:6},last_press:null,observations:[]}};
 for(const simulation of [
  {...base.simulation,levels:{left:'5',right:6}},
  {...base.simulation,observations:[{observation_id:'x',run_id:'other'}]},
  {...base.simulation,baselines:{a:{observation_id:'x',device_observation:{field_summary:{rms:'100',unit:'adc_count'}}}}}
 ])assert.throws(()=>validateRun({...base,simulation},'r'),/유효하지/);
});


test("again rehearsal starts one new run only after a confirmed terminal result",async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let starts=0;
 const result={...activeRun,status:'uncertain',instruction:null,verdict:{status:'uncertain',reason_codes:['observations_missing'],evidence_ids:[]}};
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok(demoMetadata);
  if(url==='/api/runs'&&options.method==='POST'){starts++;return ok({...result,run_id:'r'+starts});}
  return ok({...result,run_id:'r'+starts});
 });
 try{
  assert.equal(app.el('restart').disabled,true);
  await app.el('goal-form').fire('submit');assert.equal(app.el('restart').disabled,false);
  await app.el('restart').fire('click');assert.equal(starts,2);
 }finally{app.close();}
});

test("drawers close with Escape and supported mock speech clearly labels the simulation",async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');
 const app=await loadApp(async url=>ok(url==='/api/demo'?demoMetadata:activeRun));
 try{
  app.el('settings-panel').open=true;await app.el('settings-panel').fire('keydown',{key:'Escape'});assert.equal(app.el('settings-panel').open,false);
  await app.el('goal-form').fire('submit');assert.match(app.spoken.at(-1),/^모의 시연\./);
  await app.el('restart').fire('click');assert.equal(app.el('restart').disabled,true);
 }finally{app.close();}
});


test("stopped and failed summaries do not reuse a preceding no-change verdict",async()=>{
 const {screenSummary}=await import('../../frontend/screen.mjs');
 const run={execution_mode:'mock_llm',source:'mock',verdict:{status:'mismatch',reason_codes:['no_output_change'],evidence_ids:['obs']}};
 assert.match(screenSummary({...run,status:'stopped'}).result,/종료/);
 assert.doesNotMatch(screenSummary({...run,status:'stopped'}).result,/출력 변화/);
 assert.match(screenSummary({...run,status:'failed'}).result,/오류/);
});


function signalRun({id='flow-run',action='flow-action',status='succeeded',beep=1,rms=120,quality='valid',observations=true}={}) {
 const device={source:'mock',zone_id:'right',beep_count:beep,field_summary:rms===null?null:{rms,unit:'adc_count'},quality:{status:quality}};
 return {run_id:id,source:'mock',status,simulation:{kind:'virtual_cooktop',levels:{left:5,right:6},last_press:{action_id:action},
  baselines:{[action]:{observation_id:'baseline',device_observation:{field_summary:{rms:100,unit:'adc_count'}}}},
  observations:observations?[{run_id:id,action_id:action,observation_id:'receipt-1',device_observation:device}]:[]}};
}

test('signal flow waits for consumed evidence and animates each receipt only once',async()=>{
 const {createSignalFlow}=await import('../../frontend/signals.mjs');const flow=createSignalFlow();
 assert.equal(flow.update(signalRun({status:'observing',observations:false})).phase,'waiting');
 const delivered=flow.update(signalRun());assert.equal(delivered.phase,'delivering');assert.equal(delivered.beepLabel,'1회');assert.equal(delivered.fieldLabel,'Δ +20');
 assert.equal(flow.update(signalRun()).sequence,delivered.sequence);
 flow.finish(delivered.sequence);
 assert.equal(flow.update(signalRun()).phase,'received');
 const next=signalRun({action:'new-action'});assert.equal(flow.update(next).phase,'delivering');
 assert.equal(flow.update(signalRun({id:'new-run',action:'new-action'})).phase,'delivering');
});

test('zero change is delivered neutrally; missing and invalid channels never emit healthy packets',async()=>{
 const {createSignalFlow}=await import('../../frontend/signals.mjs');
 const zero=createSignalFlow().update(signalRun({status:'awaiting_user',beep:0,rms:100}));
 assert.equal(zero.phase,'delivering');assert.equal(zero.beepLabel,'0회');assert.equal(zero.fieldLabel,'Δ 0');assert.match(zero.note,/변화 없음/);
 for(const quality of ['invalid','degraded']){
  const bad=createSignalFlow().update(signalRun({beep:null,rms:null,quality,status:'uncertain'}));
  assert.equal(bad.phase,'received');assert.equal(bad.beep,'warning');assert.equal(bad.field,'warning');assert.match(bad.note,/누락\/품질/);
 }
 const partialFlow=createSignalFlow();const partial=partialFlow.update(signalRun({beep:null,status:'uncertain'}));
 assert.equal(partial.beep,'warning');assert.equal(partial.field,'delivering');
 assert.match(partialFlow.finish(partial.sequence).note,/누락\/품질/);
 const absent=createSignalFlow().update(signalRun({observations:false,status:'uncertain'}));
 assert.equal(absent.phase,'idle');assert.doesNotMatch(absent.note,/전달됨/);
});

test('signal flow excludes stale actions, other runs and non-Mock observations',async()=>{
 const {createSignalFlow}=await import('../../frontend/signals.mjs');
 for(const mutate of [r=>r.simulation.observations[0].action_id='old',r=>r.simulation.observations[0].run_id='other',r=>r.simulation.observations[0].device_observation.source='live',r=>r.source='live']){
  const run=signalRun();mutate(run);assert.equal(createSignalFlow().update(run).phase,'idle');
 }
});

test('cancel, unknown state and reduced motion stop packets without replaying old evidence',async()=>{
 const {createSignalFlow}=await import('../../frontend/signals.mjs');const flow=createSignalFlow();
 const first=flow.update(signalRun());flow.pause();assert.equal(flow.finish(first.sequence).phase,'blocked');
 assert.equal(flow.update(signalRun()).phase,'received');
 const unknown=createSignalFlow();assert.equal(unknown.update(signalRun(),{blocked:true}).phase,'blocked');assert.equal(unknown.update(signalRun()).phase,'received');
 for(const status of ['stopped','failed'])assert.equal(createSignalFlow().update(signalRun({status})).phase,'blocked');
 const quiet=createSignalFlow();assert.equal(quiet.update(signalRun(),{reducedMotion:true}).phase,'received');
 assert.equal(quiet.update(signalRun()).phase,'received');
 const moving=createSignalFlow();moving.update(signalRun());assert.equal(moving.update(signalRun(),{reducedMotion:true}).phase,'received');
});

test('actual app renders a consumed receipt once and masks animation on failed state lookup',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let broken=false;
 const ended={...activeRun,...signalRun({id:activeRun.run_id}),execution_mode:'mock_llm',instruction:null,verdict:{status:'uncertain',reason_codes:[],evidence_ids:[]},status:'uncertain'};
 const app=await loadApp(async(url)=>{if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});if(broken)throw new Error('offline');return ok(ended);});
 try{
  await app.el('goal-form').fire('submit');
  assert.equal(app.el('signal-flow').attributes.get('data-phase'),'delivering');
  const sequence=app.el('signal-flow').attributes.get('data-sequence');
  await app.el('retry').fire('click');assert.equal(app.el('signal-flow').attributes.get('data-sequence'),sequence);
  broken=true;await app.el('retry').fire('click');assert.equal(app.el('signal-flow').attributes.get('data-phase'),'blocked');
  broken=false;await app.el('retry').fire('click');assert.equal(app.el('signal-flow').attributes.get('data-phase'),'received');
 }finally{app.close();}
});

test('actual app cancels a moving receipt immediately and resets it before the next start response',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let cancelled=false,startResolve,starts=0;
 const run={...activeRun,...signalRun({id:activeRun.run_id,status:'awaiting_user'}),instruction:activeRun.instruction};
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(url==='/api/runs' && options.method==='POST' && ++starts===2)return new Promise(resolve=>{startResolve=resolve;});
  if(url.endsWith('/cancel')){cancelled=true;return ok({run_id:run.run_id,status:'stopped'});}
  return ok(cancelled?{...run,status:'stopped',instruction:null}:run);
 });
 try{
  await app.el('goal-form').fire('submit');assert.equal(app.el('signal-flow').attributes.get('data-phase'),'delivering');
  await app.el('cancel').fire('click');assert.equal(app.el('signal-flow').attributes.get('data-phase'),'blocked');
  const pending=app.el('goal-form').fire('submit');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.el('signal-flow').attributes.get('data-phase'),'idle');assert.equal(app.el('flow-beep-label').textContent,'대기');
  startResolve(ok({...run,status:'stopped',instruction:null}));await pending;
 }finally{app.close();}
});


test('actual app shows waiting without packets until the observation response arrives',async()=>{
 const {loadApp,ok,demoMetadata,activeRun,flush}=await import('./app-harness.mjs');let stepResolve;
 const sim=signalRun({id:activeRun.run_id,status:'observing',observations:false}).simulation;
 const initial={...activeRun,simulation:{...sim,last_press:null}};
 const observed={...activeRun,status:'observing',simulation:sim};
 const ended={...activeRun,...signalRun({id:activeRun.run_id,status:'uncertain'}),instruction:null,verdict:{status:'uncertain',reason_codes:[],evidence_ids:[]}};
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(url.endsWith('/press'))return ok(observed);
  if(url.endsWith('/step'))return new Promise(resolve=>{stepResolve=resolve;});
  return ok(initial);
 });
 try{
  await app.el('goal-form').fire('submit');await app.el('virtual-right-increase').fire('click');
  assert.equal(app.el('signal-flow').attributes.get('data-phase'),'waiting');
  assert.equal(app.el('signal-flow').attributes.get('data-beep'),'idle');
  stepResolve(ok(ended));await flush();
  assert.equal(app.el('signal-flow').attributes.get('data-phase'),'delivering');
 }finally{app.close();}
});


test('409 busy preserves only an explicit active identity and malformed recovery metadata is rejected',async()=>{
 const api=createApi('',async()=>({ok:false,status:409,json:async()=>({code:'busy',message:'진행 중',active_run_id:'run-active'})}));
 await assert.rejects(api.start('방귀','mock','right'),e=>e.code==='busy'&&e.activeRunId==='run-active');
 const other=createApi('',async()=>({ok:false,status:404,json:async()=>({code:'unknown_run',message:'없음',active_run_id:'run-active'})}));
 await assert.rejects(other.get('old'),e=>e.activeRunId===null);
 const bad=createApi('',async()=>({ok:true,json:async()=>({enabled:false,active_run_id:42})}));
 await assert.rejects(bad.demoInfo(),/실행 ID/);
});

test('reload offers explicit recovery and clarification editing cancels before another start',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');const calls=[];let cancelled=false;
 const run={...activeRun,status:'awaiting_clarification',instruction:null};
 const app=await loadApp(async(url,options)=>{
  calls.push([url,options.method]);
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,active_run_id:cancelled?null:run.run_id});
  if(url.endsWith('/cancel')){cancelled=true;return ok({run_id:run.run_id,status:'stopped'});}
  if(url==='/api/runs')return ok({...activeRun,run_id:'run-new'});
  return ok(cancelled?{...run,status:'stopped'}:run);
 });
 try{
  assert.equal(app.el('recover-run').hidden,false);assert.equal(app.el('start').disabled,true);assert.equal(app.el('demo-apply').disabled,true);
  assert.deepEqual(calls,[['/api/demo','GET']]);
  await app.el('recover-run').fire('click');assert.equal(app.el('cancel').textContent,'입력 수정하기');
  assert.equal(app.el('user-input').value,'');
  assert.equal(calls.filter(([,method])=>method==='POST').length,0);
  assert.match(app.el('instruction').textContent,/입력 수정하기/);
  await app.el('cancel').fire('click');assert.equal(app.el('start').disabled,false);assert.equal(app.el('recover-run').hidden,true);
  assert.equal(app.el('demo-apply').disabled,false);assert.match(app.el('status').textContent,/목표를 수정/);
  assert.equal(calls.filter(([url])=>url==='/api/runs').length,0);
 }finally{app.close();}
});

test('a racing start conflict offers recovery without cancelling or creating a replacement automatically',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');const posts=[];
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,active_run_id:null});
  if(options.method==='POST'){posts.push(url);return {ok:false,status:409,json:async()=>({code:'busy',message:'진행 중',active_run_id:activeRun.run_id})};}
  return ok(activeRun);
 });
 try{
  await app.el('goal-form').fire('submit');assert.equal(app.el('recover-run').hidden,false);assert.equal(app.el('start').disabled,true);
  assert.match(app.el('instruction').textContent,/기존 요청/);
  await app.el('goal-form').fire('submit');assert.deepEqual(posts,['/api/runs']);
  await app.el('recover-run').fire('click');assert.equal(app.el('recover-run').hidden,true);assert.equal(app.el('cancel').textContent,'실행 취소');
  assert.deepEqual(posts,['/api/runs']);assert.match(app.el('instruction').textContent,/오른쪽/);
 }finally{app.close();}
});

test('settings conflict also exposes recovery without changing an active execution',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');const posts=[];
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,active_run_id:null});
  if(options.method==='POST'){posts.push(url);return {ok:false,status:409,json:async()=>({code:'busy',message:'진행 중',active_run_id:activeRun.run_id})};}
  return ok({...activeRun,status:'awaiting_clarification',instruction:null});
 });
 try{
  await app.el('demo-apply').fire('click');assert.equal(app.el('recover-run').hidden,false);assert.equal(app.el('demo-apply').disabled,true);
  await app.el('recover-run').fire('click');assert.equal(app.el('cancel').textContent,'입력 수정하기');assert.deepEqual(posts,['/api/demo/settings']);
 }finally{app.close();}
});

test('lost clarification cancel keeps starting blocked until the stopped state is confirmed',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let stopped=false;const posts=[];
 const run={...activeRun,status:'awaiting_clarification',instruction:null};
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(options.method==='POST')posts.push(url);
  if(url.endsWith('/cancel')){stopped=true;throw new Error('lost');}
  return ok(stopped?{...run,status:'stopped'}:run);
 });
 try{
  await app.el('goal-form').fire('submit');await app.el('cancel').fire('click');assert.equal(app.el('start').disabled,true);
  await app.el('goal-form').fire('submit');assert.equal(posts.filter(p=>p==='/api/runs').length,1);
  await app.el('retry').fire('click');assert.equal(app.el('start').disabled,false);
 }finally{app.close();}
});

test('malformed recovery metadata after a missing ID cannot unlock starting',async()=>{
 const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');let lookups=0;
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,active_run_id:lookups++?'': 'old-run'});
  return {ok:false,status:404,json:async()=>({code:'unknown_run',message:'없음'})};
 });
 try{
  await app.el('recover-run').fire('click');assert.equal(app.el('start').disabled,true);assert.equal(app.el('recover-run').hidden,true);
  assert.equal(app.el('settings-panel').open,true);
 }finally{app.close();}
});


test('server restart during recovery allows a new start after metadata confirms no active run',async()=>{
 const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');let lookups=0;
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,active_run_id:lookups++?null:'old-run'});
  return {ok:false,status:404,json:async()=>({code:'unknown_run',message:'없음'})};
 });
 try{
  await app.el('recover-run').fire('click');assert.equal(app.el('start').disabled,false);assert.equal(app.el('recover-run').hidden,true);
 }finally{app.close();}
});


test('paired diagnostic exposes a correction draft and original goal only after explicit verified correction',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let starts=0;
 const make=()=>({...activeRun,run_id:'r'+starts,status:starts===1?'uncertain':'succeeded',instruction:null,
 verdict:{status:starts===1?'uncertain':'success',verification_level:starts===1?'none':'exact_step',goal_satisfied:starts===1?null:true,reason_codes:[],evidence_ids:['b','p']},
 diagnosis:{code:starts===1?'other_zone_changed':'goal_verified',message:starts===1?'왼쪽 출력 변화가 확인됐습니다.':'요청한 변화 확인',next_action:'상태를 확인하세요.',source:'mock',evidence_ids:['b','p'],zones:{left:{delta:starts===1?20:-20},right:{delta:0}},correction:starts===1?{zone_id:'left',direction:'decrease'}:null}});
 const app=await loadApp(async(url,options)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(url==='/api/runs'&&options.method==='POST')starts++;
  return ok(make());
 });
 try{
  await app.el('goal-form').fire('submit');assert.equal(app.el('apply-correction').hidden,false);assert.equal(app.el('signal-left').textContent,'+20');
  await app.el('apply-correction').fire('click');assert.equal(starts,1);assert.match(app.el('user-input').value,/왼쪽.*내려/);
  await app.el('goal-form').fire('submit');assert.equal(starts,2);assert.equal(app.el('return-goal').hidden,false);
  await app.el('return-goal').fire('click');assert.equal(starts,2);assert.match(app.el('user-input').value,/오른쪽.*올려/);
  await app.el('goal-form').fire('submit');assert.equal(starts,3);
 }finally{app.close();}
});

test('device reset clears stale speech and evidence only after confirmed success',async()=>{
 const {loadApp,ok,demoMetadata,activeRun}=await import('./app-harness.mjs');let resets=0;
 const run={...activeRun,status:'uncertain',instruction:null};
 const app=await loadApp(async(url)=>{
  if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true});
  if(url==='/api/demo/device/reset'){resets++;return ok({...demoMetadata,virtual_cooktop:true,virtual_levels:{left:5,right:5}});}
  return ok(run);
 });
 try{
  await app.el('goal-form').fire('submit');await app.el('reset-device').fire('click');
  assert.equal(resets,1);assert.equal(app.el('level-left').textContent,'5');assert.equal(app.el('level-right').textContent,'5');
  assert.equal(app.el('repeat').disabled,true);assert.equal(app.el('export-evidence').attributes.get('aria-disabled'),'true');assert.equal(app.el('agent-stage').textContent,'요청 대기');
 }finally{app.close();}
});

test('lost reset response masks state and blocks new start instead of inventing reset levels',async()=>{
 const {loadApp,ok,demoMetadata}=await import('./app-harness.mjs');
 const app=await loadApp(async(url)=>{if(url==='/api/demo')return ok({...demoMetadata,virtual_cooktop:true,virtual_levels:{left:6,right:7}});throw new Error('lost');});
 try{
  await app.el('reset-device').fire('click');assert.equal(app.el('start').disabled,true);assert.equal(app.el('signal-flow').attributes.get('data-phase'),'blocked');
  assert.equal(app.el('level-left').textContent,'6');assert.equal(app.el('level-right').textContent,'7');
 }finally{app.close();}
});
