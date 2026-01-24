"""
增强内容解析器

支持多格式文档解析、OCR文本提取和智能内容分块。
遵循简单优先、本地优先的原则。

支持的格式：
- 文本文件: TXT, MD
- Office文档: PDF, DOCX, PPTX
- 图像: PNG, JPG, JPEG (OCR)
- 网页: HTML (现有parser.py)
"""

import logging
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
import re

logger = logging.getLogger(__name__)


@dataclass
class ParsedContent:
    """解析后的内容"""
    title: Optional[str] = None
    content: str = ""
    content_type: str = "text/plain"  # MIME type
    metadata: Dict[str, Any] = field(default_factory=dict)
    chunks: List[str] = field(default_factory=list)
    extraction_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "title": self.title,
            "content": self.content,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "chunk_count": len(self.chunks),
            "extraction_info": self.extraction_info
        }


@dataclass
class ChunkConfig:
    """内容分块配置"""
    max_chunk_size: int = 1000  # 最大块大小（字符数）
    chunk_overlap: int = 200     # 块重叠大小
    separator: str = "\n\n"      # 分隔符
    respect_sentence: bool = True  # 是否尊重句子边界
    min_chunk_size: int = 100    # 最小块大小


class ContentParser:
    """内容解析器基类"""

    def __init__(self):
        self.supported_formats = []

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """
        解析文件内容

        Args:
            file_path: 文件路径

        Returns:
            ParsedContent: 解析后的内容
        """
        raise NotImplementedError("子类必须实现parse方法")

    def is_supported(self, file_path: Union[str, Path]) -> bool:
        """
        检查文件格式是否支持

        Args:
            file_path: 文件路径

        Returns:
            bool: 是否支持
        """
        file_path = Path(file_path)
        return file_path.suffix.lower() in self.supported_formats


class TextParser(ContentParser):
    """纯文本解析器"""

    def __init__(self):
        super().__init__()
        self.supported_formats = ['.txt', '.md', '.log', '.csv']

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """解析纯文本文件"""
        file_path = Path(file_path)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 使用文件名作为标题（去掉扩展名）
            title = file_path.stem

            return ParsedContent(
                title=title,
                content=content,
                content_type="text/plain",
                extraction_info={
                    "parser": "TextParser",
                    "file_size": file_path.stat().st_size,
                    "encoding": "utf-8"
                }
            )

        except UnicodeDecodeError:
            # 尝试其他编码
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    content = f.read()

                return ParsedContent(
                    title=file_path.stem,
                    content=content,
                    content_type="text/plain",
                    extraction_info={
                        "parser": "TextParser",
                        "file_size": file_path.stat().st_size,
                        "encoding": "gbk"
                    }
                )
            except Exception as e:
                logger.error(f"无法解析文件 {file_path}: {e}")
                raise

        except Exception as e:
            logger.error(f"解析文本文件失败 {file_path}: {e}")
            raise


class PDFParser(ContentParser):
    """PDF解析器"""

    def __init__(self):
        super().__init__()
        self.supported_formats = ['.pdf']

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """解析PDF文件"""
        file_path = Path(file_path)

        try:
            import PyPDF2
        except ImportError:
            logger.warning("PyPDF2未安装，尝试使用pdfplumber")
            try:
                import pdfplumber
                return self._parse_with_pdfplumber(file_path)
            except ImportError:
                raise ImportError(
                    "PDF解析需要安装PyPDF2或pdfplumber: "
                    "uv pip install pypdf2 或 uv pip install pdfplumber"
                )

        return self._parse_with_pypdf2(file_path)

    def _parse_with_pypdf2(self, file_path: Path) -> ParsedContent:
        """使用PyPDF2解析PDF"""
        import PyPDF2

        try:
            with open(file_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)

                # 提取文本
                text_parts = []
                for page in reader.pages:
                    try:
                        text = page.extract_text()
                        if text.strip():
                            text_parts.append(text)
                    except Exception as e:
                        logger.warning(f"提取PDF页面失败: {e}")
                        continue

                content = "\n\n".join(text_parts)

                # 提取元数据
                metadata = {}
                if reader.metadata:
                    metadata = {
                        k: str(v) for k, v in reader.metadata.items()
                        if v is not None
                    }

                return ParsedContent(
                    title=metadata.get('/Title', file_path.stem),
                    content=content,
                    content_type="application/pdf",
                    metadata=metadata,
                    extraction_info={
                        "parser": "PyPDF2",
                        "page_count": len(reader.pages),
                        "file_size": file_path.stat().st_size
                    }
                )

        except Exception as e:
            logger.error(f"解析PDF文件失败 {file_path}: {e}")
            raise

    def _parse_with_pdfplumber(self, file_path: Path) -> ParsedContent:
        """使用pdfplumber解析PDF"""
        import pdfplumber

        try:
            with pdfplumber.open(file_path) as pdf:
                text_parts = []
                for page in pdf.pages:
                    try:
                        text = page.extract_text()
                        if text and text.strip():
                            text_parts.append(text)
                    except Exception as e:
                        logger.warning(f"提取PDF页面失败: {e}")
                        continue

                content = "\n\n".join(text_parts)

                return ParsedContent(
                    title=file_path.stem,
                    content=content,
                    content_type="application/pdf",
                    metadata={"pages": len(pdf.pages)},
                    extraction_info={
                        "parser": "pdfplumber",
                        "page_count": len(pdf.pages),
                        "file_size": file_path.stat().st_size
                    }
                )

        except Exception as e:
            logger.error(f"解析PDF文件失败 {file_path}: {e}")
            raise


