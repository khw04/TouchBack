const phases = {
  planning: ["1/3 · 목표 확인", "서버가 요청을 해석하고 조작 계획을 준비합니다."],
  awaiting_clarification: ["1/3 · 목표 확인", "화구나 목표를 확인한 뒤 새 요청으로 시작하세요."],
  awaiting_user: ["2/3 · 사용자 조작", "현재 안내를 듣고 직접 조작한 뒤 완료 알림을 누르세요."],
  observing: ["3/3 · 결과 확인", "서버가 센서 관측과 조작 결과를 확인하고 있습니다."],
  recovering: ["2/3 · 복구 안내", "새 안내가 나올 때까지 다시 조작하지 마세요."],
  succeeded: ["결과 · 목표 확인", "서버가 목표 충족을 보고했습니다. 입력 출처와 근거를 확인하세요."],
  uncertain: ["결과 · 확인 불가", "서버가 결과를 확정하지 못했습니다. 재조작 전에 현재 출력을 확인하세요."],
  stopped: ["결과 · 실행 중단", "인덕션을 끈다는 뜻이 아닙니다. 상태를 직접 확인하세요."],
  failed: ["결과 · 실행 실패", "서버 오류 내용을 확인하고 필요한 경우 다시 시작하세요."],
};

export function phaseFor(run) {
  if (!run) return ["시작 전", "목표를 입력하면 다음 단계가 여기에 표시됩니다."];
  if ((run.execution_mode === "ui_fixture" || run.execution_mode === "backend_stub") && run.status === "succeeded") {
    return ["결과 · 성공 근거 없음", "화면 예시 또는 백엔드 스텁의 성공 상태는 실제 검증 결과가 아닙니다."];
  }
  return phases[run.status] || ["상태 확인 중", "서버 응답을 확인하세요."];
}

export function verdictLabel(run) {
  const verdict = run?.verdict;
  if (!verdict) return "서버 판정을 기다리는 중입니다.";
  const fixture = run.execution_mode === "ui_fixture";
  const stub = run.execution_mode === "backend_stub";
  const status = (fixture || stub) && verdict.status === "succeeded" ? "성공 근거 없음" : verdict.status || "확인 필요";
  const source = fixture ? "화면 예시 결과" : stub ? "백엔드 스텁 응답" : "서버 판정";
  return `${source}: ${status} · 근거 코드: ${(verdict.reason_codes || []).join(", ") || "없음"}`;
}

export function actionFor(run) {
  if (!run) return "목표를 입력하세요.";
  if (run.instruction?.text) return run.instruction.text;
  if ((run.execution_mode === "ui_fixture" || run.execution_mode === "backend_stub") && run.status === "succeeded") {
    return "실제 인덕션 상태를 직접 확인하세요.";
  }
  return ({
    planning: "서버의 조작 계획을 기다리세요.",
    awaiting_clarification: "화구와 목표를 명확히 입력해 새로 시작하세요.",
    awaiting_user: "서버의 조작 안내를 기다리세요.",
    observing: "조작 결과를 확인하는 중입니다. 다시 누르지 마세요.",
    recovering: "복구 안내를 기다리세요. 다시 누르지 마세요.",
    succeeded: "입력 출처와 판정 근거를 확인하세요.",
    uncertain: "인덕션 상태를 직접 확인하세요.",
    stopped: "인덕션 상태를 직접 확인하세요.",
    failed: "오류 내용을 확인하고 다시 시작하세요.",
  })[run.status] || "서버 상태를 확인하세요.";
}
