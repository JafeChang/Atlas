"""
Atlas RSS 采集器

专门用于采集 RSS/Atom 订阅源的内容。
"""

import time
import asyncio
from typing import Any, Dict, List, Optional
import feedparser
from urllib.parse import urljoin

from .base import BaseCollector
from .http_client import Response


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

        source_name = source_config.get('name', 'unknown')
        self.logger.info(f"开始 RSS 采集", source=source_name, url=url)

        try:
            # 发送请求获取 RSS 内容
            response = self.make_request(url)
            if not response:
                self.logger.error(f"RSS 请求失败", source=source_name, url=url)
                return []

            # 解析 RSS
            feed = self.parse_rss(response.text)

            if feed.bozo:
                self.logger.warning(f"RSS 解析警告", source=source_name,
                                url=url, warning=str(feed.bozo_exception))
                # 即使有警告也尝试处理

            items = []
            entries = feed.entries if hasattr(feed, 'entries') else []

            for entry in entries:
                try:
                    # 提取条目信息
                    item = self._extract_entry(entry, url)
                    if item and self.validate_rss_entry(item):
                        # 标准化数据
                        standardized_item = self.standardize_item(item, source_config)
                        if standardized_item:
                            items.append(standardized_item)
                    else:
                        self.logger.debug(f"跳过无效 RSS 条目", source=source_name,
                                        entry_id=getattr(entry, 'id', 'unknown'))

                except Exception as e:
                    self.logger.warning(f"处理 RSS 条目失败", source=source_name,
                                    entry_id=getattr(entry, 'id', 'unknown'), error=str(e))
                    continue

            self.logger.info(f"RSS 采集完成", source=source_name, items=len(items))
            return items

        except Exception as e:
            self.logger.exception(f"RSS 采集失败", source=source_name, url=url, error=str(e))
            return []

    async def collect_async(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """异步采集 RSS 数据

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

        source_name = source_config.get('name', 'unknown')
        self.logger.info(f"开始异步 RSS 采集", source=source_name, url=url)

        try:
            # 发送异步请求获取 RSS 内容
            response = await self.make_request_async(url)
            if not response:
                self.logger.error(f"异步 RSS 请求失败", source=source_name, url=url)
                return []

            # 解析 RSS
            feed = self.parse_rss(response.text)

            if feed.bozo:
                self.logger.warning(f"异步 RSS 解析警告", source=source_name,
                                url=url, warning=str(feed.bozo_exception))

            items = []
            entries = feed.entries if hasattr(feed, 'entries') else []

            for entry in entries:
                try:
                    # 提取条目信息
                    item = self._extract_entry(entry, url)
                    if item and self.validate_rss_entry(item):
                        # 标准化数据
                        standardized_item = self.standardize_item(item, source_config)
                        if standardized_item:
                            items.append(standardized_item)
                    else:
                        self.logger.debug(f"跳过无效异步 RSS 条目", source=source_name,
                                        entry_id=getattr(entry, 'id', 'unknown'))

                except Exception as e:
                    self.logger.warning(f"处理异步 RSS 条目失败", source=source_name,
                                    entry_id=getattr(entry, 'id', 'unknown'), error=str(e))
                    continue

            self.logger.info(f"异步 RSS 采集完成", source=source_name, items=len(items))
            return items

        except Exception as e:
            self.logger.exception(f"异步 RSS 采集失败", source=source_name, url=url, error=str(e))
            return []

    def _extract_entry(self, entry, base_url: str = '') -> Dict[str, Any]:
        """提取 RSS 条目信息

        Args:
            entry: feedparser 条目对象
            base_url: 基础URL，用于处理相对链接

        Returns:
            提取的数据字典
        """
        item = {}

        # 基本信息
        item['title'] = getattr(entry, 'title', '')
        raw_link = getattr(entry, 'link', '')
        # 处理相对链接
        if raw_link and base_url:
            item['link'] = urljoin(base_url, raw_link)
        else:
            item['link'] = raw_link

        # 内容处理 - 增强的内容提取逻辑
        content = ''
        content_type = 'text'

        # 优先级: content > description > summary > title
        if hasattr(entry, 'content') and entry.content:
            for content_item in entry.content:
                if content_item.get('type') in ['text/html', 'application/xhtml+xml']:
                    content = content_item.get('value', '')
                    content_type = 'html'
                    break
                elif content_item.get('type') in ['text/plain']:
                    content = content_item.get('value', '')
                    content_type = 'text'
        elif hasattr(entry, 'description'):
            content = entry.description
            content_type = 'html' if '<' in content else 'text'
        elif hasattr(entry, 'summary'):
            content = entry.summary
            content_type = 'html' if '<' in content else 'text'

        item['content'] = content
        item['content_type'] = content_type
        item['description'] = content  # 为了兼容性

        # 时间信息 - 增强的时间处理
        pub_date = None

        # 尝试解析的时间字段
        time_fields = ['published_parsed', 'updated_parsed']
        for field in time_fields:
            if hasattr(entry, field):
                parsed_time = getattr(entry, field)
                if parsed_time and len(parsed_time) >= 6:
                    try:
                        pub_date = time.strftime('%Y-%m-%d %H:%M:%S', parsed_time)
                        break
                    except (ValueError, TypeError):
                        continue

        # 如果解析失败，尝试字符串格式
        if not pub_date:
            time_str_fields = ['published', 'updated', 'date']
            for field in time_str_fields:
                if hasattr(entry, field):
                    time_str = getattr(entry, field)
                    if time_str:
                        # 简单的时间字符串处理
                        pub_date = str(time_str)
                        break

        item['pub_date'] = pub_date or ''
        item['published'] = pub_date  # 别名

        # 作者信息
        author = getattr(entry, 'author', '')
        if not author and hasattr(entry, 'author_detail'):
            author = entry.author_detail.get('name', '')
        item['author'] = author

        # 唯一标识
        entry_id = getattr(entry, 'id', '')
        if not entry_id:
            # 使用链接作为ID
            entry_id = item.get('link', '')
            if not entry_id:
                # 使用标题作为最后的备选
                entry_id = item.get('title', '')
        item['id'] = entry_id

        # 标签处理
        tags = []
        if hasattr(entry, 'tags') and entry.tags:
            for tag in entry.tags:
                if isinstance(tag, dict):
                    tag_term = tag.get('term', '')
                else:
                    tag_term = getattr(tag, 'term', '') if hasattr(tag, 'term') else str(tag)
                if tag_term:
                    tags.append(tag_term)
        item['tags'] = tags

        # 分类信息
        categories = []
        if hasattr(entry, 'tags') and entry.tags:
            for tag in entry.tags:
                if isinstance(tag, dict) and 'term' in tag:
                    categories.append(tag['term'])
                elif hasattr(tag, 'term'):
                    categories.append(tag.term)
        item['categories'] = categories

        # 媒体信息增强
        media = []
        if hasattr(entry, 'media_content') and entry.media_content:
            for media_item in entry.media_content:
                media_info = {
                    'url': media_item.get('url', ''),
                    'type': media_item.get('type', ''),
                    'medium': media_item.get('medium', ''),
                    'width': media_item.get('width', 0),
                    'height': media_item.get('height', 0)
                }
                if media_info['url']:
                    media.append(media_info)

        # 缩略图
        if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
            for thumbnail in entry.media_thumbnail:
                thumbnail_info = {
                    'url': thumbnail.get('url', ''),
                    'width': thumbnail.get('width', 0),
                    'height': thumbnail.get('height', 0)
                }
                if thumbnail_info['url'] and thumbnail_info not in media:
                    media.append(thumbnail_info)

        item['media'] = media

        # 语言
        item['language'] = getattr(entry, 'language', '')

        # 版权信息
        item['rights'] = getattr(entry, 'rights', '')

        # 评论链接
        item['comments'] = getattr(entry, 'comments', '')

        # 更新时间
        updated = None
        if hasattr(entry, 'updated_parsed') and entry.updated_parsed:
            try:
                updated = time.strftime('%Y-%m-%d %H:%M:%S', entry.updated_parsed)
            except (ValueError, TypeError):
                pass

        if not updated and hasattr(entry, 'updated'):
            updated = str(entry.updated)

        item['updated'] = updated or ''

        return item

    def validate_rss_entry(self, entry: Dict[str, Any]) -> bool:
        """验证RSS条目的有效性

        Args:
            entry: RSS条目数据

        Returns:
            是否有效
        """
        # 必须有标题或链接
        if not entry.get('title') and not entry.get('link'):
            return False

        # 内容不能为空
        content = entry.get('content', '') or entry.get('description', '')
        if not content or len(content.strip()) < 10:
            return False

        # 链接格式检查
        link = entry.get('link', '')
        if link and not (link.startswith('http://') or link.startswith('https://')):
            return False

        return True