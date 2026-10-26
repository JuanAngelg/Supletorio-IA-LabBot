"""Errores tipados del sistema.

Se distinguen tres familias porque el orquestador las trata distinto:
- DomainError: una regla de negocio o de autorización rechazó la operación (rechazo correcto).
- ServiceUnavailable: un servicio no respondió; el resultado real puede ser desconocido.
- ValidationFailure: la entrada o la salida no cumple el esquema.
"""
from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str, articulo: str | None = None, category: str = "regla_negocio"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.articulo = articulo
        self.category = category  # regla_negocio | no_autorizado | conflicto | no_encontrado

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "articulo": self.articulo, "category": self.category}


class Forbidden(DomainError):
    def __init__(self, message: str, code: str = "NO_AUTORIZADO", articulo: str | None = None):
        super().__init__(code, message, articulo, category="no_autorizado")


class Conflict(DomainError):
    def __init__(self, message: str, code: str = "CONFLICTO_HORARIO", articulo: str | None = "REG-LAB-001#art4"):
        super().__init__(code, message, articulo, category="conflicto")


class NotFound(DomainError):
    def __init__(self, message: str, code: str = "NO_ENCONTRADO"):
        super().__init__(code, message, None, category="no_encontrado")


class ServiceUnavailable(Exception):
    def __init__(self, service: str, detail: str = ""):
        super().__init__(f"{service} no disponible: {detail}")
        self.service = service
        self.detail = detail


class ValidationFailure(Exception):
    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []
