"""
文本清理与标准化模块

提供文本清理、标准化、格式转换和内容增强功能。
"""

import re
import html
import unicodedata
from typing import Dict, List, Optional, Any, Union, Tuple
from dataclasses import dataclass
from pathlib import Path

from atlas.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class NormalizationConfig:
    """标准化配置"""
    # 清理选项
    remove_extra_whitespace: bool = True
    normalize_unicode: bool = True
    fix_encoding_issues: bool = True
    remove_html_entities: bool = True

    # 格式选项
    normalize_punctuation: bool = True
    normalize_quotes: bool = True
    normalize_dashes: bool = True

    # 内容选项
    remove_empty_lines: bool = True
    max_line_length: Optional[int] = None
    preserve_paragraphs: bool = True

    # 语言特定选项
    chinese_spacing: bool = True
    english_spacing: bool = True


class TextNormalizer:
    """文本标准化器"""

    def __init__(self, config: Optional[NormalizationConfig] = None):
        """
        初始化文本标准化器

        Args:
            config: 标准化配置
        """
        self.config = config or NormalizationConfig()
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

        # 预编译正则表达式
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """预编译常用的正则表达式"""
        self.patterns = {
            # 空白字符
            'multiple_spaces': re.compile(r'[ \t]+'),
            'multiple_newlines': re.compile(r'\n\s*\n'),
            'trailing_whitespace': re.compile(r'[ \t]+$'),
            'leading_whitespace': re.compile(r'^[ \t]+'),

            # HTML实体
            'html_entities': re.compile(r'&[a-zA-Z]+;|&#[0-9]+;'),

            # 标点符号
            'multiple_periods': re.compile(r'\.{3,}'),
            'multiple_exclamations': re.compile(r'!{3,}'),
            'multiple_questions': re.compile(r'\?{3,}'),
            'repeated_punctuation': re.compile(r'([.!?])\1+'),

            # 引号
            'fancy_quotes': re.compile(r'[""'']'),

            # 破折号
            'various_dashes': re.compile(r'[-–—]'),

            # 中文特殊处理
            'chinese_punctuation_spacing': re.compile(r'([，。！？；：""''（）【】])\\s+([a-zA-Z0-9])'),
            'english_punctuation_spacing': re.compile(r'([a-zA-Z0-9])\\s+([，。！？；：""''（）【】])'),

            # URL和邮箱
            'url_pattern': re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+'),
            'email_pattern': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        }

    def normalize(self, text: str) -> str:
        """
        标准化文本

        Args:
            text: 输入文本

        Returns:
            标准化后的文本
        """
        if not text:
            return ""

        try:
            normalized = text

            # 1. 修复编码问题
            if self.config.fix_encoding_issues:
                normalized = self._fix_encoding_issues(normalized)

            # 2. Unicode标准化
            if self.config.normalize_unicode:
                normalized = self._normalize_unicode(normalized)

            # 3. 移除HTML实体
            if self.config.remove_html_entities:
                normalized = self._remove_html_entities(normalized)

            # 4. 标准化标点符号
            if self.config.normalize_punctuation:
                normalized = self._normalize_punctuation(normalized)

            # 5. 清理空白字符
            if self.config.remove_extra_whitespace:
                normalized = self._clean_whitespace(normalized)

            # 6. 语言特定处理
            if self.config.chinese_spacing or self.config.english_spacing:
                normalized = self._fix_language_spacing(normalized)

            # 7. 格式化段落
            if self.config.preserve_paragraphs:
                normalized = self._format_paragraphs(normalized)

            # 8. 处理行长度
            if self.config.max_line_length:
                normalized = self._wrap_lines(normalized)

            self.logger.debug(f"Text normalized: {len(text)} -> {len(normalized)} characters")
            return normalized

        except Exception as e:
            self.logger.error(f"Failed to normalize text: {e}")
            return text

    def _fix_encoding_issues(self, text: str) -> str:
        """修复常见的编码问题"""
        # 修复常见的编码错误
        encoding_fixes = {
            'Ã©': 'é', 'Ã¨': 'è', 'Ãª': 'ê', 'Ã«': 'ë',
            'Ã ': 'à', 'Ã¢': 'â', 'Ã¤': 'ä', 'Ã£': 'ã',
            'Ãº': 'ú', 'Ã¹': 'ù', 'Ã»': 'û', 'Ã¼': 'ü',
            'Ã³': 'ó', 'Ã²': 'ò', 'Ã´': 'ô', 'Ã¶': 'ö', 'Ãµ': 'õ',
            'Ã­': 'í', 'Ã¬': 'ì', 'Ã®': 'î', 'Ã¯': 'ï',
            'Ã±': 'ñ', 'Ã§': 'ç', 'Ã¿': 'ÿ', 'Ã½': 'ý',
            'â‚¬': '"', 'â€™': "'", 'â€œ': '"',
            'â€¦': '...', 'â€“': '–', 'â€”': '—'
        }

        fixed_text = text
        for wrong, correct in encoding_fixes.items():
            fixed_text = fixed_text.replace(wrong, correct)

        return fixed_text

    def _normalize_unicode(self, text: str) -> str:
        """Unicode标准化"""
        # 使用NFKC标准化，处理全角字符等
        return unicodedata.normalize('NFKC', text)

    def _remove_html_entities(self, text: str) -> str:
        """移除HTML实体"""
        def replace_entity(match):
            entity = match.group(0)
            try:
                return html.unescape(entity)
            except:
                return entity

        return self.patterns['html_entities'].sub(replace_entity, text)

    def _normalize_punctuation(self, text: str) -> str:
        """标准化标点符号"""
        normalized = text

        # 标准化引号
        if self.config.normalize_quotes:
            normalized = self.patterns['fancy_quotes'].sub(
                lambda m: '"' if m.group() in ['"', '"'] else "'",
                normalized
            )

        # 标准化破折号
        if self.config.normalize_dashes:
            normalized = self.patterns['various_dashes'].sub('-', normalized)

        # 清理重复的标点符号
        normalized = self.patterns['multiple_periods'].sub('...', normalized)
        normalized = self.patterns['multiple_exclamations'].sub('!!!', normalized)
        normalized = self.patterns['multiple_questions'].sub('???', normalized)
        normalized = self.patterns['repeated_punctuation'].sub(r'\1', normalized)

        return normalized

    def _clean_whitespace(self, text: str) -> str:
        """清理空白字符"""
        cleaned = text

        # 移除每行开头和结尾的空白
        lines = cleaned.split('\n')
        lines = [self.patterns['leading_whitespace'].sub('', line) for line in lines]
        lines = [self.patterns['trailing_whitespace'].sub('', line) for line in lines]
        cleaned = '\n'.join(lines)

        # 标准化空格
        cleaned = self.patterns['multiple_spaces'].sub(' ', cleaned)

        # 清理多个空行
        if self.config.remove_empty_lines:
            cleaned = self.patterns['multiple_newlines'].sub('\n\n', cleaned)

        return cleaned.strip()

    def _fix_language_spacing(self, text: str) -> str:
        """修复中英文间距"""
        fixed = text

        if self.config.chinese_spacing:
            # 中文标点符号后不应有空格（如果后面是英文）
            fixed = self.patterns['chinese_punctuation_spacing'].sub(r'\1\2', fixed)

        if self.config.english_spacing:
            # 英文后不应有空格（如果后面是中文标点）
            fixed = self.patterns['english_punctuation_spacing'].sub(r'\1\2', fixed)

        return fixed

    def _format_paragraphs(self, text: str) -> str:
        """格式化段落"""
        if not self.config.preserve_paragraphs:
            return text

        # 确保段落之间有适当的间距
        paragraphs = text.split('\n\n')
        formatted_paragraphs = []

        for paragraph in paragraphs:
            if paragraph.strip():
                # 移除段落内的多余换行
                paragraph = re.sub(r'\n+', ' ', paragraph)
                # 标准化空格
                paragraph = re.sub(r' +', ' ', paragraph)
                formatted_paragraphs.append(paragraph.strip())

        return '\n\n'.join(formatted_paragraphs)

    def _wrap_lines(self, text: str) -> str:
        """自动换行"""
        if not self.config.max_line_length:
            return text

        lines = []
        current_line = ""

        for word in text.split():
            if not current_line:
                current_line = word
            elif len(current_line) + len(word) + 1 <= self.config.max_line_length:
                current_line += " " + word
            else:
                lines.append(current_line)
                current_line = word

        if current_line:
            lines.append(current_line)

        return '\n'.join(lines)

    def extract_urls(self, text: str) -> List[str]:
        """提取文本中的URL"""
        return self.patterns['url_pattern'].findall(text)

    def extract_emails(self, text: str) -> List[str]:
        """提取文本中的邮箱地址"""
        return self.patterns['email_pattern'].findall(text)

    def remove_urls_and_emails(self, text: str) -> str:
        """移除URL和邮箱地址"""
        cleaned = self.patterns['url_pattern'].sub('[URL]', text)
        cleaned = self.patterns['email_pattern'].sub('[EMAIL]', cleaned)
        return cleaned

    def detect_language(self, text: str) -> str:
        """检测文本的主要语言"""
        if not text:
            return "unknown"

        # 简单的语言检测基于字符分布
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        total_chars = len(re.sub(r'\s', '', text))

        if total_chars == 0:
            return "unknown"

        chinese_ratio = chinese_chars / total_chars

        if chinese_ratio > 0.3:
            return "chinese"
        elif chinese_ratio < 0.05:
            return "english"
        else:
            return "mixed"


