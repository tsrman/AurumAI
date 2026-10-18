from opentelemetry import trace

from src.core.telemetry import init_telemetry


def test_init_telemetry_console_endpoint_returns_a_tracer():
    tracer = init_telemetry("business-backend-test", endpoint="console")
    assert isinstance(tracer, trace.Tracer)


def test_init_telemetry_can_be_called_more_than_once():
    """Both entrypoints (main_business.py, main_admin.py) call this at
    import time — importing both in one process (as a test run can) must not
    raise, even though only the first TracerProvider actually takes effect."""
    init_telemetry("business-backend-test-a", endpoint="console")
    init_telemetry("business-backend-test-b", endpoint="console")
