# BV视频 AI 总结工具

> 授人以渔版工作流：yt-dlp 提取字幕 → OpenAI 兼容 API 生成结构化总结 → Web 界面可视化管理

## 两种使用方式

| 方式 | 适用场景 | 命令 |
|---|---|---|
| **Web 界面**（推荐） | 日常高频使用、可视化管理、实时查看进度 | `python app.py` |
| **命令行** | 批量脚本、CI/CD、定时任务 | `python main.py urls.txt` |

---

## Web 界面核心能力

- **实时任务看板**：左侧任务列表 + 右侧详情，状态实时推送（SSE）
- **视频信息关联**：同一面板内展示总结 / 字幕 / 元信息，一目了然
- **一键复制**：独立按钮复制总结、复制字幕，不用手动框选
- **搜索与筛选**：按状态（待处理/完成/失败）筛选，关键词搜索标题或 UP 主
- **配置可视化**：弹窗内直接修改总结模型、ASR 设置、Prompt 模板
- **错误重试**：失败任务一键重试，不用重新输入 URL
- **ASR 集成**：无字幕视频自动走语音识别（需在设置中启用）

---

## 快速开始（Web 界面版）

### 1. 环境准备

将 `video-ai-summary` 文件夹放到任意位置，例如 `D:/Tools/video-ai-summary`。

安装依赖（首次）：

```powershell
cd D:/Tools/video-ai-summary
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

### 2. 填入 API Key

编辑 `config.yaml`：

```yaml
summary:
  base_url: "https://token-plan-cn.xiaomimimo.com/v1"  # 或任意 OpenAI 兼容端点
  api_key: "sk-xxxx-xxxxxxxx"                            # ← 总结模型 API Key
  model: "mimo-v2.5"                                     # 例如：mimo-v2.5、DeepSeek-V3 等

asr:
  enabled: true
  base_url: "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
  api_key: "sk-xxxx-xxxxxxxx"                            # ← ASR API Key（无字幕视频用）
```

> 当前后端使用 `GenericChatClient` 与 `GenericASRClient`，兼容任何 OpenAI API 格式的服务商。

### 3. 启动 Web 服务

```powershell
.\venv\Scripts\python app.py
```

浏览器自动访问（或手动打开）：**http://localhost:5000**

---

## Web 界面使用指南

### 新建任务

1. 点击左上角「新建任务」
2. 粘贴 B 站视频 URL（如 `https://www.videovideo.com/video/BV1xx411c7mD`）
3. 可选：勾选「强制使用语音识别」（跳过字幕下载，直接用 ASR）
4. 点击「开始处理」

### 查看结果

- **左侧列表**：显示所有任务，按创建时间倒序，彩色状态标签区分进度
- **点击任务**：右侧展示详情
  - **顶部**：视频链接、BV号、状态、操作按钮（打开视频 / 重试 / 删除）
  - **AI 总结**：Markdown 渲染的结构化总结，带「复制总结」按钮
  - **原始字幕**：可折叠面板，显示提取的字幕全文，带「复制字幕」按钮
  - **元信息**：来源（subtitle / audio_asr）、使用模型、时间戳

### 管理任务

- **搜索**：左侧面板顶部输入框，实时过滤标题 / UP主
- **筛选**：全部 / 待处理 / 完成 / 失败 四个快捷标签
- **重试**：失败任务右侧出现「重试」按钮，点击后重新跑完整流程
- **删除**：删除按钮清空该任务及数据

### 修改配置

点击左下角「设置」按钮，弹窗内可改：
- 总结模型 API Key、base_url、模型名
- ASR 开关、Key、base_url、语言（auto/zh/en）
- System Prompt 与 User Prompt Template（实时生效，改完下次新建任务即用）

---

## 命令行版

如需批量脚本或定时任务，命令行版仍可用：

```powershell
# 单视频
.\venv\Scripts\python main.py "https://www.videovideo.com/video/BV1xx411c7mD"

# 批量
.\venv\Scripts\python main.py urls.txt

# 干运行（只提取字幕不调用 API）
.\venv\Scripts\python main.py "https://..." --dry-run
```

`main.py` 与 `app.py` 共用同一套 `config.yaml` 配置（`summary`/`asr` 节点）。

---

## 文件结构

