import asyncio
import pytest
from app.adapters.virtual_cooktop import VirtualCooktopAdapter
from app.agent.orchestrator import AgentOrchestrator
from app.agent.verifier import IntentVerifier
from app.agent.recovery import RecoveryDecisionType
from app.contracts.models import Goal

@pytest.mark.parametrize('direction,late_rms,expected,wrong',[
    ('increase',120,'올라갔습니다','내려갔습니다'),
    ('decrease',80,'내려갔습니다','올라갔습니다')])
def test_paired_original_action_late_success_reports_goal_direction(direction,late_rms,expected,wrong):
    a=VirtualCooktopAdapter('normal','right')
    a._pending.extend([
        a.frame(10,500,3500,3500,100,0,zone='right'),a.frame(11,500,3500,3500,100,None,zone='left'),
        a.frame(12,500,3500,4000,late_rms,1,zone='right'),a.frame(13,500,3500,4000,100,None,zone='left')])
    orch=AgentOrchestrator(adapter=a,verifier=IntentVerifier(calibration_provider=a.calibration_for_zone))
    goal=Goal(zone_id='right',direction=direction,requested_steps=1)
    verdict,_=asyncio.run(orch.execute_observation_and_verification(goal,'act',run_id='run',window_start_ms=500,window_end_ms=3500))
    assert verdict.status=='mismatch' and 'no_output_change' in verdict.reason_codes
    recovered=orch.handle_recovery(goal,verdict,0,run_id='run',action_id='act',window_start_ms=500,window_end_ms=3500)
    assert recovered.decision==RecoveryDecisionType.LATE_SUCCESS
    assert recovered.updated_verdict.status=='success' and recovered.updated_verdict.goal_satisfied
    assert recovered.instruction is None
    assert expected in recovered.message and wrong not in recovered.message

