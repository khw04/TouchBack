const kindLabels = {
  plan: "계획", instruction: "조작 안내", tool_call: "도구 호출",
  observation: "센서 관측", verdict: "판정", recovery: "복구 안내", error: "오류",
};
const sourceLabels = { ui_fixture: "화면 예시", mock: "모의 관측", replay: "녹화 재생", live: "실시간 센서" };

export function eventLabel(kind) { return kindLabels[kind] || kind || "기록"; }
export function eventSource(source) { return sourceLabels[source] || source || "미상"; }

// Event IDs are immutable in the run contract. Append only genuinely new events so an
// expanded evidence panel and keyboard focus survive each status poll.
export function eventChange(renderedIds, events) {
  const ids = events.map((event, index) => event?.event_id || `missing-id-${index}`);
  const prefixMatches = renderedIds.length <= ids.length && renderedIds.every((id, index) => id === ids[index]);
  return { reset: !prefixMatches, ids, from: prefixMatches ? renderedIds.length : 0 };
}
