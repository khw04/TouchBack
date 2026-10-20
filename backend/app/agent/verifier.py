"""Intent output verification bridge using C-KH-03 verify_intent and MockCalibration."""
from pathlib import Path
from typing import Callable

from app.contracts.models import Goal, Observation, Verdict, Zone
from app.verification.models import ActionContext, MockCalibration
from app.verification.verifier import verify_intent

DEFAULT_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[1] / "verification/mock-calibration.json"
)


def load_default_mock_calibration() -> MockCalibration:
    """기본 Mock 보정 프로필 로드 (backend/app/verification/mock-calibration.json)."""
    if DEFAULT_CALIBRATION_PATH.exists():
        return MockCalibration.model_validate_json(
            DEFAULT_CALIBRATION_PATH.read_text(encoding="utf-8")
        )
    return MockCalibration(
        source="mock",
        calibration_id="mock-test-v1",
        device_id="mock-device-01",
        session_id="mock-session-01",
        zone_id="right",
        unit="adc_count",
        method="ac_rms_v1",
        sample_rate_hz=1000,
        baseline_rms_min=95.0,
        baseline_rms_max=105.0,
        no_change_tolerance=2.0,
        increase_min=5.0,
        min_observation_ms=500,
        max_baseline_age_ms=500,
        max_observation_age_ms=5000,
        max_transport_delay_ms=4000,
        step_bands=[
            {"steps": 1, "delta_min": 18.0, "delta_max": 22.0},
            {"steps": 2, "delta_min": 38.0, "delta_max": 42.0},
        ],
    )


class IntentVerifier:
    """공통 verify_intent 계약을 연결하는 의도 검증 브리지."""

    def __init__(self, calibration: MockCalibration | None = None, *, calibration_provider: Callable[[Zone], MockCalibration | None] | None = None) -> None:
        self._calibration = calibration or load_default_mock_calibration()
        self._calibration_provider = calibration_provider

    @property
    def calibration(self) -> MockCalibration:
        return self._calibration

    def get_calibration_for_zone(self, zone_id: Zone) -> MockCalibration | None:
        """지정된 화구에 맞는 MockCalibration 프로필 반환."""
        if self._calibration_provider is not None:
            return self._calibration_provider(zone_id)
        if self._calibration.zone_id == zone_id:
            return self._calibration
        return self._calibration.model_copy(update={"zone_id": zone_id})

    def verify(
        self,
        goal: Goal,
        baseline: Observation | None,
        observations: list[Observation],
        context: ActionContext,
        calibration: MockCalibration | None = None,
    ) -> Verdict:
        """C-KH-03 verify_intent 계약을 실행하여 엄격한 검증을 수행합니다.

        보정 프로필, 화구 일치, 센서 품질, 시계 동기화, 시간창 및 샘플 검사를
        우회하지 않고 실제 관측 ID들을 evidence_ids로 보존합니다.
        """
        cal = calibration or self.get_calibration_for_zone(goal.zone_id)
        return verify_intent(
            goal=goal,
            baseline=baseline,
            observations=observations,
            context=context,
            calibration=cal,
        )


default_verifier = IntentVerifier()
