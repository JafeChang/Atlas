"""
数据验证模块

提供数据结构验证、内容质量检查和业务规则验证功能。
"""

import re
import html
from typing import Dict, List, Optional, Any, Callable, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from urllib.parse import urlparse

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class ValidationLevel(Enum):
    """验证级别"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ValidationType(Enum):
    """验证类型"""
    REQUIRED = "required"
    FORMAT = "format"
    LENGTH = "length"
    RANGE = "range"
    PATTERN = "pattern"
    CUSTOM = "custom"


@dataclass
class ValidationRule:
    """验证规则"""
    name: str
    type: ValidationType
    level: ValidationLevel = ValidationLevel.ERROR
    message: Optional[str] = None
    required: bool = False
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    custom_validator: Optional[Callable[[Any], bool]] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """验证结果"""
    is_valid: bool
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    info: List[Dict[str, Any]] = field(default_factory=list)

    def add_error(self, field: str, message: str, level: ValidationLevel = ValidationLevel.ERROR):
        """添加验证错误"""
        error_item = {
            'field': field,
            'message': message,
            'level': level.value,
            'timestamp': datetime.now().isoformat()
        }

        if level == ValidationLevel.ERROR or level == ValidationLevel.CRITICAL:
            self.errors.append(error_item)
        elif level == ValidationLevel.WARNING:
            self.warnings.append(error_item)
        else:
            self.info.append(error_item)

    @property
    def has_errors(self) -> bool:
        """是否有错误"""
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        """是否有警告"""
        return len(self.warnings) > 0

    @property
    def total_issues(self) -> int:
        """总问题数"""
        return len(self.errors) + len(self.warnings)

    def get_summary(self) -> Dict[str, Any]:
        """获取验证摘要"""
        return {
            'is_valid': self.is_valid,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
            'info_count': len(self.info),
            'total_issues': self.total_issues
        }


class ContentValidator:
    """内容验证器"""

    def __init__(self):
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")
        self.rules: Dict[str, List[ValidationRule]] = {}
        self.custom_validators: Dict[str, Callable] = {}

        # 初始化默认验证规则
        self._initialize_default_rules()

    def _initialize_default_rules(self):
        """初始化默认验证规则"""
        # 标题验证规则
        self.rules['title'] = [
            ValidationRule(
                name="title_required",
                type=ValidationType.REQUIRED,
                required=True,
                message="标题不能为空"
            ),
            ValidationRule(
                name="title_length",
                type=ValidationType.LENGTH,
                min_length=5,
                max_length=200,
                message="标题长度应在5-200字符之间"
            ),
            ValidationRule(
                name="title_format",
                type=ValidationType.PATTERN,
                pattern=r'^[^<>]*$',
                message="标题不能包含HTML标签"
            )
        ]

        # 内容验证规则
        self.rules['content'] = [
            ValidationRule(
                name="content_required",
                type=ValidationType.REQUIRED,
                required=True,
                message="内容不能为空"
            ),
            ValidationRule(
                name="content_min_length",
                type=ValidationType.LENGTH,
                min_length=50,
                message="内容长度至少50字符"
            ),
            ValidationRule(
                name="content_max_length",
                type=ValidationType.LENGTH,
                max_length=100000,
                level=ValidationLevel.WARNING,
                message="内容过长，建议分段处理"
            )
        ]

        # 作者验证规则
        self.rules['author'] = [
            ValidationRule(
                name="author_length",
                type=ValidationType.LENGTH,
                max_length=100,
                level=ValidationLevel.WARNING,
                message="作者名过长"
            ),
            ValidationRule(
                name="author_format",
                type=ValidationType.PATTERN,
                pattern=r'^[^<>]*$',
                level=ValidationLevel.WARNING,
                message="作者名不应包含特殊字符"
            )
        ]

        # URL验证规则
        self.rules['url'] = [
            ValidationRule(
                name="url_format",
                type=ValidationType.FORMAT,
                message="URL格式不正确",
                custom_validator=self._validate_url
            )
        ]

        # 日期验证规则
        self.rules['publish_date'] = [
            ValidationRule(
                name="date_format",
                type=ValidationType.FORMAT,
                message="日期格式不正确",
                custom_validator=self._validate_date
            )
        ]

        # 标签验证规则
        self.rules['tags'] = [
            ValidationRule(
                name="tags_format",
                type=ValidationType.FORMAT,
                message="标签格式不正确",
                custom_validator=self._validate_tags
            )
        ]

    def add_rule(self, field: str, rule: ValidationRule):
        """
        添加验证规则

        Args:
            field: 字段名
            rule: 验证规则
        """
        if field not in self.rules:
            self.rules[field] = []
        self.rules[field].append(rule)

    def add_custom_validator(self, name: str, validator: Callable):
        """
        添加自定义验证器

        Args:
            name: 验证器名称
            validator: 验证函数
        """
        self.custom_validators[name] = validator

    def validate(self, document: Dict[str, Any]) -> ValidationResult:
        """
        验证文档

        Args:
            document: 文档数据

        Returns:
            验证结果
        """
        result = ValidationResult(is_valid=True)

        try:
            # 验证每个字段
            for field, rules in self.rules.items():
                field_value = document.get(field)

                # 检查必填字段
                if self._check_required_field(field, field_value, rules, result):
                    continue  # 必填字段缺失，跳过其他验证

                # 执行字段验证
                self._validate_field(field, field_value, rules, result)

            # 执行文档级验证
            self._validate_document_level(document, result)

            # 更新总体验证状态
            result.is_valid = not result.has_errors

            self.logger.debug(f"Document validation completed: valid={result.is_valid}, errors={len(result.errors)}")

        except Exception as e:
            self.logger.error(f"Error during validation: {e}")
            result.add_error("validation", f"验证过程发生错误: {e}", ValidationLevel.CRITICAL)
            result.is_valid = False

        return result

    def _check_required_field(self, field: str, value: Any, rules: List[ValidationRule], result: ValidationResult) -> bool:
        """检查必填字段"""
        for rule in rules:
            if rule.type == ValidationType.REQUIRED and rule.required:
                if not value or (isinstance(value, str) and not value.strip()):
                    result.add_error(field, rule.message or f"{field}是必填字段", rule.level)
                    return True
        return False

    def _validate_field(self, field: str, value: Any, rules: List[ValidationRule], result: ValidationResult):
        """验证单个字段"""
        for rule in rules:
            if rule.type == ValidationType.REQUIRED:
                continue  # 已在required检查中处理

            try:
                if not self._apply_rule(field, value, rule):
                    result.add_error(field, rule.message or f"{field}验证失败", rule.level)
            except Exception as e:
                self.logger.warning(f"Error applying rule {rule.name} to field {field}: {e}")
                result.add_error(field, f"规则 {rule.name} 执行错误: {e}", ValidationLevel.WARNING)

    def _apply_rule(self, field: str, value: Any, rule: ValidationRule) -> bool:
        """应用单个验证规则"""
        if value is None:
            return True  # 空值跳过非required规则

        if rule.type == ValidationType.LENGTH:
            return self._validate_length(value, rule.min_length, rule.max_length)

        elif rule.type == ValidationType.FORMAT:
            return self._validate_format(value, rule.custom_validator)

        elif rule.type == ValidationType.PATTERN:
            return self._validate_pattern(value, rule.pattern)

        elif rule.type == ValidationType.CUSTOM:
            if rule.custom_validator:
                return rule.custom_validator(value)
            return True

        return True

    def _validate_length(self, value: Any, min_length: Optional[int], max_length: Optional[int]) -> bool:
        """验证长度"""
        if not isinstance(value, (str, list)):
            return True

        length = len(value)

        if min_length is not None and length < min_length:
            return False

        if max_length is not None and length > max_length:
            return False

        return True

    def _validate_format(self, value: Any, validator: Optional[Callable]) -> bool:
        """验证格式"""
        if validator:
            return validator(value)
        return True

    def _validate_pattern(self, value: str, pattern: Optional[str]) -> bool:
        """验证正则表达式模式"""
        if not pattern or not isinstance(value, str):
            return True

        return bool(re.match(pattern, value))

    def _validate_url(self, url: str) -> bool:
        """验证URL格式"""
        if not url or not isinstance(url, str):
            return False

        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    def _validate_date(self, date_str: str) -> bool:
        """验证日期格式"""
        if not date_str or not isinstance(date_str, str):
            return False

        # 支持的日期格式
        date_formats = [
            '%Y-%m-%d',
            '%Y-%m-%d %H:%M:%S',
            '%Y/%m/%d',
            '%Y/%m/%d %H:%M:%S',
            '%Y年%m月%d日',
            '%Y年%m月%d日 %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S.%fZ'
        ]

        for fmt in date_formats:
            try:
                datetime.strptime(date_str, fmt)
                return True
            except ValueError:
                continue

        return False

    def _validate_tags(self, tags: Any) -> bool:
        """验证标签格式"""
        if not tags:
            return True

        if not isinstance(tags, list):
            return False

        for tag in tags:
            if not isinstance(tag, str):
                return False
            if len(tag.strip()) == 0 or len(tag) > 50:
                return False
            # 检查是否包含非法字符
            if re.search(r'[<>"\'\n\r\t]', tag):
                return False

        return True

    def _validate_document_level(self, document: Dict[str, Any], result: ValidationResult):
        """文档级别验证"""
        # 检查内容质量
        content = document.get('content', '')
        if content:
            self._check_content_quality(content, result)

        # 检查数据一致性
        self._check_data_consistency(document, result)

        # 检查业务规则
        self._check_business_rules(document, result)

    def _check_content_quality(self, content: str, result: ValidationResult):
        """检查内容质量"""
        if not content:
            return

        # 检查HTML标签残留
        if re.search(r'<[^>]+>', content):
            result.add_error('content', "内容中包含HTML标签", ValidationLevel.WARNING)

        # 检查重复内容
        words = content.lower().split()
        if len(words) > 0:
            unique_words = set(words)
            repetition_rate = 1 - (len(unique_words) / len(words))
            if repetition_rate > 0.5:
                result.add_error('content', "内容重复率过高", ValidationLevel.WARNING)

        # 检查字符编码问题
        encoding_issues = ['Ã©', 'Ã¨', 'Ãª', 'Ã«', 'â€', 'â€', 'â€"']
        for issue in encoding_issues:
            if issue in content:
                result.add_error('content', "内容可能存在编码问题", ValidationLevel.WARNING)
                break

    def _check_data_consistency(self, document: Dict[str, Any], result: ValidationResult):
        """检查数据一致性"""
        # 检查标题和内容的一致性
        title = document.get('title', '')
        content = document.get('content', '')

        if title and content:
            # 标题不应完全出现在内容开头
            if content.startswith(title):
                result.add_error('content', "内容开头不应重复标题", ValidationLevel.INFO)

            # 标题和内容的相似度不应过高
            similarity = self._calculate_similarity(title, content)
            if similarity > 0.8:
                result.add_error('title', "标题与内容相似度过高", ValidationLevel.WARNING)

    def _check_business_rules(self, document: Dict[str, Any], result: ValidationResult):
        """检查业务规则"""
        # 检查发布日期的合理性
        publish_date = document.get('publish_date')
        if publish_date:
            try:
                # 尝试解析日期
                for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d']:
                    try:
                        parsed_date = datetime.strptime(publish_date, fmt)
                        # 检查日期是否在合理范围内
                        now = datetime.now()
                        if parsed_date > now:
                            result.add_error('publish_date', "发布日期不能是未来时间", ValidationLevel.WARNING)
                        # 检查日期是否过于久远
                        if (now - parsed_date).days > 365 * 10:
                            result.add_error('publish_date', "发布日期过于久远", ValidationLevel.INFO)
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # 检查标签数量
        tags = document.get('tags', [])
        if isinstance(tags, list) and len(tags) > 20:
            result.add_error('tags', "标签数量过多，建议控制在20个以内", ValidationLevel.WARNING)

    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """计算字符串相似度"""
        try:
            import difflib
            return difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()
        except ImportError:
            # 简单的相似度计算
            s1_words = set(str1.lower().split())
            s2_words = set(str2.lower().split())
            intersection = s1_words.intersection(s2_words)
            union = s1_words.union(s2_words)
            return len(intersection) / len(union) if union else 0

    def batch_validate(self, documents: List[Dict[str, Any]]) -> List[ValidationResult]:
        """
        批量验证文档

        Args:
            documents: 文档列表

        Returns:
            验证结果列表
        """
        results = []
        for i, document in enumerate(documents):
            try:
                result = self.validate(document)
                result.document_index = i  # 添加文档索引
                results.append(result)
            except Exception as e:
                self.logger.error(f"Error validating document {i}: {e}")
                error_result = ValidationResult(is_valid=False)
                error_result.add_error("validation", f"文档验证失败: {e}", ValidationLevel.CRITICAL)
                results.append(error_result)

        # 统计验证结果
        valid_count = sum(1 for r in results if r.is_valid)
        self.logger.info(f"Batch validation completed: {valid_count}/{len(results)} documents valid")

        return results

    def get_validation_summary(self, results: List[ValidationResult]) -> Dict[str, Any]:
        """
        获取验证摘要统计

        Args:
            results: 验证结果列表

        Returns:
            验证摘要
        """
        if not results:
            return {}

        total_documents = len(results)
        valid_documents = sum(1 for r in results if r.is_valid)
        total_errors = sum(len(r.errors) for r in results)
        total_warnings = sum(len(r.warnings) for r in results)

        # 统计最常见的错误
        error_counts = {}
        for result in results:
            for error in result.errors:
                field = error['field']
                error_counts[field] = error_counts.get(field, 0) + 1

        most_common_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            'total_documents': total_documents,
            'valid_documents': valid_documents,
            'invalid_documents': total_documents - valid_documents,
            'validity_rate': valid_documents / total_documents if total_documents > 0 else 0,
            'total_errors': total_errors,
            'total_warnings': total_warnings,
            'average_errors_per_document': total_errors / total_documents if total_documents > 0 else 0,
            'most_common_errors': most_common_errors
        }


class DocumentSchemaValidator:
    """文档模式验证器"""

    def __init__(self):
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

    def validate_schema(self, document: Dict[str, Any], required_fields: List[str] = None,
                       optional_fields: List[str] = None) -> ValidationResult:
        """
        验证文档模式

        Args:
            document: 文档数据
            required_fields: 必需字段列表
            optional_fields: 可选字段列表

        Returns:
            验证结果
        """
        result = ValidationResult(is_valid=True)

        required_fields = required_fields or ['title', 'content', 'source']
        optional_fields = optional_fields or ['author', 'publish_date', 'tags', 'summary', 'url']

        # 检查必需字段
        for field in required_fields:
            if field not in document or document[field] is None:
                result.add_error(field, f"缺少必需字段: {field}", ValidationLevel.ERROR)
            elif isinstance(document[field], str) and not document[field].strip():
                result.add_error(field, f"必需字段不能为空: {field}", ValidationLevel.ERROR)

        # 检查未知字段
        known_fields = set(required_fields + optional_fields)
        for field in document.keys():
            if field not in known_fields:
                result.add_error(field, f"未知字段: {field}", ValidationLevel.WARNING)

        # 检查字段类型
        type_rules = {
            'title': str,
            'content': str,
            'author': str,
            'url': str,
            'publish_date': str,
            'summary': str,
            'source': str,
            'tags': list
        }

        for field, expected_type in type_rules.items():
            if field in document and not isinstance(document[field], expected_type):
                result.add_error(field, f"字段类型错误，期望 {expected_type.__name__}，实际 {type(document[field]).__name__}", ValidationLevel.ERROR)

        result.is_valid = not result.has_errors
        return result