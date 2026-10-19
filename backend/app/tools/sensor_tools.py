"""Sensor observation tools for Agent execution (docs/아키텍처.md, docs/실행계약.md)."""
from typing import Any, Callable
from app.tools.base import BaseTool, ToolResult


def sensor_safety_errors(data: dict[str, Any], *, integrity: bool = False) -> list[str]:
    """Fail closed on missing or compromised sensor safety metadata."""
    errors = list(data.get("reasons") or [])
    if data.get("quality") != "valid":
        errors.append("sensor_quality_not_valid")
    if integrity and data.get("connected") is not True:
        errors.append("sensor_disconnected")
    if data.get("clock_sync") != "verified":
        errors.append("clock_unverified")
    if data.get("saturation") is not False:
        errors.append("sensor_saturation_or_unknown")
    if data.get("calibrated") is not True:
        errors.append("calibration_missing")
    return list(dict.fromkeys(errors))


class MockSensorAdapter:
    """명시적인 Mock 센서 어댑터 (출처: mock)."""

    def __init__(
        self,
        source: str = "mock",
        default_rms: float = 120.5,
        default_field_delta: float = 45.2,
        is_connected: bool = True,
        is_calibrated: bool = True,
    ) -> None:
        self.source = source
        self.default_rms = default_rms
        self.default_field_delta = default_field_delta
        self.is_connected = is_connected
        self.is_calibrated = is_calibrated

    def read_field(self, zone_id: str) -> dict[str, Any]:
        return {
            "zone_id": zone_id,
            "rms": self.default_rms,
            "unit": "adc_count",
            "quality": "valid",
            "reasons": [],
            "clock_sync": "verified" if self.is_connected else "unsynced",
            "saturation": False,
            "calibrated": self.is_calibrated,
            "beep_count": 0,
            "source": self.source,
        }

    def await_touch_result(self, action_id: str, window_ms: int = 3000) -> dict[str, Any]:
        return {
            "action_id": action_id,
            "window_ms": window_ms,
            "beep_count": 1,
            "field_delta_rms": self.default_field_delta,
            "status": "valid",
            "source": self.source,
        }

    def check_integrity(self) -> dict[str, Any]:
        return {
            "connected": self.is_connected,
            "saturation": False,
            "clock_sync": "verified" if self.is_connected else "unsynced",
            "calibrated": self.is_calibrated,
            "source": self.source,
            "quality": "valid" if self.is_connected else "invalid",
            "reasons": [],
        }


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

        if not self._provider:
            return ToolResult(
                success=False,
                error="센서 어댑터가 설정되지 않았거나 입력이 준비되지 않았습니다.",
            )

        data = self._provider(zone_id)
        reasons = sensor_safety_errors(data)
        if reasons:
            reason_str = ", ".join(reasons)
            return ToolResult(
                success=False,
                error=f"화구({zone_id}) 센서 신호 품질 불량: {reason_str}",
                data=data,
            )
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

        if not self._provider:
            return ToolResult(
                success=False,
                error="센서 어댑터가 설정되지 않았거나 입력이 준비되지 않았습니다.",
            )

        window_ms = int(kwargs.get("window_ms", 3000))
        data = self._provider(str(action_id), window_ms)
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
        if not self._provider:
            return ToolResult(
                success=False,
                error="센서 어댑터가 설정되지 않았습니다.",
                data={
                    "connected": False,
                    "saturation": False,
                    "clock_sync": "unsynced",
                    "calibrated": False,
                },
            )

        data = self._provider()
        reasons = sensor_safety_errors(data, integrity=True)
        if reasons:
            reason_str = ", ".join(reasons)
            return ToolResult(
                success=False,
                error=f"센서 무결성 점검 실패: 연결 또는 신호 상태 이상 ({reason_str})",
                data=data,
            )
        return ToolResult(success=True, data=data)


def create_mock_sensor_tools(adapter: MockSensorAdapter | None = None) -> list[BaseTool]:
    """명시적인 MockSensorAdapter를 사용하는 도구 3종을 생성합니다."""
    act = adapter or MockSensorAdapter()
    return [
        ReadFieldTool(provider=act.read_field),
        AwaitTouchResultTool(provider=act.await_touch_result),
        CheckSensorIntegrityTool(provider=act.check_integrity),
    ]
