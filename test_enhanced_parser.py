"""
增强内容解析器测试脚本
"""

import sys
from pathlib import Path

# 添加src到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from atlas.processors.enhanced_parser import (
    EnhancedParser,
    TextParser,
    PDFParser,
    DOCXParser,
    PPTXParser,
    OCRParser,
    ContentChunker,
    ChunkConfig,
    parse_file,
    parse_text,
    chunk_content
)

print("=" * 80)
print("Atlas 增强内容解析器测试")
print("=" * 80)

# 测试1: 文本解析器
print("\n1. 测试文本解析器...")
try:
    # 创建测试文本文件
    test_file = Path("test_sample.txt")
    test_file.write_text("这是一个测试文本文件。\n\n包含多行内容。\n\n用于测试文本解析器。", encoding='utf-8')

    parser = TextParser()
    result = parser.parse(test_file)

    print(f"   ✓ 文本解析成功")
    print(f"   ✓ 标题: {result.title}")
    print(f"   ✓ 内容长度: {len(result.content)} 字符")
    print(f"   ✓ 内容类型: {result.content_type}")

    # 清理
    test_file.unlink()

except Exception as e:
    print(f"   ✗ 文本解析失败: {e}")

# 测试2: 内容分块器
print("\n2. 测试内容分块器...")
try:
    long_text = """
    这是第一段内容。这是第一段内容的延续。
    这是第二段内容。
    这是第三段内容。这是第三段内容的延续。这是第三段更多内容。

    这是第四段内容。
    这是第五段内容。
    """ * 10  # 重复以创建更长的文本

    chunker = ContentChunker(ChunkConfig(max_chunk_size=200))
    chunks = chunker.chunk(long_text)

    print(f"   ✓ 内容分块成功")
    print(f"   ✓ 原始文本长度: {len(long_text)} 字符")
    print(f"   ✓ 分块数量: {len(chunks)}")
    if chunks:
        print(f"   ✓ 第一块长度: {len(chunks[0])} 字符")
        print(f"   ✓ 最后一块长度: {len(chunks[-1])} 字符")

except Exception as e:
    print(f"   ✗ 内容分块失败: {e}")

# 测试3: 句子分割
print("\n3. 测试句子分割...")
try:
    text = "这是第一句话。这是第二句话！这是第三句话？这是第四句话。"
    chunks = chunk_content(text, max_chunk_size=50)

    print(f"   ✓ 句子分割成功")
    print(f"   ✓ 分块数量: {len(chunks)}")
    for i, chunk in enumerate(chunks[:3]):
        print(f"     块 {i + 1}: {chunk[:30]}...")

except Exception as e:
    print(f"   ✗ 句子分割失败: {e}")

# 测试4: 解析器支持格式检查
print("\n4. 测试解析器支持格式...")
try:
    parser = EnhancedParser()

    supported_formats = []
    for p in parser.parsers:
        supported_formats.extend(p.supported_formats)

    print(f"   ✓ 支持的格式数量: {len(supported_formats)}")
    print(f"   ✓ 支持的格式: {', '.join(sorted(set(supported_formats)))}")

except Exception as e:
    print(f"   ✗ 格式检查失败: {e}")

# 测试5: 直接文本解析
print("\n5. 测试直接文本解析...")
try:
    parser = EnhancedParser()
    result = parser.parse_text(
        "这是直接传入的文本内容。\n\n不需要文件。",
        title="测试文本",
        enable_chunking=True
    )

    print(f"   ✓ 直接文本解析成功")
    print(f"   ✓ 标题: {result.title}")
    print(f"   ✓ 内容长度: {len(result.content)} 字符")
    print(f"   ✓ 分块数量: {len(result.chunks)}")

except Exception as e:
    print(f"   ✗ 直接文本解析失败: {e}")

# 测试6: 便捷函数
print("\n6. 测试便捷函数...")
try:
    # 测试parse_text便捷函数
    result = parse_text("测试便捷函数。")
    print(f"   ✓ parse_text便捷函数成功")

    # 测试chunk_content便捷函数
    chunks = chunk_content("测试。内容。分块。")
    print(f"   ✓ chunk_content便捷函数成功，分块数: {len(chunks)}")

except Exception as e:
    print(f"   ✗ 便捷函数测试失败: {e}")

# 测试7: 错误处理
print("\n7. 测试错误处理...")
try:
    parser = EnhancedParser()

    # 测试不存在的文件
    try:
        parser.parse("nonexistent.txt")
        print("   ✗ 应该抛出FileNotFoundError")
    except FileNotFoundError:
        print("   ✓ 正确处理不存在的文件")

    # 测试不支持的格式
    try:
        parser.parse("test.xyz")
        print("   ✗ 应该抛出ValueError")
    except ValueError:
        print("   ✓ 正确处理不支持的格式")

except Exception as e:
    print(f"   ✗ 错误处理测试失败: {e}")

# 测试8: 分块配置测试
print("\n8. 测试分块配置...")
try:
    # 测试不同的分块配置
    configs = [
        ChunkConfig(max_chunk_size=100, chunk_overlap=20),
        ChunkConfig(max_chunk_size=500, chunk_overlap=100),
        ChunkConfig(max_chunk_size=1000, chunk_overlap=200, respect_sentence=False)
    ]

    test_text = "这是测试内容。" * 50

    for i, config in enumerate(configs):
        chunker = ContentChunker(config)
        chunks = chunker.chunk(test_text)
        print(f"   ✓ 配置 {i + 1}: 最大={config.max_chunk_size}, 重叠={config.chunk_overlap}, 分块数={len(chunks)}")

except Exception as e:
    print(f"   ✗ 分块配置测试失败: {e}")

# 测试9: 空内容处理
print("\n9. 测试空内容处理...")
try:
    parser = EnhancedParser()

    # 空字符串
    result = parser.parse_text("")
    print(f"   ✓ 空字符串处理成功，内容长度: {len(result.content)}")

    # 只有空白字符
    result = parser.parse_text("   \n\n   ")
    print(f"   ✓ 空白字符处理成功，内容长度: {len(result.content)}")

except Exception as e:
    print(f"   ✗ 空内容处理失败: {e}")

# 测试10: 依赖库检查
print("\n10. 检查可选依赖...")
dependencies = {
    "PyPDF2": "PDF解析（PyPDF2）",
    "pdfplumber": "PDF解析（pdfplumber）",
    "python-docx": "DOCX解析",
    "python-pptx": "PPTX解析",
    "pytesseract": "OCR解析",
    "PIL": "OCR图像处理"
}

for module, description in dependencies.items():
    try:
        __import__(module.replace("-", "_"))
        print(f"   ✓ {description} 已安装")
    except ImportError:
        print(f"   ⚠ {description} 未安装（可选）")

print("\n" + "=" * 80)
print("测试完成!")
print("=" * 80)

# 总结
print("\n总结:")
print("✓ 文本解析器: 支持TXT, MD, LOG, CSV")
print("✓ PDF解析器: 支持PDF（需要PyPDF2或pdfplumber）")
print("✓ DOCX解析器: 支持DOCX（需要python-docx）")
print("✓ PPTX解析器: 支持PPTX（需要python-pptx）")
print("✓ OCR解析器: 支持图像OCR（需要pytesseract）")
print("✓ 内容分块器: 支持智能分块、句子边界、自定义配置")
print("\n建议: 根据需要安装可选依赖")
print("  uv pip install pypdf2 python-docx python-pptx")
print("  OCR需要额外安装: uv pip install pytesseract pillow")
