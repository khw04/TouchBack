import { TERMINAL } from "./api.mjs";

export function evidenceReport(run, { fixtureMode = false, connection = "unknown", lastConfirmedAt = null, capturedAt = new Date().toISOString(), submittedRequest = null } = {}) {
  if (!run?.run_id) throw new Error("저장할 실행 기록이 없습니다.");
  const classification = fixtureMode || run.execution_mode === "ui_fixture" ? "ui_fixture"
    : run.execution_mode === "backend_stub" ? "backend_stub" : "server_response";
  const freshness = classification === "ui_fixture" ? "not_applicable"
    : connection === "online" ? "last_confirmed_snapshot" : "stale_or_unverified_snapshot";
  return {
    report_version: "touchback-demo-evidence-v1",
    captured_at: capturedAt,
    classification,
    snapshot_freshness: freshness,
    last_confirmed_at: classification === "ui_fixture" || !lastConfirmedAt ? null : new Date(lastConfirmedAt).toISOString(),
    note: classification === "ui_fixture" ? "화면 연습 기록입니다. 실제 센서·Agent·서버 판정 근거가 아닙니다."
      : classification === "backend_stub" ? "백엔드 스텁 응답입니다. 실제 Agent 성공 근거가 아닙니다."
      : "서버 응답의 화면 저장본입니다. 실제 장치·정답 검증은 별도 근거가 필요합니다.",
    submitted_request: submittedRequest && {
      user_input: submittedRequest.user_input,
      source: submittedRequest.source,
      zone_id: submittedRequest.zone_id,
    },
    run: {
      run_id: run.run_id,
      status: run.status ?? null,
      execution_mode: run.execution_mode ?? null,
      source: run.source ?? null,
      zone_id: run.zone_id ?? null,
      user_input: run.user_input ?? null,
      fixture_scenario: classification === "ui_fixture" ? run.scenario ?? null : null,
      complete: TERMINAL.has(run.status),
      instruction: run.instruction ?? null,
      verdict: run.verdict ?? null,
      events: Array.isArray(run.events) ? run.events.map(item => ({
        event_id: item.event_id ?? null,
        action_id: item.action_id ?? null,
        timestamp: item.timestamp ?? null,
        kind: item.kind ?? null,
        source: item.source ?? null,
        message: item.message ?? null,
        evidence_ids: Array.isArray(item.evidence_ids) ? item.evidence_ids : [],
      })) : [],
    },
  };
}

export function evidenceFilename(report) {
  const safeId = String(report.run.run_id).replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64) || "run";
  return `touchback-${report.classification}-${safeId}.json`;
}
