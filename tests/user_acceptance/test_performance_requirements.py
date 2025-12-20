"""
性能要求用户验收测试

验证Atlas系统的性能是否满足用户需求。
"""

import pytest
import asyncio
import tempfile
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from atlas.core.config import get_config
from atlas.collectors.rss_collector import RSSCollector
from atlas.processors.parser import HTMLParser
from atlas.processors.normalizer import TextNormalizer


class TestPerformanceRequirements:
    """性能要求验收测试"""

    @pytest.fixture
    def test_environment(self):
        """创建性能测试环境"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            data_dir = Path(temp_dir) / "data"
            log_dir = Path(temp_dir) / "logs"

            for directory in [config_dir, data_dir, log_dir]:
                directory.mkdir(parents=True, exist_ok=True)

            config = get_config(config_dir=config_dir)
            config._config_data["data_dir"] = str(data_dir)
            config._config_data["log_dir"] = str(log_dir)

            yield {
                "config": config,
                "data_dir": data_dir
            }

    def test_perf_01_rss_collection_speed(self, test_environment):
        """性能测试1: RSS采集速度要求"""
        print("🚀 性能测试 1: RSS采集速度")

        rss_collector = RSSCollector(
            user_agent="Atlas/1.0 (Performance Test)",
            timeout=30,
            max_concurrent=5
        )

        # 使用多个RSS源进行并发测试
        rss_urls = [
            "https://feeds.bbci.co.uk/news/rss.xml",
            "https://rss.cnn.com/rss/edition.rss",
            "https://feeds.reuters.com/reuters/topNews"
        ]

        async def concurrent_collection():
            start_time = time.time()

            tasks = [rss_collector.collect_rss(url) for url in rss_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            end_time = time.time()
            total_time = end_time - start_time

            successful_results = [r for r in results if not isinstance(r, Exception)]
            total_items = sum(len(result.items) if hasattr(result, 'items') else 0
                             for result in successful_results)

            # 性能要求: 每个RSS源采集时间不应超过30秒
            avg_time_per_source = total_time / len(rss_urls)
            assert avg_time_per_source <= 30.0, f"平均采集时间过长: {avg_time_per_source:.2f}秒"

            # 性能要求: 总采集时间不应超过60秒
            assert total_time <= 60.0, f"总采集时间过长: {total_time:.2f}秒"

            # 性能要求: 应该采集到内容
            assert total_items > 0, "应该采集到RSS内容"
            assert len(successful_results) >= len(rss_urls) * 0.7, "成功率应该不低于70%"

            print(f"✅ 采集性能达标:")
            print(f"   - 总时间: {total_time:.2f}秒")
            print(f"   - 平均每源: {avg_time_per_source:.2f}秒")
            print(f"   - 成功率: {len(successful_results)}/{len(rss_urls)}")
            print(f"   - 总条目: {total_items}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(concurrent_collection())
        finally:
            loop.close()

    def test_perf_02_html_processing_speed(self, test_environment):
        """性能测试2: HTML处理速度要求"""
        print("🚀 性能测试 2: HTML处理速度")

        html_parser = HTMLParser()
        text_normalizer = TextNormalizer()

        # 生成大量HTML内容进行性能测试
        test_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>性能测试页面</title>
            <meta name="description" content="这是一个用于性能测试的页面">
        </head>
        <body>
            <h1>主标题</h1>
            <div class="article">
                <p>段落1: Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>
                <p>段落2: Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>
                <p>段落3: Ut enim ad minim veniam, quis nostrud exercitation ullamco.</p>
            </div>
            <div class="sidebar">
                <h2>侧边栏</h2>
                <p>侧边栏内容</p>
            </div>
        </body>
        </html>
        """

        # 性能要求: 单个HTML文档处理时间不应超过1秒
        num_documents = 50
        start_time = time.time()

        for i in range(num_documents):
            try:
                parsed = html_parser.parse_html_content(
                    test_html,
                    url=f"https://example.com/test-{i}",
                    title_selector="h1",
                    content_selector=".article p"
                )

                normalized = text_normalizer.normalize_text(parsed.content)

                # 验证处理结果
                assert parsed.title == "主标题", f"文档{i}: 标题解析错误"
                assert len(normalized.strip()) > 0, f"文档{i}: 标准化后内容为空"

            except Exception as e:
                pytest.fail(f"文档{i}处理失败: {e}")

        end_time = time.time()
        total_time = end_time - start_time
        avg_time_per_doc = total_time / num_documents

        # 性能验证
        assert avg_time_per_doc <= 1.0, f"平均处理时间过长: {avg_time_per_doc:.3f}秒/文档"
        assert total_time <= 30.0, f"总处理时间过长: {total_time:.2f}秒"

        print(f"✅ HTML处理性能达标:")
        print(f"   - 文档数量: {num_documents}")
        print(f"   - 总时间: {total_time:.2f}秒")
        print(f"   - 平均每文档: {avg_time_per_doc:.3f}秒")
        print(f"   - 处理速度: {num_documents/total_time:.1f} 文档/秒")

    def test_perf_03_memory_usage(self, test_environment):
        """性能测试3: 内存使用要求"""
        print("🚀 性能测试 3: 内存使用")

        import psutil
        import os

        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB

        # 创建多个组件实例测试内存使用
        components = []
        for i in range(20):
            rss_collector = RSSCollector(
                user_agent=f"Atlas/{i}.0 (Memory Test)",
                timeout=10,
                max_concurrent=2
            )
            html_parser = HTMLParser()
            text_normalizer = TextNormalizer()

            components.append({
                'rss': rss_collector,
                'parser': html_parser,
                'normalizer': text_normalizer
            })

        peak_memory = process.memory_info().rss / 1024 / 1024  # MB
        memory_increase = peak_memory - initial_memory

        # 性能要求: 内存增长不应超过100MB
        assert memory_increase <= 100.0, f"内存使用过多: {memory_increase:.2f}MB"

        # 清理组件
        del components

        final_memory = process.memory_info().rss / 1024 / 1024  # MB
        memory_recovered = peak_memory - final_memory

        print(f"✅ 内存使用性能达标:")
        print(f"   - 初始内存: {initial_memory:.2f}MB")
        print(f"   - 峰值内存: {peak_memory:.2f}MB")
        print(f"   - 内存增长: {memory_increase:.2f}MB")
        print(f"   - 内存回收: {memory_recovered:.2f}MB")

    def test_perf_04_concurrent_processing(self, test_environment):
        """性能测试4: 并发处理能力"""
        print("🚀 性能测试 4: 并发处理能力")

        html_parser = HTMLParser()
        text_normalizer = TextNormalizer()

        test_html = """
        <html>
        <body>
            <h1>并发测试文档</h1>
            <p>这是一个用于并发性能测试的文档。</p>
        </body>
        </html>
        """

        async def process_document(doc_id):
            """处理单个文档的异步函数"""
            try:
                parsed = html_parser.parse_html_content(
                    test_html,
                    url=f"https://example.com/concurrent-{doc_id}",
                    title_selector="h1",
                    content_selector="p"
                )

                normalized = text_normalizer.normalize_text(parsed.content)

                return {
                    'doc_id': doc_id,
                    'success': True,
                    'title': parsed.title,
                    'content_length': len(normalized)
                }
            except Exception as e:
                return {
                    'doc_id': doc_id,
                    'success': False,
                    'error': str(e)
                }

        async def concurrent_test():
            num_documents = 100
            concurrency_levels = [5, 10, 20]

            for concurrency in concurrency_levels:
                print(f"   测试并发级别: {concurrency}")

                start_time = time.time()

                # 创建信号量限制并发数
                semaphore = asyncio.Semaphore(concurrency)

                async def limited_process(doc_id):
                    async with semaphore:
                        return await process_document(doc_id)

                # 执行并发任务
                tasks = [limited_process(i) for i in range(num_documents)]
                results = await asyncio.gather(*tasks)

                end_time = time.time()
                total_time = end_time - start_time

                successful = sum(1 for r in results if r['success'])
                throughput = successful / total_time

                # 性能要求: 并发处理吞吐量不应低于10文档/秒
                assert throughput >= 10.0, f"并发级别{concurrency}: 吞吐量过低 {throughput:.1f}文档/秒"

                # 性能要求: 成功率不应低于95%
                success_rate = successful / num_documents
                assert success_rate >= 0.95, f"并发级别{concurrency}: 成功率过低 {success_rate:.1%}"

                print(f"     - 时间: {total_time:.2f}秒")
                print(f"     - 成功: {successful}/{num_documents}")
                print(f"     - 吞吐量: {throughput:.1f}文档/秒")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(concurrent_test())
        finally:
            loop.close()

        print("✅ 并发处理性能达标")

    def test_perf_05_system_stability(self, test_environment):
        """性能测试5: 系统稳定性"""
        print("🚀 性能测试 5: 系统稳定性")

        rss_collector = RSSCollector(
            user_agent="Atlas/1.0 (Stability Test)",
            timeout=15,
            max_concurrent=3
        )

        html_parser = HTMLParser()
        text_normalizer = TextNormalizer()

        async def stability_test():
            # 运行长时间的混合操作
            num_iterations = 30
            errors = 0
            successful_operations = 0

            start_time = time.time()

            for iteration in range(num_iterations):
                try:
                    # 交替执行不同操作
                    if iteration % 3 == 0:
                        # RSS采集操作
                        result = await rss_collector.collect_rss(
                            "https://feeds.bbci.co.uk/news/rss.xml"
                        )
                        if result and len(result.items) > 0:
                            successful_operations += 1
                        else:
                            errors += 1

                    elif iteration % 3 == 1:
                        # HTML处理操作
                        parsed = html_parser.parse_html_content(
                            "<html><body><h1>Stability Test</h1></body></html>",
                            url="https://example.com/stability",
                            title_selector="h1"
                        )
                        if parsed and parsed.title:
                            successful_operations += 1
                        else:
                            errors += 1

                    else:
                        # 文本标准化操作
                        normalized = text_normalizer.normalize_text(
                            "这是一段用于稳定性测试的文本内容。"
                        )
                        if len(normalized.strip()) > 0:
                            successful_operations += 1
                        else:
                            errors += 1

                    # 每10次迭代输出进度
                    if (iteration + 1) % 10 == 0:
                        print(f"   进度: {iteration + 1}/{num_iterations}")

                except Exception as e:
                    errors += 1
                    print(f"   迭代 {iteration} 错误: {e}")

            end_time = time.time()
            total_time = end_time - start_time

            # 稳定性要求: 错误率不应超过10%
            error_rate = errors / num_iterations
            assert error_rate <= 0.10, f"错误率过高: {error_rate:.1%}"

            # 稳定性要求: 总体成功率不应低于90%
            success_rate = successful_operations / num_iterations
            assert success_rate >= 0.90, f"成功率过低: {success_rate:.1%}"

            print(f"✅ 系统稳定性达标:")
            print(f"   - 总迭代: {num_iterations}")
            print(f"   - 成功操作: {successful_operations}")
            print(f"   - 错误数: {errors}")
            print(f"   - 成功率: {success_rate:.1%}")
            print(f"   - 总时间: {total_time:.2f}秒")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(stability_test())
        finally:
            loop.close()


if __name__ == "__main__":
    print("🚀 开始Atlas系统性能验收测试")
    print("=" * 50)

    pytest.main([__file__, "-v", "-s"])