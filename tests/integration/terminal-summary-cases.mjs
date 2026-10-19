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
