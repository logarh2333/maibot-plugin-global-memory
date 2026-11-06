"""全局记忆插件。

模型可以自主创建、更新、删除记忆文件；每轮只把「记忆索引 + 简介」注入上下文，
模型按需调用工具读取具体内容，避免把全部记忆塞进 prompt。

存储：``<data_dir>/memories/<id>.json``，索引：``<data_dir>/index.json``。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

PLUGIN_ID = "cn.logarh.global-memory"

# 记忆 ID 形如 M-0001
ID_PATTERN = re.compile(r"^M-\d{4,}$")


# ────────────────────────────  配置模型  ────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """MaiBot 要求的基础插件配置节。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用全局记忆插件")
    config_version: str = Field(default="1.0.0", description="配置版本，由插件维护")


class InjectSectionConfig(PluginConfigBase):
    """上下文注入配置。"""

    __ui_label__ = "上下文注入"
    __ui_icon__ = "inbox"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否每轮注入记忆索引")
    inject_target: str = Field(
        default="both",
        description="注入位置：planner / replyer / both",
        json_schema_extra={"choices": ["planner", "replyer", "both"]},
    )
    max_index_items: int = Field(
        default=40,
        description="索引里最多列出多少条记忆",
        ge=1,
        le=200,
    )
    max_summary_chars: int = Field(
        default=60,
        description="每条记忆简介最多显示多少字符",
        ge=10,
        le=300,
    )
    max_total_chars: int = Field(
        default=2000,
        description="整段索引注入的最大字符数",
        ge=200,
        le=10000,
    )
    include_pinned: bool = Field(default=True, description="是否优先展示置顶记忆")


class MemorySectionConfig(PluginConfigBase):
    """记忆存储配置。"""

    __ui_label__ = "记忆存储"
    __ui_icon__ = "database"
    __ui_order__ = 2

    max_memories: int = Field(default=500, description="最多保存多少条记忆", ge=10, le=10000)
    max_content_chars: int = Field(
        default=20000,
        description="单条记忆正文最大字符数",
        ge=500,
        le=200000,
    )
    max_keywords: int = Field(default=8, description="每条记忆最多多少个关键词", ge=1, le=30)
    allow_delete: bool = Field(default=True, description="是否允许模型删除记忆")


class MemoryGlobalConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    inject: InjectSectionConfig = Field(default_factory=InjectSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)


# ────────────────────────────  工具函数  ────────────────────────────


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _neg_time(value: Any) -> float:
    """把 ISO 时间转成负数时间戳，便于升序排序时实现「新的在前」。"""

    if not value:
        return 0.0
    try:
        return -datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _normalize_keywords(raw: Any, limit: int) -> list[str]:
    """把关键词规整成去重列表。"""

    if isinstance(raw, str):
        items = re.split(r"[,，、\s]+", raw)
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = []

    result: list[str] = []
    for item in items:
        word = item.strip()
        if not word or word in result:
            continue
        result.append(word)
        if len(result) >= limit:
            break
    return result


# ────────────────────────────  插件主体  ────────────────────────────


