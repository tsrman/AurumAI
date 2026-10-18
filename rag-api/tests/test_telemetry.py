from opentelemetry import trace

from src.telemetry import init_telemetry


def test_init_telemetry_defaults_to_console_and_returns_a_tracer(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = init_telemetry("rag-api-test")
    assert isinstance(tracer, trace.Tracer)
