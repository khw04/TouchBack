"""Verifier configuration; numerical defaults exist only in explicit mock profiles."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.contracts.models import Contract, NonNegativeInt, PositiveInt, Text, Zone


class ActionContext(Contract):
    run_id: Text
    action_id: Text
    window_start_ms: NonNegativeInt
    window_end_ms: NonNegativeInt
    now_ms: NonNegativeInt

    @model_validator(mode="after")
    def ordered_window(self) -> "ActionContext":
        if self.window_start_ms >= self.window_end_ms or self.now_ms < self.window_end_ms:
            raise ValueError("verification requires a closed action window")
        return self


class StepBand(Contract):
    steps: int
    delta_min: float
    delta_max: float

    @model_validator(mode="after")
    def ordered_band(self) -> "StepBand":
        if self.steps == 0 or self.delta_min > self.delta_max:
            raise ValueError("step band must be ordered and nonzero")
        return self


class MockCalibration(Contract):
    # A mock profile cannot authorize live or replay success.
    source: Literal["mock"]
    calibration_id: Text
    device_id: Text
    session_id: Text
    zone_id: Zone
    unit: Literal["adc_count", "millivolt"]
    method: Text
    sample_rate_hz: PositiveInt
    baseline_rms_min: Annotated[float, Field(ge=0)]
    baseline_rms_max: Annotated[float, Field(ge=0)]
    no_change_tolerance: Annotated[float, Field(ge=0)]
    increase_min: Annotated[float, Field(gt=0)]
    min_observation_ms: PositiveInt
    max_baseline_age_ms: NonNegativeInt
    max_observation_age_ms: NonNegativeInt
    max_transport_delay_ms: NonNegativeInt
    step_bands: list[StepBand]

    @model_validator(mode="after")
    def consistent_profile(self) -> "MockCalibration":
        if self.baseline_rms_min > self.baseline_rms_max or self.increase_min <= self.no_change_tolerance:
            raise ValueError("invalid baseline range or overlapping change thresholds")
        bands = sorted(self.step_bands, key=lambda band: band.delta_min)
        for band in bands:
            if band.delta_min <= self.no_change_tolerance and band.delta_max >= -self.no_change_tolerance:
                raise ValueError("step band overlaps no-change region")
            if band.steps > 0 and band.delta_min < self.increase_min:
                raise ValueError("positive step must be inside increase region")
            if band.steps < 0 and band.delta_max >= -self.no_change_tolerance:
                raise ValueError("negative step must be in decrease region")
        for first, second in zip(bands, bands[1:]):
            if first.delta_max >= second.delta_min:
                raise ValueError("step bands must not overlap")
        return self
