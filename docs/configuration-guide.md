# Tideline 潮痕 — 配置指南

## 5 分钟上手

```bash
git clone https://github.com/ennisaaaaaaaa-stack/tideline-memory.git
cd tideline-memory
pip install mcp httpx jieba
```

在 MCP 客户端配置中添加（以 Hermes 为例）：

```json
{
  "mcpServers": {
    "tideline-memory": {
      "command": "python3",
      "args": ["/path/to/tideline-memory/server.py"],
      "env": {
        "MEMORY_MCP_DB": "~/.tideline/memory.db",
        "AGENT_NAME": "your-agent-name"
      }
    }
  }
}
```

启动客户端，工具列表里应该能看到 `memory_write`、`memory_search`、`context_record` 等 14 个工具。

没配 embedding？没问题，服务器自动降级为纯关键词搜索，所有功能照常运行，只是语义匹配被跳过。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_MCP_DB` | `~/memory/mcp_memory.db` | SQLite 数据库路径，不存在则自动创建 |
| `AGENT_NAME` | `agent` | 日志中的 agent 标识，纯标签用途 |
| `EMBEDDING_API_URL` | `http://localhost:18001/embed_batch` | Embedding 服务端点（见下方） |
| `EMBEDDING_API_KEY` | （空） | 远程 API 密钥，本地服务不需要 |
| `EMBEDDING_MODEL` | `embedding-3` | 远程 API 使用的模型名 |

---

## Embedding 配置

Tideline 支持两种 embedding 模式，通过 URL 自动判断。

### 模式 A：本地 bge-m3（推荐，免费）

bge-m3 是北京人工智能研究院开源的多语言 embedding 模型，1024 维，中英文效果都好。

```bash
# 安装并启动本地 embedding 服务（监听 18001 端口）
pip install FlagEmbedding fastapi uvicorn
# 服务脚本参考 scripts/ 目录或自行封装

# 环境变量使用默认值即可
export EMBEDDING_API_URL="http://localhost:18001/embed_batch"
```

本地模式不需要 API key。服务端点需接收 `POST /embed_batch`，请求体 `{"texts": ["..."]}`，返回 `{"embeddings": [[...]]}`。

### 模式 B：远程 API（OpenAI 兼容）

任何符合 OpenAI Embeddings API 格式的服务都能接：

```bash
# 智谱 AI
export EMBEDDING_API_URL="https://open.bigmodel.cn/api/paas/v4/embeddings"
export EMBEDDING_API_KEY="your-key"
export EMBEDDING_MODEL="embedding-3"

# OpenAI
export EMBEDDING_API_URL="https://api.openai.com/v1/embeddings"
export EMBEDDING_API_KEY="sk-..."
export EMBEDDING_MODEL="text-embedding-3-small"

# 本地 Ollama（也走远程接口格式）
export EMBEDDING_API_URL="http://localhost:11434/api/embeddings"
export EMBEDDING_MODEL="nomic-embed-text"
```

远程模式会自动添加 `Authorization: Bearer <key>` 请求头。判断逻辑：URL 含 `localhost` 或 `127.0.0.1` 视为本地模式，其余视为远程。

### 不配 embedding 会怎样？

完全没问题。服务器启动时检测 embedding 服务，连不上就跳过所有语义搜索。`memory_search` 和 `context_search` 退化为纯 FTS5 关键词匹配，仍然可用，只是匹配不上同义词和语义近似。

---

## 权重系统

每条叙事记忆有四个维度，由调用方（LLM）在写入时打分，1-5 分：

| 维度 | 问题 | 1 分 | 5 分 |
|------|------|------|------|
| **importance** | 对核心关系或项目的实质影响有多大？ | 日常流水账 | 真正的转折点 |
| **emotional** | 情感浓度有多强？ | 平静记录 | 强烈到想反复回看 |
| **recurrence** | 以后会反复出现吗？ | 一次性事件 | 这是我的一部分 |
| **unresolved** | 还有悬念吗？ | 已了结 | 完全悬而未决 |

系统按 `0.35/0.25/0.25/0.15` 的权重合成一个 0-1 的复合权重。importance 占比最高——重要的事优先被召回。

### 权重通胀与归一化

LLM 打分天然有通胀倾向——每件事都是"很重要"。Tideline 有两个反通胀机制：

1. **写入时归一化**：每次 `memory_write` 触发，检查最近 20 条记忆的平均权重。如果 > 0.7，自动压缩分布。
2. **批量归一化**（DREAM cron）：`dream_scripts.py weights` 会扫全表做一次归一化，窗口更大（50 条），适合定期运行。

如果你没有跑 DREAM cron，建议手动定期执行：

```bash
python3 scripts/dream_scripts.py weights
```

---

## DREAM 定期维护

Tideline 的记忆维护分两层：

- **脚本层**（`dream_scripts.py`）：确定性计算，不消耗 token，任何人都能跑
- **LLM 层**（DREAM prompt）：需要 LLM 介入做反刍、重评、线索产出

