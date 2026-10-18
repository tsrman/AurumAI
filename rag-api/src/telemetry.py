import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def init_telemetry(service_name: str) -> trace.Tracer:
    """Initialise OpenTelemetry tracing for ``service_name``.

    Mirrors conversation-runner/src/telemetry.py. Reads the plain
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var directly rather than through
    ``Settings`` (whose ``RAG_API_`` prefix would otherwise require a
    confusingly-named ``RAG_API_OTEL_EXPORTER_OTLP_ENDPOINT``) — "console"
    prints spans to stdout instead of exporting via OTLP/gRPC, which is the
    only usable value until an OTel collector exists in this stack.

    ``service_name`` is set as the ``service.name`` resource attribute (not
    just the tracer's own name) — without it every span reports
    ``service.name: unknown_service``, indistinguishable from any other
    service once a real collector is in the picture.
    """
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "console")

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))

    exporter: ConsoleSpanExporter | OTLPSpanExporter
    if otlp_endpoint.lower() == "console":
        exporter = ConsoleSpanExporter()
    else:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    return trace.get_tracer(service_name)
