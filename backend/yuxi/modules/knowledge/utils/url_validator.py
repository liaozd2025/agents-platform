"""基于域名白名单的 URL 解析校验。"""

import os
from urllib.parse import urlparse

YUXI_URL_WHITELIST_ENV = "YUXI_URL_WHITELIST"


def validate_url(url: str) -> tuple[bool, str]:
    """校验 URL 是否命中白名单，返回 (是否有效, 失败原因)。"""
    if not url:
        return False, "URL cannot be empty"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL format: {e}"

    if not parsed.scheme:
        return False, "URL must start with http:// or https://"
    if parsed.scheme not in ("http", "https"):
        return False, "URL must use HTTP or HTTPS protocol"

    hostname = parsed.hostname
    if not hostname:
        return False, "Invalid URL: no hostname found"

    whitelist = _get_whitelist()
    # 白名单未配置时整体禁用 URL 解析。
    if not whitelist:
        return False, "URL parsing feature is disabled"

    for allowed in whitelist:
        # 通配符 *.example.com 允许域本身及其子域；精确项同样放行子域。
        domain = allowed[2:] if allowed.startswith("*.") else allowed
        if hostname == domain or hostname.endswith(f".{domain}"):
            return True, ""

    return False, f"Domain '{hostname}' is not in the whitelist"


def is_url_parsing_enabled() -> bool:
    """白名单已配置时 URL 解析能力可用。"""
    return len(_get_whitelist()) > 0


def _get_whitelist() -> list[str]:
    """读取逗号分隔的域名白名单环境变量。"""
    whitelist_str = os.environ.get(YUXI_URL_WHITELIST_ENV, "")
    if not whitelist_str:
        return []
    return [item.strip() for item in whitelist_str.split(",") if item.strip()]