class DOCXParser(ContentParser):
    """DOCX解析器"""

    def __init__(self):
        super().__init__()
        self.supported_formats = ['.docx']

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """解析DOCX文件"""
        file_path = Path(file_path)

        try:
            from docx import Document
        except ImportError:
            raise ImportError(
                "DOCX解析需要安装python-docx: "
                "uv pip install python-docx"
            )

        try:
            doc = Document(file_path)

            # 提取段落文本
            paragraphs = []
            for para in doc.paragraphs:
                if para.text.strip():
                    paragraphs.append(para.text)

            content = "\n\n".join(paragraphs)

            # 提取表格
            tables = []
            for table in doc.tables:
                table_data = []
                for row in table.rows:
                    row_data = [cell.text for cell in row.cells]
                    table_data.append(row_data)
                tables.append(table_data)

            # 提取元数据
            metadata = {
                "paragraph_count": len(doc.paragraphs),
                "table_count": len(tables)
            }

            # 尝试获取标题
            title = file_path.stem
            if doc.core_properties.title:
                title = doc.core_properties.title
            elif paragraphs:
                # 第一段通常作为标题
                title = paragraphs[0][:100]

            return ParsedContent(
                title=title,
                content=content,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                metadata=metadata,
                extraction_info={
                    "parser": "python-docx",
                    "tables": tables,
                    "file_size": file_path.stat().st_size
                }
            )

        except Exception as e:
            logger.error(f"解析DOCX文件失败 {file_path}: {e}")
            raise


class PPTXParser(ContentParser):
    """PPTX解析器"""

    def __init__(self):
        super().__init__()
        self.supported_formats = ['.pptx']

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """解析PPTX文件"""
        file_path = Path(file_path)

        try:
            from pptx import Presentation
        except ImportError:
            raise ImportError(
                "PPTX解析需要安装python-pptx: "
                "uv pip install python-pptx"
            )

        try:
            prs = Presentation(file_path)

            slides_text = []
            for slide_num, slide in enumerate(prs.slides):
                slide_texts = []

                # 提取文本框
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_texts.append(shape.text)

                if slide_texts:
                    slides_text.append(f"[Slide {slide_num + 1}]\n" + "\n".join(slide_texts))

            content = "\n\n".join(slides_text)

            return ParsedContent(
                title=file_path.stem,
                content=content,
                content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                metadata={
                    "slide_count": len(prs.slides)
                },
                extraction_info={
                    "parser": "python-pptx",
                    "file_size": file_path.stat().st_size
                }
            )

        except Exception as e:
            logger.error(f"解析PPTX文件失败 {file_path}: {e}")
            raise


class OCRParser(ContentParser):
    """OCR图像解析器"""

    def __init__(self):
        super().__init__()
        self.supported_formats = ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff']

    def parse(self, file_path: Union[str, Path]) -> ParsedContent:
        """解析图像并提取文本（OCR）"""
        file_path = Path(file_path)

        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            raise ImportError(
                "OCR解析需要安装pytesseract和Pillow: "
                "uv pip install pytesseract pillow"
            )

        try:
            # 打开图像
            image = Image.open(file_path)

            # 执行OCR
            text = pytesseract.image_to_string(image, lang='chi_sim+eng')

            return ParsedContent(
                title=file_path.stem,
                content=text.strip(),
                content_type=f"image/{file_path.suffix[1:]}",
                metadata={
                    "image_format": image.format,
                    "image_size": image.size,
                    "image_mode": image.mode
                },
                extraction_info={
                    "parser": "Tesseract OCR",
                    "ocr_confidence": "N/A",
                    "file_size": file_path.stat().st_size
                }
            )

        except Exception as e:
            logger.error(f"OCR解析失败 {file_path}: {e}")
            raise


