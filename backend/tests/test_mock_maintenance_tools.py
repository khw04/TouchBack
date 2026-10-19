import asyncio
from app.adapters.virtual_cooktop import VirtualCooktopAdapter
from app.tools.registry import create_registry_from_adapter, required_preflight


def test_mock_calibration_and_recalibration_are_real_tools_with_measured_baselines():
    adapter=VirtualCooktopAdapter("normal","right")
    registry=create_registry_from_adapter(adapter,for_planning=True,calibration=adapter.calibration_for_zone("right"))
    for state in adapter.tracker.zones.values():state.invalidate("test_invalidated")
    assert not asyncio.run(registry.execute("state_resynchronization")).success
    for name in ("auto_calibration","sensor_recalibration","state_resynchronization"):
        result=asyncio.run(registry.execute(name))
        assert result.success and result.data["source"]=="mock"
        assert result.data["physical_validation_performed"] is False
        assert all(v["observation_id"] for v in result.data["zones"].values())
        assert "levels" not in str(result.data) and "scenario" not in str(result.data)
    calls=asyncio.run(required_preflight(registry,"right"))
    assert calls[0]["name"]=="state_resynchronization" and all(c["result"]["success"] for c in calls)


def test_maintenance_never_fabricates_fresh_or_healthy_observations():
    adapter=VirtualCooktopAdapter("sensor_error","right")
    registry=create_registry_from_adapter(adapter,for_planning=True)
    adapter.press("a","right","increase",500,3500)
    adapter.await_zone_pair(run_id="r",action_id="a",zone_id="right",window_start_ms=500,window_end_ms=3500)
    before=adapter.operator_view()["observations"]
    for name in ("auto_calibration","sensor_recalibration","state_resynchronization"):
        assert not asyncio.run(registry.execute(name)).success
    assert adapter.operator_view()["observations"]==before
    assert not asyncio.run(registry.execute("sensor_recalibration",force=True)).success


def test_unconfigured_or_live_registry_does_not_register_mock_maintenance():
    assert create_registry_from_adapter(None).get("sensor_recalibration") is None


def test_failed_required_resynchronization_cannot_publish_an_instruction(monkeypatch):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.runs import run_store
    from app.config import settings
    from app.tools.base import ToolResult
    run_store.reset()
    monkeypatch.setattr(settings,"execution_mode","backend_stub")
    monkeypatch.setattr(settings,"demo_enabled",False)
    monkeypatch.setattr(settings,"openai_api_key","")
    try:
        with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as c:
            c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":"normal"})
            with patch("app.tools.mock_state_tools.MockStateTool.execute",return_value=ToolResult(success=False,error="invalid other channel")):
                r=c.post("/api/runs",json={"user_input":"오른쪽 화력 올려줘","source":"mock","zone_id":None}).json()
            run=c.get("/api/runs/"+r["run_id"]).json()
            assert run["status"]=="uncertain" and run["instruction"] is None
            assert c.get("/api/demo").json()["active_run_id"] is None
    finally:run_store.reset()
