"""Tests de la configuración de CORS por entorno."""

import pytest

from shared.web.cors import (
    CORS_ORIGINS_ENV,
    DEFAULT_ALLOW_HEADERS,
    DEFAULT_ALLOW_METHODS,
    DEFAULT_DEV_ORIGINS,
    add_cors,
    parse_origins,
)


class FakeApp:
    """Captura lo que se le registra, sin levantar un servidor real."""

    def __init__(self) -> None:
        self.middlewares: list[tuple[type, dict]] = []

    def add_middleware(self, middleware_class: type, **kwargs) -> None:
        self.middlewares.append((middleware_class, kwargs))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)


# --------------------------------------------------------------------------- #
# parse_origins
# --------------------------------------------------------------------------- #


def test_defaults_to_localhost_in_development():
    assert parse_origins() == ["http://localhost"]
    assert parse_origins() == list(DEFAULT_DEV_ORIGINS)


def test_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(CORS_ORIGINS_ENV, "https://app.example.com")
    assert parse_origins() == ["https://app.example.com"]


def test_splits_a_csv_and_trims_blanks(monkeypatch):
    monkeypatch.setenv(
        CORS_ORIGINS_ENV,
        " https://app.example.com , https://admin.example.com ,,",
    )
    assert parse_origins() == ["https://app.example.com", "https://admin.example.com"]


@pytest.mark.parametrize("value", ["", "   ", ",", ",,"])
def test_empty_values_fall_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv(CORS_ORIGINS_ENV, value)
    assert parse_origins() == list(DEFAULT_DEV_ORIGINS)


def test_accepts_an_explicit_raw_string():
    """El `raw` explícito gana sobre el entorno: sirve para tests y para overrides."""
    assert parse_origins("https://uno.test,https://dos.test") == [
        "https://uno.test",
        "https://dos.test",
    ]


def test_env_mapping_can_be_injected():
    parsed = parse_origins(env={CORS_ORIGINS_ENV: "https://inyectado.test"})
    assert parsed == ["https://inyectado.test"]


def test_wildcard_is_rejected_with_credentials():
    """`*` con credenciales hace que el browser descarte la respuesta.

    El servicio quedaría inaccesible sin ningún error visible, así que es mejor
    fallar al arrancar que publicar un origen abierto por accidente.
    """
    with pytest.raises(ValueError, match="no admite"):
        parse_origins("*")


def test_wildcard_is_rejected_from_the_env_too(monkeypatch):
    monkeypatch.setenv(CORS_ORIGINS_ENV, "*")
    with pytest.raises(ValueError, match="no admite"):
        parse_origins()


def test_wildcard_among_valid_origins_is_still_rejected():
    with pytest.raises(ValueError, match="no admite"):
        parse_origins("*,https://app.example.com")


def test_wildcard_is_allowed_without_credentials():
    assert parse_origins("*", allow_credentials=False) == ["*"]


# --------------------------------------------------------------------------- #
# add_cors
# --------------------------------------------------------------------------- #


def test_add_cors_registers_the_starlette_middleware():
    from starlette.middleware.cors import CORSMiddleware

    app = FakeApp()
    origins = add_cors(app, origins=["https://app.example.com"])

    assert origins == ["https://app.example.com"]
    assert len(app.middlewares) == 1
    middleware_class, kwargs = app.middlewares[0]
    assert middleware_class is CORSMiddleware
    assert kwargs["allow_origins"] == ["https://app.example.com"]
    assert kwargs["allow_credentials"] is True


def test_add_cors_exposes_the_request_id_header():
    """El frontend tiene que poder leer X-Request-Id para reportar errores."""
    _, kwargs = _register(origins=["https://app.example.com"])

    assert "X-Request-Id" in kwargs["expose_headers"]
    assert "X-Request-Id" in kwargs["allow_headers"]


def test_add_cors_reads_the_env_var_by_default(monkeypatch):
    monkeypatch.setenv(CORS_ORIGINS_ENV, "https://desde-env.test")
    app = FakeApp()

    origins = add_cors(app)

    assert origins == ["https://desde-env.test"]
    assert app.middlewares[0][1]["allow_origins"] == ["https://desde-env.test"]


def test_add_cors_passes_the_default_methods_and_headers():
    _, kwargs = _register(origins=["https://app.example.com"])

    assert kwargs["allow_methods"] == list(DEFAULT_ALLOW_METHODS)
    assert kwargs["allow_headers"] == list(DEFAULT_ALLOW_HEADERS)


def test_add_cors_rejects_the_wildcard_before_touching_the_app(monkeypatch):
    monkeypatch.setenv(CORS_ORIGINS_ENV, "*")
    app = FakeApp()

    with pytest.raises(ValueError, match="no admite"):
        add_cors(app)

    assert app.middlewares == []


def _register(**kwargs) -> tuple[type, dict]:
    app = FakeApp()
    add_cors(app, **kwargs)
    return app.middlewares[0]


# --------------------------------------------------------------------------- #
# Comportamiento real sobre una app Starlette
# --------------------------------------------------------------------------- #


@pytest.fixture
def cors_client(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def home(request):
        return PlainTextResponse("hola")

    monkeypatch.setenv(CORS_ORIGINS_ENV, "https://app.example.com")
    app = Starlette(routes=[Route("/", home)])
    add_cors(app)
    return TestClient(app)


def test_allowed_origin_receives_the_header(cors_client):
    response = cors_client.get("/", headers={"Origin": "https://app.example.com"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"


def test_unknown_origin_receives_no_cors_headers(cors_client):
    """Sin el header ACAO, el browser bloquea la respuesta del lado del cliente."""
    response = cors_client.get("/", headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_allows_post_and_exposes_the_request_id(cors_client):
    response = cors_client.options(
        "/",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]


def test_actual_response_exposes_the_request_id_header():
    """Starlette manda expose-headers en la respuesta real, no en el preflight."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from shared.web.logging import RequestIdMiddleware

    async def home(request):
        return PlainTextResponse("hola")

    app = Starlette(routes=[Route("/", home)])
    add_cors(app, origins=["https://app.example.com"])
    app.add_middleware(RequestIdMiddleware)

    response = TestClient(app).get(
        "/",
        headers={"Origin": "https://app.example.com", "X-Request-Id": "abc-123"},
    )

    assert "X-Request-Id" in response.headers["access-control-expose-headers"]
    assert response.headers["x-request-id"] == "abc-123"
