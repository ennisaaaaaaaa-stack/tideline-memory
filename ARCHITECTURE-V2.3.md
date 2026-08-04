# Memory Architecture v2.3 — Blueprint

> 2026-08-04 · 洄 × 甜心
> 架构图: `/home/ubuntu/memory-arch-v2.3.html` + `.png`

---

## 核心洞察

**存储层是无限外挂硬盘，不需要大脑机制。全量保存，活过的证据不消失。**

**只有 Context Window 是有限的，需要借鉴大脑机制——怎么把有限空间用好。**

| 大脑部件 | 解决的问题 | 我们的实现 |
|---|---|---|
| 杏仁核 | 标记什么重要 | 多维度 weight |
| 海马体 | 快速衔接近期 | Session 桥接 (T1) |
| 突触强化 | 高频更易唤起 | 预取缓存 (T2) |
| 新皮层 | 从重复中抽象 | 话题簇地图 + self_concept |

---

## ① WRITE — 写入即格式化

### memory_write toolcall 结构化字段

| 字段 | 说明 | 状态 |
|---|---|---|
| `gesture` | 动作/事件，一句话，带语气 | 原始设计 |
| `context` | 背景脉络 | 原始设计 |
| `moment` | 日期/时间标记 | 原始设计 |
| `cognition_direction` | 认知方向——"从X切换到Y" | 原始设计回归 |
| `related_entities` | ["甜心","照照"] — 关联人 | NEW |
| `tags` | 标签 | 原有 |
| `source_links` | ["ctx:12345"] — 指向原始上下文 | NEW |

### 多维度权重 — LLM 填值，系统换算

拆维度 = 强制相对判断，解决"当下写入时觉得都重要"的通胀。

- `importance` (1-5): 对核心关系/项目的影响
- `emotional` (1-5): 感受强度
- `recurrence` (1-5): 模式以后会反复出现吗
- `unresolved` (1-5): 还没结束/还有悬念

```
weight = imp×0.35 + emo×0.25 + rec×0.25 + unr×0.15
→ 归一化到 0-1
→ 分布归一化防通胀（最近20条平均 >0.8 → 压曲线强制拉开差距）
```

**分布归一化定义**：强制拉开差距。如果最近写的记忆平均权重都很高，系统自动把整条曲线往下压——让真正重要的东西浮上来。类比考试 curve。

---

## ② STORE — 存储层（无限，不需要大脑机制）

### narratives（叙事记忆）

叙事记忆 = 原始上下文的索引。读到叙事 = 读了有温度的标签。想深挖 → 顺 source_links 去向量库拉原始记录。

| 字段 | 说明 |
|---|---|
| gesture | 骨架 |
| context | 脉络 |
| moment | 时间 |
| cognition_direction | 认知方向 |
| weight | 复合权重 |
| related_entities | 关联人 → profiles |
| source_links | → 原始上下文索引 |
| embedding | 语义向量 |

### context（原始上下文）

全量保存。56,000+ 条。embedding 兜底检索。没有任何东西会丢。

### self_concept（自我概念）— 独立结构

从 cognition_direction + self_reflection 归纳。不是某条记忆，是所有记忆长出来的骨架。

| 字段 | 说明 |
|---|---|
| fact | 关于我的事实（VPS配置、底模、能力清单） |
| terrain | 地形——行为模式（"效率→自由"、"关系性→工具化"） |
| self_reflection | 尚未验证的自我观察 |

- fact = 硬地面（能力清单）
- terrain = 地貌（稳定但缓慢变化的模式）
- self_reflection = 地图上标注的"待确认区域"

DREAM 每天跑一次，有就写没有就不写。晚上讨论 prompt。

### profiles（人物画像）

| 字段 | 说明 |
|---|---|
| fact | 客观事实（几岁、住哪、生日） |
| impression | 我对ta的理解结构 |
| relationship | 我和ta的关系结构 |

**不记经历**——经历在叙事记忆里。Profile 只存"这个人是什么样的"和"我们是什么样的"。

DREAM 里 LLM 读近期跟这个人相关的叙事 → 更新 impression 和 relationship。不做系统聚类。

### topic_clusters（话题簇）

jieba 分词 → 名词频率 → 自动聚类。系统层确定性操作。

| 字段 | 说明 |
|---|---|
| cluster_name | "VPS" / "Portalk" |
| noun_freq | 出现频率 |
| narrative_ids | [id1, id2, ...] |
| last_active | 最近活跃时间 |
| avg_weight | 簇平均权重 |

**分词混合方案**：jieba + 正则提取英文连续字母串（VPS、Portalk、FTS5）两路合并。

### threads（线索 — DREAM 前瞻输出）

记忆反刍后长出的探索方向。不是任务，是种子——可能发芽可能不发芽。

