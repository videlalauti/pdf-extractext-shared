"""Tests del middleware de timing.

`TimingMiddleware` emite un único log `request completed` por request con
`method`, `path`, `status_code` y `duration_ms`. El `request_id` llega solo
porque el middleware corre dentro del contexto que fija `RequestIdMiddleware`.

`setup_logging` muta el root logger, así que una fixture restaura el estado
original después de cada test.
"""

import logging
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from shared.web.logging import RequestIdMiddleware, TimingMiddleware, setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    uvicorn = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error")
    }

    yield

    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.handlers = handlers
    root.setLevel(level)

    for name, (old_handlers, old_propagate) in uvicorn.items():
        logger = logging.getLogger(name)
        logger.handlers = old_handlers
        logger.propagate = old_propagate


async def ok(request):
    return PlainTextResponse("hola")


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"hola"})


async def server_error(request):
    return PlainTextResponse("boom", status_code=503)


async def client_error(request):
    return PlainTextResponse("no", status_code=404)


def build_app(handler) -> Starlette:
    app = Starlette(routes=[Route("/resource", handler)])
    app.add_middleware(TimingMiddleware)
    return app


async def call(app, path: str = "/resource", headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


def timing_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == "request completed"]


# --------------------------------------------------------------------------- #
# Duración y status
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_logs_duration_and_status_of_a_successful_request(caplog):
    with caplog.at_level(logging.INFO):
        response = await call(build_app(ok))

    assert response.status_code == 200
    records = timing_records(caplog)
    assert len(records) == 1, f"se esperaba un log por request: {records}"

    record = records[0]
    assert record.method == "GET"
    assert record.path == "/resource"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, float)
    assert record.duration_ms > 0
    assert record.duration_ms == round(record.duration_ms, 2)


@pytest.mark.asyncio
async def test_duration_is_rounded_to_two_decimals(monkeypatch, caplog):
    moments = iter([1.0, 1.23456])
    monkeypatch.setattr(time, "perf_counter", lambda: next(moments))

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/resource", "headers": []}

    with caplog.at_level(logging.INFO):
        await TimingMiddleware(ok_app)(scope, receive, send)

    assert timing_records(caplog)[0].duration_ms == 234.56


# --------------------------------------------------------------------------- #
# Niveles de log
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_errors_are_logged_a_level_above_info(caplog):
    with caplog.at_level(logging.INFO):
        response = await call(build_app(server_error))

    assert response.status_code == 503
    record = timing_records(caplog)[0]
    assert record.status_code == 503
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_client_errors_stay_at_info(caplog):
    """Un 4xx es una decisión del cliente: no merece subir el nivel del log."""
    with caplog.at_level(logging.INFO):
        response = await call(build_app(client_error))

    assert response.status_code == 404
    assert timing_records(caplog)[0].levelno == logging.INFO


# --------------------------------------------------------------------------- #
# No alterar la respuesta
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_response_body_passes_through_untouched(caplog):
    with caplog.at_level(logging.INFO):
        response = await call(build_app(ok))

    assert response.text == "hola"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_non_http_scopes_are_not_timed(caplog):
    seen: list[str] = []

    async def lifespan_app(scope, receive, send):
        seen.append(scope["type"])

    with caplog.at_level(logging.INFO):
        await TimingMiddleware(lifespan_app)({"type": "lifespan"}, None, None)

    assert seen == ["lifespan"]
    assert timing_records(caplog) == []


@pytest.mark.asyncio
async def test_a_crashing_app_still_logs_one_request(caplog):
    async def broken(scope, receive, send):
        raise RuntimeError("boom")

    scope = {"type": "http", "method": "GET", "path": "/resource", "headers": []}

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            await TimingMiddleware(broken)(scope, receive, send)

    records = timing_records(caplog)
    assert len(records) == 1
    assert records[0].status_code == 500
    assert records[0].levelno == logging.WARNING


# --------------------------------------------------------------------------- #
# Integración con el request-id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_timing_log_carries_the_request_id(capsys):
    setup_logging("extraction-service")

    app = Starlette(routes=[Route("/resource", ok)])
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    await call(app, headers={"X-Request-Id": "abc-123"})

    assert "abc-123" in capsys.readouterr().out
