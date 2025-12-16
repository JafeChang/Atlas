"""
Atlas RSS 采集器

专门用于采集 RSS/Atom 订阅源的内容。
"""

import time
from typing import Any, Dict, List
import feedparser

from .base import BaseCollector


class RSSCollector(BaseCollector):
    """RSS 采集器"""

    def collect(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """采集 RSS 数据

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        # 配置 User-Agent
        self.configure_user_agent(source_config)

        url = source_config.get('url')
        if not url:
            self.logger.error("RSS 源缺少 URL 配置", source=source_config.get('name'))
            return []

        self.logger.log_collection(source_config.get('name', 'unknown'), 0, "started")

        try:
            # 发送请求获取 RSS 内容
            response = self.make_request(url)
            if not response:
                self.logger.log_collection(source_config.get('name', 'unknown'), 0, "failed")
                return []

            # 解析 RSS
            feed = self.parse_rss(response.text)

            if feed.bozo:
                self.logger.warning(f"RSS 解析警告", source=source_config.get('name'),
                                url=url, warning=str(feed.bozo_exception))
                # 即使有警告也尝试处理

            items = []
            entries = feed.entries if hasattr(feed, 'entries') else []

            for entry in entries:
                try:
                    # 提取条目信息
                    item = self._extract_entry(entry)
                    if item:
                        # 标准化数据
                        standardized_item = self.standardize_item(item, source_config)
                        if standardized_item:
                            items.append(standardized_item)

                except Exception as e:
                    self.logger.warning(f"处理 RSS 条目失败", source=source_config.get('name'),
                                    entry_id=getattr(entry, 'id', 'unknown'), error=str(e))
                    continue

            self.logger.log_collection(source_config.get('name', 'unknown'), len(items), "completed")
            return items

        except Exception as e:
            self.logger.exception(f"RSS 采集失败", source=source_config.get('name'),
                                url=url, error=str(e))
            self.logger.log_collection(source_config.get('name', 'unknown'), 0, "failed")
            return []

    def _extract_entry(self, entry) -> Dict[str, Any]:
        """提取 RSS 条目信息

        Args:
            entry: feedparser 条目对象

        Returns:
            提取的数据字典
        """
        item = {}

        # 基本信息
        item['title'] = getattr(entry, 'title', '')
        item['link'] = getattr(entry, 'link', '')

        # 内容处理
        # 优先使用 content，然后 description，然后 summary
        content = ''
        if hasattr(entry, 'content') and entry.content:
            for content_item in entry.content:
                if content_item.get('type') == 'text/html' or content_item.get('type') == 'text/plain':
                    content = content_item.get('value', '')
                    break
        elif hasattr(entry, 'description'):
            content = entry.description
        elif hasattr(entry, 'summary'):
            content = entry.summary

        item['content'] = content
        item['description'] = content  # 为了兼容性

        # 时间信息
        # 尝试多个时间字段
        time_fields = ['published_parsed', 'updated_parsed']
        pub_date = None
        for field in time_fields:
            if hasattr(entry, field) and getattr(entry, field):
                pub_date = getattr(entry, field)
                break

        if pub_date:
            item['pub_date'] = time.strftime('%Y-%m-%d %H:%M:%S', pub_date)
        else:
            # 尝试字符串格式的时间
            time_str_fields = ['published', 'updated']
            for field in time_str_fields:
                if hasattr(entry, field):
                    item['pub_date'] = getattr(entry, field)
                    break

        # 其他信息
        item['author'] = getattr(entry, 'author', '')
        item['id'] = getattr(entry, 'id', item.get('link', ''))

        # 标签
        item['tags'] = []
        if hasattr(entry, 'tags') and entry.tags:
            item['tags'] = [tag.get('term', '') for tag in entry.tags if tag.get('term')]

        # 媒体信息（如果有）
        if hasattr(entry, 'media_content') and entry.media_content:
            item['media'] = [{'url': media.get('url'), 'type': media.get('type')}
                            for media in entry.media_content]

        return item