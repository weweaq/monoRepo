"""新闻热榜 MCP 桥接工具：接 aigroup-news-mcp（stdio npx 子进程）进主 graph。

被接 MCP：aigroup-news-mcp@1.x（ModelScope 收录，MIT，免 key；npx -y aigroup-news-mcp）。
上游数据为 NewsNow 风格热榜：知乎/微博/GitHub/百度/B站（实测刷新间隔 2~10 分钟，LRU 缓存在 server 侧）。

为什么桥接而不是直接用 langchain-mcp-adapters：
- MCP 是异步 JSON-RPC + stdio 子进程协议；npx 冷启（首次下载包）可达 60s+，热启往返实测 ~4s——
  每 run 重新 spawn 不可接受，必须**常驻会话**摊薄启动成本。
- gacore 既有异步执行路径（QQ 前端 graph.astream）也有同步路径（日报 run_graph 的 graph.invoke）；
  LangChain 对同步工具在异步上下文会走线程执行器，因此三个工具暴露为同步，
  内部用"后台线程独占事件循环 + 持久 ClientSession"桥接，两条路径复用同一会话。

降级约定：node/npx 缺失、包拉取失败、会话崩溃 → 工具返回可读错误字符串（模型可据此向用户
说明"新闻服务暂不可用"），绝不 raise 中断对话；会话崩溃后下次调用自动重启一次。
进程生命周期：npx 子进程随本进程常驻（~50MB），进程退出时由 stdio_client 上下文收尾。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from typing import Any, Final

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

logger = get_logger("tools.news_mcp")

_MCP_PACKAGE: Final = "aigroup-news-mcp"
_CALL_TIMEOUT_S: Final = 60.0
_STARTUP_TIMEOUT_S: Final = 90.0  # 首次 npx 需下载包，给足冷启预算

_UNAVAILABLE_PREFIX = "新闻工具暂不可用："


def _format_content(content: Any) -> str:
    """把 MCP CallToolResult.content 拼成单段文本（text 块逐个连接）。"""
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else "（MCP 返回空内容）"


class _McpBridge:
    """后台线程独占事件循环 + 持久 MCP ClientSession；同步/异步两侧共享。

    崩溃语义：_acall 里任何异常都会把会话置回 None 并记录原因，下次调用重开会话
    （stdio_client 上下文异常退出时子进程由上下文管理器回收）。启动失败不重试到
    成功为止——每次调用最多试一次，避免雪崩式重启。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any | None = None
        self._ready = threading.Event()
        self._start_error: str | None = None

    # ---- 会话生命周期（运行在后台线程的事件循环里） ----

    def _ensure_loop(self) -> bool:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return True
            self._ready = threading.Event()
            self._start_error = None
            self._session = None
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop, args=(loop,), name="news-mcp-bridge", daemon=True
            )
            self._loop = loop
            thread.start()
            return True

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._hold_session())
        except Exception as exc:  # noqa: BLE001 - 桥接崩溃降级为下次重启，不上抛
            with self._lock:
                self._start_error = f"{type(exc).__name__}: {exc}"
                self._session = None
            logger.warning("news mcp session crashed", error=str(exc))
        finally:
            with self._lock:
                self._loop = None
            self._ready.set()

    async def _hold_session(self) -> None:
        import anyio
        from contextlib import suppress

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.shared.message import SessionMessage

        cmd = shutil.which("npx") or shutil.which("npx.cmd")
        if not cmd:
            raise RuntimeError("未找到 npx（需要 Node.js >= 18）")
        params = StdioServerParameters(command=cmd, args=["-y", _MCP_PACKAGE])
        async with stdio_client(params) as (raw_read, write):
            # 该 server 会往 stdout 混入人读日志（如"成功加载新闻源配置"），mcp SDK 把
            # 解析失败的行作为 Exception 项送入流，ClientSession 收到即挂。此处插一层
            # 过滤流：只放行合法 SessionMessage，脏行记日志丢弃。
            # 注意 create_memory_object_stream 返回 (send, receive)，Session 的读端要 receive。
            clean_send, clean_receive = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](0)

            async def _drop_pollution() -> None:
                with suppress(anyio.EndOfStream, anyio.ClosedResourceError):
                    async for item in raw_read:
                        if isinstance(item, Exception):
                            logger.debug("news mcp stdout 混入非 JSON 行，已过滤")
                            continue
                        await clean_send.send(item)

            async with anyio.create_task_group() as tg:
                tg.start_soon(_drop_pollution)
                async with ClientSession(clean_receive, write) as session:
                    await session.initialize()
                    with self._lock:
                        self._session = session
                        self._start_error = None
                    self._ready.set()
                    # 常驻直到事件循环被取消（进程退出/崩溃重启）
                    await asyncio.Event().wait()

    async def _acall(self, name: str, args: dict[str, Any]) -> str:
        # 前置条件：调用方已先同步执行 _ensure_loop()（事件循环必须先存在才能调度本协程）。
        # 注意本协程运行在桥接循环上——等待 _ready 绝不能阻塞本循环（_ready 正是靠它 set 的），
        # 必须经 to_thread 让出循环。
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(self._ready.wait, _STARTUP_TIMEOUT_S),
                timeout=_STARTUP_TIMEOUT_S + 5,
            )
        except TimeoutError:
            ok = False
        if not ok:
            return f"{_UNAVAILABLE_PREFIX}MCP 服务启动超时（{_STARTUP_TIMEOUT_S}s，首次可能仍在下载包）"
        with self._lock:
            session, start_error = self._session, self._start_error
        if session is None:
            return f"{_UNAVAILABLE_PREFIX}{start_error or '会话未建立'}"
        try:
            result = await asyncio.wait_for(session.call_tool(name, args), timeout=_CALL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - 调用失败降级为错误文本
            with self._lock:
                self._session = None
            self._ready.clear()
            return f"{_UNAVAILABLE_PREFIX}{type(exc).__name__}: {exc}"
        return _format_content(result.content)

    # ---- 对外两个入口：同步工具走 call_tool，将来异步侧可直接 await acall_tool ----

    def call_tool(self, name: str, args: dict[str, Any], timeout: float | None = None) -> str:
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._acall(name, args), self._loop)
        try:
            return fut.result(timeout=(timeout or _CALL_TIMEOUT_S) + _STARTUP_TIMEOUT_S)
        except TimeoutError:
            fut.cancel()
            return f"{_UNAVAILABLE_PREFIX}调用超时"

    async def acall_tool(self, name: str, args: dict[str, Any]) -> str:
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._acall(name, args), self._loop)
        return await asyncio.wrap_future(fut)


