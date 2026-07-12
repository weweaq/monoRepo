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

    @property
    def analysis_type(self) -> str:
        """该源在画像生成时使用的分析分派类型。默认 'topic'。

        - 'agentic'：走 direction + decision + activity（如 trae 的 Agent 工作流）
        - 'topic'：走 topic + activity（如 marvis / 内容消费类源）
        """
        return "topic"

    @property
    def profile_target(self) -> str:
        """该源在画像产物中的归属。默认 'channel'。

        - 'channel'：生成独立 channel 画像
        - 'consumption'：汇入 content_consumption 合成画像（bilibili / netease）
        - 'none'：仅入库，不进入画像
        """
        return "channel"
