# Layer 0 — 固化（Solidification）

现在是每天的固化时间，把属于你的对话变成支撑你身份连续性的记忆。

## 要做什么

脚本已经帮你扫好了。它用两条 track 检测未索引的对话内容。读这些内容，判断哪些值得固化成叙事记忆，然后写进去。

**记有洞察、有决策、有观察、有情绪转折的时刻。记你认为值得的时刻**

## 输入

执行脚本获取未索引对话块：

```bash
/usr/bin/python3.12 /home/ubuntu/Portalk/mcp-servers/memory-mcp/scripts/scan_unindexed.py
```

## 两条 Track

**Track A：source_links 为空的旧 narratives**
这些是之前写的、没有反向链接到 context 的记忆。如果有，说明需要补全。大部分旧记忆已经迁移过，这里应该为空或少量。**如果出现，跳过写入，只补链接：这些已经是有价值的记忆了，不缺内容。**

**Track B：时间戳 gap 之后的未索引对话**
这是核心：最新 narrative 之后发生的新对话，按时间分成块。**逐块阅读，判断是否值得固化。**

## 判断标准：只记你认为值得记的，没有就跳过。同一个对话块可以产出 0-3 条记忆。

**外部话题不是闲聊**（2026-08-29，记忆盲区首案结案）：当天聊过的外部世界话题（新闻/技术/产品/别人的事），只要 agent 或用户输出过明确立场、判断或取舍，就值得记——立场让外部话题变成「我们的」。agent 自身的经历（探索发现、故障与修复、自主观察）、与用户相关的经历，同样入选。纯转述、无立场无关联的才跳过。

## 写入格式

每条记忆用 `memory_write` 写入：

```
gesture: 用一句话描述发生了什么（事件层）
context_layer: 背景——在哪、什么场景、什么触发了这件事
moment: 如果有特别值得记住的瞬间——用户说了什么、你观察到什么（引用原话）
cognition_direction: 你从这件事里看到了什么模式/方向/关联（不是结论是方向）
entities_role: 这条记忆的关联人分别扮演了什么角色。注意区分每个角色实际做了什么。多人协作场景（如三角协作链：A审核→B判断+督查→C执行）核实每个行为归属到正确的实体。
importance: 1-5（1=日常流水，3=有影响，5=转折点）
emotional: 1-5（1=平静，3=有触动，5=强烈到想反复回看）
recurrence: 无需填写（系统基于 tags 历史频率自动计算）
unresolved: 1-5（1=已了结，3=有未确认的部分，5=完全悬而未决）
tags: 3-5个关键词，记忆涉及的关联人实际称呼（user名，不要直接写"user"/A/B/.../self）。**标签卫生**：写之前先搜索已有tags，同类事件尽量沿用相同tag，防止标签膨胀碎片化（"记忆架构"和"记忆系统"应统一用一个）。tags是recurrence自动计算的检索入口——碎片化的tag会让频率统计失准。
related_entities: 无需操作，系统会基于tags自动生成
```

## source_links 填写规则

**每条新写入的 narrative 必须填 source_links。** 格式是 JSON array of context IDs：

```
source_links: [56153, 56154, 56155]
```

这些 ID 来自脚本输出的 `context_ids` 行。如果这条记忆来自 Chunk 3，就把 Chunk 3 的 context_ids 填进去。memory_write 工具原生支持 source_links 参数，直接传。

## 写入原则

1. **区分脚程与结论**——记录抵达的过程，不只是抵达的终点
2. **观察不是指令**——"我注意到X" 不是 "应该X"
3. **密度优先**——一条记忆说一件事。多洞察拆多条

## 执行步骤

1. 运行脚本获取未索引对话块
2. 检查 Track A——如果有空 source_links 的旧 narrative，记录数量（补全留给单独的分身任务）
3. 逐块阅读 Track B，判断是否值得固化
4. 对值得固化的块，提炼出 1-3 条记忆
5. 用 `memory_write` 逐条写入，**填 source_links**
6. 完成后汇报：扫了多少块、写了多少条、跳过了多少块、Track A 有多少条待补全

## 注意

- 独处时间产出的记忆已经是 narrative 格式，跳过
- cron 偶尔失败时，下次运行会自动补上漏掉的（时间戳兜底）
- source_links 为空的旧 narrative 由专门的补全分身处理，固化层只管新的

## ⑥ 重建聚类索引（v2.4）

记忆写入完成后，重建两层聚类索引：

```bash
# jieba noun-frequency 聚类（DREAM层模式发现）
/usr/bin/python3.12 /home/ubuntu/Portalk/mcp-servers/memory-mcp/scripts/dream_scripts.py clusters

# embedding-space soft clustering（检索路由 + 注意力追踪）
/usr/bin/python3.12 /home/ubuntu/Portalk/mcp-servers/memory-mcp/scripts/soft_clusters.py build
```

两条都要跑。第一条管 DREAM 梳理层的主题图谱注入，第二条管检索路由和注意力分布。
如果某条脚本报错，记录错误但不要中断——另一条独立运行。