```
video-ai-summary/
├── app.py                  # Flask 后端 + Web 服务
├── index.html              # 前端单页应用 (SPA)
├── main.py                 # 命令行入口
├── run.bat                 # Windows 一键脚本（命令行版）
├── config.yaml             # 配置：API Key、Prompt、清洗规则
├── requirements.txt        # Python 依赖
├── src/
│   ├── subtitle_extractor.py   # yt-dlp 字幕/音频提取 + 清洗
│   ├── volcengine_client.py    # 通用 OpenAI 兼容 Chat / ASR 客户端
│   └── volc_seed_asr.py        # 火山 SeedASR WebSocket 客户端（可选）
├── temp/                    # 临时字幕/音频（保留空目录）
└── output/                  # 命令行版输出目录（保留空目录）
```

---

## 高频场景配置建议

### 场景 1：知识类/科普视频（信息密度高）

修改 `config.yaml` 的 `summary_prompt.user_prompt_template`：

```yaml
user_prompt_template: |
  以下是科普视频字幕，请生成学术阅读笔记风格总结：
  1. 核心论点与支撑证据；
  2. 关键概念定义；
  3. 作者立场与潜在局限；
  4. 可延伸阅读的 3 个方向。
  {transcript}
```

### 场景 2：教程/软件操作视频

```yaml
user_prompt_template: |
  以下是软件教程字幕，请生成实操手册：
  1. 前置条件与环境要求；
  2. 分步骤操作流（Step 1/2/3）；
  3. 每个步骤的界面截图提示（如有）；
  4. 常见报错与解决。
  {transcript}
```

### 场景 3：播客/访谈（口语化严重）

增加口癖清洗，并调高合并间隔：

```yaml
cleaning:
  filler_words:
    - "[（(]嗯[）)]"
    - "[（(]这个[）)]"
    - "[（(]那个[）)]"
    - "[（(]然后[）)]"
    - "[（(]就是[）)]"
    - "[（(]对吧[）)]"
    - "[（(]所以[）)]"
  merge_gap_seconds: 5   # 播客停顿长，多合并
```

---

## 无字幕视频怎么办？

Web 界面已内置 ASR 支持，按以下步骤启用：

1. 打开 http://localhost:5000
2. 点击左下角「设置」
3. 在「ASR」区域：
   - 勾选「启用 ASR」
   - 填入你的 ASR API Key（支持 OpenAI 兼容音频识别接口，如 MiMo-V2.5-ASR）
   - 语言建议选 `auto` 或 `zh`
4. 保存设置

处理流程：
- 有 CC 字幕的视频：自动下载字幕 → 清洗 → 总结（最快）
- 无字幕的视频：自动提取音频 → ASR 识别 → 清洗 → 总结

---

## 原理与授人以渔

这套工作流的核心价值不在代码量，而在于**流程可拆解、每步可替换**：

| 环节 | 当前实现 | 可替换方案 |
|---|---|---|
| 字幕获取 | yt-dlp | 浏览器插件、手动 F12 Network 抓取、videovideo API |
| 语音识别 | OpenAI 兼容 ASR（如 MiMo-V2.5-ASR、火山 SeedASR） | Whisper、剪映、飞书妙记 |
| 文本清洗 | Python 正则 | Node.js 脚本、AI 辅助清洗 |
| 总结模型 | OpenAI 兼容 Chat API（如 MiMo-V2.5、DeepSeek、火山方舟） | Claude、GPT-4、本地 Ollama |
| 输出格式 | Markdown / Web UI | Notion API、飞书多维表格、Excel |

---

## 常见问题

**Q: yt-dlp 下载失败或解析不出 B 站？**
A: 保持 yt-dlp 最新：`pip install -U yt-dlp`。若仍失败，尝试 `yt-dlp --update-to nightly`。

**Q: ASR 返回错误？**
A: 确认 API Key 有效且余额充足。ASR 支持 mp3/wav 格式，音频过大时可先用 ffmpeg 压缩：`ffmpeg -i input.mp3 -ar 16000 -ac 1 output.mp3`。

**Q: 字幕是繁体或外语怎么办？**
A: `subtitle_extractor.py` 中 `preferred_langs` 列表已按 `zh-CN → zh-Hans → zh-TW → zh-Hant → zh → en` 优先级尝试。

**Q: API 返回内容太长被截断？**
A: 调高 `config.yaml` 中 `summary_prompt.max_tokens`（例如 4096）。若字幕超长，可在 `config.yaml` 中调整 `summary.max_context_tokens`，`app.py` 会自动按 token 分段总结后合并。

**Q: 我想定时每天早上自动总结收藏夹视频？**
A: 可用 Windows 任务计划程序定时执行 `main.py urls.txt`，配合 B 站收藏夹 API 抓取 URL。此扩展需额外开发爬虫模块。

---

## License

MIT — 随意改造，欢迎PR。
