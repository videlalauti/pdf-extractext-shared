"""Tests del logging con request-id.

`setup_logging` muta el root logger, así que una fixture restaura el estado
original después de cada test: si no, el formato de un test se filtra al resto
de la suite.
"""

import asyncio
import logging

import pytest

from shared.web.logging import (
    REQUEST_ID_HEADER,
    RequestIdFormatter,
    RequestIdMiddleware,
    get_request_id,
    new_request_id,
    set_request_id,
    setup_logging,
)

HEADER = REQUEST_ID_HEADER.encode("ascii")


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


def parse_line(line: str) -> dict[str, str]:
    """Parte una línea del formato `tiempo nivel service request_id message`."""
    parts = line.strip().split(" ", 4)
    assert len(parts) == 5, f"formato inesperado: {line!r}"
    return {
        "time": parts[0],
        "level": parts[1],
        "service": parts[2],
        "request_id": parts[3],
        "message": parts[4],
    }


async def call_asgi(app, headers: list[tuple[bytes, bytes]] | None = None) -> list[dict]:
    """Invoca la app ASGI y devuelve los mensajes enviados."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    await RequestIdMiddleware(app)({"type": "http", "headers": headers or []}, receive, send)
    return sent


async def ok_app(scope, receive, send):
    """App mínima que responde 200."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def response_request_id(sent: list[dict]) -> str:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])[HEADER].decode("latin-1")


# --------------------------------------------------------------------------- #
# setup_logging
# --------------------------------------------------------------------------- #


def test_logging_writes_to_stdout(capsys):
    setup_logging("validation-service")
    logging.getLogger("prueba").info("mensaje de prueba")

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out


def test_log_format_has_time_level_service_request_id_and_message(capsys):
    setup_logging("extraction-service")
    token = set_request_id("req-123")
    try:
        logging.getLogger("prueba").info("hola")
    finally:
        from shared.web.logging import reset_request_id

        reset_request_id(token)

    fields = parse_line(capsys.readouterr().out.strip())
    assert fields["level"] == "INFO"
    assert fields["service"] == "extraction-service"
    assert fields["request_id"] == "req-123"
    assert fields["message"] == "hola"
    assert fields["time"][:4].isdigit()


def test_request_id_outside_a_request_is_a_dash(capsys):
    setup_logging("summary-service")
    logging.getLogger("prueba").info("sin request")

    assert parse_line(capsys.readouterr().out.strip())["request_id"] == "-"


@pytest.mark.parametrize(
    ("level", "expected"),
    [("debug", "DEBUG"), ("info", "INFO"), ("warning", "WARNING"), ("error", "ERROR")],
)
def test_levels_are_rendered(capsys, level, expected):
    setup_logging("persistence-service", level=logging.DEBUG)
    getattr(logging.getLogger("prueba"), level)("mensaje")

    assert parse_line(capsys.readouterr().out.strip())["level"] == expected


def test_the_default_level_filters_debug(capsys):
    """INFO por default: no quiero ensuciar stdout con debug en los servicios."""
    setup_logging("persistence-service")
    logging.getLogger("prueba").debug("no debe aparecer")

    assert capsys.readouterr().out.strip() == ""


def test_setup_logging_respects_the_level():
    setup_logging("validation-service", level=logging.WARNING)
    assert logging.getLogger().level == logging.WARNING


def owned_handlers() -> list[logging.Handler]:
    """Handlers que instaló setup_logging, sin los que mete pytest."""
    return [h for h in logging.getLogger().handlers if getattr(h, "_pdf_extractext", False)]


def test_setup_logging_is_idempotent(capsys):
    """Un reload de uvicorn no debe duplicar handlers ni duplicar los logs."""
    setup_logging("validation-service")
    setup_logging("validation-service")
    setup_logging("validation-service")

    assert len(owned_handlers()) == 1

    logging.getLogger("prueba").info("una vez")

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, f"log duplicado: {out}"


def test_setup_logging_removes_the_previous_handler():
    setup_logging("validation-service")
    first = owned_handlers()[0]

    setup_logging("extraction-service")

    assert first not in logging.getLogger().handlers
    assert len(owned_handlers()) == 1


def test_setup_logging_leaves_foreign_handlers_alone():
    """Los handlers que no son suyos (pytest, libraries) no se tocan."""
    antes = list(logging.getLogger().handlers)
    setup_logging("validation-service")

    for handler in antes:
        assert handler in logging.getLogger().handlers


