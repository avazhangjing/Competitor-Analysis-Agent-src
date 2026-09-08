"""MCP (Model Context Protocol) 客户端。

实现与外部 MCP Server 的连接和工具调用。
MCP 是 2026 年 AI Agent 领域的热点技术，用于标准化 AI 模型与外部工具/数据源的连接。

面试话术：
"MCP 和 Function Calling 的区别：
- Function Calling 是 LLM 层面的能力，让模型能生成结构化的工具调用请求
- MCP 是一个协议标准，定义了 Agent 如何发现、连接、调用外部工具服务器
- MCP 支持 stdio 和 SSE 两种传输协议
- 我的项目中通过 MCP 接入了企业信息查询服务，获取竞品的融资、团队等结构化数据"

MCP 架构：
- MCP Host: Agent 应用本身
- MCP Client: 与 Server 建立 1:1 连接
- MCP Server: 提供工具、资源、prompt 的外部服务
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPToolDefinition:
    """MCP 工具定义。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPToolResult:
    """MCP 工具调用结果。"""

    tool_name: str
    success: bool
    content: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class MCPClient:
    """MCP 客户端基类。

    实际生产中，这里会通过 stdio 或 SSE 连接到外部 MCP Server。
    当前实现为模拟版本，展示 MCP 的调用流程和接口设计。
    """

    def __init__(self, server_name: str, server_url: str = ""):
        self.server_name = server_name
        self.server_url = server_url
        self._connected = False
        self._tools: list[MCPToolDefinition] = []

    async def connect(self) -> bool:
        """连接到 MCP Server。

        实际实现中，这里会：
        1. 通过 stdio 启动本地 MCP Server 进程
        2. 或通过 SSE 连接到远程 MCP Server
        3. 执行 initialize 握手
        4. 获取可用工具列表
        """
        logger.info("Connecting to MCP server: %s", self.server_name)
        # 模拟连接成功
        self._connected = True
        await self._discover_tools()
        return True

    async def disconnect(self) -> None:
        """断开与 MCP Server 的连接。"""
        self._connected = False
        self._tools = []
        logger.info("Disconnected from MCP server: %s", self.server_name)

    async def list_tools(self) -> list[MCPToolDefinition]:
        """获取 MCP Server 提供的工具列表。"""
        if not self._connected:
            await self.connect()
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """调用 MCP Server 上的工具。

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具调用结果
        """
        if not self._connected:
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error="Not connected to MCP server",
            )

        logger.info("Calling MCP tool: %s with args: %s", tool_name, arguments)

        # 模拟工具调用（实际生产中这里会通过 JSON-RPC 发送请求）
        try:
            result = await self._execute_tool(tool_name, arguments)
            return result
        except Exception as exc:
            logger.error("MCP tool call failed: %s - %s", tool_name, exc)
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=str(exc),
            )

    async def _discover_tools(self) -> None:
        """发现 MCP Server 上的可用工具。"""
        # 子类实现具体的工具发现逻辑
        pass

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """执行工具调用。子类实现具体逻辑。"""
        return MCPToolResult(
            tool_name=tool_name,
            success=False,
            error=f"Tool {tool_name} not implemented",
        )


class CompanyInfoMCPServer(MCPClient):
    """企业信息查询 MCP Server 客户端。

    模拟接入企业信息查询服务（如天眼查、企查查 API），
    获取竞品的公司基本信息、融资历史、团队规模等结构化数据。
    """

    def __init__(self):
        super().__init__(
            server_name="company-info-mcp",
            server_url="http://localhost:3001/mcp",
        )

    async def _discover_tools(self) -> None:
        """注册企业信息查询工具。"""
        self._tools = [
            MCPToolDefinition(
                name="get_company_info",
                description="获取公司基本信息，包括成立时间、注册资本、法人代表、经营范围等",
                input_schema={
                    "type": "object",
                    "properties": {
                        "company_name": {
                            "type": "string",
                            "description": "公司名称",
                        },
                    },
                    "required": ["company_name"],
                },
            ),
            MCPToolDefinition(
                name="get_funding_history",
                description="获取公司融资历史，包括融资轮次、金额、投资方等",
                input_schema={
                    "type": "object",
                    "properties": {
                        "company_name": {
                            "type": "string",
                            "description": "公司名称",
                        },
                    },
                    "required": ["company_name"],
                },
            ),
            MCPToolDefinition(
                name="get_team_size",
                description="获取公司团队规模和人员结构信息",
                input_schema={
                    "type": "object",
                    "properties": {
                        "company_name": {
                            "type": "string",
                            "description": "公司名称",
                        },
                    },
                    "required": ["company_name"],
                },
            ),
        ]

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """执行企业信息查询。

        注意：当前为模拟实现，实际生产中需要替换为真实的 API 调用。
        """
        company_name = arguments.get("company_name", "")

        if tool_name == "get_company_info":
            # 模拟返回公司基本信息
            return MCPToolResult(
                tool_name=tool_name,
                success=True,
                content=json.dumps({
                    "company_name": company_name,
                    "founded": "数据需接入真实API",
                    "registered_capital": "数据需接入真实API",
                    "legal_representative": "数据需接入真实API",
                    "business_scope": "数据需接入真实API",
                    "note": "这是MCP模拟响应，实际需接入天眼查/企查查API",
                }, ensure_ascii=False),
                metadata={"source": "mcp_company_info"},
            )

        elif tool_name == "get_funding_history":
            return MCPToolResult(
                tool_name=tool_name,
                success=True,
                content=json.dumps({
                    "company_name": company_name,
                    "funding_rounds": [],
                    "note": "这是MCP模拟响应，实际需接入真实数据源",
                }, ensure_ascii=False),
                metadata={"source": "mcp_funding"},
            )

        elif tool_name == "get_team_size":
            return MCPToolResult(
                tool_name=tool_name,
                success=True,
                content=json.dumps({
                    "company_name": company_name,
                    "team_size": "数据需接入真实API",
                    "note": "这是MCP模拟响应，实际需接入真实数据源",
                }, ensure_ascii=False),
                metadata={"source": "mcp_team"},
            )

        return MCPToolResult(
            tool_name=tool_name,
            success=False,
            error=f"Unknown tool: {tool_name}",
        )


# 全局 MCP 客户端实例
_mcp_clients: dict[str, MCPClient] = {}


async def get_mcp_client(server_type: str = "company_info") -> MCPClient | None:
    """获取指定类型的 MCP 客户端。

    Args:
        server_type: MCP Server 类型（company_info, news, weather 等）

    Returns:
        MCP 客户端实例，如果该类型未配置则返回 None
    """
    if server_type in _mcp_clients:
        return _mcp_clients[server_type]

    if server_type == "company_info":
        client = CompanyInfoMCPServer()
        await client.connect()
        _mcp_clients[server_type] = client
        return client

    return None


async def query_company_info_via_mcp(company_name: str) -> dict[str, Any]:
    """通过 MCP 查询企业信息（便捷函数）。

    Args:
        company_name: 公司名称

    Returns:
        企业信息字典
    """
    client = await get_mcp_client("company_info")
    if not client:
        return {}

    result = await client.call_tool("get_company_info", {"company_name": company_name})
    if result.success:
        try:
            return json.loads(result.content)
        except json.JSONDecodeError:
            return {"raw": result.content}
    return {"error": result.error}
