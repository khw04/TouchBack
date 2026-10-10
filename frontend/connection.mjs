export function connectionLabel(mode, state) {
  if (mode === "fixture") return "화면 예시 · 서버 연결 없음";
  return ({
    unknown: "서버 연결 확인 전",
    online: "서버 연결됨",
    offline: "연결 끊김 · 화면은 마지막 확인 값입니다.",
    invalid: "API 응답 형식 오류 · 화면은 마지막 확인 값입니다.",
    unverified: "요청 결과 확인 필요 · 상태를 다시 확인하세요.",
  })[state] || "서버 연결 확인 전";
}

export function canSendAction(mode, state) {
  return mode === "fixture" || state === "online";
}
