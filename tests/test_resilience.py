"""Tests de resiliencia: circuit breaker y reintentos con backoff."""

import asyncio
import errno

import pytest

from shared.web.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    is_transient_error,
    retry_with_backoff,
)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeHttpError(Exception):
    """Imita httpx.HTTPStatusError, que expone el status vía `.response`."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.response = FakeResponse(status_code)


class ConnectTimeout(Exception):
    """Imita httpx.ConnectTimeout: se reconoce por nombre, sin importar httpx."""


async def _always_fails(message: str = "boom") -> None:
    raise ConnectionError(message)


async def _always_succeeds(value: str = "ok") -> str:
    return value


class SleepRecorder:
    """Doble de `asyncio.sleep` que anota los delays sin esperar de verdad."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_breaker_starts_closed():
    breaker = CircuitBreaker(service="persistence")
    assert breaker.state is CircuitState.CLOSED
    assert await breaker.call(_always_succeeds) == "ok"


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_failures():
    breaker = CircuitBreaker(service="persistence", failure_threshold=3)

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await breaker.call(_always_fails)
    assert breaker.state is CircuitState.CLOSED

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    assert breaker.state is CircuitState.OPEN
    assert breaker.failure_count == 3


@pytest.mark.asyncio
async def test_open_breaker_fails_fast_without_calling_the_peer():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1)
    calls = 0

    async def failing() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("peer caído")

    with pytest.raises(ConnectionError):
        await breaker.call(failing)

    # Después de abierto, ni una llamada más llega al peer.
    for _ in range(5):
        with pytest.raises(CircuitOpenError):
            await breaker.call(failing)

    assert calls == 1
    assert breaker.state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(service="persistence", failure_threshold=3)

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    assert breaker.failure_count == 2

    await breaker.call(_always_succeeds)

    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_slow_call_hits_call_timeout_and_counts_as_failure():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1, call_timeout=0.01)

    async def slow() -> None:
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError):
        await breaker.call(slow)

    assert breaker.state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_half_open_allows_a_single_probe_after_recovery_timeout():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1, recovery_timeout=0.05)

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    assert breaker.state is CircuitState.OPEN

    await asyncio.sleep(0.06)

    # Venció la ventana: el estado ya es medio-abierto aunque nadie haya sondado.
    assert breaker.state is CircuitState.HALF_OPEN
    assert await breaker.call(_always_succeeds) == "ok"
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_half_open_reopens_when_the_probe_fails():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1, recovery_timeout=0.05)

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)

    await asyncio.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    assert breaker.state is CircuitState.OPEN

    # Recién pasada otra ventana vuelve a medio-abierto.
    await asyncio.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_admits_only_one_probe_at_a_time():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1, recovery_timeout=0.05)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe() -> str:
        entered.set()
        await release.wait()
        return "tarde"

    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)

    await asyncio.sleep(0.06)

    first = asyncio.create_task(breaker.call(slow_probe))
    await entered.wait()

    # Mientras el sondeo está en vuelo, el resto del tráfico se corta sin invocar.
    with pytest.raises(CircuitOpenError):
        await breaker.call(_always_succeeds)

    release.set()
    assert await first == "tarde"
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_breaker_passes_arguments_through():
    breaker = CircuitBreaker(service="persistence")

    async def echo(value: str, suffix: str = "") -> str:
        return f"{value}{suffix}"

    assert await breaker.call(echo, "hola", suffix="!") == "hola!"


@pytest.mark.asyncio
async def test_reset_returns_the_breaker_to_closed():
    breaker = CircuitBreaker(service="persistence", failure_threshold=1)
    with pytest.raises(ConnectionError):
        await breaker.call(_always_fails)
    assert breaker.state is CircuitState.OPEN

    breaker.reset()

    assert breaker.state is CircuitState.CLOSED
    assert await breaker.call(_always_succeeds) == "ok"


# --------------------------------------------------------------------------- #
# Clasificación de errores transitorios
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionError(),
        # Los errno se leen del módulo: los valores difieren entre Windows y Linux.
        OSError(errno.ECONNREFUSED, "Connection refused"),
        OSError(errno.ECONNRESET, "Connection reset by peer"),
        ConnectTimeout(),
        FakeHttpError(500),
        FakeHttpError(502),
        FakeHttpError(503),
    ],
)
def test_transient_errors_are_retried(error):
    assert is_transient_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        ValueError("payload inválido"),
        FakeHttpError(400),
        FakeHttpError(401),
        FakeHttpError(403),
        FakeHttpError(404),
        FakeHttpError(409),
        FakeHttpError(422),
    ],
)
def test_non_transient_errors_are_not_retried(error):
    assert is_transient_error(error) is False


