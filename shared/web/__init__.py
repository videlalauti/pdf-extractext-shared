"""Utilidades web compartidas: logging, resiliencia y CORS.

Cada módulo se importa por separado y ninguno arrastra dependencias obligatorias:
`logging` y `resilience` usan solo la stdlib, y `cors` resuelve Starlette en el
momento de usarse, cuando el servicio ya lo tiene instalado.
"""
