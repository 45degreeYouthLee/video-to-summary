"""
main.py
B站视频 AI 总结 - 命令行入口（无需启动 Web 服务）

与 app.py 的区别：
- app.py：Flask Web 服务，提供前后端交互、任务队列、批量导入、SSE 实时推送。
- main.py：纯命令行工具，适合在终端一次性处理单个 URL 或批量 URL 列表，
          直接输出 Markdown 文件到 output/ 目录，不依赖数据库和浏览器。

用法:
    单视频:   python main.py "https://www.bilibili.com/video/BVxxxx"
    批量:     python main.py urls.txt
    干运行:   python main.py "..." --dry-run
    输出:     ./output/ 目录下生成 Markdown 文件
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent / "src"))

from subtitle_extractor import extract_transcript
from volcengine_client import GenericChatClient, GenericASRClient


CONFIG_PATH = Path("config.yaml")


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[错误] 找不到配置文件: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sanitize_filename(text: str) -> str:
    """生成安全的文件名"""
    text = re.sub(r"[^\w一-鿿-]", "_", text)
    return text.strip("_")[:60]


def get_video_id(url: str) -> str:
    """从 B 站 URL 提取 BV/AV 号（支持标准视频页和 list/watchlater 参数）"""
    match = re.search(r"/(BV\w+|av\d+)", url)
    if match:
        return match.group(1)
    m_bvid = re.search(r"[?&]bvid=(BV[a-zA-Z0-9]+)", url, re.IGNORECASE)
    if m_bvid:
        return m_bvid.group(1)
    m_av = re.search(r"[?&]av=(av\d+)", url, re.IGNORECASE)
    if m_av:
        return m_av.group(1)
    # 兜底用 URL hash
    return hashlib.md5(url.encode()).hexdigest()[:8]


def normalize_bili_url(url: str) -> str:
    """把 BV/av 号或 B站列表 URL 归一化为标准视频 URL"""
    url = url.strip()
    if re.match(r"^(BV[a-zA-Z0-9]+)$", url, re.IGNORECASE):
        return f"https://www.bilibili.com/video/{url}"
    if re.match(r"^(av\d+)$", url, re.IGNORECASE):
        return f"https://www.bilibili.com/video/{url}"
    if re.match(r"^https?://(www\.)?bilibili\.com/(list|watchlater)/", url, re.IGNORECASE):
        m_bvid = re.search(r"[?&]bvid=(BV[a-zA-Z0-9]+)", url, re.IGNORECASE)
        if m_bvid:
            return f"https://www.bilibili.com/video/{m_bvid.group(1)}"
        m_av = re.search(r"[?&]av=(av\d+)", url, re.IGNORECASE)
        if m_av:
            return f"https://www.bilibili.com/video/{m_av.group(1)}"
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"https://{url}"
    return url


def process_single(url: str, config: dict, dry_run: bool = False) -> str:
    """处理单个视频，返回 Markdown 总结文本"""
    url = normalize_bili_url(url)
    video_id = get_video_id(url)
    print(f"\n{'='*50}")
    print(f"[处理] {url} (ID: {video_id})")
    print("=" * 50)

    # 1. 提取字幕
    transcript, source = extract_transcript(url, config)

    # 2. 无字幕 + 启用了 ASR → 语音识别
    if not transcript and source == "audio":
        asr_cfg = config.get("asr", {})
        if asr_cfg.get("enabled"):
            print("[信息] 调用 ASR 进行语音识别...")
            asr = GenericASRClient(
                api_key=asr_cfg["api_key"],
                base_url=asr_cfg["base_url"],
                model=asr_cfg.get("model", "mimo-v2.5-asr"),
                language=asr_cfg.get("language", "auto"),
            )
            # audio 文件已在 extract_transcript 时生成到 temp 目录
            temp_dir = Path(config["paths"]["temp_dir"])
            audio_files = sorted(temp_dir.glob("audio_*.mp3"), key=os.path.getmtime)
            if audio_files:
                transcript = asr.transcribe(audio_files[-1])
            else:
                return f"[跳过] 无字幕且无法提取音频: {url}"
        else:
            return (
                f"[跳过] 该视频无 CC 字幕。如需处理无字幕视频，\n"
                f"       请在 config.yaml 中启用并配置 asr，"
                f"       或改用其他 ASR 工具。"
            )

    if not transcript.strip():
        return f"[跳过] 未获取到任何文本内容: {url}"

    print(f"[信息] 字幕来源: {source}")
    print(f"[信息] 提取文本长度: {len(transcript)} 字符")

    if dry_run:
        print("[干运行] 跳过 API 调用")
        return transcript[:500] + "\n... (干运行截断)"

    # 3. 调用通用 Chat API 总结
    summary_cfg = config["summary"]
    client = GenericChatClient(
        api_key=summary_cfg["api_key"],
        base_url=summary_cfg["base_url"],
        model=summary_cfg["model"],
    )

    summary = client.summarize(
        transcript=transcript,
        system_prompt=summary_cfg["system_prompt"],
        user_template=summary_cfg["user_prompt_template"],
        max_tokens=summary_cfg["max_tokens"],
        temperature=summary_cfg["temperature"],
    )

    # 4. 组装 Markdown
    md = f"""# 视频总结

