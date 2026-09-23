"""链路追踪（OpenTelemetry）接入。

为什么值得做：
指标（Prometheus）回答「出了什么事」，日志（Loki）回答「具体报了什么」，
但都答不了「这一次请求到底卡在哪一环」。追踪补的就是这一块 ——
一次请求从进了 ingress → 应用 → 数据库，各段耗时多少，一目了然。

设计取舍：
- **完全可选**：没配 `OTEL_EXPORTER_OTLP_ENDPOINT` 就什么都不做。
  本地开发、单测都不需要起一个 collector，也不该因为追踪没配就起不来。
- 用 OTLP/HTTP（4318）而不是 gRPC（4317）：纯 HTTP 更容易穿过各种网络限制，
  也用不着额外的 gRPC 依赖。
- 采样率默认 1.0（全采）。这个应用请求量极低，全采不会把 Tempo 压垮；
  高流量的服务应该降到 0.01 之类。
- 关闭时**不注册任何 instrumentation**，避免在 /healthz 这种高频探针上
  产生无意义的 span（探针每 10 秒一次，会产生大量噪音）。
"""

from __future__ import annotations

import os

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "radar")
SAMPLE_RATIO = float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0"))
# 探针路径不打 span：它们由 kubelet 每 10 秒调用一次，纯噪音
EXCLUDED_PATHS = ("/healthz", "/metrics")
_enabled = False


def enabled() -> bool:
    return _enabled


def setup(app) -> bool:
    """给 FastAPI 应用装上追踪。返回是否真的启用了。"""
    global _enabled
    if not OTLP_ENDPOINT:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError:  # pragma: no cover - 依赖没装时静默降级
        print("OpenTelemetry 依赖未安装，跳过链路追踪", flush=True)
        return False

    resource = Resource.create({
        "service.name": SERVICE_NAME,
        # 让 Grafana 里能按「哪个版本」筛 trace，定位新版本引入的问题时很有用
        "service.version": os.getenv("RADAR_VERSION", "unknown"),
        "deployment.environment": os.getenv("RADAR_ENV", "prod"),
    })
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(SAMPLE_RATIO)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls=",".join(EXCLUDED_PATHS))
    _enabled = True
    print(f"链路追踪已启用: endpoint={OTLP_ENDPOINT} service={SERVICE_NAME}", flush=True)
    return True
