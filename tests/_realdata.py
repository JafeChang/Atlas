"""历史实测回归使用冻结的真实快照，避免在线采集改变固定语料的判据。

默认位置 data/test-baseline/store；可用 ATLAS_TEST_STORE_ROOT 指向另一份完整快照。
快照仍不进 git，缺失时各真实数据测试沿用自身的 skip 规则。
在线追加与浏览的联调由 test_webui_entries_render 覆盖，不读取此快照。
"""

import os
from pathlib import Path


def real_store_root() -> Path:
    default = Path(__file__).resolve().parents[1] / "data" / "test-baseline" / "store"
    return Path(os.environ.get("ATLAS_TEST_STORE_ROOT", default))
