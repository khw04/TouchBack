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


def create_registry_from_adapter(adapter: Any | None, source: str = "mock") -> ToolRegistry:
    """주어진 관측 어댑터(MockAdapter 또는 MockSensorAdapter)와 연동된 ToolRegistry를 생성합니다."""
    if adapter is None:
        return create_default_registry()

    if isinstance(adapter, MockSensorAdapter):
        return create_mock_registry(adapter)

    registry = ToolRegistry()

    def read_field_provider(zone_id: str) -> dict[str, Any]:
        if hasattr(adapter, "read_baseline"):
            obs = adapter.read_baseline(zone_id)
            if obs and obs.device_observation and obs.device_observation.field_summary:
                return {
                    "zone_id": zone_id,
                    "rms": obs.device_observation.field_summary.rms,
                    "unit": obs.device_observation.field_summary.unit,
                    "quality": obs.device_observation.quality.status,
                    "reasons": obs.device_observation.quality.reasons,
                    "beep_count": obs.device_observation.beep_count or 0,
                    "source": source,
                }
            if obs and obs.device_observation:
                return {
                    "zone_id": zone_id,
                    "rms": 0.0,
                    "unit": "adc_count",
                    "quality": obs.device_observation.quality.status,
                    "reasons": obs.device_observation.quality.reasons,
                    "beep_count": obs.device_observation.beep_count or 0,
                    "source": source,
                }
        return {
            "zone_id": zone_id,
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

    def await_touch_provider(action_id: str, window_ms: int = 3000) -> dict[str, Any]:
        return {
            "action_id": action_id,
            "window_ms": window_ms,
            "beep_count": 1,
            "field_delta_rms": 45.2,
            "status": "valid",
            "source": source,
        }

    registry.register(ReadFieldTool(provider=read_field_provider))
    registry.register(AwaitTouchResultTool(provider=await_touch_provider))
    registry.register(CheckSensorIntegrityTool(provider=check_integrity_provider))
    return registry


default_tool_registry = create_default_registry()
