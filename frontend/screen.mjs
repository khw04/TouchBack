// Plain-language summaries of server state; never infer sensor measurements.
import { claimableSuccess } from "./presentation.mjs?v=20";
const reasons = {
  sensor_adapter_unconfigured: "센서 입력이 설정되지 않아 변화를 확인할 수 없어요.",
  sensor_integrity_check_failed: "센서 사전 점검을 통과하지 못해 조작을 중단했어요.",
  field_missing: "자기장 관측이 없어 출력 변화를 확인할 수 없어요.",
  sensor_quality_invalid: "센서 관측 품질이 불량해 결과를 확정하지 않았어요.",
  sensor_quality_degraded: "관측 품질이 저하돼 결과를 확정하지 않았어요.",
  missing_observations: "관측이 도착하지 않아 결과를 확인할 수 없어요.",
  no_output_change: "출력 변화가 확인되지 않았어요.",
  observation_timeout: "제한 시간 안에 관측 결과를 받지 못했어요.",
  observation_processing_error: "관측을 처리하지 못해 결과를 확정하지 않았어요.",
  UI_FIXTURE_ONLY: "화면 연습 결과입니다. Agent 판정은 없어요.",
  UI_FIXTURE_SENSOR_ERROR: "센서 오류 화면 예시입니다. 실제 관측은 없어요.",
};
export function screenSummary(run) {
  if (!run) return {agent:"요청 대기",sensor:"관측 대기",detail:"서버가 확인한 진행 상태만 표시합니다.",result:"아직 판정이 없습니다."};
  const status=run.status;
  const result=!["stopped","failed"].includes(status) && run.verdict?.reason_codes?.map(code=>reasons[code]).find(Boolean);
  const count=Array.isArray(run.verdict?.evidence_ids)?run.verdict.evidence_ids.length:0;
  const agent=({planning:"목표와 계획 확인 중",awaiting_clarification:"요청 확인이 필요해요",awaiting_user:"조작 안내 준비됨",observing:"관측과 목표 비교 중",recovering:"재안내 조건 확인 중",succeeded:"판정 완료",uncertain:"확인할 근거가 부족해요",failed:"실행을 완료하지 못했어요",stopped:"실행을 중단했어요"})[status]||"상태 확인 중";
  const sensor=status==="observing"?"관측 처리 중":status==="recovering"?"지연 관측 확인 중":count?`판정 근거 ${count}건`:"수신 수치 확인 전";
  return {agent,sensor,detail:run.execution_mode==="ui_fixture"?"화면 연습 · 실제 Agent 호출 없음":run.execution_mode==="mock_llm"?"Mock LLM · 실제 AI 호출 없음":run.execution_mode==="real_llm"?"실제 AI 실행 모드 · 상세 호출 기록 확인":"백엔드 준비 모드 · 실제 AI 호출 없음",
    result: result || (claimableSuccess(run)?`${run.source==="mock"?"모의 관측에서":"관측에서"} 한 단계 증가를 확인했어요.`:status==="succeeded"?"성공 근거가 부족해 결과를 확정하지 않았어요.":status==="uncertain"?"관측만으로 목표 달성을 확인하지 못했어요. 상세 근거를 확인하세요.":status==="failed"?"연결·모델 또는 서버 오류가 있어요. 상세 기록의 오류를 확인하세요.":status==="stopped"?"취소 또는 대기 시간 만료로 종료했어요.":"아직 최종 판정이 없습니다.")};
}
