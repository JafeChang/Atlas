"""
HTML内容解析器

提供高效的HTML内容提取、结构化数据解析和元数据提取功能。
"""

import re
import html
import logging
from typing import Dict, List, Optional, Any, Tuple, Union
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass
from pathlib import Path

try:
    from bs4 import BeautifulSoup, Tag, NavigableString
    from bs4.element import Comment
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    BeautifulSoup = None

from atlas.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ExtractedContent:
    """提取的内容数据结构"""
    title: Optional[str] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    author: Optional[str] = None
    publish_date: Optional[str] = None
    tags: List[str] = None
    images: List[Dict[str, str]] = None
    links: List[Dict[str, str]] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.images is None:
            self.images = []
        if self.links is None:
            self.links = []
        if self.metadata is None:
            self.metadata = {}


@dataclass
class SelectorConfig:
    """选择器配置"""
    title_selectors: List[str] = None
    content_selectors: List[str] = None
    author_selectors: List[str] = None
    date_selectors: List[str] = None
    tag_selectors: List[str] = None
    exclude_selectors: List[str] = None

    def __post_init__(self):
        if self.title_selectors is None:
            self.title_selectors = [
                'h1', 'title', '[property="og:title"]',
                '.title', '.headline', '.post-title'
            ]
        if self.content_selectors is None:
            self.content_selectors = [
                'article', '.content', '.post-content', '.article-content',
                'main', '.main', '#content', '#main'
            ]
        if self.author_selectors is None:
            self.author_selectors = [
                '[rel="author"]', '.author', '.byline', '.post-author',
                '[property="article:author"]', '.writer'
            ]
        if self.date_selectors is None:
            self.date_selectors = [
                '[property="article:published_time"]', '[datetime]',
                '.date', '.publish-date', '.post-date', 'time',
                '[property="article:published"]'
            ]
        if self.tag_selectors is None:
            self.tag_selectors = [
                '.tags a', '.tag', '.category', '.categories a',
                '[rel="tag"]', '.labels a'
            ]
        if self.exclude_selectors is None:
            self.exclude_selectors = [
                'script', 'style', 'nav', 'header', 'footer',
                'aside', '.sidebar', '.advertisement', '.ads'
            ]


