# MCP Remote — 自然语言遥控器 + 背景音乐模块

## 架构

```
聊天消息 / cron 输出
        ↓
  mcp-remote.py (常驻轻量进程)
        ↓
   匹配规则 (关键词/正则)
        ↓
  ┌─────┴─────┐
  ↓           ↓
直接调MCP    背景音乐播放器
(portalk)    (SomaFM/本地mp3)
```

## 两个模块

### 1. 遥控器 (mcp-remote.py)
- 常驻进程，监听一个 input 文件: `~/.hermes/mcp_remote/inbox.txt`
- 匹配规则文件: `~/.hermes/mcp_remote/rules.json`
- 任何人（人/cron/agent）写一行自然语言到 inbox → 进程匹配 → 直接执行
- 不经过 LLM，不消耗 token
- 执行完清空 inbox

### 2. 背景音乐 (bg-music.py)
- 独立常驻进程
- 支持 SomaFM 流媒体 + 本地音乐文件
- HTTP API: play/stop/next/status
- 人和 agent 都能通过 HTTP 切歌
- 音乐在服务端播放（VPS 本地），不依赖谁在对话
