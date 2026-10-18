from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def init_telemetry(service_name: str, endpoint: str) -> trace.Tracer:
    """Initialise OpenTelemetry tracing for ``service_name``.

    Mirrors conversation-runner/src/telemetry.py: ``endpoint`` "console"
    prints spans to stdout instead of exporting via OTLP/gRPC — this repo has
    no OTel collector running, so "console" (the default) is the only usable
    value until one is added.

    ``service_name`` is set as the ``service.name`` resource attribute (not
    just the tracer's own name) — without it every span reports
    ``service.name: unknown_service``, indistinguishable from any other
    service once a real collector is in the picture.
    """
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))

    exporter: ConsoleSpanExporter | OTLPSpanExporter
    if endpoint.lower() == "console":
        exporter = ConsoleSpanExporter()
    else:
        exporter = OTLPSpanExporter(endpoint=endpoint)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    return trace.get_tracer(service_name)
