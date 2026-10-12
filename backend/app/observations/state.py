"""Mock calibration activation and conservative per-zone resynchronization."""
from app.calibration.fitting import CalibrationCandidate
from app.contracts.models import Goal, Observation, Quality, Verdict
from app.verification.models import ActionContext, MockCalibration, StepBand
from app.verification.verifier import verify_intent, verdict


class ZoneState:
    def __init__(self, zone_id: str):
        if zone_id not in ("right","left"):
            raise ValueError("zone must be right or left")
        self.zone_id=zone_id
        self.status="needs_calibration"
        self.reason="calibration_missing"
        self.level: int | None=None
        self._candidate: CalibrationCandidate | None=None
        self._baseline: Observation | None=None
        self._last_seq: int | None=None

    def invalidate(self, reason: str) -> None:
        self.status="needs_calibration"
        self.reason=reason
        self.level=None
        self._baseline=None
        self._candidate=None
        self._last_seq=None

    def activate_mock(self, candidate: CalibrationCandidate, *, installation_id: str) -> None:
        candidate=CalibrationCandidate.model_validate_json(candidate.model_dump_json())
        if candidate.source!="mock" or candidate.zone_id!=self.zone_id or candidate.installation_id!=installation_id:
            raise ValueError("only matching mock installation candidates can be activated")
        self._candidate=candidate
        self._baseline=None
        self._last_seq=None
        self.level=None
        self.status="needs_baseline"
        self.reason="baseline_missing"

    def observe(self, observation: Observation, *, now_ms: int, max_age_ms: int = 5000) -> bool:
        if type(now_ms) is not int or type(max_age_ms) is not int or now_ms<0 or max_age_ms<0:
            raise ValueError("invalid state observation times")
        try:
            observation=Observation.model_validate_json(observation.model_dump_json())
        except ValueError:
            self.invalidate("invalid_input");return False
        p=self._candidate
        if p is None:
            self.reason="calibration_missing";return False
        d=observation.device_observation; f=d.field_summary
        if d.zone_id!=self.zone_id:
            return False  # other-zone evidence must not erase this zone's state
        if (d.device_id,d.session_id,d.source,d.calibration_id)!=(p.device_id,p.session_id,p.source,p.calibration_id):
            self.invalidate("device_session_calibration_changed");return False
        start,end=observation.mapped_window_start_ms,observation.mapped_window_end_ms
        if d.quality.status!="valid" or d.quality.reasons or f is None:
            self.invalidate("sensor_quality_invalid");return False
        if observation.clock_quality!="verified" or start is None or end is None or not end<=observation.received_monotonic_ms<=now_ms or now_ms-end>max_age_ms:
            self.invalidate("time_unverified_or_stale");return False
        if (f.unit,f.method,f.sample_rate_hz)!=(p.unit,p.method,p.sample_rate_hz) or f.sample_count<(end-start)*f.sample_rate_hz/1000:
            self.invalidate("measurement_settings_mismatch");return False
        if self._last_seq is not None and d.seq<=self._last_seq:
            if self._baseline==observation:
                return True
            self.invalidate("sequence_regressed");return False
        signatures=[item for item in p.signatures if item.rms_min<=f.rms<=item.rms_max]
        if len(signatures)!=1:
            self.invalidate("level_unclassified");return False
        self.level=signatures[0].level
        self._baseline=observation
        self._last_seq=d.seq
        self.status="ready"
        self.reason="state_resynchronized"
        return True

    def verify(self, goal: Goal, observations: list[Observation], context: ActionContext, *, no_change_tolerance: float=2.0, increase_min: float=5.0) -> Verdict:
        p=self._candidate; baseline=self._baseline
        if p is None or baseline is None or self.level is None or self.status!="ready":
            return verdict("uncertain","none",None,[self.reason],[])
        if goal.zone_id!=self.zone_id:
            return verdict("uncertain","none",None,["zone_mismatch"],[])
        signature=next(item for item in p.signatures if item.level==self.level)
        rms=baseline.device_observation.field_summary.rms
        try:
            bands=[StepBand(steps=item.level-self.level,delta_min=item.rms_min-rms,delta_max=item.rms_max-rms) for item in p.signatures if item.level!=self.level]
            profile=MockCalibration(source="mock",calibration_id=p.calibration_id,device_id=p.device_id,session_id=p.session_id,zone_id=p.zone_id,
                unit=p.unit,method=p.method,sample_rate_hz=p.sample_rate_hz,baseline_rms_min=signature.rms_min,baseline_rms_max=signature.rms_max,
                no_change_tolerance=no_change_tolerance,increase_min=increase_min,min_observation_ms=500,max_baseline_age_ms=500,max_observation_age_ms=5000,max_transport_delay_ms=4000,step_bands=bands)
        except ValueError:
            return verdict("uncertain","none",None,["calibration_resolution_insufficient"],[])
        return verify_intent(goal,baseline,observations,context,profile)

    def snapshot(self) -> dict:
        return {"zone_id":self.zone_id,"status":self.status,"reason":self.reason,"level":self.level,
                "observation_id":None if self._baseline is None else self._baseline.observation_id}


class StateTracker:
    def __init__(self):
        self.zones={zone:ZoneState(zone) for zone in ("right","left")}

    def invalidate_all(self, reason: str) -> None:
        for state in self.zones.values():state.invalidate(reason)

    def handle_integrity(self, quality: Quality, *, zone_id: str | None = None) -> None:
        quality = Quality.model_validate_json(quality.model_dump_json())
        if zone_id is not None and zone_id not in self.zones:
            raise ValueError("zone must be right or left")
        if quality.status != "valid" or quality.reasons:
            if zone_id is None:
                self.invalidate_all("sensor_integrity_invalid")
            else:
                self.zones[zone_id].invalidate("sensor_integrity_invalid")
