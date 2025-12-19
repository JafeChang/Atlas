"""
内容处理器模块测试

测试HTML解析、文本标准化、去重和验证功能。
"""

import pytest
from pathlib import Path

from atlas.processors.parser import HTMLParser, ContentExtractor, ExtractedContent, SelectorConfig
from atlas.processors.normalizer import TextNormalizer, ContentStandardizer, NormalizationConfig
from atlas.processors.dedup_base import ContentDeduplicator, DeduplicationConfig, HashStrategy, BatchDeduplicator
from atlas.processors.validator import ContentValidator, ValidationRule, ValidationType, ValidationLevel
from tests.test_config import TEST_CONFIG


class TestHTMLParser:
    """HTML解析器测试"""

    @pytest.fixture
    def sample_html(self):
        """示例HTML内容"""
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Test Article Title</title>
            <meta name="description" content="This is a test article description">
            <meta name="author" content="Test Author">
            <meta property="article:published_time" content="2024-01-20T10:00:00Z">
            <meta name="keywords" content="test, article, python">
        </head>
        <body>
            <header>
                <h1>Main Title</h1>
            </header>
            <main>
                <article class="content">
                    <h2>Article Title</h2>
                    <div class="author">By Test Author</div>
                    <time datetime="2024-01-20">January 20, 2024</time>
                    <div class="post-content">
                        <p>This is the first paragraph of the article content.</p>
                        <p>This is the second paragraph with <strong>bold text</strong>.</p>
                        <img src="test.jpg" alt="Test Image">
                        <a href="https://example.com">Example Link</a>
                    </div>
                    <div class="tags">
                        <a href="/tag/python">Python</a>
                        <a href="/tag/test">Test</a>
                    </div>
                </article>
            </main>
            <footer>
                <p>Footer content</p>
            </footer>
        </body>
        </html>
        """

    @pytest.fixture
    def parser(self):
        """HTML解析器实例"""
        config = SelectorConfig(
            title_selectors=['h1', 'h2'],
            content_selectors=['.post-content', '.content'],
            author_selectors=['.author', '[name="author"]']
        )
        return HTMLParser(config)

    def test_parse_basic_html(self, parser, sample_html):
        """测试基本HTML解析"""
        result = parser.parse(sample_html)

        assert isinstance(result, ExtractedContent)
        assert result.title is not None
        assert result.content is not None
        assert "Article Title" in result.title
        assert "first paragraph" in result.content

    def test_extract_metadata(self, parser, sample_html):
        """测试元数据提取"""
        result = parser.parse(sample_html)

        assert result.author == "Test Author"
        assert result.publish_date == "2024-01-20"
        assert "test" in result.tags
        assert "article" in result.tags
        assert "python" in result.tags

    def test_extract_images(self, parser, sample_html):
        """测试图片提取"""
        result = parser.parse(sample_html)

        assert len(result.images) > 0
        assert result.images[0]['src'] == 'test.jpg'
        assert result.images[0]['alt'] == 'Test Image'

    def test_extract_links(self, parser, sample_html):
        """测试链接提取"""
        result = parser.parse(sample_html)

        assert len(result.links) > 0
        assert result.links[0]['href'] == 'https://example.com'
        assert result.links[0]['text'] == 'Example Link'

    def test_content_extractor_factory(self):
        """测试内容提取器工厂"""
        # 测试默认提取器
        extractor = ContentExtractor.create_extractor()
        assert isinstance(extractor, HTMLParser)

        # 测试特定类型提取器
        news_extractor = ContentExtractor.create_extractor("news")
        assert isinstance(news_extractor, HTMLParser)


class TestTextNormalizer:
    """文本标准化器测试"""

    @pytest.fixture
    def normalizer(self):
        """文本标准化器实例"""
        config = NormalizationConfig(
            remove_extra_whitespace=True,
            normalize_unicode=True,
            normalize_punctuation=True
        )
        return TextNormalizer(config)

    def test_whitespace_normalization(self, normalizer):
        """测试空白字符标准化"""
        text = "  Multiple    spaces\n\nand\ttabs  "
        result = normalizer.normalize(text)

        assert "  " not in result
        assert "\t" not in result
        assert result.strip() == "Multiple spaces and tabs"

    def test_unicode_normalization(self, normalizer):
        """测试Unicode标准化"""
        text = "café naïve résumé"
        result = normalizer.normalize(text)

        # 检查Unicode字符是否保留
        assert "café" in result
        assert "naïve" in result

    def test_html_entity_removal(self, normalizer):
        """测试HTML实体移除"""
        text = "Hello &amp; world &lt;test&gt;"
        result = normalizer.normalize(text)

        assert "Hello & world <test>" == result

    def test_punctuation_normalization(self, normalizer):
        """测试标点符号标准化"""
        text = "Smart quotes'' and —dashes..."
        result = normalizer.normalize(text)

        assert '"' in result
        assert '-' in result
        assert "..." in result

    def test_language_specific_spacing(self):
        """测试语言特定间距处理"""
        config = NormalizationConfig(chinese_spacing=True, english_spacing=True)
        normalizer = TextNormalizer(config)

        text = "中文，English。测试test"
        result = normalizer.normalize(text)

        assert "中文，English。测试test" == result

    def test_content_standardizer(self):
        """测试内容标准化器"""
        standardizer = ContentStandardizer()

        document = {
            'title': '  【Python】Test Article  ',
            'content': 'This is test content with  multiple   spaces.',
            'tags': ['  Python  ', 'TEST', 'programming'],
            'author': 'By: Test Author ',
            'publish_date': '2024/01/20'
        }

        result = standardizer.standardize_document(document)

        assert result['title'] == 'Test Article'
        assert '  ' not in result['content']
        assert result['tags'] == ['programming', 'python', 'test']
        assert result['author'] == 'Test Author'
        assert result['publish_date'] == '2024-01-20'


class TestContentDeduplicator:
    """内容去重器测试"""

    @pytest.fixture
    def deduplicator(self):
        """内容去重器实例"""
        config = DeduplicationConfig(
            hash_strategy=HashStrategy.SHA256,
            similarity_threshold=0.8
        )
        return ContentDeduplicator(config)

    def test_exact_duplicate_detection(self, deduplicator):
        """测试精确重复检测"""
        doc1 = {
            'title': 'Test Title',
            'content': 'This is a test article content for deduplication testing.'
        }
        doc2 = {
            'title': 'Test Title',
            'content': 'This is a test article content for deduplication testing.'
        }

        assert not deduplicator.is_duplicate(doc1)
        assert deduplicator.is_duplicate(doc2)

    def test_title_duplicate_detection(self, deduplicator):
        """测试标题重复检测"""
        doc1 = {
            'title': 'Same Title',
            'content': 'Different content for first article.'
        }
        doc2 = {
            'title': 'Same Title',
            'content': 'Different content for second article.'
        }

        assert not deduplicator.is_duplicate(doc1)
        assert deduplicator.is_duplicate(doc2)

    def test_similar_content_detection(self, deduplicator):
        """测试相似内容检测"""
        doc1 = {
            'title': 'Python Tutorial',
            'content': 'This is a comprehensive Python tutorial for beginners.'
        }
        doc2 = {
            'title': 'Python Guide',
            'content': 'This is a comprehensive Python guide for beginners.'
        }

        assert not deduplicator.is_duplicate(doc1)
        # 由于内容相似度很高，应该被检测为重复
        assert deduplicator.is_duplicate(doc2)

    def test_content_length_filter(self, deduplicator):
        """测试内容长度过滤"""
        short_doc = {
            'title': 'Short',
            'content': 'Too short'
        }

        # 内容太短，不应该被处理
        assert not deduplicator.is_duplicate(short_doc)
        assert deduplicator.get_stats()['total_checked'] == 0

    def test_different_hash_strategies(self):
        """测试不同哈希策略"""
        doc = {
            'title': 'Test',
            'content': 'Test content for hash strategy testing.'
        }

        strategies = [HashStrategy.MD5, HashStrategy.SHA1, HashStrategy.SHA256]
        hashes = []

        for strategy in strategies:
            config = DeduplicationConfig(hash_strategy=strategy)
            dedup = ContentDeduplicator(config)
            dedup.is_duplicate(doc)
            hashes.append(list(dedup.seen_hashes)[0])

        # 不同策略应该产生不同的哈希值
        assert len(set(hashes)) == len(hashes)

    def test_batch_deduplicator(self):
        """测试批量去重器"""
        documents = [
            {'title': 'Doc 1', 'content': 'Unique content 1'},
            {'title': 'Doc 2', 'content': 'Unique content 2'},
            {'title': 'Doc 1', 'content': 'Unique content 1'},  # 重复
            {'title': 'Doc 3', 'content': 'Unique content 3'},
            {'title': 'Doc 2', 'content': 'Unique content 2'},  # 重复
        ]

        batch_dedup = BatchDeduplicator()
        unique_docs, duplicate_indices = batch_dedup.deduplicate_documents(documents)

        assert len(unique_docs) == 3
        assert len(duplicate_indices) == 2
        assert duplicate_indices == [2, 4]

    def test_stats_tracking(self, deduplicator):
        """测试统计信息跟踪"""
        docs = [
            {'title': 'Doc 1', 'content': 'Content 1'},
            {'title': 'Doc 1', 'content': 'Content 1'},  # 重复
            {'title': 'Doc 2', 'content': 'Content 2'},
        ]

        for doc in docs:
            deduplicator.is_duplicate(doc)

        stats = deduplicator.get_stats()
        assert stats['total_checked'] == 3
        assert stats['duplicates_found'] == 1
        assert stats['duplicate_rate'] == 1/3


class TestContentValidator:
    """内容验证器测试"""

    @pytest.fixture
    def validator(self):
        """内容验证器实例"""
        return ContentValidator()

    def test_valid_document(self, validator):
        """测试有效文档验证"""
        doc = {
            'title': 'Valid Article Title',
            'content': 'This is a valid article content with sufficient length for validation testing.',
            'author': 'Test Author',
            'url': 'https://example.com/article',
            'tags': ['python', 'testing'],
            'publish_date': '2024-01-20'
        }

        result = validator.validate(doc)
        assert result.is_valid
        assert len(result.errors) == 0

    def test_missing_required_fields(self, validator):
        """测试缺失必需字段"""
        doc = {
            'content': 'Content without title'
        }

        result = validator.validate(doc)
        assert not result.is_valid
        assert len(result.errors) > 0

        # 检查是否包含标题错误
        title_errors = [e for e in result.errors if e['field'] == 'title']
        assert len(title_errors) > 0

    def test_title_validation(self, validator):
        """测试标题验证"""
        # 标题太短
        doc1 = {
            'title': 'Hi',
            'content': 'Valid content length for testing purposes.'
        }
        result1 = validator.validate(doc1)
        assert not result1.is_valid

        # 标题包含HTML
        doc2 = {
            'title': '<script>alert("xss")</script>',
            'content': 'Valid content length for testing purposes.'
        }
        result2 = validator.validate(doc2)
        assert not result2.is_valid

    def test_content_validation(self, validator):
        """测试内容验证"""
        # 内容太短
        doc = {
            'title': 'Valid Title',
            'content': 'Too short'
        }
        result = validator.validate(doc)
        assert not result.is_valid

    def test_url_validation(self, validator):
        """测试URL验证"""
        # 无效URL
        doc = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'url': 'invalid-url'
        }
        result = validator.validate(doc)
        assert not result.is_valid

        # 有效URL
        doc2 = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'url': 'https://example.com'
        }
        result2 = validator.validate(doc2)
        assert result2.is_valid

    def test_date_validation(self, validator):
        """测试日期验证"""
        # 无效日期
        doc = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'publish_date': 'invalid-date'
        }
        result = validator.validate(doc)
        assert not result.is_valid

        # 有效日期
        doc2 = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'publish_date': '2024-01-20'
        }
        result2 = validator.validate(doc2)
        assert result2.is_valid

    def test_tags_validation(self, validator):
        """测试标签验证"""
        # 无效标签格式
        doc = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'tags': 'not-a-list'
        }
        result = validator.validate(doc)
        assert not result.is_valid

        # 有效标签
        doc2 = {
            'title': 'Valid Title',
            'content': 'Valid content length for testing purposes.' * 5,
            'tags': ['python', 'testing', 'validation']
        }
        result2 = validator.validate(doc2)
        assert result2.is_valid

    def test_custom_rule(self, validator):
        """测试自定义验证规则"""
        # 添加自定义规则：标题必须包含"Python"
        custom_rule = ValidationRule(
            name="title_contains_python",
            type=ValidationType.CUSTOM,
            custom_validator=lambda x: "Python" in x if isinstance(x, str) else False,
            message="标题必须包含'Python'"
        )
        validator.add_rule('title', custom_rule)

        # 不包含Python的标题
        doc1 = {
            'title': 'JavaScript Tutorial',
            'content': 'Valid content length for testing purposes.' * 5
        }
        result1 = validator.validate(doc1)
        assert not result1.is_valid

        # 包含Python的标题
        doc2 = {
            'title': 'Python Tutorial',
            'content': 'Valid content length for testing purposes.' * 5
        }
        result2 = validator.validate(doc2)
        assert result2.is_valid

    def test_batch_validation(self, validator):
        """测试批量验证"""
        docs = [
            {
                'title': 'Valid Document 1',
                'content': 'Valid content length for testing purposes.' * 5
            },
            {
                'title': 'Invalid',
                'content': 'Too short'
            },
            {
                'title': 'Valid Document 2',
                'content': 'Valid content length for testing purposes.' * 5
            }
        ]

        results = validator.batch_validate(docs)
        assert len(results) == 3
        assert results[0].is_valid
        assert not results[1].is_valid
        assert results[2].is_valid

        summary = validator.get_validation_summary(results)
        assert summary['total_documents'] == 3
        assert summary['valid_documents'] == 2
        assert summary['invalid_documents'] == 1

    def test_validation_levels(self, validator):
        """测试不同验证级别"""
        doc = {
            'title': 'Very Long Title That Exceeds Normal Length Expectations And Should Trigger Warning Level Validation',
            'content': 'Valid content length for testing purposes.' * 5
        }

        result = validator.validate(doc)
        # 长标题应该产生警告而不是错误
        assert result.has_warnings
        assert len(result.warnings) > 0


class TestIntegration:
    """集成测试"""

    def test_complete_processing_pipeline(self):
        """测试完整的内容处理流水线"""
        # 创建测试HTML内容
        html_content = """
        <html>
        <head><title>Test Article</title></head>
        <body>
            <h1>Test Article</h1>
            <div class="content">
                <p>This is a test article with multiple paragraphs.</p>
                <p>Second paragraph with additional content.</p>
            </div>
        </body>
        </html>
        """

        # 1. HTML解析
        parser = HTMLParser()
        extracted = parser.parse(html_content)
        assert extracted.title == "Test Article"
        assert "multiple paragraphs" in extracted.content

        # 2. 内容标准化
        standardizer = ContentStandardizer()
        doc = {
            'title': extracted.title,
            'content': extracted.content,
            'author': extracted.author or 'Unknown',
            'source': 'integration_test'
        }
        standardized = standardizer.standardize_document(doc)

        # 3. 数据验证
        validator = ContentValidator()
        validation_result = validator.validate(standardized)
        assert validation_result.is_valid

        # 4. 去重检查
        deduplicator = ContentDeduplicator()
        is_duplicate = deduplicator.is_duplicate(standardized)
        assert not is_duplicate

        # 第二次检查应该被识别为重复
        is_duplicate_2 = deduplicator.is_duplicate(standardized.copy())
        assert is_duplicate_2

    def test_processor_configuration_integration(self):
        """测试处理器配置集成"""
        # 使用自定义配置创建处理器
        normalizer_config = NormalizationConfig(
            remove_extra_whitespace=True,
            normalize_unicode=True,
            max_line_length=80
        )
        normalizer = TextNormalizer(normalizer_config)

        dedup_config = DeduplicationConfig(
            similarity_threshold=0.9,
            hash_strategy=HashStrategy.SIMHASH
        )
        deduplicator = ContentDeduplicator(dedup_config)

        # 测试配置是否生效
        text = "This is   a test    text with   extra spaces"
        normalized = normalizer.normalize(text)
        assert "  " not in normalized

        doc = {'title': 'Test', 'content': 'Test content'}
        assert not deduplicator.is_duplicate(doc)
        assert deduplicator.is_duplicate(doc)  # 第二次应该重复