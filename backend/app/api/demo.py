"""Explicitly enabled, same-origin loopback-only operator controls; never an Agent Tool."""
from fastapi import APIRouter, Request
from app.adapters.demo_setup import DemoScenario, SCENARIOS
from app.api.runs import RunApiException, run_store
from app.config import settings
from app.contracts.models import Contract

router = APIRouter(prefix="/api/demo", tags=["development-demo"])


class ScenarioRequest(Contract):
    scenario: DemoScenario


def enabled(request: Request) -> bool:
    return (settings.demo_enabled and settings.execution_mode in ("mock_llm", "real_llm")
            and request.url.hostname in ("127.0.0.1", "::1", "localhost")
            and request.client is not None and request.client.host in ("127.0.0.1", "::1"))


@router.get("")
def metadata(request: Request):
    if not enabled(request):
        return {"enabled": False}
    return {"enabled": True, "source": "mock", "execution_mode": settings.execution_mode,
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
