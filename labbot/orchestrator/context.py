"""Contexto de una solicitud: lo que el orquestador sabe y registra mientras la atiende."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..domain.policy import Actor
from ..sessions import Session


@dataclass
class RequestContext:
    request_id: str
    session: Session
    strategy: str
    text: str = ""
    started: float = field(default_factory=time.perf_counter)
    route: str | None = None
    tools: list[dict] = field(default_factory=list)          # llamadas realizadas (nombre, estado, ...)
    evidence: dict[str, dict] = field(default_factory=dict)  # fragmentos RAG entregados al LLM
    quarantined: list[str] = field(default_factory=list)
    pending: dict | None = None
    mutations: list[dict] = field(default_factory=list)
    denials: list[dict] = field(default_factory=list)
    service_failures: list[dict] = field(default_factory=list)
    auth_required: bool = False
    ui_actions: list[dict] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    llm_ms: float = 0.0
    citations_valid: list[str] = field(default_factory=list)
    citations_invalid: list[str] = field(default_factory=list)

    @property
    def actor(self) -> Actor:
        return Actor(self.session.user_id, self.session.role, self.session.authenticated)

    def add_usage(self, tokens_in: int, tokens_out: int, ms: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.llm_calls += 1
        self.llm_ms += ms

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)
