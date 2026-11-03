# Changelog

Todos los cambios notables de `pdf-extractext-shared`. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el versionado es
[SemVer](https://semver.org/lang/es/).

## [1.3.0] - 2026-11-03

### Fixed

- `TimingMiddleware` emite `method`, `path`, `status_code` y `duration_ms` en la
  línea de log formateada, no solo en `extra`. Antes la línea quedaba como
  `request completed` y las latencias se perdían en producción, porque
  `RequestIdFormatter` no renderiza `extra`.

### Added

- Test de regresión que formatea el record con el `RequestIdFormatter` real.

### Notes

- `LOG_FORMAT` no cambia: agregarle placeholders obligaría a que todo record
  tenga esos atributos y rompería cualquier log normal.

## [1.2.0] - 2026-10-31

### Added

- `request_id_header()` y `REQUEST_ID_HEADER_OUTBOUND` para propagar el
  `X-Request-Id` en las llamadas salientes.

## [1.1.0] - 2026-10-14

### Added

- `TimingMiddleware` para medir y loguear la duración de cada request HTTP.

## [1.0.0] - 2026-09-29

### Added

- Dominio compartido: `PdfValidator`, `PyPdfTextExtractor`, `has_pdf_extension`
  y `MAX_PDF_SIZE_BYTES`.
- Utilidades web: `logging` con request-id, `resilience` y `cors`.
