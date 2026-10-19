"""Explicitly enabled, same-origin loopback-only operator controls; never an Agent Tool."""
from typing import Literal
from fastapi import APIRouter, Request
from app.adapters.demo_setup import DemoScenario, SCENARIOS
from app.api.runs import RunApiException, run_store
from app.config import settings
from app.contracts.models import Contract, Zone, Text

router = APIRouter(prefix="/api/demo", tags=["development-demo"])


class ScenarioRequest(Contract):
    scenario: DemoScenario


def local(request: Request) -> bool:
    return (request.url.hostname in ("127.0.0.1", "::1", "localhost")
            and request.client is not None and request.client.host in ("127.0.0.1", "::1"))


def enabled(request: Request) -> bool:
    return local(request) and settings.demo_enabled and settings.execution_mode in ("mock_llm", "real_llm")


class PressRequest(Contract):
    action_id: Text
    zone_id: Zone
    operation: Literal["increase", "decrease"]


@router.post("/runs/{run_id}/press")
def press(run_id: str, req: PressRequest, request: Request):
    require_local_write(request)
    if not enabled(request): raise RunApiException(403,"validation","시연을 먼저 활성화하세요.")
    return run_store.press_virtual(run_id,req.action_id,req.zone_id,req.operation)


class SettingsRequest(Contract):
    execution_mode: Literal["mock_llm", "real_llm"]
    scenario: DemoScenario = "normal"


def require_local_write(request: Request):
    if not local(request):
        raise RunApiException(403, "validation", "로컬 시연 화면에서만 설정할 수 있습니다.")
    origin = request.headers.get("origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise RunApiException(403, "validation", "같은 출처의 로컬 화면에서만 변경할 수 있습니다.")


@router.post("/settings")
def configure_settings(req: SettingsRequest, request: Request):
    require_local_write(request)
    run_store.configure_demo(req.execution_mode, req.scenario)
    return metadata(request)


@router.get("")
def metadata(request: Request):
    if not local(request):
        return {"enabled": False}
    active_run_id = run_store.active_run_id()
    if not enabled(request):
        return {"enabled": False, "manageable": True, "active_run_id": active_run_id, "api_key_configured": settings.is_openai_configured(),
                "model": settings.openai_model, "execution_mode": settings.execution_mode,
                "scenario": settings.mock_scenario, "scenarios": list(SCENARIOS)}
    return {"enabled": True, "manageable": True, "active_run_id": active_run_id, "api_key_configured": settings.is_openai_configured(),
            "model": settings.openai_model, "virtual_cooktop": run_store.virtual_demo, "source": "mock", "execution_mode": settings.execution_mode,
            "scenario": settings.mock_scenario, "scenarios": list(SCENARIOS),
            "physical_validation_performed": False}


@router.post("/scenario")
def configure(req: ScenarioRequest, request: Request):
    if not enabled(request):
        raise RunApiException(403, "validation", "로컬 개발 시연이 활성화되지 않았습니다.")
    # No cross-origin browser form can mutate the operator configuration.
    origin = request.headers.get("origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise RunApiException(403, "validation", "같은 출처의 로컬 시연 화면에서만 변경할 수 있습니다.")
    run_store.configure_mock_scenario(req.scenario)
    return metadata(request)
