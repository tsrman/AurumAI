import os
from typing import Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def init_telemetry(
    service_name: str,
    endpoint: Optional[str] = None,
) -> trace.Tracer:
    """Initialise OpenTelemetry tracing for ``service_name``.

    The exporter endpoint is read from the ``OTEL_EXPORTER_OTLP_ENDPOINT``
    environment variable, falling back to ``endpoint`` and finally to the
    default ``http://localhost:4317``.  If the endpoint is ``"console"`` then
    spans are printed to stdout instead of being exported via OTLP/gRPC.
    """
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint or "http://localhost:4317")

    provider = TracerProvider()

    if otlp_endpoint.lower() == "console":
        exporter: ConsoleSpanExporter | OTLPSpanExporter = ConsoleSpanExporter()
    else:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    return trace.get_tracer(service_name)
