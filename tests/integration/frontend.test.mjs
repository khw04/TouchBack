import test from "node:test";
import assert from "node:assert/strict";
import { createApi, ApiError, freshSpeechItems } from "../../frontend/api.mjs";
import { createFixture } from "../../frontend/fixtures.mjs";

test("minimum runs contract uses one request per action and carries source", async () => {
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ run_id: "r1", status: "planning", execution_mode: "backend_stub" }) };
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

test("same event and action are spoken only once across polls", () => {
  const run = { instruction: { action_id: "a1", text: "버튼을 누르세요" }, events: [
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
  assert.deepEqual(freshSpeechItems(seen, run).messages.map(item => item.message), ["상태를 확인하세요"]);
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
