"""Mock-only calibration maintenance. Never read simulator levels or press truth."""
from app.tools.base import BaseTool, ToolResult
from app.verification.verifier import observation_calibration_errors


class MockStateTool(BaseTool):
    parameters={"type":"object","properties":{},"additionalProperties":False}
    def __init__(self, adapter, name):
        self.adapter=adapter;self.name=name
        self.description={"auto_calibration":"명시적 합성 Mock 개발 프로필을 검증하고 센서 기준을 확보합니다. 실물 보정이 아닙니다.",
            "state_resynchronization":"좌우 센서의 신선한 수신 기준으로 관측 상태를 재동기화합니다.",
            "sensor_recalibration":"유효한 새 관측이 있을 때만 Mock 보정을 재활성화하고 상태를 재동기화합니다."}[name]

    async def execute(self, **kwargs):
        if kwargs:return ToolResult(success=False,error="인수를 받지 않는 Mock 유지 도구입니다.")
        with self.adapter.guard:
            # Get measurements before altering a profile. Bad/missing/stale data must not become healthy.
            bases={z:self.adapter.read_baseline(z) for z in ("left","right")}
            for zone,base in bases.items():
                profile=self.adapter.mock_profiles[zone]
                if base is None or base.device_observation.source!="mock" or base.clock_quality!="verified" or base.device_observation.quality.status!="valid" or base.device_observation.quality.reasons:
                    return ToolResult(success=False,error="유효한 Mock 기준 관측이 없습니다.")
                end=base.mapped_window_end_ms
                if end is None or not 0<=self.adapter.clock.now_ms-end<=500:
                    return ToolResult(success=False,error="Mock 기준 관측이 오래됐습니다.")
                d=base.device_observation;f=d.field_summary
                if f is None or (d.device_id,d.session_id,d.zone_id,d.calibration_id)!=(profile.device_id,profile.session_id,profile.zone_id,profile.calibration_id) or (f.unit,f.method,f.sample_rate_hz)!=(profile.unit,profile.method,profile.sample_rate_hz):
                    return ToolResult(success=False,error="Mock 프로필과 기준 관측이 다릅니다.")
            if self.name in ("auto_calibration","sensor_recalibration"):
                for zone in bases:
                    state=self.adapter.tracker.zones[zone]
                    state.invalidate("explicit_mock_recalibration")
                    state.activate_mock(self.adapter.mock_profiles[zone],installation_id="virtual-only")
            for zone,base in bases.items():
                state=self.adapter.tracker.zones[zone]
                if not state.observe(base,now_ms=self.adapter.clock.now_ms,max_age_ms=500):
                    return ToolResult(success=False,error="Mock 상태를 관측으로 재확보하지 못했습니다.")
                if observation_calibration_errors(base,state.calibration(),zone,baseline=True):
                    state.invalidate("mock_profile_verification_failed")
                    return ToolResult(success=False,error="Mock 기준의 보정 검증을 통과하지 못했습니다.")
            return ToolResult(success=True,data={"source":"mock","physical_validation_performed":False,"zones":{z:{"status":self.adapter.tracker.zones[z].status,"observation_id":b.observation_id} for z,b in bases.items()}})
