"""registry 自动发现 + 外挂式信息源架构的回归测试。

确保：
  - 4 个已知源被自动发现，且顺序/归属与重构前一致；
  - 未知源名在 ingest 解析中被过滤；
  - 新增源只需放一个 reader 文件即可被发现（外挂验证）。
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profile.io.registry import (
    channel_reader_names,
    consumption_reader_names,
    get_reader,
    list_reader_names,
)
from profile.io.base import BaseReader


def test_discovers_four_known_readers():
    names = list_reader_names()
    assert set(names) >= {"trae", "marvis", "bilibili", "netease"}


def test_channel_reader_order_and_membership():
    channels = channel_reader_names()
    assert channels == ["trae", "marvis"]


def test_consumption_reader_membership():
    consumption = consumption_reader_names()
    assert set(consumption) == {"bilibili", "netease"}


def test_get_reader_instantiates():
    reader = get_reader("trae")
    assert reader is not None
    assert isinstance(reader, BaseReader)
    assert reader.source_name == "trae"
    assert reader.analysis_type == "agentic"
    assert reader.profile_target == "channel"


def test_unknown_source_resolves_to_none():
    assert get_reader("does_not_exist") is None


def test_ingest_filters_unknown_names():
    # 模拟 cli.ingest 的过滤逻辑：未知名被剔除
    available = list_reader_names()
    argv = ["trae", "bogus", "marvis"]
    names = [n for n in argv if n in available]
    assert names == ["trae", "marvis"]


def test_dropin_plugin_discovered(tmp_path):
    """外挂验证：在 io/ 放一个 reader 文件即可被自动发现，无需改中央注册。"""
    import profile.io.registry as registry

    plugin = tmp_path / "myplugin_reader.py"
    plugin.write_text(
        "from profile.io.base import BaseReader\n"
        "from profile.models import ChatRecord\n"
        "class MyPluginReader(BaseReader):\n"
        "    @property\n"
        "    def source_name(self):\n"
        "        return 'myplugin'\n"
        "    def is_available(self):\n"
        "        return False\n"
        "    def ingest(self):\n"
        "        return 0\n"
        "    def read(self):\n"
        "        return []\n",
        encoding="utf-8",
    )

    # 把临时目录作为 profile.io 的一个搜索路径不可行（包结构固定），
    # 故改为在 profile.io 包内临时写入并清理，验证自动发现。
    import profile.io as io_pkg

    target = Path(io_pkg.__path__[0]) / "_probe_reader.py"
    try:
        target.write_text(plugin.read_text(encoding="utf-8"), encoding="utf-8")
        # 重置发现缓存后重新发现
        registry._DISCOVERED = None
        assert "myplugin" in registry.list_reader_names()
        assert "myplugin" in registry.channel_reader_names()
        reader = registry.get_reader("myplugin")
        assert reader is not None
        assert reader.profile_target == "channel"
    finally:
        if target.exists():
            target.unlink()
        registry._DISCOVERED = None
