"""TouchBack backend server with runs API and frontend static mount."""
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.runs import RunApiException, router as runs_router
from app.contracts.models import ErrorResponse

app = FastAPI(title="TouchBack", version="0.1.0")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    agent_ready: Literal[False]


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", agent_ready=False)


@app.exception_handler(RunApiException)
async def run_api_exception_handler(_: Request, exc: RunApiException) -> JSONResponse:
    error = ErrorResponse(code=exc.code, message=exc.message)
    return JSONResponse(status_code=exc.status_code, content=error.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    error = ErrorResponse(
        code="validation",
        message=f"요청 형식이 올바르지 않습니다: {exc.errors()}",
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=error.model_dump())


app.include_router(runs_router)

# Mount frontend directory for browser UI
frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