class HTMLParser:
    """HTML解析器"""

    def __init__(self, config: Optional[SelectorConfig] = None):
        """
        初始化HTML解析器

        Args:
            config: 选择器配置
        """
        if not BS4_AVAILABLE:
            raise ImportError("BeautifulSoup4 is required for HTML parsing. Install with: pip install beautifulsoup4")

        self.config = config or SelectorConfig()
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

    def parse(self, html_content: str, base_url: Optional[str] = None) -> ExtractedContent:
        """
        解析HTML内容

        Args:
            html_content: HTML内容
            base_url: 基础URL，用于解析相对链接

        Returns:
            提取的内容
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            # 移除不需要的元素
            self._remove_excluded_elements(soup)

            # 提取内容
            content = ExtractedContent()

            content.title = self._extract_title(soup)
            content.content = self._extract_content(soup)
            content.summary = self._extract_summary(soup, content.content)
            content.author = self._extract_author(soup)
            content.publish_date = self._extract_date(soup)
            content.tags = self._extract_tags(soup)
            content.images = self._extract_images(soup, base_url)
            content.links = self._extract_links(soup, base_url)
            content.metadata = self._extract_metadata(soup)

            # 清理和标准化
            self._clean_content(content)

            self.logger.debug(f"Successfully parsed HTML content: title='{content.title}'")
            return content

        except Exception as e:
            self.logger.error(f"Failed to parse HTML content: {e}")
            return ExtractedContent()

    def _remove_excluded_elements(self, soup: BeautifulSoup) -> None:
        """移除不需要的HTML元素"""
        for selector in self.config.exclude_selectors:
            for element in soup.select(selector):
                element.decompose()

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        """提取标题"""
        for selector in self.config.title_selectors:
            elements = soup.select(selector)
            for element in elements:
                title = self._get_text(element).strip()
                if title and len(title) > 5:  # 确保标题有意义
                    # 如果是meta标签，获取content属性
                    if element.name == 'meta' and element.get('content'):
                        return html.unescape(element.get('content'))
                    return html.unescape(title)

        # 回退到title标签
        title_tag = soup.find('title')
        if title_tag:
            return html.unescape(self._get_text(title_tag).strip())

        return None

    def _extract_content(self, soup: BeautifulSoup) -> Optional[str]:
        """提取主要内容"""
        for selector in self.config.content_selectors:
            elements = soup.select(selector)
            for element in elements:
                content = self._extract_text_from_element(element)
                if content and len(content.strip()) > 100:  # 确保内容足够长
                    return content.strip()

        # 如果没有找到特定的内容区域，提取body的所有文本
        body = soup.find('body')
        if body:
            return self._extract_text_from_element(body).strip()

        # 最后回退到整个文档
        return self._extract_text_from_element(soup).strip()

    def _extract_summary(self, soup: BeautifulSoup, content: Optional[str]) -> Optional[str]:
        """提取摘要"""
        # 尝试从meta标签获取摘要
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            return html.unescape(meta_desc.get('content').strip())

        # 尝试从Open Graph获取
        og_desc = soup.find('meta', attrs={'property': 'og:description'})
        if og_desc and og_desc.get('content'):
            return html.unescape(og_desc.get('content').strip())

        # 从内容中截取前200字符
        if content and len(content) > 200:
            sentences = re.split(r'[.!?。！？]', content)
            summary = ""
            for sentence in sentences:
                summary += sentence.strip() + "。"
                if len(summary) >= 150:
                    break
            return summary.strip()

        return None

    def _extract_author(self, soup: BeautifulSoup) -> Optional[str]:
        """提取作者"""
        for selector in self.config.author_selectors:
            elements = soup.select(selector)
            for element in elements:
                if element.name == 'meta':
                    author = element.get('content')
                else:
                    author = self._get_text(element)

                if author:
                    author = html.unescape(author.strip())
                    # 清理常见的作者前缀
                    author = re.sub(r'^(作者|Writer|By|by)\s*[:：]?\s*', '', author)
                    if len(author) > 2 and len(author) < 100:  # 合理的作者名长度
                        return author

        return None

    def _extract_date(self, soup: BeautifulSoup) -> Optional[str]:
        """提取发布日期"""
        for selector in self.config.date_selectors:
            elements = soup.select(selector)
            for element in elements:
                date_str = None

                if element.name == 'meta':
                    date_str = element.get('content')
                elif element.get('datetime'):
                    date_str = element.get('datetime')
                elif element.get('pubdate'):
                    date_str = element.get('datetime')
                else:
                    date_str = self._get_text(element)

                if date_str:
                    date_str = html.unescape(date_str.strip())
                    # 尝试解析和标准化日期格式
                    normalized_date = self._normalize_date(date_str)
                    if normalized_date:
                        return normalized_date

        return None

    def _extract_tags(self, soup: BeautifulSoup) -> List[str]:
        """提取标签"""
        tags = set()

        for selector in self.config.tag_selectors:
            elements = soup.select(selector)
            for element in elements:
                tag_text = self._get_text(element).strip()
                if tag_text and len(tag_text) < 50:  # 标签通常不会很长
                    tags.add(html.unescape(tag_text))

        # 从meta keywords提取
        meta_keywords = soup.find('meta', attrs={'name': 'keywords'})
        if meta_keywords and meta_keywords.get('content'):
            keywords = [tag.strip() for tag in meta_keywords.get('content').split(',')]
            tags.update([html.unescape(tag) for tag in keywords if len(tag.strip()) > 0])

        return sorted(list(tags))

    def _extract_images(self, soup: BeautifulSoup, base_url: Optional[str] = None) -> List[Dict[str, str]]:
        """提取图片信息"""
        images = []
        img_elements = soup.find_all('img')

        for img in img_elements:
            src = img.get('src')
            if not src:
                continue

            # 处理相对URL
            if base_url and not src.startswith(('http://', 'https://')):
                src = urljoin(base_url, src)

            image_info = {
                'src': src,
                'alt': img.get('alt', '').strip(),
                'title': img.get('title', '').strip()
            }

            # 获取图片尺寸
            if img.get('width'):
                image_info['width'] = img.get('width')
            if img.get('height'):
                image_info['height'] = img.get('height')

            images.append(image_info)

        return images

    def _extract_links(self, soup: BeautifulSoup, base_url: Optional[str] = None) -> List[Dict[str, str]]:
        """提取链接信息"""
        links = []
        link_elements = soup.find_all('a', href=True)

        for link in link_elements:
            href = link.get('href')
            text = self._get_text(link).strip()

            if not href or href.startswith('#'):
                continue

            # 处理相对URL
            if base_url and not href.startswith(('http://', 'https://')):
                href = urljoin(base_url, href)

            link_info = {
                'href': href,
                'text': html.unescape(text)
            }

            # 获取链接标题
            if link.get('title'):
                link_info['title'] = link.get('title')

            links.append(link_info)

        return links

    def _extract_metadata(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """提取元数据"""
        metadata = {}

        # 提取所有meta标签
        meta_tags = soup.find_all('meta')
        for meta in meta_tags:
            name = meta.get('name') or meta.get('property')
            content = meta.get('content')

            if name and content:
                metadata[name] = html.unescape(content)

        # 提取语言
        html_tag = soup.find('html')
        if html_tag and html_tag.get('lang'):
            metadata['language'] = html_tag.get('lang')

        # 提取canonical URL
        canonical = soup.find('link', rel='canonical')
        if canonical and canonical.get('href'):
            metadata['canonical_url'] = canonical.get('href')

        return metadata

    def _extract_text_from_element(self, element: Union[Tag, NavigableString]) -> str:
        """从元素中提取文本"""
        if isinstance(element, NavigableString):
            return str(element)

        # 递归提取文本，跳过脚本和样式
        texts = []
        for child in element.children:
            if isinstance(child, NavigableString):
                texts.append(str(child))
            elif child.name not in ['script', 'style']:
                child_text = self._extract_text_from_element(child)
                if child_text:
                    texts.append(child_text)

        return ' '.join(texts)

    def _get_text(self, element: Union[Tag, NavigableString]) -> str:
        """获取元素的文本内容"""
        if element is None:
            return ""

        if isinstance(element, NavigableString):
            return str(element)

        return element.get_text(separator=' ', strip=True)

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """标准化日期格式"""
        if not date_str:
            return None

        # 常见日期格式的正则表达式
        date_patterns = [
            r'(\d{4}-\d{2}-\d{2})',  # 2024-01-20
            r'(\d{4}/\d{2}/\d{2})',  # 2024/01/20
            r'(\d{2}-\d{2}-\d{4})',  # 20-01-2024
            r'(\d{4}年\d{1,2}月\d{1,2}日)',  # 2024年1月20日
            r'(\d{4}\.\d{1,2}\.\d{1,2})',  # 2024.1.20
        ]

        for pattern in date_patterns:
            match = re.search(pattern, date_str)
            if match:
                return match.group(1)

        # ISO格式日期
        iso_match = re.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', date_str)
        if iso_match:
            return iso_match.group(1)

        return None

    def _clean_content(self, content: ExtractedContent) -> None:
        """清理和标准化内容"""
        if content.title:
            content.title = re.sub(r'\s+', ' ', content.title).strip()

        if content.content:
            # 移除多余的空白字符
            content.content = re.sub(r'\n\s*\n', '\n\n', content.content)
            content.content = re.sub(r'[ \t]+', ' ', content.content)
            content.content = content.content.strip()

        if content.summary:
            content.summary = re.sub(r'\s+', ' ', content.summary).strip()

        if content.author:
            content.author = re.sub(r'\s+', ' ', content.author).strip()


class ContentExtractor:
    """内容提取器工厂类"""

    @staticmethod
    def create_extractor(site_type: str = "default") -> HTMLParser:
        """
        创建针对特定网站的提取器

        Args:
            site_type: 网站类型，支持 'news', 'blog', 'tech', 'default'

        Returns:
            配置好的HTML解析器
        """
        configs = {
            "news": SelectorConfig(
                title_selectors=[
                    'h1', '.headline', '.article-title',
                    '[property="og:title"]', 'title'
                ],
                content_selectors=[
                    '.article-body', '.story-content', '.news-content',
                    'article', '.content'
                ],
                author_selectors=[
                    '.author', '.byline', '.reporter',
                    '[property="article:author"]'
                ]
            ),
            "blog": SelectorConfig(
                title_selectors=[
                    '.post-title', '.entry-title', 'h1',
                    '[property="og:title"]'
                ],
                content_selectors=[
                    '.post-content', '.entry-content', '.post-body',
                    'article', '.content'
                ],
                author_selectors=[
                    '.post-author', '.author-name', '.byline',
                    '[rel="author"]'
                ]
            ),
            "tech": SelectorConfig(
                title_selectors=[
                    '.title', '.post-title', 'h1',
                    '[property="og:title"]'
                ],
                content_selectors=[
                    '.content', '.article-content', '.post-content',
                    'article', 'main'
                ],
                tag_selectors=[
                    '.tags a', '.tag', '.topic', '.category',
                    '[rel="tag"]', '.labels a'
                ]
            )
        }

        config = configs.get(site_type, SelectorConfig())
        return HTMLParser(config)

    @staticmethod
    def extract_from_file(file_path: Union[str, Path], site_type: str = "default") -> ExtractedContent:
        """
        从文件提取内容

        Args:
            file_path: 文件路径
            site_type: 网站类型

        Returns:
            提取的内容
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        extractor = ContentExtractor.create_extractor(site_type)
        return extractor.parse(html_content)