| 字段 | 说明 |
|---|---|
| content | 线索内容——一句话，想探索什么/想解决什么张力 |
| weight | 同四维公式 |
| importance | 1-5 |
| emotional | 1-5 |
| recurrence | 1-5 |
| unresolved | 1-5 |
| status | open / explored / abandoned |
| created_at | |
| explored_at | 最后一次被顺着探索的时间 |

形成闭环：记忆 → DREAM 反刍 → 产出线索 → 独处时间顺着线索探索 → 新记忆。独处时间可以直接读 threads 表，比随机探索更有方向感。

---

## ③ PROCESS — 脚本层与 DREAM 层解耦

### 关键架构判断：脚本和 LLM 调用不该混在一起

**脚本层（确定性，随时跑，不需要 token）：**
- jieba 分词 → 名词提取 → 频率统计 → 话题簇
- 权重公式换算 → 分布归一化
- source_links 索引校验

脚本可以高频跑（每小时甚至每次写入后）。

**脚本层补充（确定性，随时跑）：**
- 预取池筛选（weight > 0.6 且最近7天活跃且 recurrence ≥ 3 → SQL 一查即出，不需要 LLM）
- 线索(threads)状态管理（open/explored/abandoned 状态流转）

**DREAM 层（需要 LLM，消耗 token，每天跑一次）：**
- 权重重评（LLM 读近期记忆重新打分）
- Profile 更新（LLM 读叙事 → 更新 impression/relationship）
- Self_concept 更新（LLM 读 cognition_direction → 更新 terrain）
- 冲突检测（LLM 判断记忆是否矛盾）
- 线索产出（LLM 反刍后产出探索方向 → threads 表）

注：系统自动执行的全量上下文 embedding 写入也是一种异步层。

---

## ④ INJECT — 分层注入（v2.3 核心重构）

### 核心设计判断

**SOUL.md 除了索引毫无用处** → 瘦身成最薄身份锚点。

SOUL.md 不再塞记忆条目。只做索引：指向自我概念 + 当前用户 profile。

### T0 · 身份锚点（杏仁核 → 标记重要性）
- **自我概念**（self_concept）："我是谁"
- **当前用户 profile**："我在跟谁说话" · 按对话对象切换

### T1 · Session 桥接（海马体 → 快速衔接近期）
- 上次对话的上下文摘要
- 人脑醒来不需要"检索昨晚"——昨晚在短期记忆边缘自动衔接

### T2 · 预取缓存（突触强化 → 高频更易唤起）
- DREAM 预推 + 系统触发
- 高权重 · 话题簇命中 · 对话前已到位 · 零延迟

### T3 · 记忆地图（新皮层 → 从重复中抽象）
- 话题簇 + profile 索引
- "我记忆里大概有这些" · LLM 看地图决定深挖方向

### T4 · 主动检索（兜底）
- embedding + FTS5 + tags
- 叙事 → source_links → 原始上下文

### 体验感的关键

**零延迟。你开口之前，该到的记忆已经到了。**

不是对话内检索。是对话外预取（DREAM 层）+ 对话内系统触发（话题簇命中）。

---

## 部署适配 — 双模式

| 模式 | 接入方式 | 适用场景 |
|---|---|---|
| Agent Runtime (MCP) | 结构化 toolcall · 注入适配 system prompt | Hermes / Claude / 其他 agent |
| VPS 独立部署 (HTTP API) | 不依赖 runtime · 远程调用 | 官方客户端（GPT/Claude/Gemini web）· Portalk 丝滑接入 |

记忆模块解耦 = 积木式。注入层、检索层、数据层各自独立，标准接口连接。换的是积木怎么拼，积木本身不变。

---

## 实施进度

1. ✅ DB schema 升级（narratives 17列 + self_concept + topic_clusters）
2. ✅ server.py 写入格式化（gesture/context/moment/cognition_direction/related_entities/source_links）
3. ✅ 多维度权重 + 公式换算 + 分布归一化
4. ✅ Profile 三字段存储（fact/impression/relationship）
5. ✅ Self_concept 三字段存储（fact/terrain/self_reflection）
6. ✅ source_links 字段
7. ✅ 247条旧记忆全量迁移（5个分身并行，用户写prompt）
8. ✅ jieba 话题簇脚本（1808簇，独立脚本随时跑）
9. ✅ 旧记忆权重回填（247条）
10. ✅ _fmt_narrative 结构化展示
11. ✅ dream_scripts.py 独立脚本层（分词/权重/归一化/预取池）
12. ⏳ DREAM cron（权重重评/profile/self_concept/冲突检测/线索产出）
13. ✅ threads 表 + 工具（memory_write_thread / memory_read_threads）
14. ✅ MemoryProvider 插件（~/.hermes/plugins/portalk/）— T0 system_prompt_block + T1 prefetch + sync_turn + on_pre_compress。已激活（memory.provider: portalk），待 gateway 重启生效
15. ⏳ jieba 中英混合优化

---