class ContentChunker:
    """内容分块器"""

    def __init__(self, config: Optional[ChunkConfig] = None):
        """
        初始化分块器

        Args:
            config: 分块配置
        """
        self.config = config or ChunkConfig()

    def chunk(self, content: str) -> List[str]:
        """
        将内容分块

        Args:
            content: 原始内容

        Returns:
            List[str]: 内容块列表
        """
        if not content or not content.strip():
            return []

        # 按分隔符分割
        splits = content.split(self.config.separator)

        chunks = []
        current_chunk = ""

        for split in splits:
            split = split.strip()
            if not split:
                continue

            # 如果添加这个split会超过最大大小，且当前块不为空
            if len(current_chunk) + len(split) > self.config.max_chunk_size:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = split
                else:
                    # 单个split太长，需要强制分割
                    if self.config.respect_sentence:
                        # 尝试按句子分割
                        sentences = self._split_by_sentences(split)
                        for sentence in sentences:
                            if len(current_chunk) + len(sentence) > self.config.max_chunk_size:
                                if current_chunk:
                                    chunks.append(current_chunk.strip())
                                current_chunk = sentence
                            else:
                                current_chunk += " " + sentence
                    else:
                        # 强制按字符分割
                        for i in range(0, len(split), self.config.max_chunk_size):
                            chunk = split[i:i + self.config.max_chunk_size]
                            if len(chunk) >= self.config.min_chunk_size:
                                chunks.append(chunk)
            else:
                if current_chunk:
                    current_chunk += self.config.separator + split
                else:
                    current_chunk = split

        # 添加最后一块
        if current_chunk and len(current_chunk.strip()) >= self.config.min_chunk_size:
            chunks.append(current_chunk.strip())

        return chunks

    def _split_by_sentences(self, text: str) -> List[str]:
        """
        按句子分割文本

        Args:
            text: 文本内容

        Returns:
            List[str]: 句子列表
        """
        # 中文和英文句子分隔符
        sentence_endings = r'([。！？.!?]+)\s*'
        sentences = re.split(sentence_endings, text)

        # 重组句子（保留分隔符）
        result = []
        for i in range(0, len(sentences) - 1, 2):
            sentence = sentences[i] + (sentences[i + 1] if i + 1 < len(sentences) else "")
            if sentence.strip():
                result.append(sentence.strip())

        # 处理最后一个元素
        if len(sentences) % 2 == 1 and sentences[-1].strip():
            result.append(sentences[-1].strip())

        return result


class EnhancedParser:
    """增强内容解析器（统一入口）"""

    def __init__(self, chunk_config: Optional[ChunkConfig] = None):
        """
        初始化解析器

        Args:
            chunk_config: 内容分块配置
        """
        self.parsers = [
            TextParser(),
            PDFParser(),
            DOCXParser(),
            PPTXParser(),
            OCRParser()
        ]
        self.chunker = ContentChunker(chunk_config)

    def parse(
        self,
        file_path: Union[str, Path],
        enable_chunking: bool = False
    ) -> ParsedContent:
        """
        自动检测格式并解析文件

        Args:
            file_path: 文件路径
            enable_chunking: 是否启用内容分块

        Returns:
            ParsedContent: 解析后的内容
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 查找合适的解析器
        parser = None
        for p in self.parsers:
            if p.is_supported(file_path):
                parser = p
                break

        if not parser:
            raise ValueError(
                f"不支持的文件格式: {file_path.suffix}. "
                f"支持的格式: {[fmt for p in self.parsers for fmt in p.supported_formats]}"
            )

        # 解析文件
        parsed_content = parser.parse(file_path)

        # 内容分块（如果启用）
        if enable_chunking and parsed_content.content:
            parsed_content.chunks = self.chunker.chunk(parsed_content.content)

        return parsed_content

    def parse_text(
        self,
        text: str,
        content_type: str = "text/plain",
        title: Optional[str] = None,
        enable_chunking: bool = False
    ) -> ParsedContent:
        """
        直接解析文本内容

        Args:
            text: 文本内容
            content_type: 内容类型
            title: 标题
            enable_chunking: 是否启用内容分块

        Returns:
            ParsedContent: 解析后的内容
        """
        parsed_content = ParsedContent(
            title=title,
            content=text,
            content_type=content_type,
            extraction_info={
                "parser": "EnhancedParser",
                "source": "direct_text"
            }
        )

        # 内容分块（如果启用）
        if enable_chunking and text:
            parsed_content.chunks = self.chunker.chunk(text)

        return parsed_content


# =============================================================================
# 便捷函数
# =============================================================================

def parse_file(
    file_path: Union[str, Path],
    enable_chunking: bool = False
) -> ParsedContent:
    """
    解析文件（便捷函数）

    Args:
        file_path: 文件路径
        enable_chunking: 是否启用内容分块

    Returns:
        ParsedContent: 解析后的内容
    """
    parser = EnhancedParser()
    return parser.parse(file_path, enable_chunking=enable_chunking)


def parse_text(
    text: str,
    enable_chunking: bool = False
) -> ParsedContent:
    """
    解析文本（便捷函数）

    Args:
        text: 文本内容
        enable_chunking: 是否启用内容分块

    Returns:
        ParsedContent: 解析后的内容
    """
    parser = EnhancedParser()
    return parser.parse_text(text, enable_chunking=enable_chunking)


def chunk_content(
    content: str,
    max_chunk_size: int = 1000,
    chunk_overlap: int = 200
) -> List[str]:
    """
    内容分块（便捷函数）

    Args:
        content: 内容
        max_chunk_size: 最大块大小
        chunk_overlap: 块重叠大小

    Returns:
        List[str]: 内容块列表
    """
    config = ChunkConfig(
        max_chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap
    )
    chunker = ContentChunker(config)
    return chunker.chunk(content)
