"""Agent 工具集。

将 web_search 和 web_fetch 注册为 LangChain Tool，
支持通过 Function Calling 机制让 LLM 自主决定调用。

面试话术：
"工具通过 LangChain 的 @tool 装饰器注册，带有完整的 JSON Schema 描述。
LLM 通过 bind_tools 绑定工具后，可以自主决定调用哪个工具、传什么参数。
工具调用失败时有重试和降级机制。"
"""

from langchain_core.tools import tool

from .web_search import web_search as _web_search_impl
from .web_fetch import web_fetch as _web_fetch_impl


@tool
async def web_search_tool(query: str, max_results: int = 3) -> list[dict]:
    """Search the web for information about a specific topic.

    Use this tool to find up-to-date information about competitors,
    products, features, pricing, and market trends.

    Args:
        query: The search query string. Should be specific and relevant
               to the competitive analysis task.
        max_results: Maximum number of results to return (1-10, default 3).

    Returns:
        A list of search results, each containing title, url, and content.
    """
    max_results = max(1, min(10, max_results))
    return await _web_search_impl(query=query, max_results=max_results)


@tool
async def web_fetch_tool(url: str, limit: int = 4000) -> str:
    """Fetch and extract text content from a web page.

    Use this tool to get detailed information from a specific URL,
    such as a competitor's official website, pricing page, or documentation.

    Args:
        url: The URL to fetch content from. Must be a valid HTTP/HTTPS URL.
        limit: Maximum characters to extract (default 4000).

    Returns:
        The extracted text content from the web page.
    """
    return await _web_fetch_impl(url=url, limit=limit)


# 工具列表，用于 bind_tools
AGENT_TOOLS = [web_search_tool, web_fetch_tool]

__all__ = ["web_search_tool", "web_fetch_tool", "AGENT_TOOLS"]
