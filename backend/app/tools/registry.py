"""Standard tool registry initialization with default sensor tools."""
from typing import Any
from app.tools.base import ToolRegistry
from app.tools.sensor_tools import (
    AwaitTouchResultTool,
    CheckSensorIntegrityTool,
    MockSensorAdapter,
    ReadFieldTool,
    create_mock_sensor_tools,
)


def create_default_registry() -> ToolRegistry:
    """센서 provider가 미설정된 기본 레지스트리 (호출 시 미설정 오류 반환)."""
    registry = ToolRegistry()
    registry.register(ReadFieldTool())
    registry.register(AwaitTouchResultTool())
    registry.register(CheckSensorIntegrityTool())
    return registry


def create_mock_registry(adapter: MockSensorAdapter | None = None) -> ToolRegistry:
    """명시적인 Mock 센서 어댑터를 사용하는 레지스트리."""
    registry = ToolRegistry()
    for tool in create_mock_sensor_tools(adapter):
        registry.register(tool)
    return registry


def create_registry_from_adapter(
    adapter: Any | None,
    source: str = "mock",
    run_id: str | None = None,
    zone_id: str | None = None,
    for_planning: bool = False,
) -> ToolRegistry:
    """주어진 관측 어댑터(MockAdapter 또는 MockSensorAdapter)와 연동된 ToolRegistry를 생성합니다.

    for_planning=True인 경우 조작 전 사전 점검 도구(read_field, check_sensor_integrity)만 노출합니다.
    """
    if adapter is None:
        return create_default_registry()

    if isinstance(adapter, MockSensorAdapter):
        if for_planning:
            reg = ToolRegistry()
            reg.register(ReadFieldTool(provider=adapter.read_field))
            reg.register(CheckSensorIntegrityTool(provider=adapter.check_integrity))
            return reg
        return create_mock_registry(adapter)

    registry = ToolRegistry()

    def read_field_provider(zid: str) -> dict[str, Any]:
        if hasattr(adapter, "read_baseline"):
            obs = adapter.read_baseline(zid)
            if obs and obs.device_observation and obs.device_observation.field_summary:
                return {
                    "zone_id": zid,
                    "rms": obs.device_observation.field_summary.rms,
                    "unit": obs.device_observation.field_summary.unit,
                    "quality": obs.device_observation.quality.status,
                    "reasons": obs.device_observation.quality.reasons,
                    "beep_count": obs.device_observation.beep_count or 0,
                    "source": source,
                }
            if obs and obs.device_observation:
                return {
                    "zone_id": zid,
                    "rms": 0.0,
                    "unit": "adc_count",
                    "quality": obs.device_observation.quality.status,
                    "reasons": obs.device_observation.quality.reasons,
                    "beep_count": obs.device_observation.beep_count or 0,
                    "source": source,
                }
        return {
            "zone_id": zid,
            "rms": 0.0,
            "unit": "adc_count",
            "quality": "invalid",
            "reasons": ["missing_baseline"],
            "beep_count": 0,
            "source": source,
        }

    def check_integrity_provider() -> dict[str, Any]:
        if hasattr(adapter, "check_sensor_integrity"):
            quality = adapter.check_sensor_integrity()
            connected = quality.status != "invalid" and "disconnected" not in quality.reasons
            calibrated = quality.status != "invalid"
            clock_sync = "verified" if (connected and "clock_unverified" not in quality.reasons) else "unsynced"
            return {
                "connected": connected,
                "saturation": False,
                "clock_sync": clock_sync,
                "calibrated": calibrated,
                "source": source,
                "quality": quality.status,
                "reasons": quality.reasons,
            }
        return {
            "connected": False,
            "saturation": False,
            "clock_sync": "unsynced",
            "calibrated": False,
            "source": source,
            "quality": "invalid",
            "reasons": ["unsupported_adapter"],
        }

    def await_touch_provider(act_id: str, window_ms: int = 3000) -> dict[str, Any]:
        if hasattr(adapter, "await_touch_result"):
            try:
                now_ms = getattr(getattr(adapter, "clock", None), "now_ms", 500)
                target_zone = zone_id or "right"
                baseline_rms = 0.0
                if hasattr(adapter, "read_baseline"):
                    base_obs = adapter.read_baseline(target_zone)
                    if base_obs and base_obs.device_observation and base_obs.device_observation.field_summary:
                        baseline_rms = base_obs.device_observation.field_summary.rms

                obs_list = adapter.await_touch_result(
                    run_id=run_id or "run-tool",
                    action_id=act_id,
                    zone_id=target_zone,
                    window_start_ms=now_ms,
                    window_end_ms=now_ms + window_ms,
                    timeout_ms=window_ms,
                )
                if not obs_list:
                    return {
                        "action_id": act_id,
                        "window_ms": window_ms,
                        "beep_count": 0,
                        "field_delta_rms": 0.0,
                        "status": "missing_samples",
                        "source": source,
                    }
                total_beeps = sum(o.device_observation.beep_count or 0 for o in obs_list if o.device_observation)
                max_rms = max(
                    (o.device_observation.field_summary.rms for o in obs_list if o.device_observation and o.device_observation.field_summary),
                    default=0.0,
                )
                field_delta_rms = max(0.0, round(max_rms - baseline_rms, 2))

                last_obs = obs_list[-1]
                quality_status = last_obs.device_observation.quality.status if last_obs.device_observation else "valid"
                reasons = last_obs.device_observation.quality.reasons if last_obs.device_observation else []
                return {
                    "action_id": act_id,
                    "window_ms": window_ms,
                    "beep_count": total_beeps,
                    "field_delta_rms": field_delta_rms,
                    "status": quality_status,
                    "reasons": reasons,
                    "source": source,
                }
            except Exception as exc:
                return {
                    "action_id": act_id,
                    "window_ms": window_ms,
                    "beep_count": 0,
                    "field_delta_rms": 0.0,
                    "status": "invalid",
                    "reasons": [str(exc)],
                    "source": source,
                }
        return {
            "action_id": act_id,
            "window_ms": window_ms,
            "beep_count": 0,
            "field_delta_rms": 0.0,
            "status": "invalid",
            "reasons": ["unsupported_adapter"],
            "source": source,
        }

    registry.register(ReadFieldTool(provider=read_field_provider))
    registry.register(CheckSensorIntegrityTool(provider=check_integrity_provider))
    if not for_planning:
        registry.register(AwaitTouchResultTool(provider=await_touch_provider))
    return registry


default_tool_registry = create_default_registry()
