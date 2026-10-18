from fastapi import FastAPI
import uvicorn

health_app = FastAPI()


@health_app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def run_health_server(port: int = 8080) -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="warning")
