"""Standard tool registry initialization with default sensor tools."""
from app.tools.base import ToolRegistry
from app.tools.sensor_tools import AwaitTouchResultTool, CheckSensorIntegrityTool, ReadFieldTool


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFieldTool())
    registry.register(AwaitTouchResultTool())
    registry.register(CheckSensorIntegrityTool())
    return registry


default_tool_registry = create_default_registry()