def test_setup_logging_hijacks_the_uvicorn_loggers(capsys):
    """Los access logs de uvicorn también llevan service y request-id."""
    setup_logging("extraction-service")
    logging.getLogger("uvicorn.access").info("127.0.0.1 - GET /health")

    fields = parse_line(capsys.readouterr().out.strip())
    assert fields["service"] == "extraction-service"


def test_new_request_ids_are_unique():
    assert new_request_id() != new_request_id()
    assert len(new_request_id()) == 32


def test_formatter_falls_back_to_a_dash_for_unknown_records():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    formatted = RequestIdFormatter("svc").format(record)
    assert " svc - m" in formatted


# --------------------------------------------------------------------------- #
# RequestIdMiddleware
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_generates_a_request_id_when_the_client_sends_none():
    sent = await call_asgi(ok_app)
    request_id = response_request_id(sent)

    assert len(request_id) == 32
    assert all(char in "0123456789abcdef" for char in request_id)


@pytest.mark.asyncio
async def test_propagates_the_client_request_id():
    sent = await call_asgi(ok_app, [(HEADER, b"abc-123")])
    assert response_request_id(sent) == "abc-123"


@pytest.mark.asyncio
async def test_request_id_is_case_insensitive():
    sent = await call_asgi(ok_app, [(b"X-Request-ID", b"mayusculas")])
    assert response_request_id(sent) == "mayusculas"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        b"a" * 65,
        b"mal\r\nX-Injected: 1",
        b"con\ttab",
        b"",
        b"con\x00nulo",
        b"\x1b[31minyeccion",
    ],
)
async def test_hostile_request_ids_are_replaced(hostile):
    """Un id del cliente se refleja en logs y cabeceras: hay que filtrarlo.

    Sin este filtro, un salto de línea permitiría inyectar cabeceras de
    respuesta y falsear entradas del log.
    """
    sent = await call_asgi(ok_app, [(HEADER, hostile)])
    request_id = response_request_id(sent)

    assert request_id != hostile.decode("latin-1")
    assert len(request_id) == 32


@pytest.mark.asyncio
async def test_response_keeps_the_existing_headers():
    async def app_with_headers(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    sent = await call_asgi(app_with_headers, [(HEADER, b"abc-123")])
    headers = dict(next(m for m in sent if m["type"] == "http.response.start")["headers"])

    assert headers[b"content-type"] == b"application/json"
    assert headers[HEADER] == b"abc-123"


@pytest.mark.asyncio
async def test_the_contextvar_is_clean_after_the_request():
    assert get_request_id() == "-"
    await call_asgi(ok_app, [(HEADER, b"abc-123")])
    assert get_request_id() == "-"


@pytest.mark.asyncio
async def test_the_contextvar_is_clean_even_if_the_app_raises():
    async def broken(scope, receive, send):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await call_asgi(broken, [(HEADER, b"abc-123")])

    assert get_request_id() == "-"


@pytest.mark.asyncio
async def test_the_request_id_reaches_the_logs(capsys):
    setup_logging("extraction-service")

    async def logging_app(scope, receive, send):
        logging.getLogger("prueba").info("dentro de la request")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await call_asgi(logging_app, [(HEADER, b"abc-123")])

    assert parse_line(capsys.readouterr().out.strip())["request_id"] == "abc-123"


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched():
    seen = []

    async def lifespan_app(scope, receive, send):
        seen.append(scope["type"])

    await RequestIdMiddleware(lifespan_app)({"type": "lifespan"}, None, None)
    await RequestIdMiddleware(lifespan_app)({"type": "websocket"}, None, None)

    assert seen == ["lifespan", "websocket"]
    assert get_request_id() == "-"


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_id():
    """Con un solo id global, los logs de dos requests concurrentes se pisan."""
    seen: list[str] = []

    async def slow_app(scope, receive, send):
        await asyncio.sleep(0.01)
        seen.append(get_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await asyncio.gather(
        *(call_asgi(slow_app, [(HEADER, f"req-{index}".encode())]) for index in range(8))
    )

    assert sorted(seen) == [f"req-{index}" for index in range(8)]


@pytest.mark.asyncio
async def test_the_middleware_works_as_starlette_middleware():
    """`app.add_middleware(RequestIdMiddleware)` la envuelve como clase ASGI."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def home(request):
        return PlainTextResponse("hola")

    app = Starlette(routes=[Route("/", home)])
    app.add_middleware(RequestIdMiddleware)

    response = TestClient(app).get("/", headers={"X-Request-Id": "abc-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "abc-123"
