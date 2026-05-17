from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
import logging

def setup_metrics(service_name="rt-performance-monitor"):
    resource = Resource(attributes={
        ResourceAttributes.SERVICE_NAME: service_name
    })

    # Use Console exporter for visibility in this task
    exporter = ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5000)

    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    meter = metrics.get_meter("rt.performance")

    return meter

def get_meter():
    return metrics.get_meter("rt.performance")
