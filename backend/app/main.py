"""C-KH-01 server skeleton; execution routes are not implemented yet."""
from typing import Literal
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="TouchBack", version="0.1.0")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    agent_ready: Literal[False]


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", agent_ready=False)
