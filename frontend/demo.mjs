import { TERMINAL } from "./api.mjs?v=17";
const labels = {
  normal: "정상 증가", recovery: "미인식 후 한 번 복구", sensor_error: "센서 오류", missing: "관측 누락",
  delayed: "늦게 도착한 관측", no_change: "변화 없음·기준 만료", duplicate: "관측 중복", recovery_exhausted: "재시도 후에도 변화 없음",
};
export function demoLabel(name) { return labels[name] || "지원하지 않는 모의 입력"; }
export function demoControlsDisabled({ busy, startUnknown, run }) {
  return Boolean(busy || startUnknown || run && !TERMINAL.has(run.status));
}
