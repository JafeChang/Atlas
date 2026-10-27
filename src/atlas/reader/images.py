"""仅存缓存目录的 Disk LRU。无数据库归档、图片 BLOB 或长期图片副本。"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from lxml import etree

from atlas.collect.article import public_url

MAX_IMAGE_CACHE_BYTES = 500_000_000
MAX_IMAGE_BYTES = 20_000_000


class ImageUnavailable(ValueError):
    pass


def image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    if data.lstrip().startswith(b"<svg"):
        return "image/svg+xml"
    raise ImageUnavailable("图片格式不支持或响应不是图片")


def fetch_image(url: str) -> bytes:
    for _ in range(5):
        public_url(url)
        with requests.get(
            url,
            headers={
                "User-Agent": "Atlas/0.1 (image cache)",
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif",
            },
            timeout=(5, 12),
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ImageUnavailable("图片跳转缺少目标")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise ImageUnavailable(f"图片 HTTP {response.status_code}")
            if int(response.headers.get("Content-Length", "0")) > MAX_IMAGE_BYTES:
                raise ImageUnavailable("图片超过 20MB")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ImageUnavailable("图片超过 20MB")
            return bytes(data)
    raise ImageUnavailable("图片跳转次数过多")


class DiskImageCache:
    """mtime 记录最近使用时间，重启后仍按 LRU 淘汰；写入与淘汰跨线程/进程互斥。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = MAX_IMAGE_CACHE_BYTES,
        downloader=fetch_image,
    ):
        if not 0 < max_bytes <= MAX_IMAGE_CACHE_BYTES:
            raise ValueError("图片缓存容量必须在 1 到 500MB 之间")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.downloader = downloader
        self._secret = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._downloads = threading.BoundedSemaphore(4)
        self._key_locks = [threading.Lock() for _ in range(64)]

    def url_for(self, url: str) -> str:
        signature = hmac.new(self._secret, url.encode(), hashlib.sha256).hexdigest()
        return "/image?" + urlencode({"url": url, "signature": signature})

    def get_signed(self, query: str) -> tuple[bytes, str]:
        params = parse_qs(query, keep_blank_values=True)
        if set(params) != {"url", "signature"} or any(
            len(v) != 1 for v in params.values()
        ):
            raise ImageUnavailable("图片地址参数非法")
        url, signature = params["url"][0], params["signature"][0]
        expected = hmac.new(self._secret, url.encode(), hashlib.sha256).hexdigest()
        if len(url) > 8192 or not hmac.compare_digest(signature, expected):
            raise ImageUnavailable("图片地址签名无效")
        return self.get(url)

    def render_images(self, document: str) -> str:
        soup = BeautifulSoup(document, "lxml")
        for node in soup.select("img[src]"):
            node["src"] = self.url_for(str(node["src"]))
        return (soup.body or soup).decode_contents()

    @contextmanager
    def _locked(self):
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".lock").open("a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    for partial in self.root.glob("*.part-*"):
                        partial.unlink(missing_ok=True)
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _files(self):
        return [p for p in self.root.glob("*.img") if p.is_file()]

    def _evict(self, incoming=0):
        files = sorted(self._files(), key=lambda p: (p.stat().st_mtime_ns, p.name))
        used = sum(p.stat().st_size for p in files)
        while files and used + incoming > self.max_bytes:
            oldest = files.pop(0)
            used -= oldest.stat().st_size
            oldest.unlink()

    def get(self, url: str) -> tuple[bytes, str]:
        key = hashlib.sha256(url.encode()).hexdigest()
        with self._key_locks[int(key[:8], 16) % len(self._key_locks)]:
            return self._get(url, key)

    def _get(self, url: str, key: str) -> tuple[bytes, str]:
        target = self.root / (key + ".img")
        with self._locked():
            self._evict()
            if target.is_file():
                data = target.read_bytes()
                mime = image_type(data)
                target.touch()
                return data, mime
        # 下载期间不持有磁盘锁；较慢的来源不会阻塞其他图片的缓存命中。
        with self._downloads:
            data = self.downloader(url)
        if b"<svg" in data[:512].lower():
            data = sanitize_svg(data)
        mime = image_type(data)
        if len(data) > min(MAX_IMAGE_BYTES, self.max_bytes):
            raise ImageUnavailable("图片超过缓存允许的单图容量")
        with self._locked():
            self._evict(len(data))
            temporary = self.root / (key + ".part-" + secrets.token_hex(6))
            try:
                temporary.write_bytes(data)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return data, mime


def sanitize_svg(data: bytes) -> bytes:
    if (
        len(data) > 2_000_000
        or b"<!doctype" in data.lower()
        or b"<!entity" in data.lower()
    ):
        raise ImageUnavailable("SVG 图片不受支持")
    try:
        root = etree.fromstring(
            data, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError as exc:
        raise ImageUnavailable("SVG 图片无效") from exc
    if etree.QName(root).localname != "svg":
        raise ImageUnavailable("响应不是 SVG 图片")
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        if etree.QName(node).localname.lower() in (
            "script",
            "foreignobject",
            "style",
            "animate",
            "animatetransform",
            "animatemotion",
            "set",
        ):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        for name, value in list(node.attrib.items()):
            local = etree.QName(name).localname.lower()
            if (
                local.startswith("on")
                or local == "style"
                or "url(" in value.lower()
                or (local in ("href", "base") and not value.startswith("#"))
            ):
                del node.attrib[name]
    return etree.tostring(root, encoding="utf-8")