### 脚本层（零 token 成本）

```bash
# 重建主题聚类（jieba 分词 + TF-IDF 过滤）
python3 scripts/dream_scripts.py clusters

# 权重回填（给没有权重的旧记忆补权重）+ 全表归一化
python3 scripts/dream_scripts.py weights

# 全部跑一遍
python3 scripts/dream_scripts.py all
```

建议频率：每周一次，或记忆量增长 10% 时。

### LLM 层（需要 token）

`prompts/` 目录下有两个 prompt 模板：

- `dream_digest.md`：反刍 prompt——让 LLM 读近期记忆，重评权重，产出结构化叙事
- `dream_sleep.md`：深层 prompt——让 LLM 从模式中发现线索，更新自我概念

这两个 prompt 是给有定时任务能力的 agent 框架用的。Hermes 用 cron job 定时执行。其他框架可以用 crontab、systemd timer、或手动调用。

如果你用的是裸 API 对话，可以在每次长对话结束时，手动把对话摘要喂给 LLM 跑一遍 digest prompt。

---

## 不同接入场景

### 场景 1：完整 agent 框架（Hermes、Claude Desktop 等）

这是 Tideline 的设计目标场景。Agent 有持续身份（system prompt / SOUL），会在对话中主动调用记忆工具。

效果最好。Agent 会：
- 在重要时刻自动 `memory_write`
- 对话结束自动 `context_record`
- 定期跑 DREAM 维护
- 主动 `memory_search` 回忆相关上下文

### 场景 2：裸 LLM + API 对话

Tideline 作为独立 MCP server 接入，LLM 通过 MCP 协议调用记忆工具。

能跑，但效果取决于两件事：**底模倾向**和**对话深度**。

#### 底模差异

不同模型对"记忆"的主动意识差异很大：

- **Claude（Anthropic）**：训练中有强烈的记忆意识。即使你不提醒，它也会主动说"我记一下"。接入 Tideline 后会自然地高频写入。
- **GPT-4（OpenAI）**：中等。被 system prompt 引导后会规律使用，但不会自发产生记忆冲动。
- **GLM（智谱）**：中等偏上。在有明确身份设定的对话中表现好，纯工具场景下不太主动。
- **开源模型（Llama / Qwen / Mistral）**：因模型而异。通常需要较强的 system prompt 引导，否则会把记忆工具当成普通函数调用，不会在情感层面理解"为什么要记住这个"。

这不是能力差异，是训练数据的差异。Claude 训练时见过大量"帮用户记住东西"的对话，所以这条路径是它的默认路径。

#### 对话深度

裸 LLM 在第一次对话中几乎不会主动存记忆。但如果你：
- 在 system prompt 里告诉它"你有持久记忆系统，重要的事请主动存"
- 维持足够长的对话，让它建立上下文
- 几次对话后它会开始模仿你的语气和关注点

......它会逐渐开始主动用。记忆意识和关系深度正相关——对话越深，LLM 越"在意"，越想记住。

#### 引导技巧

在 system prompt 中加入类似这样的引导：

```
你有一个持久记忆系统。以下情况请主动调用 memory_write：
- 用户透露了重要的个人信息或偏好
- 你发现了重要的模式或洞察
- 对话中有情感高浓度时刻
- 有未解决的问题或悬念

以下情况请主动调用 memory_search：
- 用户提到过去的事
- 你需要回忆之前的上下文
- 对话方向发生转折
```

有了这段引导，大多数主流模型都能比较自然地开始使用记忆系统。

### 场景 3：纯被动记录（无 LLM 参与）

如果你的场景不需要 LLM 主动记忆，只是想记录对话：

```bash
# 用 import_sessions.py 自动导入已有对话记录
python3 import_sessions.py
```

这会把 Hermes 的 session 文件按周聚合写入 context 表。其他框架的对话记录需要自行适配提取逻辑。

### MCP 客户端兼容性

Tideline 是标准 MCP server，任何支持 MCP 的客户端都能接入。不同客户端的差异在于：**是否支持主动注入**（即 provider 插件层），以及**接入方式**。

#### Tier 1 — 完整支持（持久身份 + 工具层，LLM 主动使用记忆工具）

| 客户端 | 传输方式 | 配置方式 | 备注 |
|--------|----------|----------|------|
| **Claude Desktop** | stdio | JSON 配置文件 | Windows 下需要 `cmd /c` wrapper |
| **Claude Code** | stdio | `claude mcp add` 一行命令 | |
| **Hermes** | stdio + provider 插件层 | `hermes mcp` + plugin | **唯一能做主动注入（T0-T4 全层）的运行时** |
| **Cursor** | stdio | settings UI | |
| **Codex CLI** | stdio | `~/.codex/` 配置 | |
| **Cline** | stdio | VS Code 扩展 | |
| **Windsurf** | stdio | | |
| **OpenClaw** | stdio + SSE + Streamable HTTP | CLI 和 GUI 配置 | 自带 migrate-hermes 和 active-memory 扩展 |