class GlobalMemoryPlugin(MaiBotPlugin):
    """全局记忆：模型自主维护的记忆文件库。"""

    config_model = MemoryGlobalConfig

    def __init__(self) -> None:
        super().__init__()
        self._root: Path | None = None
        self._index: dict[str, Any] = {"next_id": 1, "memories": []}

    # ── 生命周期 ──

    async def on_load(self) -> None:
        self._root = Path(self.ctx.paths.data_dir) / "memories"
        self._root.mkdir(parents=True, exist_ok=True)
        self._load_index()
        self.ctx.logger.info(
            "全局记忆插件已加载：共 %d 条记忆，注入=%s，上限=%d 条",
            len(self._index["memories"]),
            self.config.inject.inject_target,
            self.config.memory.max_memories,
        )

    async def on_unload(self) -> None:
        self._save_index()
        self.ctx.logger.info("全局记忆插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        if scope == "self":
            self.ctx.logger.info("全局记忆插件配置已更新：version=%s", version)

    # ── 索引读写 ──

    def _index_path(self) -> Path:
        assert self._root is not None
        return self._root / "index.json"

    def _memory_path(self, memory_id: str) -> Path:
        assert self._root is not None
        # 只允许 M-0001 形态，避免路径穿越
        if not ID_PATTERN.match(memory_id):
            raise ValueError(f"非法记忆 ID：{memory_id}")
        return self._root / f"{memory_id}.json"

    def _load_index(self) -> None:
        path = self._index_path()
        if not path.is_file():
            self._index = {"next_id": 1, "memories": []}
            self._save_index()
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("memories"), list):
                self._index = payload
                self._index.setdefault("next_id", 1)
                self._migrate_index()
            else:
                raise ValueError("索引结构不正确")
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取记忆索引失败，重建：%s", exc)
            self._index = {"next_id": 1, "memories": []}

    def _migrate_index(self) -> None:
        """补齐旧索引缺失的字段（content_preview）。"""

        changed = False
        for entry in self._index["memories"]:
            if "content_preview" in entry:
                continue
            memory_id = str(entry.get("id") or "")
            payload = self._read_memory(memory_id) or {}
            entry["content_preview"] = str(payload.get("content") or "")[:500]
            changed = True
        if changed:
            self._save_index()
            self.ctx.logger.info("已为 %d 条记忆补齐索引缓存", len(self._index["memories"]))

    def _save_index(self) -> None:
        if self._root is None:
            return
        path = self._index_path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            self.ctx.logger.warning("写入记忆索引失败：%s", exc)

    def _next_id(self) -> str:
        number = int(self._index.get("next_id") or 1)
        self._index["next_id"] = number + 1
        return f"M-{number:04d}"

    def _find_entry(self, memory_id: str) -> dict[str, Any] | None:
        for entry in self._index["memories"]:
            if entry.get("id") == memory_id:
                return entry
        return None

    def _read_memory(self, memory_id: str) -> dict[str, Any] | None:
        path = self._memory_path(memory_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取记忆 %s 失败：%s", memory_id, exc)
            return None

    def _write_memory(self, memory_id: str, payload: dict[str, Any]) -> None:
        path = self._memory_path(memory_id)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self.ctx.logger.error("写入记忆 %s 失败：%s", memory_id, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ── 业务操作 ──

    def _create(
        self,
        title: str,
        content: str,
        keywords: list[str],
        summary: str = "",
        tags: list[str] | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        cfg = self.config.memory
        if len(self._index["memories"]) >= cfg.max_memories:
            raise ValueError(f"记忆数量已达上限 {cfg.max_memories} 条，请先清理旧记忆")

        memory_id = self._next_id()
        now = _now_iso()
        payload = {
            "id": memory_id,
            "title": title.strip()[:120] or "未命名记忆",
            "summary": (summary.strip() or _clip(content, 80))[:300],
            "keywords": keywords[: cfg.max_keywords],
            "tags": [str(tag).strip() for tag in (tags or []) if str(tag).strip()][:10],
            "content": content[: cfg.max_content_chars],
            "pinned": bool(pinned),
            "created_at": now,
            "updated_at": now,
            "read_count": 0,
        }
        self._write_memory(memory_id, payload)
        self._index["memories"].append(
            {
                "id": memory_id,
                "title": payload["title"],
                "summary": payload["summary"],
                "keywords": payload["keywords"],
                "tags": payload["tags"],
                "pinned": payload["pinned"],
                "content_preview": payload["content"][:500],
                "created_at": now,
                "updated_at": now,
            }
        )
        self._save_index()
        return payload

    def _update(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        summary: str | None = None,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        payload = self._read_memory(memory_id)
        if payload is None:
            raise ValueError(f"记忆 {memory_id} 不存在")

        cfg = self.config.memory
        if title is not None:
            payload["title"] = title.strip()[:120] or payload["title"]
        if content is not None:
            payload["content"] = content[: cfg.max_content_chars]
        if summary is not None:
            payload["summary"] = summary.strip()[:300]
        if keywords is not None:
            payload["keywords"] = keywords[: cfg.max_keywords]
        if tags is not None:
            payload["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()][:10]
        if pinned is not None:
            payload["pinned"] = bool(pinned)

        payload["updated_at"] = _now_iso()
        self._write_memory(memory_id, payload)

        entry = self._find_entry(memory_id)
        if entry is not None:
            for key in ("title", "summary", "keywords", "tags", "pinned", "updated_at"):
                entry[key] = payload[key]
            entry["content_preview"] = str(payload.get("content") or "")[:500]
            self._save_index()
        return payload

    def _delete(self, memory_id: str) -> bool:
        if not self.config.memory.allow_delete:
            raise ValueError("当前配置不允许删除记忆")
        path = self._memory_path(memory_id)
        if path.is_file():
            path.unlink()
        before = len(self._index["memories"])
        self._index["memories"] = [
            entry for entry in self._index["memories"] if entry.get("id") != memory_id
        ]
        changed = len(self._index["memories"]) != before
        if changed:
            self._save_index()
        return changed

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """按关键词、标题、简介、正文做加权匹配。"""

        query = (query or "").strip().lower()
        if not query:
            return []

        terms = [term for term in re.split(r"[,，、\s]+", query) if term]
        scored: list[tuple[float, dict[str, Any]]] = []

        for entry in self._index["memories"]:
            title = str(entry.get("title") or "").lower()
            summary = str(entry.get("summary") or "").lower()
            keywords = [str(word).lower() for word in entry.get("keywords") or []]
            # 索引里缓存正文预览，避免每次搜索都读文件
            content = str(entry.get("content_preview") or "").lower()

            score = 0.0
            for term in terms:
                if term in keywords:
                    score += 5.0
                if term in title:
                    score += 3.0
                if term in summary:
                    score += 2.0
                if term in content:
                    score += 1.0
            if score > 0:
                if entry.get("pinned"):
                    score += 0.5
                scored.append((score, entry))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def _build_index_text(self) -> str:
        """生成注入用的记忆索引文本。"""

        cfg = self.config.inject
        entries = list(self._index["memories"])
        if not entries:
            return ""

        if cfg.include_pinned:
            # 置顶优先，同组内按更新时间倒序
            entries.sort(
                key=lambda item: (
                    0 if item.get("pinned") else 1,
                    _neg_time(item.get("updated_at")),
                )
            )
        else:
            entries.sort(key=lambda item: _neg_time(item.get("updated_at")))

        entries = entries[: cfg.max_index_items]

        lines = [
            "[全局记忆索引]",
            f"你有一个全局记忆库，共 {len(self._index['memories'])} 条。以下是索引，需要细节时用 read_memory 按 ID 读取：",
        ]
        for entry in entries:
            memory_id = entry.get("id")
            title = _clip(entry.get("title"), 40)
            summary = _clip(entry.get("summary"), cfg.max_summary_chars)
            keywords = "、".join(str(word) for word in (entry.get("keywords") or [])[:5])
            pin = "📌" if entry.get("pinned") else ""
            line = f"- {memory_id} {pin}{title}：{summary}"
            if keywords:
                line += f"（关键词：{keywords}）"
            lines.append(line)

        lines.append("需要查看某条记忆的完整内容，调用 read_memory(memory_id=...)。")
        text = "\n".join(lines)
        if len(text) > cfg.max_total_chars:
            text = text[: cfg.max_total_chars].rstrip() + "\n…（索引已截断）"
        return text

    # ── 工具 ──

    @Tool(
        "create_memory",
        brief_description="创建一条全局记忆，用于长期记事",
        detailed_description=(
            "当你需要记住重要信息（用户偏好、约定、事件、结论等）时使用。\n"
            "title 简短概括，summary 一句话说明，keywords 用于以后检索，content 写完整内容。"
        ),
        parameters=[
            ToolParameterInfo(
                name="title",
                param_type=ToolParamType.STRING,
                description="记忆标题，简短概括",
                required=True,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="记忆正文，完整内容",
                required=True,
            ),
            ToolParameterInfo(
                name="keywords",
                param_type=ToolParamType.STRING,
                description="关键词，逗号分隔，用于以后检索",
                required=False,
            ),
            ToolParameterInfo(
                name="summary",
                param_type=ToolParamType.STRING,
                description="一句话简介，留空自动截取正文",
                required=False,
            ),
            ToolParameterInfo(
                name="tags",
                param_type=ToolParamType.STRING,
                description="标签，逗号分隔",
                required=False,
            ),
            ToolParameterInfo(
                name="pinned",
                param_type=ToolParamType.BOOLEAN,
                description="是否置顶（置顶记忆优先出现在索引里）",
                required=False,
                default=False,
            ),
        ],
    )
    async def handle_create_memory(
        self,
        title: str = "",
        content: str = "",
        keywords: str = "",
        summary: str = "",
        tags: str = "",
        pinned: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}
        if not title.strip() or not content.strip():
            return {"success": False, "content": "title 和 content 都不能为空。"}

        try:
            payload = self._create(
                title=title,
                content=content,
                keywords=_normalize_keywords(keywords, self.config.memory.max_keywords),
                summary=summary,
                tags=_normalize_keywords(tags, 10),
                pinned=bool(pinned),
            )
        except ValueError as exc:
            return {"success": False, "content": str(exc)}

        self.ctx.logger.info("已创建全局记忆 %s：%s", payload["id"], payload["title"])
        return {
            "success": True,
            "content": f"已创建记忆 {payload['id']}：{payload['title']}",
            "memory_id": payload["id"],
            "memory": payload,
        }

    @Tool(
        "read_memory",
        brief_description="按 ID 读取一条全局记忆的完整内容",
        detailed_description="索引里只显示简介，需要完整内容时用这个工具按 memory_id 读取。",
        parameters=[
            ToolParameterInfo(
                name="memory_id",
                param_type=ToolParamType.STRING,
                description="记忆 ID，形如 M-0001",
                required=True,
            ),
        ],
    )
    async def handle_read_memory(self, memory_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}

        memory_id = (memory_id or "").strip()
        if not ID_PATTERN.match(memory_id):
            return {"success": False, "content": f"记忆 ID 格式不正确：{memory_id}"}

        payload = self._read_memory(memory_id)
        if payload is None:
            return {"success": False, "content": f"没有找到记忆 {memory_id}。"}

        payload["read_count"] = int(payload.get("read_count") or 0) + 1
        try:
            self._write_memory(memory_id, payload)
        except OSError:
            pass

        lines = [
            f"【{payload['id']}】{payload.get('title')}",
            f"简介：{payload.get('summary')}",
        ]
        keywords = payload.get("keywords") or []
        if keywords:
            lines.append(f"关键词：{'、'.join(str(word) for word in keywords)}")
        lines.append(f"更新时间：{payload.get('updated_at')}")
        lines.append("")
        lines.append(str(payload.get("content") or ""))

        return {
            "success": True,
            "content": "\n".join(lines),
            "memory": payload,
        }

    @Tool(
        "search_memory_global",
        brief_description="搜索全局记忆，返回匹配的记忆索引",
        detailed_description=(
            "当你需要回忆某件事但不确定是哪条记忆时使用。\n"
            "按关键词、标题、简介、正文匹配，返回最相关的记忆索引，再用 read_memory 读取详情。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索词，可用空格或逗号分隔多个关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回条数，默认 5",
                required=False,
                default=5,
            ),
        ],
    )
    async def handle_search_memory_global(
        self,
        query: str = "",
        limit: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}

        query = (query or "").strip()
        if not query:
            return {"success": False, "content": "搜索词不能为空。"}

        final_limit = max(1, min(int(limit or 5), 20))
        hits = self._search(query, final_limit)
        if not hits:
            return {
                "success": True,
                "content": f"没有找到与「{query}」相关的记忆。",
                "count": 0,
            }

        lines = [f"搜索「{query}」命中 {len(hits)} 条记忆："]
        for entry in hits:
            memory_id = entry.get("id")
            title = _clip(entry.get("title"), 40)
            summary = _clip(entry.get("summary"), self.config.inject.max_summary_chars)
            keywords = "、".join(str(word) for word in (entry.get("keywords") or [])[:5])
            line = f"- {memory_id} {title}：{summary}"
            if keywords:
                line += f"（关键词：{keywords}）"
            lines.append(line)
        lines.append("用 read_memory(memory_id=...) 读取完整内容。")

        return {
            "success": True,
            "content": "\n".join(lines),
            "count": len(hits),
            "memories": hits,
        }

    @Tool(
        "update_memory",
        brief_description="更新一条已有全局记忆",
        detailed_description="补充或修正记忆内容时使用，只需传要改的字段。",
        parameters=[
            ToolParameterInfo(
                name="memory_id",
                param_type=ToolParamType.STRING,
                description="记忆 ID，形如 M-0001",
                required=True,
            ),
            ToolParameterInfo(
                name="title",
                param_type=ToolParamType.STRING,
                description="新标题，留空不改",
                required=False,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="新正文，留空不改",
                required=False,
            ),
            ToolParameterInfo(
                name="summary",
                param_type=ToolParamType.STRING,
                description="新简介，留空不改",
                required=False,
            ),
            ToolParameterInfo(
                name="keywords",
                param_type=ToolParamType.STRING,
                description="新关键词，逗号分隔，留空不改",
                required=False,
            ),
            ToolParameterInfo(
                name="tags",
                param_type=ToolParamType.STRING,
                description="新标签，逗号分隔，留空不改",
                required=False,
            ),
            ToolParameterInfo(
                name="pinned",
                param_type=ToolParamType.BOOLEAN,
                description="是否置顶",
                required=False,
            ),
        ],
    )
    async def handle_update_memory(
        self,
        memory_id: str = "",
        title: str = "",
        content: str = "",
        summary: str = "",
        keywords: str = "",
        tags: str = "",
        pinned: bool | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}

        memory_id = (memory_id or "").strip()
        if not ID_PATTERN.match(memory_id):
            return {"success": False, "content": f"记忆 ID 格式不正确：{memory_id}"}

        try:
            payload = self._update(
                memory_id,
                title=title if title.strip() else None,
                content=content if content.strip() else None,
                summary=summary if summary.strip() else None,
                keywords=_normalize_keywords(keywords, self.config.memory.max_keywords) if keywords.strip() else None,
                tags=_normalize_keywords(tags, 10) if tags.strip() else None,
                pinned=pinned,
            )
        except ValueError as exc:
            return {"success": False, "content": str(exc)}

        self.ctx.logger.info("已更新全局记忆 %s", memory_id)
        return {
            "success": True,
            "content": f"已更新记忆 {memory_id}：{payload.get('title')}",
            "memory": payload,
        }

    @Tool(
        "delete_memory",
        brief_description="删除一条全局记忆",
        detailed_description="当记忆过时或错误时使用。删除后不可恢复。",
        parameters=[
            ToolParameterInfo(
                name="memory_id",
                param_type=ToolParamType.STRING,
                description="记忆 ID，形如 M-0001",
                required=True,
            ),
        ],
    )
    async def handle_delete_memory(self, memory_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}

        memory_id = (memory_id or "").strip()
        if not ID_PATTERN.match(memory_id):
            return {"success": False, "content": f"记忆 ID 格式不正确：{memory_id}"}

        try:
            changed = self._delete(memory_id)
        except ValueError as exc:
            return {"success": False, "content": str(exc)}

        if not changed:
            return {"success": False, "content": f"没有找到记忆 {memory_id}。"}

        self.ctx.logger.info("已删除全局记忆 %s", memory_id)
        return {"success": True, "content": f"已删除记忆 {memory_id}。"}

    @Tool(
        "list_memories",
        brief_description="列出全局记忆索引",
        detailed_description="查看当前所有记忆的编号、标题和简介。",
        parameters=[
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="最多列出多少条，默认 30",
                required=False,
                default=30,
            ),
        ],
    )
    async def handle_list_memories(self, limit: int = 30, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "全局记忆插件已被停用。"}

        entries = self._index["memories"]
        if not entries:
            return {"success": True, "content": "记忆库还是空的。", "count": 0}

        final_limit = max(1, min(int(limit or 30), 200))
        lines = [f"共 {len(entries)} 条记忆："]
        for entry in entries[:final_limit]:
            pin = "📌" if entry.get("pinned") else ""
            lines.append(
                f"- {entry.get('id')} {pin}{_clip(entry.get('title'), 40)}："
                f"{_clip(entry.get('summary'), self.config.inject.max_summary_chars)}"
            )
        if len(entries) > final_limit:
            lines.append(f"…还有 {len(entries) - final_limit} 条未显示")

        return {
            "success": True,
            "content": "\n".join(lines),
            "count": len(entries),
            "memories": entries[:final_limit],
        }

    # ── 上下文注入 ──

    def _inject_into(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """把记忆索引注入 items。"""

        if not self.config.inject.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}

        items = kwargs.get("items")
        if not isinstance(items, list):
            return {"action": "continue", "modified_kwargs": kwargs}

        # 幂等：同一次请求重试时不要重复注入
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta")
            if isinstance(meta, dict) and str(meta.get("item_id", "")).startswith("global-memory:"):
                return {"action": "continue", "modified_kwargs": kwargs}

        text = self._build_index_text()
        if not text:
            return {"action": "continue", "modified_kwargs": kwargs}

        updated = list(items)
        context_item = {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": f"global-memory:{uuid4().hex}",
                "logical_turn_id": None,
                "timestamp": _now_iso(),
            },
            "parts": [{"type": "text", "text": text}],
        }
        # 插在最后一个 SystemMessageItem 之后
        insert_at = 0
        for index in range(len(updated) - 1, -1, -1):
            item = updated[index]
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
                insert_at = index + 1
                break
        updated.insert(insert_at, context_item)

        kwargs["items"] = updated
        kwargs["item_schema_version"] = kwargs.get("item_schema_version", 1)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_global_memory_planner",
        description="在 Planner 请求前注入全局记忆索引",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner(self, **kwargs: Any) -> dict[str, Any]:
        if self.config.inject.inject_target not in ("planner", "both"):
            return {"action": "continue", "modified_kwargs": kwargs}
        return self._inject_into(kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="inject_global_memory_replyer",
        description="在 Replyer 请求前注入全局记忆索引",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_replyer(self, **kwargs: Any) -> dict[str, Any]:
        if self.config.inject.inject_target not in ("replyer", "both"):
            return {"action": "continue", "modified_kwargs": kwargs}
        return self._inject_into(kwargs)

    # ── WebUI API ──

    @API("webui_search", description="搜索记忆", version="1")
    async def api_search(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"memories": [], "count": 0}
        try:
            final_limit = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            final_limit = 20
        hits = self._search(query, final_limit)
        return {
            "memories": [
                {
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "summary": entry.get("summary"),
                    "keywords": "、".join(str(word) for word in (entry.get("keywords") or [])),
                    "pinned": "是" if entry.get("pinned") else "否",
                    "updated_at": entry.get("updated_at"),
                }
                for entry in hits
            ],
            "count": len(hits),
        }

    @API("webui_toggle_pin", description="切换记忆置顶状态", version="1")
    async def api_toggle_pin(self, memory_id: str = "") -> dict[str, Any]:
        memory_id = str(memory_id or "").strip()
        entry = self._find_entry(memory_id)
        if entry is None:
            return {"changed": False, "error": f"记忆 {memory_id} 不存在"}
        new_state = not bool(entry.get("pinned"))
        try:
            self._update(memory_id, pinned=new_state)
        except ValueError as exc:
            return {"changed": False, "error": str(exc)}
        return {"changed": True, "pinned": new_state}

    @API("webui_export", description="导出全部记忆为 JSON", version="1")
    async def api_export(self) -> dict[str, Any]:
        items = []
        for entry in self._index["memories"]:
            payload = self._read_memory(str(entry.get("id") or ""))
            if payload:
                items.append(payload)
        return {
            "exported_at": _now_iso(),
            "count": len(items),
            "memories": items,
        }

    @API("webui_import", description="从 JSON 导入记忆", version="1")
    async def api_import(self, payload: str = "") -> dict[str, Any]:
        import json as _json

        raw = str(payload or "").strip()
        if not raw:
            return {"imported": 0, "error": "导入内容为空"}
        try:
            data = _json.loads(raw)
        except ValueError as exc:
            return {"imported": 0, "error": f"JSON 解析失败：{exc}"}

        items = data.get("memories") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {"imported": 0, "error": "格式不正确，需要 memories 数组"}

        imported = 0
        skipped = 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if not title or not content:
                skipped += 1
                continue
            try:
                self._create(
                    title=title,
                    content=content,
                    keywords=_normalize_keywords(item.get("keywords"), self.config.memory.max_keywords),
                    summary=str(item.get("summary") or ""),
                    tags=_normalize_keywords(item.get("tags"), 10),
                    pinned=bool(item.get("pinned")),
                )
                imported += 1
            except ValueError:
                skipped += 1
        return {"imported": imported, "skipped": skipped}

    @API("webui_summary", description="记忆库概览", version="1")
    async def api_summary(self) -> dict[str, Any]:
        entries = self._index["memories"]
        pinned = sum(1 for entry in entries if entry.get("pinned"))
        return {
            "total": len(entries),
            "pinned": pinned,
            "next_id": self._index.get("next_id"),
            "max_memories": self.config.memory.max_memories,
            "inject_enabled": self.config.inject.enabled,
        }

    @API("webui_list", description="列出全部记忆", version="1")
    async def api_list(self) -> dict[str, Any]:
        rows = []
        for entry in self._index["memories"]:
            rows.append(
                {
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "summary": entry.get("summary"),
                    "keywords": "、".join(str(word) for word in (entry.get("keywords") or [])),
                    "tags": "、".join(str(tag) for tag in (entry.get("tags") or [])),
                    "pinned": "是" if entry.get("pinned") else "否",
                    "updated_at": entry.get("updated_at"),
                }
            )
        return {"memories": rows}

    @API("webui_detail", description="读取单条记忆详情", version="1")
    async def api_detail(self, memory_id: str = "") -> dict[str, Any]:
        payload = self._read_memory(str(memory_id or "").strip())
        if payload is None:
            return {"found": False}
        return {"found": True, "memory": payload}

    @API("webui_create", description="创建记忆", version="1")
    async def api_create(
        self,
        title: str = "",
        content: str = "",
        keywords: str = "",
        summary: str = "",
        pinned: bool = False,
    ) -> dict[str, Any]:
        try:
            payload = self._create(
                title=title,
                content=content,
                keywords=_normalize_keywords(keywords, self.config.memory.max_keywords),
                summary=summary,
                pinned=bool(pinned),
            )
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}
        return {"saved": True, "memory_id": payload["id"]}

    @API("webui_update", description="更新记忆", version="1")
    async def api_update(
        self,
        memory_id: str = "",
        title: str = "",
        content: str = "",
        keywords: str = "",
        summary: str = "",
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self._update(
                str(memory_id or "").strip(),
                title=title if title.strip() else None,
                content=content if content.strip() else None,
                summary=summary if summary.strip() else None,
                keywords=_normalize_keywords(keywords, self.config.memory.max_keywords) if keywords.strip() else None,
                pinned=pinned,
            )
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}
        return {"saved": True, "memory": payload}

    @API("webui_delete", description="删除记忆", version="1")
    async def api_delete(self, memory_id: str = "") -> dict[str, Any]:
        try:
            changed = self._delete(str(memory_id or "").strip())
        except ValueError as exc:
            return {"deleted": False, "error": str(exc)}
        return {"deleted": changed}


def create_plugin() -> GlobalMemoryPlugin:
    return GlobalMemoryPlugin()