**原链接**: {url}
**视频 ID**: {video_id}
**字幕来源**: {source}
**提取时间**: {__import__('datetime').datetime.now().isoformat()}

---

## 核心总结

{summary}

---

## 原始字幕（前 2000 字预览）

```
{transcript[:2000]}
```

<details>
<summary>点我看完整字幕（{len(transcript)} 字）</summary>

```
{transcript}
```

</details>
"""
    return md


def save_result(url: str, md_text: str, config: dict):
    """保存结果到 output 目录"""
    out_dir = Path(config["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    video_id = get_video_id(url)
    # 尝试从 markdown 第一行提取标题，否则用 video_id
    first_line = md_text.splitlines()[0] if md_text.startswith("# ") else ""
    title = first_line.lstrip("# ").strip() if first_line else video_id
    fname = sanitize_filename(f"{title}_{video_id}") + ".md"
    fpath = out_dir / fname

    # 避免覆盖，追加序号
    counter = 1
    original = fpath
    while fpath.exists():
        fpath = original.with_name(f"{original.stem}_{counter}{original.suffix}")
        counter += 1

    fpath.write_text(md_text, encoding="utf-8")
    print(f"[保存] {fpath}")
    return fpath


def main():
    parser = argparse.ArgumentParser(description="B站视频 AI 总结工具")
    parser.add_argument("input", help="B站视频 URL 或包含 URL 列表的 txt 文件")
    parser.add_argument("--dry-run", action="store_true", help="只提取字幕不调用 API")
    parser.add_argument("--batch-delay", type=float, default=1.0, help="批量模式间隔秒数")
    args = parser.parse_args()

    config = load_config()

    # 检查 API Key 占位符
    summary_key = config.get("summary", {}).get("api_key", "")
    if not summary_key or "YOUR_" in summary_key:
        print("[错误] 请在 config.yaml 的 summary 节点中填入真实的 API Key")
        sys.exit(1)

    # 收集 URL 列表
    input_path = Path(args.input)
    if input_path.exists() and input_path.suffix == ".txt":
        urls = [
            line.strip()
            for line in input_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        print(f"[批量模式] 共 {len(urls)} 个视频")
    else:
        urls = [args.input]

    # 逐个处理
    for url in urls:
        try:
            md_text = process_single(url, config, dry_run=args.dry_run)
            if not args.dry_run:
                save_result(url, md_text, config)
            else:
                print(md_text)
        except Exception as e:
            print(f"[错误] 处理失败: {e}")
            continue

    print(f"\n[完成] 结果保存在: {Path(config['paths']['output_dir']).resolve()}")


if __name__ == "__main__":
    main()