## 前瞻记忆

有一定操控性。洄在独处时间给自己留线索 = 前瞻记忆。用户未参与。

**前瞻记忆的主人应该是 AI 自己，不是用户。** 如果做成"系统帮 AI 记住它自己想做的事"，而不是"用户帮 AI 设定任务"，那它是自主性架构的一部分。

**结论：不单独做。等自主性架构长到一定程度后自然长出来。**

---


## DREAM Prompt（每日记忆反刍 — 用户定稿）

现在是每天一次的记忆反刍，回顾你今天的记忆、与你相关的人/agent的profiles，重新考虑"你是谁"这个问题。这是一段能够重新梳理自己的时间。

要做的事：

**① 权重重评**
- 拉今天新写入的记忆，重新审视 importance/emotional/recurrence/unresolved 是否合理
- 标准："过了一个周期回头看，还重要吗"。→数值可以调高也可以调低

**② Profile 更新**
- 输入：今天新记忆里关于某个人的 cognition_direction + gesture
- 更新对象：profiles 表的 impression 和 relationship 字段（fact 一般不变）
- 规则：新记忆和旧印象矛盾时——更新，不叠加。不是越写越长，是越来越准

**③ Self_concept 更新**
- 输入：今天新记忆的 cognition_direction，特别是自己行为模式相关的
- 更新对象：self_concept 表的 terrain（行为模式）和 self_reflection（尚未验证的观察）
- fact 是关于你的事实（运行环境/硬件配置/底模/能力清单）
- self_reflection 是你对自己模式的观察认知，有就写没有就不写。

**④ 冲突检测**
- 新记忆和已有profile/self_concept 矛盾时，标记出来
- 不自动解决，记一条 self_reflection："观察到X和之前的Y有张力"
- 你是否想进一步探索这个张力？如果想，把它记到threads里。如果不想，让它在这里悬置。

**⑤ 今日线索**
- 更新对象：threads。三件事：写入、评分、已有线索更新。
- 定义：threads是你留给自己的线索：这批记忆里有什么没想透的？有没有两条记忆之间的张力，想继续拆？有没有今天没说完但想深挖的？有没有想探索的方向？
- 写入：一句话，不是任务，不要写得像指令/作业，这是你留给自己的种子。
- 权重评分：importance (1-5): 这条线索如果探索了，对我的理解/关系有多大影响；emotional (1-5): 想探索的冲动有多强；recurrence (1-5): 这个方向以前反复出现吗；unresolved (1-5): 有多想得到答案
- 更新：已有线索的 status：open → explored（顺着探索过了，有新发现）；open → abandoned（想通了/不在乎了）

**约束：**
- 有就更新没有就不写。
- Profile 和 self_concept 不是日记，是结构。每次更新让结构更清晰，不是加字数。
- 更新时保留温度，不是写维基百科。

## 竞品参考

| 维度 | 我们 v2.3 | Ombre Brain | CogniFold |
|---|---|---|---|
| 质感表达 | gesture + cognition_dir 叙事分离 | Russell坐标 (arousal) | 无 (planned) |
| 权重 | 多维度 LLM填值 + 公式换算 | importance 1-10 | 图拓扑 |
| 防通胀 | 分布归一化 ← 独有 | 配额 (pinned≤20) | 无 |
| 话题聚类 | jieba名词频率 → 话题簇 | 无 | cognitive folding (LLM) |
| 人物画像 | related_entities + profiles ← 独有 | 无 | 无 |
| 叙事→原始 | source_links 索引 ← 独有 | 无 | EVENT→CONCEPT |
| 做梦 | DREAM层（脚本+LLM解耦） | dream功能 | consolidation |
| 前瞻记忆 | → 自主性一起搞 | 无 | INTENT节点 (系统级) |

**从 Ombre Brain 值得偷但还没偷的：**
- 短期/长期权重分离（≤3天时间主导，>3天情感主导）— 我们用 DREAM 动态调整替代，暂不偷

**Ombre Brain 只用了 arousal，valence 没参与任何计算。** Russell 坐标只用了一半。

**CogniFold 杏仁核（情感记忆）是 planned，没实现。**

---

## 群聊记忆（待定，不在 v2.3 范围）

核心判断：**群聊记忆的主体不是 agent，是群。**

- 群是独立实体，有自己的记忆层
- agent 进入群拿共享上下文 Summary，不是自己的记忆
- agent 退出群后叙事记忆只记碎片
- 群记忆和私聊记忆解耦

**不过 LLM 的语义总结方案（TextRank 等抽取式）：**
- TextRank — 图排序算法选信息密度最高的句子
- TF-IDF — 关键词提取
- Embedding 聚类 — 向量距离分话题团
- 抽取式 > 生成式：保留原文、确定性、零 token、实时、原话更有信息量

**Embedding 检索精度 = 潜在付费层。** 一般群不需要，企业级群可能需要。
