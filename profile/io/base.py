from abc import ABC, abstractmethod

from profile.models import ChatRecord


class BaseReader(ABC):
    @abstractmethod
    def read(self) -> list[ChatRecord]:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...
