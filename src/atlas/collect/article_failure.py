"""文章获取的稳定失败分类；历史错误可无网络回填，未知原因保留原文。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

CATEGORY_LABELS = {
    "access": "访问受限",
    "network": "网络或证书问题",
    "not_found": "链接失效",
    "content": "未取得可读正文",
    "video": "视频页无可读正文",
    "dynamic": "页面依赖动态加载",
    "format": "格式或解析问题",
    "limit": "超过读取限制",
    "address": "地址不可用",
    "unknown": "其它获取错误",
}


@dataclass(frozen=True)
class ArticleFailure:
    code: str
    category: str
    detail: str
    retryable: bool = False
    http_status: int | None = None

    @property
    def label(self):
        return CATEGORY_LABELS[self.category]


def classify_article_failure(message: str, url: str = "") -> ArticleFailure:
    status_match = re.search(r"HTTP (\d{3})", message)
    status = int(status_match.group(1)) if status_match else None
    if "disallowed_by_rules" in message:
        code, category, retry = "robots_disallowed", "access", False
    elif "disallowed_robots_forbidden" in message:
        code, category, retry = "robots_forbidden", "access", False
    elif "disallowed_robots_unavailable" in message:
        code, category, retry = "robots_unavailable", "network", True
    elif "robots" in message:
        code, category, retry = "robots_unconfirmed", "access", False
    elif status in (401, 403):
        code, category, retry = "http_forbidden", "access", False
    elif status in (404, 410):
        code, category, retry = "http_not_found", "not_found", False
    elif status == 429:
        code, category, retry = "http_rate_limited", "network", True
    elif status and status >= 500:
        code, category, retry = "http_server_error", "network", True
    elif status:
        code, category, retry = "http_error", "network", True
    elif "SSLError" in message:
        code, category, retry = "tls_error", "network", True
    elif "Timeout" in message or "超时" in message:
        code, category, retry = "timeout", "network", True
    elif "域名无法解析" in message:
        code, category, retry = "dns_error", "network", True
    elif "网络请求失败" in message:
        code, category, retry = "connection_error", "network", True
    elif "访问验证" in message:
        code, category, retry = "challenge", "access", False
    elif "登录" in message or "付费墙" in message:
        code, category, retry = "authentication_required", "access", False
    elif "不是公网" in message or "URL 无效" in message:
        code, category, retry = "invalid_address", "address", False
    elif "重定向" in message:
        code, category, retry = "redirect_error", "address", False
    elif "5 MiB" in message or "超过" in message:
        code, category, retry = "response_too_large", "limit", False
    elif "PDF" in message:
        code, category, retry = "pdf_extraction_failed", "format", False
    elif "不是支持" in message:
        code, category, retry = "unsupported_format", "format", False
    elif "动态加载" in message:
        code, category, retry = "javascript_required", "dynamic", False
    elif "目录" in message:
        code, category, retry = "listing_without_body", "content", False
    elif "正文" in message:
        host = (urlsplit(url).hostname or "").lower()
        if host in ("youtube.com", "www.youtube.com", "youtu.be"):
            code, category, retry = "video_without_text", "video", False
        elif host == "www.nntp.perl.org" and urlsplit(url).path.endswith("/10.html"):
            code, category, retry = "listing_without_body", "content", False
        else:
            code, category, retry = "no_readable_body", "content", False
    else:
        code, category, retry = "unknown", "unknown", False
    return ArticleFailure(code, category, message, retry, status)
