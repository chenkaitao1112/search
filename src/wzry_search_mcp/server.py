"""
自建MCP联网搜索服务 - stdio模式
用于在阿里云百炼平台上通过uvx方式部署
支持Tavily/Serper等搜索后端，通过环境变量切换
"""
import os
import json
import logging
from urllib.parse import urlparse

import httpx
from mcp.server import Server
import mcp.server.stdio
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mcp-search")

# ==================== 配置 ====================

SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "serper")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
DEFAULT_MAX_RESULTS = int(os.getenv("MAX_RESULTS", "8"))

# 游戏问题白名单域名：只搜这些站点
# 注意：pvp.qq.com 是JS渲染页面，Tavily抓不到正文，已移除
GAME_DOMAINS = [
    "ngabbs.com",
    "nga.cn",
    "bilibili.com",
    "douyin.com",
    "kpl.qq.com",
]

# 非游戏问题优先域名（兜底模式，不强制限定，但优先排序）
GENERAL_PREFERRED_DOMAINS = [
    "xiaohongshu.com",
    "bilibili.com",
    "douyin.com",
]

# 信源优先级排序（游戏模式）
PREFERRED_DOMAINS = [
    "ngabbs.com",
    "nga.cn",
    "bilibili.com",
    "douyin.com",
    "kpl.qq.com",
]

# 黑名单域名（所有模式都降权）
LOW_QUALITY_DOMAINS = [
    "baijiahao.baidu.com",
    "zhihu.com",
    "sohu.com",
    "weibo.com",
    "toutiao.com",
    "18183.com",
    "17173.com",
    "pvp.qq.com",
]


# ==================== 工具函数 ====================

def _extract_domain(url):
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def rank_results(results, category="game"):
    domains = PREFERRED_DOMAINS if category == "game" else GENERAL_PREFERRED_DOMAINS
    domain_priority = {}
    for i, domain in enumerate(domains):
        domain_priority[domain] = len(domains) - i

    def get_priority(r):
        priority = 0
        hostname = r.get("hostname", "")
        for domain, score in domain_priority.items():
            if domain in hostname:
                priority = score
                break
        low_quality_penalty = -10 if any(d in hostname for d in LOW_QUALITY_DOMAINS) else 0
        raw_score = r.get("score", 0.0)
        return (priority + low_quality_penalty, raw_score)

    return sorted(results, key=get_priority, reverse=True)


# ==================== 搜索后端 ====================

class TavilyBackend:
    API_URL = "https://api.tavily.com/search"

    async def search(self, query, max_results=DEFAULT_MAX_RESULTS, category="game"):
        if not TAVILY_API_KEY:
            raise ValueError("TAVILY_API_KEY not configured")

        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "search_depth": "advanced",
        }

        if category == "game":
            payload["include_domains"] = GAME_DOMAINS

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for r in data.get("results", []):
            url = r.get("url", "")
            results.append({
                "title": r.get("title", ""),
                "url": url,
                "snippet": r.get("content", ""),
                "hostname": _extract_domain(url),
                "publish_date": r.get("published_date", ""),
                "score": r.get("score", 0.0),
            })
        return results


class SerperBackend:
    API_URL = "https://google.serper.dev/search"

    async def search(self, query, max_results=DEFAULT_MAX_RESULTS, category="game"):
        if not SERPER_API_KEY:
            raise ValueError("SERPER_API_KEY not configured")

        headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
        payload = {"q": query, "num": max_results}

        if category == "game":
            payload["gl"] = "cn"
            payload["hl"] = "zh-cn"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for r in data.get("organic", []):
            url = r.get("link", "")
            results.append({
                "title": r.get("title", ""),
                "url": url,
                "snippet": r.get("snippet", ""),
                "hostname": _extract_domain(url),
                "publish_date": r.get("date", ""),
                "score": float(r.get("position", 0)),
            })
        return results


def get_backend():
    if SEARCH_BACKEND == "tavily":
        return TavilyBackend()
    elif SEARCH_BACKEND == "serper":
        return SerperBackend()
    else:
        raise ValueError("unknown backend: " + SEARCH_BACKEND)


# ==================== MCP Server ====================

server = Server("web-search")

SEARCH_TOOL = Tool(
    name="web_search",
    description="联网搜索工具，输入搜索关键词，返回结构化搜索结果。适用于需要最新信息、版本更新、赛事结果等时效性内容的场景。",
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，应为完整的自然语言问句"
            },
            "count": {
                "type": "number",
                "description": "返回结果数量，默认8条",
                "default": DEFAULT_MAX_RESULTS
            },
            "category": {
                "type": "string",
                "description": "搜索类别：game=游戏相关问题(限定专业游戏信源)，general=非游戏问题(不限域名，优先小红书等生活类信源)",
                "default": "game",
                "enum": ["game", "general"]
            }
        },
        "required": ["query"]
    }
)


@server.list_tools()
async def list_tools():
    return [SEARCH_TOOL]


@server.call_tool()
async def call_tool(name, arguments):
    if name != "web_search":
        return [TextContent(type="text", text=json.dumps({"error": "unknown tool: " + name}))]

    query = arguments.get("query", "")
    count = int(arguments.get("count", DEFAULT_MAX_RESULTS))
    category = arguments.get("category", "game")

    if not query:
        return [TextContent(type="text", text=json.dumps({"error": "query is empty"}))]

    logger.info("search request: query=%s, count=%s, category=%s, backend=%s", query, count, category, SEARCH_BACKEND)

    try:
        backend = get_backend()
        results = await backend.search(query, count, category=category)
        results = rank_results(results, category=category)

        output = {
            "pages": results,
            "query": query,
            "total": len(results),
            "backend": SEARCH_BACKEND,
            "category": category,
        }

        logger.info("search done: query=%s, results=%d", query, len(results))
        return [TextContent(type="text", text=json.dumps(output, ensure_ascii=False))]

    except Exception as e:
        logger.error("search failed: %s", e, exc_info=True)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


# ==================== 入口 ====================

def main():
    import asyncio
    logger.info("MCP search service starting: backend=%s", SEARCH_BACKEND)
    logger.info("Tavily API Key: %s", "configured" if TAVILY_API_KEY else "not configured")
    logger.info("Serper API Key: %s", "configured" if SERPER_API_KEY else "not configured")
    asyncio.run(_run())


async def _run():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    main()
