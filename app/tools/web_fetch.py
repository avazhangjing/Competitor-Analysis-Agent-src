import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)

_RETRY_DELAY = 1
_FETCH_CONCURRENCY = 6
_FETCH_TIMEOUT = 6

# 内容类型防护：只解析文本类内容，PDF/图片/二进制直接跳过
_TEXT_CONTENT_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/markdown",
)
# 超过该字节数的响应直接放弃（防超大 PDF/视频等拖垮抓取）
_MAX_CONTENT_LENGTH = 2 * 1024 * 1024
_MAX_READ_BYTES = 512 * 1024

_fetch_semaphore: asyncio.Semaphore | None = None
_http_client: httpx.AsyncClient | None = None


def _get_fetch_semaphore() -> asyncio.Semaphore:
    global _fetch_semaphore
    if _fetch_semaphore is None:
        _fetch_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    return _fetch_semaphore


def _get_http_client() -> httpx.AsyncClient:
    """复用 httpx 客户端，避免每次 fetch 都新建连接。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        # 关闭自动重定向：重定向目标必须逐跳重新校验，否则可被 302 指向内网/云元数据（SSRF）
        _http_client = httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": "competitive-analysis-agent/0.1"},
        )
    return _http_client

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",
    "metadata.aws.amazon.com",
    "metadata.azure.com",
}

# 社交媒体/视频站等始终无法抓取的域名，直接跳过避免超时浪费
_UNREACHABLE_DOMAINS = {
    "linkedin.com", "www.linkedin.com",
    "facebook.com", "www.facebook.com",
    "twitter.com", "x.com",
    "instagram.com", "www.instagram.com",
    "threads.com", "www.threads.com",
    "youtube.com", "www.youtube.com",
    "tiktok.com", "www.tiktok.com",
    "reddit.com", "www.reddit.com",
    "quora.com", "www.quora.com",
    "pinterest.com", "www.pinterest.com",
}

_BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_url_safe(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed"

    hostname = parsed.hostname
    if not hostname:
        return False, "no hostname"

    if hostname.lower() in _BLOCKED_HOSTS:
        return False, f"blocked host '{hostname}'"

    # 检查是否为已知不可达域名（含子域名匹配）
    host_lower = hostname.lower()
    for domain in _UNREACHABLE_DOMAINS:
        if host_lower == domain or host_lower.endswith("." + domain):
            return False, f"unreachable domain '{domain}'"

    return True, "ok"


async def _check_ip_safe(hostname: str) -> tuple[bool, str]:
    """异步 DNS 解析，检查是否解析到内网 IP。"""
    loop = asyncio.get_event_loop()
    try:
        addr_infos = await loop.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError):
        return True, "hostname unresolvable, will fail naturally"

    for addr_info in addr_infos:
        ip = addr_info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        for network in _BLOCKED_IP_RANGES:
            if ip_obj in network:
                return False, f"host '{hostname}' resolves to private IP {ip}"

    return True, "ok"


async def _stream_fetch(client: httpx.AsyncClient, url: str) -> str | None:
    """流式抓取：先检查响应头（内容类型/大小），再限量读取正文。

    返回正文文本；非文本内容、超限响应返回 None（由调用方跳过）。
    """
    async with client.stream("GET", url) as response:
        response.raise_for_status()

        # 内容类型防护：非文本内容直接放弃，避免把 PDF/二进制当正文解析
        content_type = (response.headers.get("content-type") or "").lower()
        if not content_type.startswith(_TEXT_CONTENT_PREFIXES):
            logger.warning("Web fetch skip non-text content-type '%s' for %s", content_type, url)
            return None
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_CONTENT_LENGTH:
            logger.warning("Web fetch skip oversized response (%s bytes) for %s", content_length, url)
            return None

        # 限量读取，避免超大文件拖垮抓取
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_READ_BYTES:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)

    try:
        text = raw.decode(response.encoding or "utf-8", errors="ignore")
    except (LookupError, AttributeError):
        text = raw.decode("utf-8", errors="ignore")
    return text


async def web_fetch(url: str, limit: int = 4000) -> str:
    safe, reason = _is_url_safe(url)
    if not safe:
        logger.warning("Web fetch blocked for %s: %s", url, reason)
        return ""

    # 异步 DNS 解析检查内网 IP，避免阻塞事件循环
    hostname = urlparse(url).hostname or ""
    ip_safe, ip_reason = await _check_ip_safe(hostname)
    if not ip_safe:
        logger.warning("Web fetch blocked for %s: %s", url, ip_reason)
        return ""

    client = _get_http_client()
    async with _get_fetch_semaphore():
        current_url = url
        redirects = 0
        for attempt in range(2):
            try:
                text = await _stream_fetch(client, current_url)
                if text is None:
                    return ""
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (301, 302, 303, 307, 308) and redirects < 5:
                    location = exc.response.headers.get("location", "")
                    if not location:
                        return ""
                    next_url = str(httpx.URL(current_url).join(location))
                    safe2, reason2 = _is_url_safe(next_url)
                    if not safe2:
                        logger.warning("Web fetch redirect blocked for %s: %s", next_url, reason2)
                        return ""
                    hostname2 = urlparse(next_url).hostname or ""
                    ip_safe2, ip_reason2 = await _check_ip_safe(hostname2)
                    if not ip_safe2:
                        logger.warning("Web fetch redirect blocked for %s: %s", next_url, ip_reason2)
                        return ""
                    redirects += 1
                    current_url = next_url
                    continue
                logger.warning("Web fetch HTTP error for %s: %s", current_url, exc.response.status_code)
                return ""
            except httpx.TimeoutException:
                if attempt == 0:
                    logger.warning("Web fetch timeout for %s (attempt 1/2), retrying", url)
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
                logger.warning("Web fetch timeout for %s after 2 attempts", url)
                return ""
            except Exception as exc:
                if attempt == 0:
                    logger.warning("Web fetch failed for %s (attempt 1/2), retrying: %s", url, exc)
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
                logger.error("Web fetch failed for %s after 2 attempts: %s", url, exc)
                return ""
        else:
            return ""

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    logger.info("Web fetch '%s' extracted %d chars", url, len(text))
    return text[:limit]
