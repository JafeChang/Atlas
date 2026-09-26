"""对比 `tools/t205cjk_measure.py` 的两份输出：查询回归 + 索引体积变化。"""

from __future__ import annotations

import json
from pathlib import Path

OLD = Path("/tmp/t205cjk-old.json")
NEW = Path("/tmp/t205cjk-new.json")


def main() -> int:
    old = json.loads(OLD.read_text("utf-8"))
    new = json.loads(NEW.read_text("utf-8"))

    print("== 版本 ==")
    for label, data in (("old", old), ("new", new)):
        print(
            f"  {label}: index_version={data['index_version']} schema={data['schema_version']} "
            f"docs={data['documents']}"
        )

    print("\n== 索引体积（dbstat 精确字节） ==")
    names = sorted(set(old["per_object_bytes"]) | set(new["per_object_bytes"]))
    print(f"  {'object':<40} {'old':>10} {'new':>10} {'delta':>10}")
    for name in names:
        before = old["per_object_bytes"].get(name, 0)
        after = new["per_object_bytes"].get(name, 0)
        marker = "  <<<" if before != after else ""
        print(f"  {name:<40} {before:>10} {after:>10} {after - before:>+10}{marker}")

    print("\n== 汇总 ==")
    print(
        f"  索引本体（rebuild 前后页数差）: old {old['index_bytes']} B "
        f"({old['index_pages']} 页) → new {new['index_bytes']} B ({new['index_pages']} 页) "
        f"= {(new['index_bytes'] / old['index_bytes'] - 1) * 100:+.1f}%"
    )
    odb = old["db_bytes"]["total_bytes"]
    ndb = new["db_bytes"]["total_bytes"]
    print(f"  临时目录总字节（含归档 bin/json）: old {odb} → new {ndb} = {(ndb / odb - 1) * 100:+.1f}%")
    print(
        f"  库文件 .db: old {old['db_bytes']['by_suffix'].get('db')} → "
        f"new {new['db_bytes']['by_suffix'].get('db')}"
    )
    print(
        f"  search_documents: old {old['per_object_bytes']['search_documents']} → "
        f"new {new['per_object_bytes']['search_documents']}"
    )
    print(
        f"  FTS 倒排（_fts_data + _fts_docsize + _fts_idx）: old "
        f"{old['per_object_bytes']['search_documents_fts_data'] + old['per_object_bytes']['search_documents_fts_docsize'] + old['per_object_bytes']['search_documents_fts_idx']} → "
        f"new {new['per_object_bytes']['search_documents_fts_data'] + new['per_object_bytes']['search_documents_fts_docsize'] + new['per_object_bytes']['search_documents_fts_idx']}"
    )
    print(f"  SUM(LENGTH(text)): old {old['sum_length_text']} new {new['sum_length_text']}")
    print(f"  SUM(LENGTH(text_index)): old {old['sum_length_text_index']} new {new['sum_length_text_index']}")

    print("\n== 英文查询回归（逐条：raw_id / score / snippet） ==")
    regressions = 0
    for text in old["queries"]:
        before = old["queries"][text]
        after = new["queries"][text]
        same = before == after
        if not same:
            regressions += 1
        print(f"  {text!r}: hits old={len(before)} new={len(after)} identical={same}")
        if not same:
            for index, (a, b) in enumerate(zip(before, after)):
                if a != b:
                    print(f"    第 {index} 条不同：old={a!r}")
                    print(f"                    new={b!r}")
                    break
    print(f"\n  不同的查询数：{regressions}/{len(old['queries'])}")
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