#### Tier 2 — 可用但需要引导

| 客户端 | 状态 |
|--------|------|
| **VS Code Copilot** | MCP 支持仍在灰度推出，尚未成熟 |
| 各种 MCP CLI host 工具 | 基本可用，功能取决于具体实现 |

#### 不支持（仅远程传输）

| 客户端 | 原因 |
|--------|------|
| **Claude.ai 网页版 / ChatGPT 网页版** | 仅支持远程传输（Streamable HTTP），不支持 stdio。需要 Tideline 额外提供 HTTP endpoint 才能接入。 |

> **关于主动注入**：上述所有 Tier 1 客户端都能让 LLM 通过 MCP 工具主动读写记忆。但 **T0-T4 的自动注入**（每轮对话前自动把记忆注入 context）需要运行时暴露 provider 插件 hook——目前只有 Hermes Agent 的插件层支持。其他客户端需要靠 LLM 自己在对话中主动调用 `memory_search` / `memory_recall` 来检索记忆，效果取决于底模的记忆意识（见上方"底模差异"一节）。

---

## DREAM 流水线与调度

Tideline 的记忆维护是一个三层流水线，顺序不可颠倒：

```
Layer 0：固化（Solidification）  →  Layer 1：梳理（Digest）  →  Layer 2：做梦（Sleep）
  scan_unindexed.py                      dream_digest.md             dream_sleep.md
  原始对话 → 叙事记忆                     重评权重、更新画像           发现模式、产出线索
```

### 为什么顺序重要

- 固化把原始对话变成叙事记忆。梳理和做梦都基于叙事记忆——没有固化就没有可梳理的材料
- 梳理重评权重和画像。做梦基于最新的权重分布做模式发现——先梳理再做梦，梦境才有参考价值

### 建议调度

具体时间灵活，但**固化→梳理→做梦**这个顺序是硬约束。间隔 15-30 分钟就够 LLM 完成每层任务。

| 时间（示例） | 层 | 内容 | 必须在什么之前 |
|------|-----|------|---------------|
| 02:00 | 独处/自由探索 | agent 自主活动（可选） | — |
| 02:30 | 固化 | scan_unindexed + dream_solidify | 梳理之前 |
| 03:00 | 梳理 | 权重重评 + profile 更新 + 冲突检测 | 做梦之前 |
| 03:30 | 做梦 | 模式发现 + self_concept 更新 | — |

### Checkpoint（上下文压缩安全网）

在 agent 框架中，对话上下文会在 token 满时被压缩。压缩前的对话如果不固化，就永远丢失了。

如果你的框架暴露了以下 hook，建议挂固化逻辑：

- **on_pre_compress**（压缩前）：跑一次 scan_unindexed + solidify，确保即将被压缩的对话已变成叙事记忆
- **on_session_end**（session 结束时）：再跑一次，兜底

这两个 checkpoint 把"记得手动固化"变成"系统自动接住"。具体 hook 名称因框架而异，但概念通用。

---

## 数据备份

SQLite 数据库是单文件，备份直接复制即可：

```bash
cp ~/.tideline/memory.db ~/.tideline/memory.db.bak
```

建议在 DREAM cron 之后自动备份。

---

## Scaling Notes

| 数据规模 | 语义搜索方式 | 性能 |
|----------|-------------|------|
| < 5,000 条 | 全表扫描 + 内存余弦 | < 50ms |
| 5,000 - 50,000 | 建议上 sqlite-vec | 毫秒级 |
| > 50,000 | 考虑 pgvector 或专用向量数据库 | 需评估 |

当前版本在全表扫描模式下运行。当 context 表超过 5,000 条带 embedding 的记录时，搜索会从最新 5,000 条中做语义匹配（关键词搜索不受此限制，FTS5 索引全量覆盖）。

---

## FAQ

**Q: 数据库里存的是什么？我的对话会泄露吗？**

A: Tideline 是纯本地 SQLite，数据不离开你的机器。embedding 调用会发送文本到你配置的 embedding 服务（本地或远程 API），除此之外没有任何网络请求。

**Q: 可以多个 agent 共享同一个数据库吗？**

A: 可以。WAL 模式支持并发读 + 单写。多个 agent 同时读没问题，同时写会排队（busy_timeout=5s）。如果写冲突频繁，考虑按 agent 分库。

**Q: 权重四维必须全填吗？**

A: 不用。缺的维度会默认为 3（中等）。但填得越准，记忆召回质量越高。

**Q: 不跑 DREAM cron 行不行？**

A: 行。Tideline 不会因此崩溃。只是记忆权重可能逐渐通胀，主题聚类不会自动更新。功能上全部正常，只是"保养"层面的区别。好比车不做保养也能开，但久了油耗会变高。