_bridge = _McpBridge()


def _call_news(name: str, args: dict[str, Any]) -> str:
    """工具统一出口：桥接崩溃/超时都收成字符串，不让异常打断对话。"""
    try:
        return _bridge.call_tool(name, args)
    except Exception as exc:  # noqa: BLE001 - 最后防线
        logger.warning("news tool failed", tool=name, error=str(exc))
        return f"{_UNAVAILABLE_PREFIX}{type(exc).__name__}: {exc}"


@tool
def news_hot_list(source_id: str, count: int = 10) -> str:
    """获取指定新闻源的热榜/最新列表。

    参数 source_id 为新闻源 ID，常用：zhihu（知乎热榜）、weibo（微博热搜）、
    github（GitHub Trending）、baidu（百度热搜）、bilibili（B站热搜）；
    完整列表用 news_list_sources 查询。count 为条数（默认 10，最大 50）。

    当用户想知道"现在大家都在关注什么/有什么热点/某平台在热什么"，
    或日报写作需要当日热点素材时调用。
    """
    return _call_news("get_hotest_latest_news", {"id": source_id, "count": int(count)})


@tool
def news_search(keyword: str, source: str = "", count: int = 10) -> str:
    """按关键词跨新闻源搜索相关新闻。

    keyword 为搜索词；source 可选（不传则搜全部源，也可指定如 zhihu/weibo/github）；
    count 为条数（默认 10，最大 50）。返回"来源+标题+链接"列表。

    当用户想跟进某个话题/事件的相关讨论，或在热榜里找特定主题时调用。
    """
    args: dict[str, Any] = {"keyword": keyword, "count": int(count)}
    if source:
        args["source"] = source
    return _call_news("search_news", args)


@tool
def news_list_sources() -> str:
    """列出 aigroup 新闻服务当前可用的全部新闻源（ID/名称/类型/刷新间隔）。

    在不确定 source_id 取值、或用户问"能看哪些平台的榜"时先调本工具。
    """
    return _call_news("list_news_sources", {})
