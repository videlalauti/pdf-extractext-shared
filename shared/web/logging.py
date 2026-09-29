"""Logging a stdout con request-id, para correlación entre servicios.

Es la parte de "observabilidad" del 12-Factor: los logs van a stdout (el runtime
los recolecta) y cada request queda trazado por un identificador que viaja en
la cabecera `X-Request-Id` y se propaga al peer en la respuesta.
"""

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

REQUEST_ID_HEADER = "x-request-id"

LOG_FORMAT = "%(asctime)s %(levelname)s %(service)s %(request_id)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# El contextvar mantiene el id por task: con varias requests concurrentes cada
# una ve el suyo, porque cada una corre en su propio contexto.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """Id de la request en curso, o `-` si el log no viene de una request."""
    return _request_id.get()


def set_request_id(value: str) -> Any:
    """Fija el id y devuelve el token para restaurarlo."""
    return _request_id.set(value)


def reset_request_id(token: Any) -> None:
    """Restaura el id previo; se usa en el `finally` del middleware."""
    _request_id.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdFormatter(logging.Formatter):
    """Inyecta `service` y `request_id` en cada record.

    Se resuelve al formatear (y no al emitir) para que el id sea el de la task
    que está escribiendo, incluso con varias requests concurrentes.
    """

    def __init__(self, service_name: str, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt or LOG_FORMAT, datefmt or DATE_FORMAT)
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        record.service = self.service_name
        record.request_id = get_request_id()
        return super().format(record)


def setup_logging(service_name: str, level: int = logging.INFO) -> logging.Logger:
    """Configura el root logger a stdout con el formato `service request_id`.

    Idempotente: si se llama dos veces (p. ej. reload de uvicorn) no duplica
    handlers ni pierde el nivel ya configurado.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(RequestIdFormatter(service_name))

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_pdf_extractext", False):
            root.removeHandler(existing)
            existing.close()

    handler._pdf_extractext = True
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn reinstala sus propios handlers; los saco para que sus access logs
    # también lleven service y request-id.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    return logging.getLogger(service_name)


def _header_value(scope: dict, name: str) -> str | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == name.encode("ascii"):
            return raw_value.decode("latin-1")
    return None


def _is_trustworthy(value: str) -> bool:
    """Rechaza ids inyectados por el cliente: se reflejan en logs y cabeceras.

    Un `X-Request-Id` con saltos de línea o caracteres de control permitiría
    contaminar los logs del servicio y las cabeceras de respuesta.
    """
    if not value or len(value) > 64:
        return False
    return all(char.isprintable() for char in value)


class RequestIdMiddleware:
    """ASGI puro: toma o crea `X-Request-Id` y lo expone a todos los logs.

    No depende de Starlette para que sirva igual en FastAPI, uvicorn o cualquier
    otro servidor ASGI; se registra como `app.add_middleware(RequestIdMiddleware)`.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        # Solo las requests HTTP llevan request-id; lifespan y websocket no.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header_value(scope, REQUEST_ID_HEADER)
        request_id = incoming if incoming and _is_trustworthy(incoming) else new_request_id()

        async def send_with_request_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (REQUEST_ID_HEADER.encode("ascii"), request_id.encode("latin-1"))
                )
            await send(message)

        token = set_request_id(request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Sin el reset, el id se filtraría a la siguiente task del worker.
            reset_request_id(token)
