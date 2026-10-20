import test from 'node:test';
import assert from 'node:assert/strict';
import {screenSummary} from '../../frontend/screen.mjs';
for(const status of ['stopped','failed'])for(const previous of ['no_output_change','verified']){
  test(`review regression: ${status} summary wins over prior ${previous}`,()=>{
    const summary=screenSummary({status,source:'mock',execution_mode:'mock_llm',verdict:{status:previous==='verified'?'success':'mismatch',verification_level:previous==='verified'?'exact_step':'none',goal_satisfied:previous==='verified'?true:false,reason_codes:previous==='verified'?[]:['no_output_change'],evidence_ids:['previous']}});
    assert.match(summary.result,status==='stopped'?/취소|만료/:/연결|모델|오류/);
    assert.doesNotMatch(summary.result,/출력 변화가 확인되지|한 단계 증가/);
  });
}


test('review regression: late-success model stop remains an uncertain stop in the central summary',()=>{
  const summary=screenSummary({status:'uncertain',source:'mock',execution_mode:'real_llm',
    verdict:{status:'uncertain',verification_level:'none',goal_satisfied:null,
      reason_codes:['no_output_change','agent_stopped'],evidence_ids:['initial','late']},
    diagnosis:{code:'goal_verified',message:'관측은 확인했으나 Agent가 추가 진행을 중단했습니다.',
      correction:null,next_action:'현재 인덕션 상태를 직접 확인하세요.'}});
  assert.match(summary.result,/중단/);
  assert.doesNotMatch(summary.result,/한 단계.*확인했습니다|성공/);
  assert.equal(summary.agent,'확인할 근거가 부족해요');
});
