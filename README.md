# 全局记忆（Global Memory）

给麦麦一个可以自主维护的全局记忆库。模型自己创建、更新、删除记忆，每轮只把「编号 + 标题 + 简介 + 关键词」注入上下文，需要细节时再按 ID 读取。

> 记忆再多也不会撑爆上下文。

## 为什么这样设计

大多数「记忆插件」会把全部记忆塞进 prompt，记忆一多就爆 token、还会稀释注意力。

本插件采用**索引 + 按需读取**：

```
每轮注入：M-0001 用户偏好：喜欢简洁回复（关键词：偏好、风格）
     ↓
模型判断需要细节
     ↓
read_memory(memory_id="M-0001")  →  拿到完整正文
```

索引有字数上限，超出自动截断；正文可以写很长，不影响日常开销。

## 功能

### 6 个 LLM 工具

| 工具 | 说明 |
|---|---|
| `create_memory` | 创建记忆：标题、正文、关键词、简介、标签、置顶 |
| `read_memory` | 按 ID 读取完整内容 |
| `search_memory_global` | 按关键词搜索，返回匹配的索引 |
| `update_memory` | 更新任意字段（留空表示不改） |
| `delete_memory` | 删除记忆 |
| `list_memories` | 列出全部记忆索引 |

### 5 个 WebUI 页面

| 页面 | 位置 | 功能 |
|---|---|---|
| 记忆概览 | 顶部工作区 | 统计卡片 + 全部记忆表格 |
| 新建记忆 | 侧边栏 | 表单创建 |
| 编辑记忆 | 侧边栏 | 修改、置顶切换、删除 |
| 搜索记忆 | 侧边栏 | 关键词搜索 |
| 导入导出 | 侧边栏 | 备份与迁移 |

### 自动注入

两个 Hook：

- `maisaka.planner.before_request`
- `maisaka.replyer.before_model_request`

用 `inject_target` 控制注入到哪一边，默认两边都注入。

注入内容示例：

```
[全局记忆索引]
你有一个全局记忆库，共 12 条。以下是索引，需要细节时用 read_memory 按 ID 读取：
- M-0001 📌用户偏好：喜欢简洁的回复（关键词：偏好、风格）
- M-0002 项目约定：插件放在 plugins/ 下（关键词：插件、目录）
需要查看某条记忆的完整内容，调用 read_memory(memory_id=...)。
```

置顶记忆排在最前，其余按更新时间倒序。

## 安装

```
plugins/global-memory/
├── _manifest.json
├── plugin.py
├── webui.json
├── README.md
├── LICENSE
└── .gitignore
```

放入 MaiBot 的 `plugins/` 目录，重启或在 WebUI 中加载即可。首次加载会自动生成 `config.toml` 和 `memories/` 目录。

无需额外 Python 依赖。

## 配置

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[inject]
enabled = true
inject_target = "both"        # planner / replyer / both
max_index_items = 40          # 索引最多列多少条
max_summary_chars = 60        # 每条简介最多多少字
max_total_chars = 2000        # 整段索引的字数上限
include_pinned = true         # 置顶记忆优先展示

[memory]
max_memories = 500            # 记忆总数上限
max_content_chars = 20000     # 单条正文上限
max_keywords = 8              # 每条最多几个关键词
allow_delete = true           # 是否允许模型删除
```

## 存储结构

```
data/plugins/cn.logarh.global-memory/memories/
├── index.json      索引（编号、标题、简介、关键词、置顶、时间、正文预览）
├── M-0001.json     单条记忆完整内容
├── M-0002.json
└── ...
```

单条记忆文件：

```json
{
  "id": "M-0001",
  "title": "用户偏好",
  "summary": "喜欢简洁的回复",
  "keywords": ["偏好", "风格"],
  "tags": ["用户"],
  "content": "完整内容……",
  "pinned": false,
  "created_at": "2026-11-06T21:00:00+08:00",
  "updated_at": "2026-11-06T21:00:00+08:00",
  "read_count": 0
}
```

## 使用示例

模型可以这样调用：

- 「记住我喜欢喝美式」→ `create_memory(title="饮品偏好", content="喜欢喝美式", keywords="饮品,咖啡")`
- 「我之前说过喜欢什么咖啡」→ `search_memory_global(query="咖啡")` → `read_memory(memory_id="M-0003")`
- 「把那条记忆改成喜欢拿铁」→ `update_memory(memory_id="M-0003", content="喜欢喝拿铁")`
- 「这条不重要了，删掉」→ `delete_memory(memory_id="M-0003")`

## 设计细节

**索引缓存**：索引里存了正文前 500 字的预览，搜索时不用逐个读文件，记忆多了也不会变慢。

**幂等注入**：同一次请求重试时不会重复注入索引（通过 `item_id` 前缀判断）。

**原子写入**：索引和记忆文件都用「临时文件 + 替换」，断电不会写坏。

**路径安全**：记忆 ID 严格匹配 `M-\d{4,}`，杜绝路径穿越。

**旧数据迁移**：升级后首次加载会自动为旧索引补齐 `content_preview` 字段。

## 常见问题

**Q：记忆太多会不会拖慢聊天？**

不会。注入的只有索引，有 `max_total_chars` 上限；搜索走索引缓存，不读文件。

**Q：模型不主动创建记忆怎么办？**

可以在人设里加一句「遇到值得长期记住的事，用 create_memory 记下来」。工具描述里也写了适用场景。

**Q：怎么备份？**

WebUI「导入导出」页可以导出全部记忆为 JSON，或直接备份 `memories/` 目录。

**Q：删错了能恢复吗？**

不能。删除会同时删掉索引条目和记忆文件。建议定期导出备份。

**Q：能关掉自动注入吗？**

把 `[inject] enabled` 设为 `false`，模型仍可以用工具主动查询，但不会自动看到索引。

## 许可

MIT
