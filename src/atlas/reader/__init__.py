"""阅读视图、按需全文翻译和独立的图片磁盘缓存；不改写 Raw / Confirmed。"""

from .content import ReaderDocument, make_document
from .images import DiskImageCache
from .service import ReaderService

__all__ = ["DiskImageCache", "ReaderDocument", "ReaderService", "make_document"]