class ContentStandardizer:
    """内容标准化器"""

    def __init__(self):
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")
        self.text_normalizer = TextNormalizer()

    def standardize_document(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """
        标准化文档数据

        Args:
            document: 原始文档数据

        Returns:
            标准化后的文档
        """
        try:
            standardized = document.copy()

            # 标准化各字段
            for field, value in document.items():
                if isinstance(value, str):
                    standardized[field] = self.text_normalizer.normalize(value)
                elif isinstance(value, list):
                    standardized[field] = self._standardize_list(value)
                elif isinstance(value, dict):
                    standardized[field] = self._standardize_dict(value)

            # 特殊字段处理
            standardized = self._standardize_special_fields(standardized)

            self.logger.debug(f"Document standardized: {document.get('title', 'untitled')}")
            return standardized

        except Exception as e:
            self.logger.error(f"Failed to standardize document: {e}")
            return document

    def _standardize_list(self, items: List[Any]) -> List[Any]:
        """标准化列表"""
        standardized = []
        for item in items:
            if isinstance(item, str):
                standardized.append(self.text_normalizer.normalize(item))
            elif isinstance(item, dict):
                standardized.append(self._standardize_dict(item))
            else:
                standardized.append(item)
        return standardized

    def _standardize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """标准化字典"""
        standardized = {}
        for key, value in data.items():
            if isinstance(value, str):
                standardized[key] = self.text_normalizer.normalize(value)
            elif isinstance(value, list):
                standardized[key] = self._standardize_list(value)
            elif isinstance(value, dict):
                standardized[key] = self._standardize_dict(value)
            else:
                standardized[key] = value
        return standardized

    def _standardize_special_fields(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """标准化特殊字段"""
        standardized = document.copy()

        # 标准化标题
        if 'title' in standardized:
            standardized['title'] = self._standardize_title(standardized['title'])

        # 标准化标签
        if 'tags' in standardized:
            standardized['tags'] = self._standardize_tags(standardized['tags'])

        # 标准化日期
        if 'publish_date' in standardized:
            standardized['publish_date'] = self._standardize_date(standardized['publish_date'])

        # 标准化作者
        if 'author' in standardized:
            standardized['author'] = self._standardize_author(standardized['author'])

        # 提取和标准化摘要
        if 'content' in standardized and not standardized.get('summary'):
            standardized['summary'] = self._generate_summary(standardized['content'])

        return standardized

    def _standardize_title(self, title: str) -> str:
        """标准化标题"""
        if not title:
            return ""

        # 移除常见的站点前缀
        prefixes = [
            r'^【.*?】\s*',
            r'^\[.*?\]\s*',
            r'^.*?:\s*',
            r'^.*?-\s*',
            r'^.*?\|\s*',
            r'^.*?»\s*'
        ]

        for prefix_pattern in prefixes:
            title = re.sub(prefix_pattern, '', title)

        return title.strip()

    def _standardize_tags(self, tags: List[str]) -> List[str]:
        """标准化标签"""
        if not tags:
            return []

        standardized_tags = []
        for tag in tags:
            if isinstance(tag, str):
                # 标准化标签格式
                tag = tag.strip().lower()
                # 移除特殊字符
                tag = re.sub(r'[^\w\s-]', '', tag)
                # 替换空格为连字符
                tag = re.sub(r'\s+', '-', tag)
                # 移除多余连字符
                tag = re.sub(r'-+', '-', tag)
                # 移除首尾连字符
                tag = tag.strip('-')

                if tag and len(tag) > 1:
                    standardized_tags.append(tag)

        # 去重并排序
        return sorted(list(set(standardized_tags)))

    def _standardize_date(self, date_str: str) -> str:
        """标准化日期格式"""
        if not date_str:
            return ""

        # 尝试解析常见日期格式并转换为ISO格式
        date_patterns = [
            (r'(\d{4})-(\d{1,2})-(\d{1,2})', r'\1-\2-\3'),
            (r'(\d{4})/(\d{1,2})/(\d{1,2})', r'\1-\2-\3'),
            (r'(\d{1,2})-(\d{1,2})-(\d{4})', r'\3-\1-\2'),
            (r'(\d{1,2})/(\d{1,2})/(\d{4})', r'\3-\1-\2'),
            (r'(\d{4})年(\d{1,2})月(\d{1,2})日', r'\1-\2-\3'),
            (r'(\d{4})\.(\d{1,2})\.(\d{1,2})', r'\1-\2-\3'),
        ]

        for pattern, replacement in date_patterns:
            if re.search(pattern, date_str):
                return re.sub(pattern, replacement, date_str)

        return date_str

    def _standardize_author(self, author: str) -> str:
        """标准化作者名"""
        if not author:
            return ""

        # 移除常见前缀
        prefixes = [
            r'^(作者|Writer|By|by|文/|Written by)\s*[:：]?\s*',
            r'^(责任编辑|Editor|编者)\s*[:：]?\s*'
        ]

        for prefix_pattern in prefixes:
            author = re.sub(prefix_pattern, '', author)

        # 移除后缀
        suffixes = [
            r'\s*(著|撰|写|文|发文|报道)$',
            r'\s*\([^)]*\)$',  # 移除括号内容
        ]

        for suffix_pattern in suffixes:
            author = re.sub(suffix_pattern, '', author)

        return author.strip()

    def _generate_summary(self, content: str, max_length: int = 200) -> str:
        """生成内容摘要"""
        if not content:
            return ""

        # 移除多余空白
        clean_content = re.sub(r'\s+', ' ', content).strip()

        if len(clean_content) <= max_length:
            return clean_content

        # 尝试按句子分割
        sentences = re.split(r'[.!?。！？]', clean_content)
        summary = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if sentence:
                if len(summary) + len(sentence) <= max_length:
                    summary += sentence + "。"
                else:
                    break

        return summary.strip()

    @staticmethod
    def extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
        """提取关键词"""
        if not text:
            return []

        # 简单的关键词提取（基于词频）
        # 移除标点符号并分词
        words = re.findall(r'\b\w+\b', text.lower())

        # 过滤停用词（简化版）
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with',
            '是', '的', '了', '在', '和', '与', '或', '但是', '然而', '因此', '所以', '如果'
        }

        # 统计词频
        word_freq = {}
        for word in words:
            if len(word) > 2 and word not in stop_words:
                word_freq[word] = word_freq.get(word, 0) + 1

        # 按频率排序并返回前N个
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, freq in sorted_words[:max_keywords]]