"""Paquete compartido de los microservicios PDF ExtractExt.

Dos subpaquetes independientes:

- `shared.domain`: reglas de negocio puras (validación, excepciones, extracción).
- `shared.web`: utilidades de transporte (logging, resiliencia, CORS).

Este `__init__` no importa nada a propósito: cada subpaqueto se importa por
separado para que `shared.web` funcione sin las dependencias opcionales de
`shared.domain`.
"""
