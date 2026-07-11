from abc import ABC, abstractmethod

from profile.models import ChatRecord


class BaseReader(ABC):
    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def ingest(self) -> int:
        """读取原始数据并写入 raw_data 表，返回入库条数。"""
        ...

    @abstractmethod
    def read(self) -> list[ChatRecord]:
        """读取数据源，返回统一 ChatRecord 列表（向后兼容 / fallback 用）。"""
        ...
