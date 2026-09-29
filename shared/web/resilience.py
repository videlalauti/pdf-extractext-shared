"""Resiliencia entre servicios: circuit breaker y reintentos con backoff.

Implementa el patrón del `.md`: registrar fallas hacia un peer, cortar el
tráfico cuando el ratio de errores supera un umbral, y después de una ventana
de espera pasar a un estado intermedio que sondea la recuperación.
"""

import asyncio
import errno
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

# Errno de red que vale la pena reintentar; los demás son config/id de sistema.
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)

# Clases de los clientes HTTP habituales (httpx y compañía). Se comparan por
# nombre para no volver `httpx` una dependencia obligatoria del paquete: solo
# el servicio que llama a un peer necesita instalarlo, y ese peer ya lo tiene.
_TRANSIENT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ProtocolError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "TransportError",
        "WriteError",
        "WriteTimeout",
    }
)

_SERVER_ERROR_STATUS = 500


class CircuitState(str, Enum):
    """Cerrado = normal, abierto = cortocircuita, medio-abierto = sondea."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """El circuito está abierto: la llamada se corta sin tocar el peer."""

    def __init__(self, service: str, retry_after: float):
        self.service = service
        self.retry_after = retry_after
        super().__init__(f"Circuito abierto para '{service}'; reintentar en {retry_after:.1f}s")


def _status_code_of(error: BaseException) -> int | None:
    """Saca el status de un error HTTP sin importar el cliente usado.

    httpx expone `error.response.status_code`; otros clientes, `error.status_code`.
    """
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def is_transient_error(error: BaseException) -> bool:
    """True solo para fallas que pueden resolverse solas al reintentar.

    Red y timeout, y `5xx` del peer. Un `4xx` es una decisión de negocio (por
    ejemplo el 409 de "PDF duplicado"): reintentarlo no cambia la respuesta y
    solo multiplica la carga, así que sube como error de una.
    """
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True

    if isinstance(error, OSError) and error.errno in _TRANSIENT_ERRNOS:
        return True

    if any(klass.__name__ in _TRANSIENT_ERROR_NAMES for klass in type(error).__mro__):
        return True

    status = _status_code_of(error)
    return status is not None and status >= _SERVER_ERROR_STATUS


def _as_predicate(retry_on: Any) -> Callable[[BaseException], bool]:
    """Acepta un predicado, una excepción o una tupla de excepciones."""
    if callable(retry_on):
        return retry_on
    types = retry_on if isinstance(retry_on, tuple) else (retry_on,)
    return lambda error: isinstance(error, types)


async def retry_with_backoff(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 4,
    retry_on: Any = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Any:
    """Reintenta `func` con backoff exponencial y jitter; solo errores transitorios.

    El jitter full evita que todos los workers reintenten en el mismo instante
    contra un peer que acaba de recovering. `sleep` es inyectable para tests.
    """
    predicate = is_transient_error if retry_on is None else _as_predicate(retry_on)

    for attempt in range(1, attempts + 1):
        try:
            return await func(*args)
        except Exception as error:
            if attempt >= attempts or not predicate(error):
                raise

            ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await sleep(random.uniform(0.0, ceiling))

    raise AssertionError("retry_with_backoff agotó los intentos sin resolverse")


class CircuitBreaker:
    """Cortocircuita las llamadas a `service` cuando el peer está degradado.

    Parámetros del patrón del `.md`: umbral de fallas, ventana de recuperación y
    tiempo de expiración de la llamada.
    """

    def __init__(
        self,
        service: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30,
        call_timeout: float = 10,
    ):
        self.service = service
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.call_timeout = call_timeout

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        # Serializa las transiciones de estado, no las llamadas: el lock se
        # suelta antes de esperar al peer para no convertir el breaker en un
        # cuello de botella que serializa todas las requests.
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Estado actual; un circuito abierto vencido se reporta medio-abierto."""
        if self._state is CircuitState.OPEN and self._recovery_elapsed():
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    def _recovery_elapsed(self) -> bool:
        return time.monotonic() - self._opened_at >= self.recovery_timeout

    def _retry_after(self) -> float:
        return max(0.0, self.recovery_timeout - (time.monotonic() - self._opened_at))

    async def call(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Ejecuta `func` bajo el breaker; si está abierto no invoca al peer."""
        await self._before_call()
        try:
            result = await asyncio.wait_for(func(*args, **kwargs), timeout=self.call_timeout)
        except BaseException:
            # CancelledError también cuenta como falla: el peer no respondió.
            await self._record_failure()
            raise
        await self._record_success()
        return result

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.OPEN and self._recovery_elapsed():
                # Venció la ventana: una sola llamada sondea si el peer volvió.
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return

            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(self.service, self._retry_after())

            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(self.service, self.recovery_timeout)
                self._probe_in_flight = True

    async def _record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            self._state = CircuitState.CLOSED

    async def _record_failure(self) -> None:
        async with self._lock:
            self._probe_in_flight = False
            self._failures += 1

            # Un sondeo fallido reabre de inmediato; en cerrado, al cruzar el umbral.
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Vuelve a cerrado; útil en tests y en un endpoint de health check."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._probe_in_flight = False
        self._opened_at = 0.0
