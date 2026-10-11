"""Sensor observation tools for Agent execution (docs/아키텍처.md, docs/실행계약.md)."""
from typing import Any, Callable
from app.tools.base import BaseTool, ToolResult


class ReadFieldTool(BaseTool):
    name = "read_field"
    description = "지정한 화구의 자기장 요약 및 센서 신호 품질을 조회합니다."
    parameters = {
        "type": "object",
        "properties": {
            "zone_id": {
                "type": "string",
                "enum": ["left", "right"],
                "description": "조회할 인덕션 화구 위치",
            }
        },
        "required": ["zone_id"],
    }

    def __init__(self, provider: Callable[[str], dict[str, Any]] | None = None) -> None:
        self._provider = provider

    async def execute(self, **kwargs: Any) -> ToolResult:
        zone_id = kwargs.get("zone_id")
        if zone_id not in ("left", "right"):
            return ToolResult(success=False, error=f"유효하지 않은 화구 ID입니다: {zone_id}")

        if self._provider:
            data = self._provider(zone_id)
        else:
            data = {
                "zone_id": zone_id,
                "rms": 120.5,
                "unit": "adc_count",
                "quality": "valid",
                "beep_count": 0,
            }
        return ToolResult(success=True, data=data)


class AwaitTouchResultTool(BaseTool):
    name = "await_touch_result"
    description = "사용자의 터치 조작 직후 지정된 시간창 동안의 센서 관측 결과를 대기하고 수집합니다."
    parameters = {
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": "관측 대상 조작 ID",
            },
            "window_ms": {
                "type": "integer",
                "description": "관측 대기 시간창(밀리초)",
                "default": 3000,
            },
        },
        "required": ["action_id"],
    }

    def __init__(self, provider: Callable[[str, int], dict[str, Any]] | None = None) -> None:
        self._provider = provider

    async def execute(self, **kwargs: Any) -> ToolResult:
        action_id = kwargs.get("action_id")
        if not action_id:
            return ToolResult(success=False, error="action_id가 누락되었습니다.")

        window_ms = int(kwargs.get("window_ms", 3000))
        if self._provider:
            data = self._provider(str(action_id), window_ms)
        else:
            data = {
                "action_id": action_id,
                "window_ms": window_ms,
                "beep_count": 1,
                "field_delta_rms": 45.2,
                "status": "valid",
            }
        return ToolResult(success=True, data=data)


class CheckSensorIntegrityTool(BaseTool):
    name = "check_sensor_integrity"
    description = "센서 하드웨어 연결 상태, ADC 신호 포화 여부, 시간 동기화 및 보정 상태를 점검합니다."
    parameters = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, provider: Callable[[], dict[str, Any]] | None = None) -> None:
        self._provider = provider

    async def execute(self, **kwargs: Any) -> ToolResult:
        if self._provider:
            data = self._provider()
        else:
            data = {
                "connected": True,
                "saturation": False,
                "clock_sync": "verified",
                "calibrated": True,
            }
        return ToolResult(success=True, data=data)
