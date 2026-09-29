# pdf-extractext-shared

Fuente única de verdad del dominio y de las utilidades web de los microservicios
de **PDF ExtractExt**. Este repo reemplaza las copias de `shared/domain/` que
estaban clonadas en `validation`, `extraction`, `persistence` y `summary`.

## Por qué existe

El paquete de dominio estaba duplicado byte a byte en 4 repos y ya había
divergido: el mensaje de error de `pypdf_text_extractor` interpolaba `{error!s}`
en un repo y `{str(error)}` en otro, y el `__all__` estaba en distinto orden. El
único control era un workflow externo (`shared-domain.yml`) que hoy falla. Un
paquete instalado desde una URL pinneada a un tag elimina la copia por
definición: no hay dos versiones que puedan divergir.

## Instalación

Se instala por URL del zip del tag. No hace falta `git` ni registry privado, y
el tag acts como pinneado reproducible:

```bash
pip install "https://github.com/videlalauti/pdf-extractext-shared/archive/refs/tags/v1.0.0.zip"
```

El paquete **no tiene dependencias de runtime obligatorias**. Solo `extraction`
extrae texto, así que es el único que agrega el extra. El extra se pide con una
referencia directa PEP 508 (`nombre[extra] @ url`): `URL[pypdf]` no funciona,
porque pip interpretaría los corchetes como parte de la URL y daría 404.

```bash
pip install "pdf-extractext-shared[pypdf] @ https://github.com/videlalauti/pdf-extractext-shared/archive/refs/tags/v1.0.0.zip"
```

En `requirements.txt`, que es como lo van a usar los servicios:

```text
# validation / persistence / summary
pdf-extractext-shared @ https://github.com/videlalauti/pdf-extractext-shared/archive/refs/tags/v1.0.0.zip
```

```text
# extraction
pdf-extractext-shared[pypdf] @ https://github.com/videlalauti/pdf-extractext-shared/archive/refs/tags/v1.0.0.zip
```

| Servicio | Instala | Usa `pypdf` |
|---|---|---|
| `validation` | base | no |
| `persistence` | base | no |
| `summary` | base | no |
| `extraction` | `[pypdf]` | sí |

## Migración de un servicio

1. Borrar la copia local: `shared/domain/` (el `__init__.py` de `shared/` queda,
   o se borra también si el servicio ya no tiene código propio ahí).
2. Sacar `shared/` del `.dockerignore` si estaba excluido.
3. Reemplazar la copia en `requirements.txt` por la referencia directa al tag
   (con el extra `[pypdf]` solo en `extraction`).
4. Borrar el `sys.path.insert` de `tests/conftest.py` si solo estaba para que
   `import shared` encontrara la copia local.
5. Quitar `tests/test_pdf_validator.py` y `tests/test_pypdf_text_extractor.py`
   del servicio: ahora corren en este repo. Los tests propios del servicio
   (los de sus endpoints) se quedan.

## `shared.domain`

Dominio puro, sin I/O de disco y sin dependencias de framework.

```python
from shared.domain import MAX_PDF_SIZE_BYTES, PdfValidator, has_pdf_extension

validator = PdfValidator(max_size_bytes=MAX_PDF_SIZE_BYTES)

result = validator.validate(pdf_bytes)      # no lanza; sirve para 200/400
result = validator.validate_or_raise(pdf_bytes)  # lanza; sirve para 4xx

has_pdf_extension("reporte.PDF")  # True
```

`MAX_PDF_SIZE_BYTES` vive acá y en ningún otro lado: si cada servicio definiera su
propio límite, el pipeline aceptaría en un salto lo que otro rechaza.

### Sobre `pypdf_text_extractor`

`PyPdfTextExtractor.extract_text_from_bytes()` es `async`, pero el parseo
(`BytesIO`, `PdfReader` y el recorrido de páginas) corre dentro de
`asyncio.to_thread`. Antes era un `async def` con cuerpo síncrono: un PDF grande
congelaba el event loop y con él todas las requests concurrentes del servicio.

`PyPdfTextExtractor` se importa de forma diferida (`shared.domain.__getattr__`),
así que importar el resto del dominio no requiere `pypdf`. Los tres servicios
que no extraen texto instalan el paquete base.

## `shared.web`

Utilidades de transporte que los servicios repetían. `logging` y `resilience`
usan solo la stdlib; `cors` resuelve Starlette en el momento de usarlo.

### Logging con request-id

```python
from shared.web.logging import RequestIdMiddleware, setup_logging

setup_logging("extraction-service")           # logs a stdout
app.add_middleware(RequestIdMiddleware)       # toma o crea X-Request-Id
```

El formato es `tiempo nivel service request_id message`. El `request_id` se
guarda en un `contextvar`, así que con varias requests concurrentes cada log
trae el id de la request que lo escribió. El id del cliente se acepta solo si es
corto y sin caracteres de control: se refleja en logs y cabeceras, y un
`X-Request-Id` con saltos de línea los contaminaría. La respuesta siempre
devuelve el `X-Request-Id` con el que se atendió.

### Resiliencia

```python
from shared.web.resilience import CircuitBreaker, retry_with_backoff

breaker = CircuitBreaker(service="persistence", failure_threshold=5, recovery_timeout=30)

async def persist():
    return await breaker.call(retry_with_backoff, post_document, attempts=3)
```

`CircuitBreaker` sigue el patrón del `.md`: **cerrado** mientras todo anda,
**abierto** al cruzar el umbral de fallas (contesta rápido, sin invocar al peer)
y **medio-abierto** pasada la ventana de recuperación, donde admite un único
sondeo. Si el sondeo falla, el circuito vuelve a abrirse.

`retry_with_backoff` reintenta con backoff exponencial y jitter, y **solo**
errores transitorios: red, timeout y `5xx`. Un `4xx` sube como error de una,
porque es una decisión de negocio — el `409` de persistence ("ese PDF ya
existe", deduplicación por `checksum`) no se resuelve reintentando, y hacerlo
solo multiplica la carga y termina en un `502` engañoso.

`retry_on` acepta un predicado, una excepción o una tupla de excepciones, por si
un servicio necesita otra política.

### CORS

```python
from shared.web.cors import add_cors

add_cors(app)   # lee CORS_ORIGINS del entorno
```

```bash
CORS_ORIGINS=https://app.example.com,https://admin.example.com
```

Sin la variable usa `http://localhost` (default de desarrollo). `*` se rechaza
cuando hay credenciales, porque el browser descarta esa respuesta y el servicio
queda inaccesible sin ningún error visible.

## Desarrollo

```bash
python -m venv .venv
pip install -e ".[dev,pypdf]"
ruff check .
pytest tests/ -v
```

`tests/conftest.py` arma PDFs con la tabla `xref` y el `startxref` correctos, así
que los tests de extracción dependen de `pypdf` real y no de bytes inventados.

El CI corre `ruff`, `pytest` y además instala el paquete desde la URL del tag en
un Python limpio e importa `shared.domain.pdf_validator`: si alguna vez el
`__init__` arrastra una dependencia opcional, ese job se rompe antes de que un
servicio lo descubra en producción.

## Versionado

Semver. Subir la versión en `pyproject.toml` **y** en la URL de instalación del
README y del CI, y recién ahí taggear.
