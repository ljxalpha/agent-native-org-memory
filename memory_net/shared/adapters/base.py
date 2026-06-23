from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from memory_net.shared.models import MemoryRecord, TeamWritePlan


class MemoryAdapter(ABC):
    @abstractmethod
    def list_shared(self) -> Iterator[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    def write_team(self, plan: TeamWritePlan) -> None:
        raise NotImplementedError

    @abstractmethod
    def inject_team_hint(self, hint_spec: dict[str, object]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def remove_team_hint(self, hint_spec: dict[str, object]) -> bool:
        raise NotImplementedError
