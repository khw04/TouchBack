"""Standard tool registry initialization with default sensor tools."""
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


default_tool_registry = create_default_registry()
