"""阅读页组合接口；原文读取不抓网、不调用模型，翻译和图片分别按需加载。"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .images import DiskImageCache
from .translations import TranslationJobs


class ReaderService:
    def __init__(self, loader, cache_root, *, image_cache=None, client_factory=None):
        self.loader = loader
        self.images = image_cache or DiskImageCache(cache_root / "images")
        options = (
            {"client_factory": client_factory} if client_factory is not None else {}
        )
        self.translations = TranslationJobs(cache_root / "translations", **options)

    def document(self, entry):
        return self.loader(entry)

    def render(self, document):
        return self.images.render_images(document.html)

    def translation_status(self, key):
        result = self.translations.status(key)
        if result.get("html"):
            soup = BeautifulSoup(result["html"], "lxml")
            for node in soup.select("[id]"):
                node["id"] = "atlas-zh-" + node["id"]
            for node in soup.select('a[href^="#"]'):
                node["href"] = "#atlas-zh-" + node["href"][1:]
            result["html"] = self.images.render_images(
                (soup.body or soup).decode_contents()
            )
        return result

    def translate(self, entry):
        document = self.document(entry)
        if not document.html.strip():
            raise ValueError("尚无可翻译的正文")
        if document.native_chinese:
            return {"status": "ready", "html": self.render(document), "progress": 100}
        result = self.translations.start(document)
        return self.translation_status(result["key"])

    def close(self):
        self.translations.close()