# --------------------------------------------------------------------------- #
# Retry con backoff
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retry_returns_the_first_success_without_sleeping():
    sleeper = SleepRecorder()
    calls = 0

    async def sometimes_failing() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("transitorio")
        return "listo"

    result = await retry_with_backoff(sometimes_failing, sleep=sleeper)

    assert result == "listo"
    assert calls == 2
    assert len(sleeper.delays) == 1


@pytest.mark.asyncio
async def test_retry_does_not_retry_a_4xx():
    """El 409 de "PDF duplicado" es una decisión de negocio, no una falla técnica."""
    sleeper = SleepRecorder()
    calls = 0

    async def conflict() -> None:
        nonlocal calls
        calls += 1
        raise FakeHttpError(409)

    with pytest.raises(FakeHttpError):
        await retry_with_backoff(conflict, sleep=sleeper)

    assert calls == 1
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_retry_gives_up_after_the_configured_attempts():
    sleeper = SleepRecorder()
    calls = 0

    async def always_failing() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("sigo caído")

    with pytest.raises(ConnectionError):
        await retry_with_backoff(always_failing, attempts=3, sleep=sleeper)

    assert calls == 3
    assert len(sleeper.delays) == 2


@pytest.mark.asyncio
async def test_backoff_grows_exponentially_and_respects_max_delay():
    sleeper = SleepRecorder()

    async def always_failing() -> None:
        raise ConnectionError("caído")

    with pytest.raises(ConnectionError):
        await retry_with_backoff(
            always_failing,
            attempts=6,
            base_delay=0.5,
            max_delay=4,
            sleep=sleeper,
        )

    # 6 intentos agotados => 5 esperas; el techo por intento es
    # 0.5, 1, 2, 4 y 4 (a partir de 4 el exponential queda topado por max_delay).
    ceilings = [0.5, 1.0, 2.0, 4.0, 4.0]
    assert len(sleeper.delays) == len(ceilings)
    for ceiling, delay in zip(ceilings, sleeper.delays, strict=True):
        assert 0.0 <= delay <= ceiling


@pytest.mark.asyncio
async def test_backoff_applies_jitter():
    """El jitter desincroniza a los workers: dos corridas no dan el mismo delay."""
    runs = []
    for _ in range(2):
        sleeper = SleepRecorder()

        async def always_failing() -> None:
            raise ConnectionError("caído")

        with pytest.raises(ConnectionError):
            await retry_with_backoff(always_failing, attempts=2, sleep=sleeper)
        runs.append(sleeper.delays)

    assert runs[0][0] != runs[1][0]


@pytest.mark.asyncio
async def test_retry_accepts_a_custom_predicate():
    sleeper = SleepRecorder()
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("raro")

    with pytest.raises(RuntimeError):
        await retry_with_backoff(flaky, retry_on=RuntimeError, sleep=sleeper)

    assert calls == 3


@pytest.mark.asyncio
async def test_retry_accepts_a_tuple_of_exceptions():
    sleeper = SleepRecorder()
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        raise KeyError("raro")

    with pytest.raises(KeyError):
        await retry_with_backoff(flaky, retry_on=(ValueError, KeyError), sleep=sleeper)

    assert calls == 3


@pytest.mark.asyncio
async def test_retry_forwards_arguments():
    async def echo(value: str) -> str:
        return value

    assert await retry_with_backoff(echo, "hola") == "hola"


# --------------------------------------------------------------------------- #
# Breaker + retry, el flujo de extraction -> persistence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_breaker_and_retry_together_stop_hammering_a_dead_peer():
    """Con el breaker abierto, ni los reintentos llegan a tocar el peer."""
    breaker = CircuitBreaker(
        service="persistence",
        failure_threshold=2,
        recovery_timeout=60,
        call_timeout=5,
    )
    sleeper = SleepRecorder()
    calls = 0

    async def post_document() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("persistence caído")

    async def persist() -> str:
        return await breaker.call(retry_with_backoff, post_document, attempts=3, sleep=sleeper)

    with pytest.raises(ConnectionError):
        await persist()
    assert calls == 3

    with pytest.raises(ConnectionError):
        await persist()
    assert calls == 6

    # Ya se cruzó el umbral: el servicio responde rápido y el peer no se toca más.
    with pytest.raises(CircuitOpenError):
        await persist()

    assert calls == 6
