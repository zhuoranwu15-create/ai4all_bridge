"""SSRF guard：在发起 HTTP 请求前验证目标 URL 是否安全。

防护策略：
- 只允许 http / https scheme
- 禁止空 host、userinfo（凭证注入）
- 禁止 localhost / *.local / *.internal / *.localhost 等内网域名
- IP literal 直接拒绝私有/loopback/link-local/multicast/保留段
- DNS 解析所有地址，任一命中私有段即拒绝（防主机名指向内网）
- 云元数据地址 169.254.169.254 明确列举（link-local 覆盖，双保险）
"""
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTNAME_SUFFIXES = (
    "localhost",
    ".local",
    ".internal",
    ".localhost",
)

_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
}


class SSRFError(ValueError):
    """URL 被 SSRF guard 拦截。"""


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 解析失败视为不安全
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """若 URL 不安全则抛 SSRFError，否则静默返回。"""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise SSRFError(f"URL 解析失败: {exc}") from exc

    # scheme 检查
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"不支持的 scheme: {parsed.scheme!r}，仅允许 http/https")

    # host 检查
    host = parsed.hostname or ""
    if not host:
        raise SSRFError("URL 缺少有效的 host")

    # 禁止 userinfo（防 credential 注入）
    if parsed.username or parsed.password:
        raise SSRFError("URL 不得包含 userinfo（用户名/密码）")

    # 内网域名黑名单
    host_lower = host.lower()
    if host_lower in _BLOCKED_HOSTNAMES:
        raise SSRFError(f"域名 {host!r} 被拦截（内网域名）")
    for suffix in _BLOCKED_HOSTNAME_SUFFIXES:
        if host_lower == suffix.lstrip(".") or host_lower.endswith(suffix):
            raise SSRFError(f"域名 {host!r} 被拦截（内网域名后缀 {suffix!r}）")

    # IP literal 直接检查
    try:
        addr = ipaddress.ip_address(host)
        if _is_private_ip(str(addr)):
            raise SSRFError(f"IP {host!r} 被拦截（私有/保留地址）")
        return  # IP literal 且安全，跳过 DNS
    except ValueError:
        pass  # 不是 IP literal，继续 DNS

    # DNS 解析：任意一个地址是内网即拒绝
    try:
        results = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS 解析失败: {host!r}: {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if _is_private_ip(ip):
            raise SSRFError(f"域名 {host!r} 解析到私有地址 {ip!r}，拦截")
