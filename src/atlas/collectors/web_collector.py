"""
Atlas Web 采集器

专门用于采集静态网页内容。
"""

from typing import Any, Dict, List
from .base import BaseCollector


class WebCollector(BaseCollector):
    """Web 采集器"""

    def collect(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """采集 Web 数据

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        # 配置 User-Agent
        self.configure_user_agent(source_config)

        url = source_config.get('url')
        if not url:
            self.logger.error("Web 源缺少 URL 配置", source=source_config.get('name'))
            return []

        self.logger.log_collection(source_config.get('name', 'unknown'), 0, "started")

        try:
            # 发送请求获取网页内容
            response = self.make_request(url)
            if not response:
                self.logger.log_collection(source_config.get('name', 'unknown'), 0, "failed")
                return []

            # 解析 HTML
            soup = self.parse_html(response.text)

            # 提取页面信息
            item = self._extract_page_info(soup, source_config)
            if item:
                # 设置 URL
                item['link'] = url

                # 标准化数据
                standardized_item = self.standardize_item(item, source_config)
                if standardized_item:
                    self.logger.log_collection(source_config.get('name', 'unknown'), 1, "completed")
                    return [standardized_item]

            self.logger.log_collection(source_config.get('name', 'unknown'), 0, "completed")
            return []

        except Exception as e:
            self.logger.exception(f"Web 采集失败", source=source_config.get('name'),
                                url=url, error=str(e))
            self.logger.log_collection(source_config.get('name', 'unknown'), 0, "failed")
            return []

    def _extract_page_info(self, soup, source_config: Dict[str, Any]) -> Dict[str, Any]:
        """提取页面信息

        Args:
            soup: BeautifulSoup 对象
            source_config: 数据源配置

        Returns:
            提取的数据字典
        """
        item = {}

        # 获取选择器配置
        selectors = source_config.get('selectors', {})

        # 提取标题
        title_selector = selectors.get('title', 'title')
        title_element = soup.select_one(title_selector) if title_selector else soup.find('title')
        item['title'] = self.extract_text(title_element)

        # 提取内容
        content_selector = selectors.get('content', 'body')
        if content_selector:
            content_element = soup.select_one(content_selector)
            if content_element:
                item['content'] = self.extract_text(content_element)
            else:
                item['content'] = self.extract_text(soup.find('body'))
        else:
            item['content'] = self.extract_text(soup.find('body'))

        # 提取日期
        date_selectors = [
            selectors.get('date'),
            selectors.get('pub_date'),
            selectors.get('published_date'),
            'time[datetime]',
            '.date',
            '.publish-date',
            '.publication-date',
            'meta[property="article:published_time"]',
            'meta[name="date"]',
            'meta[name="pubdate"]'
        ]

        for selector in date_selectors:
            if not selector:
                continue

            date_element = soup.select_one(selector)
            if date_element:
                # 尝试不同的日期属性
                date_value = None
                if date_element.name == 'meta':
                    date_value = date_element.get('content') or date_element.get('datetime')
                else:
                    date_value = date_element.get('datetime') or date_element.get('content') or self.extract_text(date_element)

                if date_value:
                    item['pub_date'] = date_value
                    break

        # 提取作者
        author_selectors = [
            selectors.get('author'),
            '.author',
            '.byline',
            '.writer',
            'meta[name="author"]',
            'meta[property="article:author"]'
        ]

        for selector in author_selectors:
            if not selector:
                continue

            author_element = soup.select_one(selector)
            if author_element:
                if author_element.name == 'meta':
                    item['author'] = author_element.get('content')
                else:
                    item['author'] = self.extract_text(author_element)
                break

        # 提取描述
        description_selectors = [
            'meta[name="description"]',
            'meta[property="og:description"]',
            'meta[name="Description"]',
            '.description',
            '.excerpt'
        ]

        for selector in description_selectors:
            if not selector:
                continue

            desc_element = soup.select_one(selector)
            if desc_element:
                if desc_element.name == 'meta':
                    item['description'] = desc_element.get('content', '')
                else:
                    item['description'] = self.extract_text(desc_element)
                break

        return item