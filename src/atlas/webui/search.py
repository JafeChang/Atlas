"""Browser-only search parameters, independent of storage and inference."""

from dataclasses import dataclass

from atlas.feed.query import InvalidQueryError


@dataclass(frozen=True)
class SearchOptions:
    text: str = ""
    mode: str = "hybrid"
    view: str = "all"

    @classmethod
    def from_params(cls, params):
        for key in ("q", "search_mode", "view"):
            if key in params and len(params[key]) != 1:
                raise InvalidQueryError(key, "只能指定一次")
        text = params.get("q", [""])[0].strip()
        mode = params.get("search_mode", ["hybrid"])[0]
        view = params.get("view", ["all"])[0]
        if view not in ("all", "recommended"):
            raise InvalidQueryError("view", "只支持 all、recommended")
        if len(text) > 200 or any(ord(char) < 32 for char in text):
            raise InvalidQueryError("q", "请输入 200 字以内的搜索内容，不含控制字符")
        if mode not in ("hybrid", "keyword", "semantic"):
            raise InvalidQueryError("search_mode", "只支持 hybrid、keyword、semantic")
        if text and not any(char.isalnum() for char in text):
            raise InvalidQueryError("q", "请输入文字或数字")
        return cls(text, mode, view)

    def params(self):
        result = [("q", self.text), ("search_mode", self.mode)] if self.text else []
        if self.view == "recommended":
            result.append(("view", self.view))
        return result
