"""CORS configurado por entorno, con los orígenes explícitos.

12-Factor: la configuración viene del entorno, no del código. El comodín `*`
queda bloqueado cuando hay credenciales, porque el browser descarta la respuesta
y el service queda inaccesible en silencio.
"""

import os
from collections.abc import Sequence
from typing import Any

CORS_ORIGINS_ENV = "CORS_ORIGINS"

# Default de desarrollo: los servicios se sirven en http://localhost, no en :80.
DEFAULT_DEV_ORIGINS = ("http://localhost",)

DEFAULT_ALLOW_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
DEFAULT_ALLOW_HEADERS = ("Authorization", "Content-Type", "X-Request-Id")


def parse_origins(
    raw: str | None = None,
    *,
    allow_credentials: bool = True,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Lee `CORS_ORIGINS` (CSV) y devuelve los orígenes permitidos.

    Sin la variable devuelve los de desarrollo. `*` solo se acepta si además
    `allow_credentials` es False: es la única combinación que un browser
    respeta, y de paso evita publicar un servicio abierto por accidente.
    """
    if raw is None:
        source = env if env is not None else os.environ
        raw = source.get(CORS_ORIGINS_ENV, "")

    origins = [item.strip() for item in raw.split(",") if item.strip()]

    if not origins:
        return list(DEFAULT_DEV_ORIGINS)

    if "*" in origins and allow_credentials:
        raise ValueError(
            f"{CORS_ORIGINS_ENV} no admite '*' junto con credenciales: "
            "enumerá los orígenes permitidos (ej. https://app.example.com)"
        )

    return origins


def add_cors(
    app: Any,
    *,
    origins: Sequence[str] | None = None,
    allow_credentials: bool = True,
    allow_methods: Sequence[str] = DEFAULT_ALLOW_METHODS,
    allow_headers: Sequence[str] = DEFAULT_ALLOW_HEADERS,
) -> list[str]:
    """Registra el middleware de CORS leyendo `CORS_ORIGINS` del entorno.

    Resuelve Starlette recién acá: el paquete no lo declara como dependencia,
    pero cualquier servicio FastAPI ya lo tiene instalado.
    """
    from starlette.middleware.cors import CORSMiddleware

    allowed = list(origins) if origins is not None else parse_origins(
        allow_credentials=allow_credentials
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=allow_credentials,
        allow_methods=list(allow_methods),
        allow_headers=list(allow_headers),
        # El id de request debe poder viajar también en preflight.
        expose_headers=["X-Request-Id"],
    )

    return allowed
