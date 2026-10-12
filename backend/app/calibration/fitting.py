"""Fit bounded RMS signatures from independently labelled development samples."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from app.contracts.models import Contract, DeviceObservation, NonNegativeInt, Text, Zone


class DevelopmentSample(Contract):
    trial_id: Text
    split: Literal["development", "evaluation"]
    reference_source: Literal["manual_display", "video_display", "mock_fixture"]
    displayed_level: NonNegativeInt
    cooktop_model: Text
    cookware_id: Text
    installation_id: Text
    observation: DeviceObservation


class LevelSignature(Contract):
    level: NonNegativeInt
    rms_min: Annotated[float, Field(ge=0)]
    rms_max: Annotated[float, Field(ge=0)]


class CalibrationCandidate(Contract):
    source: Literal["mock", "live"]
    status: Literal["draft"]
    calibration_id: Text
    device_id: Text
    session_id: Text
    zone_id: Zone
    cooktop_model: Text
    cookware_id: Text
    installation_id: Text
    unit: Literal["adc_count", "millivolt"]
    method: Text
    sample_rate_hz: Annotated[int, Field(gt=0)]
    trial_ids: list[Text]
    signatures: list[LevelSignature]

    @model_validator(mode="after")
    def disjoint_increasing_levels(self):
        signatures=sorted(self.signatures,key=lambda item:item.level)
        if len(signatures)<2 or len({item.level for item in signatures})!=len(signatures):
            raise ValueError("at least two unique levels are required")
        if any(item.rms_min>item.rms_max for item in signatures):
            raise ValueError("invalid signature range")
        if any(a.rms_max>=b.rms_min for a,b in zip(signatures,signatures[1:])):
            raise ValueError("RMS signatures overlap or are not monotonic; extend features")
        return self


def fit_candidate(samples: list[DevelopmentSample], *, calibration_id: str, margin: float = 0.0, min_per_level: int = 3) -> CalibrationCandidate:
    if type(min_per_level) is not int or min_per_level<3:
        raise ValueError("development requires at least three samples per level")
    if not isinstance(margin,(int,float)) or isinstance(margin,bool) or not 0<=margin<float("inf"):
        raise ValueError("margin must be finite and nonnegative")
    if not samples:
        raise ValueError("development samples are required")
    samples=[DevelopmentSample.model_validate_json(item.model_dump_json()) for item in samples]
    if any(item.split!="development" for item in samples):
        raise ValueError("evaluation data must not be used for fitting")
    if len({item.trial_id for item in samples})!=len(samples):
        raise ValueError("duplicate trial IDs")
    if len({(item.observation.device_id,item.observation.session_id,item.observation.seq) for item in samples})!=len(samples):
        raise ValueError("duplicate measurement IDs")
    first=samples[0]; d=first.observation; field=d.field_summary
    if field is None or d.source not in ("mock","live") or d.zone_id not in ("right","left"):
        raise ValueError("missing or unsupported measurement")
    scope=(first.cooktop_model,first.cookware_id,first.installation_id,d.device_id,d.session_id,d.zone_id,d.source,field.unit,field.method,field.sample_rate_hz)
    groups: dict[int,list[float]]={}
    for item in samples:
        observation=item.observation; f=observation.field_summary
        if f is None or observation.quality.status!="valid" or observation.quality.reasons:
            raise ValueError("invalid development signal quality")
        if observation.source=="live" and item.reference_source=="mock_fixture":
            raise ValueError("live development requires independent displayed-level labels")
        current=(item.cooktop_model,item.cookware_id,item.installation_id,observation.device_id,observation.session_id,observation.zone_id,observation.source,f.unit,f.method,f.sample_rate_hz)
        if current!=scope:
            raise ValueError("mixed installation/device/session/measurement settings")
        if f.sample_count<(observation.window_end_ms-observation.window_start_ms)*f.sample_rate_hz/1000:
            raise ValueError("incomplete development sampling")
        groups.setdefault(item.displayed_level,[]).append(f.rms)
    if any(len(values)<min_per_level for values in groups.values()):
        raise ValueError("insufficient development samples per level")
    signatures=[LevelSignature(level=level,rms_min=max(0.0,min(values)-margin),rms_max=max(values)+margin) for level,values in sorted(groups.items())]
    return CalibrationCandidate(source=d.source,status="draft",calibration_id=calibration_id,device_id=d.device_id,session_id=d.session_id,zone_id=d.zone_id,
        cooktop_model=first.cooktop_model,cookware_id=first.cookware_id,installation_id=first.installation_id,unit=field.unit,method=field.method,sample_rate_hz=field.sample_rate_hz,
        trial_ids=[item.trial_id for item in samples],signatures=signatures)
