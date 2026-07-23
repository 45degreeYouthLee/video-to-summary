"""
app.py
B站视频 AI 总结 - Web 前端管理界面 (Flask)

用法:
    python app.py
    然后浏览器打开 http://localhost:5000
"""

import copy
import hashlib
import http.cookiejar as cookiejar
import json
import math
import os
import re
import sqlite3
import sys
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
import yaml
from flask import Flask, jsonify, request, Response, send_from_directory
from werkzeug.utils import secure_filename
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent / "src"))

from subtitle_extractor import SubtitleExtractor, extract_transcript as _extract_transcript_raw
from volcengine_client import GenericASRClient, GenericChatClient
from volc_seed_asr import VolcSeedASRClient

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ===================== 配置 =====================
CONFIG_PATH = Path("config.yaml")
DB_PATH = Path("data.db")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)


# ===================== 数据库 =====================
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            video_id TEXT,
            title TEXT,
            thumbnail TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            source TEXT,
            transcript TEXT,
            summary TEXT,
            model TEXT,
            audio_path TEXT,
            created_at TEXT,
            updated_at TEXT,
            error TEXT
        )
    """)
    conn.commit()
    conn.close()


def migrate_db():
    """自动迁移：为旧表添加新字段"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("PRAGMA table_info(jobs)")
    columns = [row[1] for row in c.fetchall()]
    if "title" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN title TEXT")
    if "thumbnail" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN thumbnail TEXT")
    if "description" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
    if "uploader" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN uploader TEXT")
    if "duration" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN duration INTEGER")
    if "audio_path" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN audio_path TEXT")
    conn.commit()
    conn.close()


def db_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def job_to_dict(row):
    return {k: row[k] for k in row.keys()}


def _estimate_tokens(text: str) -> int:
    """
    简易 token 估算：
    - CJK 字符按 1 token
    - 英文单词按 1 token
    - 其他字符按 0.5 token
    不精确但足够判断是否超长。
    """
    if not text:
        return 0
    cjk_chars = len(re.findall(r'[一-鿿]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    other_chars = len(text) - cjk_chars - sum(len(w) for w in re.findall(r'[a-zA-Z]+', text))
    return int(cjk_chars + english_words + max(0, other_chars) * 0.5)


def _split_transcript_into_chunks(transcript: str, max_chunk_tokens: int) -> list[str]:
    """按段落将字幕切分为多个 chunk，每个 chunk 不超过 max_chunk_tokens"""
    paragraphs = [p.strip() for p in transcript.split('\n') if p.strip()]
    chunks = []
    current = []
    current_tokens = 0
    for p in paragraphs:
        p_tokens = _estimate_tokens(p)
        if current_tokens + p_tokens > max_chunk_tokens and current:
            chunks.append('\n'.join(current))
            current = [p]
            current_tokens = p_tokens
        else:
            current.append(p)
            current_tokens += p_tokens
    if current:
        chunks.append('\n'.join(current))
    return chunks


def _summarize_long_transcript(
    client,
    transcript: str,
    config: dict,
    max_context_tokens: int,
):
    """
    分段总结：超长字幕拆分为多个片段，分别总结后合并为最终总结。
    避免单次请求超过模型上下文限制。
    """
    summary_cfg = config.get("summary", {})
    system_prompt = config["summary_prompt"]["system_prompt"]
    user_template = config["summary_prompt"]["user_prompt_template"]
    max_tokens = config["summary_prompt"].get("max_tokens", 2048)
    temperature = config["summary_prompt"].get("temperature", 0.3)

    chunks = _split_transcript_into_chunks(transcript, max_context_tokens)
    print(f"[总结] 字幕较长，拆分为 {len(chunks)} 段分别总结")

    chunk_prompt = "你是一位擅长提炼长视频核心信息的编辑。请严格根据以下视频字幕片段，提炼核心要点（3-6 条），不要编造片段外的信息。\n\n【字幕片段】\n{transcript}"
    chunk_summaries = []
    for idx, chunk in enumerate(chunks):
        print(f"[总结] 处理第 {idx + 1}/{len(chunks)} 段，约 {_estimate_tokens(chunk)} tokens")
        s = client.summarize(
            transcript=chunk,
            system_prompt=system_prompt,
            user_template=chunk_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        chunk_summaries.append(s)

    combined = "\n\n".join(
        f"## 片段 {idx + 1} 总结\n{s}" for idx, s in enumerate(chunk_summaries)
    )
    final_prompt = (
        "你是一位擅长整合长视频内容的编辑。以下是多个视频片段的总结，"
        "请整合成一份结构完整、逻辑清晰、不重复的最终总结。"
        "\n\n【片段总结】\n{transcript}"
    )
    print(f"[总结] 合并 {len(chunk_summaries)} 个片段总结，生成最终总结")
    return client.summarize(
        transcript=combined,
        system_prompt=system_prompt,
        user_template=final_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ===================== 任务处理 =====================
def process_job(job_id: str, url: str, force_asr: bool = False, resume_from: str = "start"):
    """后台线程处理单个任务：字幕优先 → ASR 备用 → 总结

    Args:
        resume_from: 'start' 从头开始, 'asr' 跳过字幕提取直接用已有音频,
                     'summary' 跳过提取/ASR 直接重新总结
    """
    url = _normalize_bili_url(url)
    conn = db_conn()
    try:
        config = load_config()
        video_id = _get_video_id(url)

        # 尽早保存 video_id 和视频元信息
        extractor = SubtitleExtractor(
            yt_dlp_path=config["paths"]["yt_dlp_path"],
            temp_dir=config["paths"]["temp_dir"],
            cookies_options=config.get("bilibili", {}),
        )
        video_info = extractor.get_video_info(url)
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE jobs SET video_id=?, title=?, thumbnail=?, description=?, uploader=?, duration=?, updated_at=? WHERE id=?",
            (
                video_id,
                video_info.get("title", ""),
                video_info.get("thumbnail", ""),
                video_info.get("description", ""),
                video_info.get("uploader", ""),
                video_info.get("duration"),
                now,
                job_id,
            ),
        )
        conn.commit()

        # 检查是否已有字幕（断点续传：直接总结）
        row = conn.execute(
            "SELECT transcript, source, audio_path FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        existing_transcript = row["transcript"] if row else None
        existing_source = row["source"] if row else None
        existing_audio_path = row["audio_path"] if row else None

        transcript, source = "", ""

        if resume_from == "summary" or (resume_from == "start" and existing_transcript and existing_transcript.strip()):
            # 重新生成总结，或从头开始时已有字幕直接总结
            if resume_from == "summary" and not (existing_transcript and existing_transcript.strip()):
                raise RuntimeError("没有可用的字幕，无法重新生成总结")
            print(f"[信息] 跳过音频提取/ASR，直接进行总结")
            transcript = existing_transcript
            source = existing_source or "cached"
        elif resume_from == "asr":
            # 跳过字幕提取，直接用已有音频进行 ASR（会覆盖已有字幕）
            if not existing_audio_path or not Path(existing_audio_path).exists():
                # 没有可用音频，降级为从头开始
                print("[警告] 未找到已保存音频，降级为从头开始")
                resume_from = "start"
            else:
                print(f"[信息] 使用已保存音频重新进行 ASR: {existing_audio_path}")
                transcript, source = _run_asr(existing_audio_path, job_id, config, conn)
        
        if resume_from == "start" and not transcript:
            # 阶段 1：尝试提取 CC 字幕
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                ("extracting", now, job_id),
            )
            conn.commit()

            try:
                transcript, source = _extract_transcript_raw(url, config, force_audio=force_asr)
            except Exception as e:
                # 字幕提取失败，但如果是强制 ASR 或无字幕，可能还在预期内
                if not force_asr:
                    print(f"[警告] 字幕提取失败或不存在: {e}")

            # 阶段 2：无字幕/强制 ASR，调用 ASR 提取字幕
            if not transcript:
                asr_cfg = config.get("asr", {})
                asr_enabled = (asr_cfg.get("enabled") or force_asr) and asr_cfg.get("api_key", "") and "YOUR_" not in asr_cfg.get("api_key", "")

                if not asr_enabled:
                    raise RuntimeError(
                        "该视频没有 CC 字幕。\n"
                        "如需处理无字幕视频，请在设置中启用 ASR 并填入有效的 API Key，"
                        "或在创建任务时勾选「强制 ASR」并确保 ASR Key 已配置。"
                    )

                conn.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                    ("asr_recognizing", now, job_id),
                )
                conn.commit()

                # 找到最新提取的音频文件
                temp_dir = Path(config["paths"]["temp_dir"])
                audio_files = sorted(
                    temp_dir.glob("audio_*.*"),
                    key=os.path.getmtime,
                    reverse=True,
                )
                if not audio_files:
                    raise RuntimeError(
                        "音频提取失败，无法找到音频文件。\n"
                        "常见原因：\n"
                        "1. 未安装 ffmpeg（音频转码需要）\n"
                        "2. B站视频需要登录 cookie 才能下载\n"
                        "3. 视频本身受地区/版权限制"
                    )

                audio_path = str(audio_files[0])
                conn.execute(
                    "UPDATE jobs SET audio_path=?, updated_at=? WHERE id=?",
                    (audio_path, datetime.now().isoformat(), job_id),
                )
                conn.commit()

                transcript, source = _run_asr(audio_path, job_id, config, conn)

        if not transcript or not transcript.strip():
            raise RuntimeError("未获取到任何文本内容，无法进行总结")

        # 阶段 3：调用 LLM 总结
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE jobs SET transcript=?, source=?, status=?, updated_at=? WHERE id=?",
            (transcript, source, "summarizing", now, job_id),
        )
        conn.commit()

        summary_cfg = config.get("summary", {})
        if not summary_cfg.get("api_key") or "YOUR_" in summary_cfg.get("api_key", ""):
            raise RuntimeError("总结模型 API Key 未配置，请在设置中填入")

        client = GenericChatClient(
            api_key=summary_cfg["api_key"],
            base_url=summary_cfg.get("base_url", "https://token-plan-cn.xiaomimimo.com/v1"),
            model=summary_cfg.get("model", "mimo-v2.5"),
        )
        used_model = summary_cfg.get("model", "mimo-v2.5")

        # 判断是否需要分段总结
        transcript_tokens = _estimate_tokens(transcript)
        max_context_tokens = summary_cfg.get("max_context_tokens", 6000)
        if transcript_tokens > max_context_tokens:
            summary = _summarize_long_transcript(
                client, transcript, config, max_context_tokens
            )
        else:
            summary = client.summarize(
                transcript=transcript,
                system_prompt=config["summary_prompt"]["system_prompt"],
                user_template=config["summary_prompt"]["user_prompt_template"],
                max_tokens=config["summary_prompt"]["max_tokens"],
                temperature=config["summary_prompt"]["temperature"],
            )

        # 完成
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE jobs SET summary=?, status=?, updated_at=?, model=? WHERE id=?",
            (summary, "completed", now, used_model, job_id),
        )
        conn.commit()

    except Exception as e:
        now = datetime.now().isoformat()
        error_msg = str(e)
        conn.execute(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
            ("failed", error_msg, now, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _get_audio_duration(audio_path: str) -> float:
    """用 ffprobe 获取音频时长（秒）"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[警告] 获取音频时长失败: {e}，假设为 30 分钟")
        return 1800  # 拿不到时长时保守取 30 分钟


def _compress_audio(audio_path: Path, duration_sec: float) -> Path:
    """根据音频时长动态压缩，返回压缩后路径。

    策略:
        < 30 分钟 → 32kbps 16kHz mono  (~7MB)
        >= 30 分钟 → 16kbps 16kHz mono  (~7MB, 上限约14MB@120min)
    """
    bitrate = "32k" if duration_sec < 1800 else "16k"
    compressed = audio_path.with_name(audio_path.stem + "_compressed" + audio_path.suffix)

    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ac", "1", "-ar", "16000", "-b:a", bitrate,
            "-map", "0:a", str(compressed),
        ], check=True, capture_output=True, timeout=120)

        orig_kb = audio_path.stat().st_size / 1024
        comp_kb = compressed.stat().st_size / 1024
        print(f"[信息] 音频压缩: {orig_kb:.0f}KB → {comp_kb:.0f}KB ({bitrate}, {duration_sec/60:.0f}分钟)")
        return compressed
    except subprocess.TimeoutExpired:
        print(f"[警告] 音频压缩超时(>120s)，回退到原始文件")
        return audio_path
    except Exception as e:
        print(f"[警告] 音频压缩失败: {e}，回退到原始文件")
        return audio_path


def _transcribe_single(audio_path: Path, config: dict) -> str:
    """发送单个音频文件给 ASR，返回识别文本"""
    asr_cfg = config.get("asr", {})
    asr_base_url = asr_cfg.get("base_url", "https://token-plan-cn.xiaomimimo.com/v1")
    print(f"[ASR] 调用 ASR 客户端: {asr_base_url}", flush=True)

    if asr_base_url.startswith("wss://") or asr_base_url.startswith("ws://"):
        asr = VolcSeedASRClient(
            api_key=asr_cfg["api_key"],
            resource_id=asr_cfg.get("model", "volc.seedasr.sauc.duration"),
            base_url=asr_base_url,
            segment_duration_ms=asr_cfg.get("segment_duration_ms", 1000),
            total_timeout=asr_cfg.get("total_timeout_seconds"),
            timeout_min_seconds=asr_cfg.get("timeout_min_seconds", 180),
            timeout_audio_multiplier=asr_cfg.get("timeout_audio_multiplier", 2.0),
            timeout_buffer_seconds=asr_cfg.get("timeout_buffer_seconds", 120),
        )
    else:
        asr = GenericASRClient(
            api_key=asr_cfg["api_key"],
            base_url=asr_base_url,
            model=asr_cfg.get("model", "mimo-v2.5-asr"),
            language=asr_cfg.get("language", "auto"),
        )
    return asr.transcribe(audio_path)


def _split_and_transcribe(audio_path: Path, duration_sec: float, config: dict) -> tuple[str, str]:
    """超长音频（>=120分钟）：切成 20 分钟一段，逐段 ASR，再组装。

    每段带 30 秒前后重叠防止切口处丢字，
    最终用去重函数处理重叠区域。
    """
    CHUNK_MINUTES = 20
    OVERLAP = 30  # 秒
    chunk_sec = CHUNK_MINUTES * 60
    total_chunks = math.ceil(duration_sec / chunk_sec)

    all_texts = []
    for i in range(total_chunks):
        start = i * chunk_sec
        end = min(start + chunk_sec + OVERLAP, duration_sec)
        if i == total_chunks - 1:
            end = duration_sec  # 最后一段不要尾部重叠

        chunk_path = audio_path.with_name(f"chunk_{i:03d}{audio_path.suffix}")
        try:
            # 切段 + 一步压缩到 16kbps
            subprocess.run([
                "ffmpeg", "-y", "-i", str(audio_path),
                "-ss", str(start), "-to", str(end),
                "-ac", "1", "-ar", "16000", "-b:a", "16k",
                "-map", "0:a", str(chunk_path),
            ], check=True, capture_output=True, timeout=120)

            print(f"[分片] ({i+1}/{total_chunks}) {start//60}-{int(end)//60} 分, "
                  f"{chunk_path.stat().st_size/1024:.0f}KB, 正在 ASR...")

            text = _transcribe_single(chunk_path, config)
            all_texts.append(text)
            print(f"[分片] ({i+1}/{total_chunks}) 完成，识别 {len(text)} 字")

        except Exception as e:
            print(f"[分片] ({i+1}/{total_chunks}) 失败: {e}")
            all_texts.append("")  # 占位，不让后续段落错位
        finally:
            try:
                chunk_path.unlink()
            except Exception:
                pass

    # 合并：直接拼接，用 deduplicate_asr_text 处理重叠区域，再分段
    raw = "\n".join(t for t in all_texts if t)
    transcript = SubtitleExtractor.deduplicate_asr_text(raw)
    transcript = SubtitleExtractor.segment_asr_text(transcript)
    print(f"[信息] 分片 ASR 合并完成: {sum(len(t) for t in all_texts)} 字 → 去重/分段后 {len(transcript)} 字")
    return transcript, "audio_asr"


def _run_asr(audio_path: str, job_id: str, config: dict, conn) -> tuple[str, str]:
    """执行 ASR 识别，含动态音频压缩 + 超长分片，返回 (transcript, source)"""
    audio_file = Path(audio_path)
    orig_size_kb = audio_file.stat().st_size / 1024
    asr_cfg = config.get("asr", {})

    # 获取音频时长
    duration = _get_audio_duration(str(audio_file))
    print(f"[信息] 音频: {audio_file.name}, {orig_size_kb:.0f}KB, 时长 {duration/60:.1f} 分钟")

    # 策略选择
    split_threshold_min = asr_cfg.get("split_threshold_minutes", 10)
    split_threshold_sec = split_threshold_min * 60
    if duration >= split_threshold_sec:
        print(f"[信息] 音频时长 {duration/60:.1f} 分钟，超过 {split_threshold_min} 分钟阈值，启用分片 ASR")
        return _split_and_transcribe(audio_file, duration, config)

    # < 阈值 → 压缩后单次 ASR
    print(f"[信息] 音频时长 {duration/60:.1f} 分钟，未超过分片阈值，单次 ASR")
    compressed = _compress_audio(audio_file, duration)
    try:
        transcript = _transcribe_single(compressed, config)
    finally:
        if compressed != audio_file:
            try:
                compressed.unlink()
            except Exception:
                pass

    # ASR 后处理：去除重复内容 + 分段
    original_len = len(transcript)
    transcript = SubtitleExtractor.deduplicate_asr_text(transcript)
    transcript = SubtitleExtractor.segment_asr_text(transcript)
    if len(transcript) < original_len * 0.7:
        print(f"[警告] ASR 结果存在大量重复，已从 {original_len} 字去重到 {len(transcript)} 字")
    print(f"[信息] ASR 识别完成，文本长度: {len(transcript)} 字符")
    return transcript, "audio_asr"


def _get_video_id(url: str) -> str:
    match = re.search(r"/(BV\w+|av\d+)", url)
    if match:
        return match.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:8]


def _check_api_config(config: dict, force_asr: bool = False) -> str | None:
    """检查 API 配置是否有效，返回 None=正常，str=错误信息

    说明：
    - 总结模型 API Key 必须始终配置
    - 如果启用了 ASR 或用户强制 ASR，则 ASR API Key 也必须配置
    """
    summary_cfg = config.get("summary", {})
    summary_key = summary_cfg.get("api_key", "")
    if not summary_key or "YOUR_" in summary_key:
        return "总结模型的 API Key 未配置，请在设置页填写"

    asr_cfg = config.get("asr", {})
    asr_enabled = asr_cfg.get("enabled", False)
    if force_asr or asr_enabled:
        asr_key = asr_cfg.get("api_key", "")
        if not asr_key or "YOUR_" in asr_key:
            return "ASR 语音识别的 API Key 未配置（当前已启用 ASR 或强制使用 ASR），请在设置页填写"
    return None


# ===================== API 路由 =====================
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/upload/cookies", methods=["POST"])
def upload_cookies():
    """接收前端上传的 cookies.txt，保存到项目目录并更新配置"""
    if "file" not in request.files:
        return jsonify({"error": "未找到文件"}), 400
    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    filename = secure_filename(file.filename)
    if not filename.endswith(".txt"):
        return jsonify({"error": "仅支持 .txt 文件"}), 400

    save_path = Path(filename)
    try:
        file.save(str(save_path))
        cfg = load_config()
        if "bilibili" not in cfg:
            cfg["bilibili"] = {}
        cfg["bilibili"]["cookies_file"] = str(save_path)
        save_config(cfg)
        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/thumbnail/<path:filename>")
def serve_thumbnail(filename):
    """提供本地缓存的封面图片"""
    config = load_config()
    temp_dir = Path(config["paths"]["temp_dir"])
    thumb_path = temp_dir / f"thumbnail_{filename}"
    if thumb_path.exists():
        return send_from_directory(str(temp_dir), f"thumbnail_{filename}")
    return "封面不存在", 404


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    status_filter = request.args.get("status")
    search = request.args.get("search", "").strip()
    conn = db_conn()
    sql = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if status_filter:
        sql += " AND status=?"
        params.append(status_filter)
    if search:
        sql += " AND (url LIKE ? OR title LIKE ? OR video_id LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify({"jobs": [job_to_dict(r) for r in rows]})


def _normalize_bili_url(url: str) -> str:
    """把 BV/av 号补全成完整 B站视频 URL"""
    url = url.strip()
    if re.match(r"^(BV[a-zA-Z0-9]+)$", url, re.IGNORECASE):
        return f"https://www.bilibili.com/video/{url}"
    if re.match(r"^(av\d+)$", url, re.IGNORECASE):
        return f"https://www.bilibili.com/video/{url}"
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return f"https://{url}"
    return url


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.get_json()
    raw_url = (data.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "URL 不能为空"}), 400
    url = _normalize_bili_url(raw_url)
    force_asr = data.get("force_asr", False)
    if not url or url == "https://":
        return jsonify({"error": "URL 不能为空"}), 400

    # 启动前检查 API 配置
    config = load_config()
    err = _check_api_config(config, force_asr)
    if err:
        return jsonify({"error": err}), 400

    job_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()

    conn = db_conn()
    conn.execute(
        "INSERT INTO jobs (id, url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, url, "pending", now, now),
    )
    conn.commit()
    conn.close()

    # 后台启动处理
    thread = threading.Thread(target=process_job, args=(job_id, url, force_asr), daemon=True)
    thread.start()

    return jsonify({"id": job_id, "status": "pending"})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    conn = db_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job_to_dict(row))


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    conn = db_conn()
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": True})


@app.route("/api/jobs/<job_id>/retry", methods=["POST"])
def retry_job(job_id):
    conn = db_conn()
    row = conn.execute("SELECT url FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404

    # 重试前检查 API 配置（至少总结模型必须可用）
    config = load_config()
    err = _check_api_config(config)
    if err:
        conn.close()
        return jsonify({"error": err}), 400

    url = row["url"]
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE jobs SET status=?, error=NULL, updated_at=? WHERE id=?",
        ("pending", now, job_id),
    )
    conn.commit()
    conn.close()

    thread = threading.Thread(target=process_job, args=(job_id, url), daemon=True)
    thread.start()
    return jsonify({"id": job_id, "status": "pending"})


@app.route("/api/jobs/<job_id>/retry-asr", methods=["POST"])
def retry_asr(job_id):
    """使用已保存的音频重新进行 ASR，不重新下载视频/字幕"""
    print(f"[API] 收到重新识别请求: job_id={job_id}", flush=True)
    conn = db_conn()
    row = conn.execute(
        "SELECT url, audio_path, transcript FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404

    url = row["url"]
    audio_path = row["audio_path"]
    if not audio_path or not Path(audio_path).exists():
        conn.close()
        return jsonify({"error": "未找到已保存的音频文件，无法重新识别"}), 400

    config = load_config()
    asr_cfg = config.get("asr", {})
    asr_key = asr_cfg.get("api_key", "")
    if not asr_key or "YOUR_" in asr_key:
        conn.close()
        return jsonify({"error": "ASR API Key 未配置，请在设置页填写"}), 400

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE jobs SET status=?, error=NULL, transcript=?, source=?, updated_at=? WHERE id=?",
        ("asr_recognizing", "", "", now, job_id),
    )
    conn.commit()
    conn.close()

    thread = threading.Thread(
        target=process_job,
        args=(job_id, url),
        kwargs={"resume_from": "asr"},
        daemon=True,
    )
    thread.start()
    return jsonify({"id": job_id, "status": "asr_recognizing"})


@app.route("/api/jobs/<job_id>/stream")
def job_stream(job_id):
    def event_stream():
        last_status = None
        for _ in range(600):  # 最多监听 10 分钟
            conn = db_conn()
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            conn.close()
            if not row:
                break
            data = job_to_dict(row)
            current_status = data["status"]
            if current_status != last_status or current_status in ("completed", "failed"):
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_status = current_status
                if current_status in ("completed", "failed"):
                    break
            time.sleep(1)

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        cfg = load_config()
        # 隐藏真实 API Key，只返回前 8 位（深拷贝，避免污染运行中的内存配置）
        safe = copy.deepcopy(cfg)
        for key in ["asr", "summary"]:
            if key in safe and "api_key" in safe[key]:
                ak = safe[key]["api_key"]
                if ak and "YOUR_" not in ak:
                    safe[key]["api_key"] = ak[:8] + "****"
        return jsonify(safe)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.get_json()
    try:
        cfg = load_config()
        # 只更新允许的字段
        if "asr" in data:
            for k, v in data["asr"].items():
                if k == "api_key" and v.endswith("****"):
                    continue  # 保留原值
                cfg["asr"][k] = v
        if "summary" in data:
            for k, v in data["summary"].items():
                if k == "api_key" and v.endswith("****"):
                    continue
                cfg["summary"][k] = v
        if "summary_prompt" in data:
            cfg["summary_prompt"].update(data["summary_prompt"])
        save_config(cfg)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== B站 API 工具 =====================

_WBI_KEYS_CACHE = {"img_key": "", "sub_key": "", "expires": 0}


# B站 Wbi 字符映射表（从 B站前端 JS 反编译）
_WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 37, 12, 52, 56, 31,
    7, 25, 20, 35, 48, 26, 40, 44, 6, 4, 65, 13, 38, 51, 55, 11,
    30, 1, 52, 34, 18, 19, 1, 24, 42, 60, 30, 48, 45, 47, 54, 17,
    63, 26, 49, 15, 3, 53, 14, 16, 33, 35, 5, 50, 7, 20, 21, 10,
    9, 44, 34, 28, 39, 55, 43, 13, 25, 46, 40, 41, 23, 36, 22, 2,
    11, 51, 31, 57, 60, 29, 12, 8, 37, 62, 6, 59, 4, 27, 38, 24,
]


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """用 B站字符映射表生成 Wbi mixin key"""
    raw = img_key + sub_key
    n = min(len(raw), len(_WBI_MIXIN_TABLE))
    result_chars = []
    for i in range(n):
        idx = _WBI_MIXIN_TABLE[i]
        if idx < len(raw):
            result_chars.append(raw[idx])
        else:
            # JavaScript 中越界访问返回 undefined 字符串
            result_chars.extend("undefined")
    mixin = "".join(result_chars)[:32]
    print(f"[调试] Wbi mixin key: {mixin}")
    return mixin


def _fetch_wbi_keys(session: requests.Session) -> tuple[str, str]:
    """获取 Wbi 签名密钥（带缓存，24h 过期）"""
    now = time.time()
    if now < _WBI_KEYS_CACHE["expires"] and _WBI_KEYS_CACHE["img_key"]:
        return _WBI_KEYS_CACHE["img_key"], _WBI_KEYS_CACHE["sub_key"]

    try:
        resp = session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            timeout=10,
        )
        data = resp.json()
        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        print(f"[调试] Wbi img_url: {img_url}")
        print(f"[调试] Wbi sub_url: {sub_url}")
        # 提取密钥：URL 路径中最后一个 / 之后、第一个 . 之前的字符串
        img_key = img_url.rstrip("/").split("/")[-1].split(".")[0] if img_url else ""
        sub_key = sub_url.rstrip("/").split("/")[-1].split(".")[0] if sub_url else ""
        print(f"[调试] Wbi img_key={img_key if img_key else '(空)'} ({len(img_key)}字)")
        print(f"[调试] Wbi sub_key={sub_key if sub_key else '(空)'} ({len(sub_key)}字)")
        _WBI_KEYS_CACHE["img_key"] = img_key
        _WBI_KEYS_CACHE["sub_key"] = sub_key
        _WBI_KEYS_CACHE["expires"] = now + 86400  # 缓存 24 小时
        print(f"[调试] Wbi 密钥获取成功")
        return img_key, sub_key
    except Exception as e:
        print(f"[警告] 获取 Wbi 密钥失败: {e}")
        return "", ""


def _sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """对参数进行 Wbi 签名，返回带 w_rid/wts 的新参数字典"""
    mixin_key = _get_mixin_key(img_key, sub_key)

    # 过滤空值、转字符串、按键名排序
    sorted_params = dict(sorted(
        {k: str(v) for k, v in params.items() if v is not None}.items()
    ))

    import urllib.parse
    query = urllib.parse.urlencode(sorted_params)
    sign_str = query + mixin_key
    wts = int(time.time())
    w_rid = hashlib.md5(sign_str.encode()).hexdigest()

    sorted_params["w_rid"] = w_rid
    sorted_params["wts"] = str(wts)
    print(f"[调试] Wbi 签名: query={query[:80]}..., wts={wts}, w_rid={w_rid}")
    return sorted_params


# ===================== 收藏夹批量导入 =====================

def _load_bilibili_session() -> requests.Session | None:
    """从配置的 cookies.txt 加载 B站登录态，返回 requests.Session
    使用 MozillaCookieJar 确保域名/路径正确匹配。
    """
    config = load_config()
    bili_cfg = config.get("bilibili", {})
    cookies_file = bili_cfg.get("cookies_file")

    if not cookies_file or not Path(cookies_file).exists():
        print(f"[警告] cookies 文件不存在: {cookies_file}")
        return None

    session = requests.Session()
    try:
        cj = cookiejar.MozillaCookieJar(cookies_file)
        cj.load()
        # Netscape 格式中特殊字符可能被 URL 编码，解码后 B站 API 才能识别
        for c in cj:
            c.value = unquote(c.value)
        session.cookies = cj
        cookies_list = list(cj)
        print(f"[调试] cookies 加载成功: {len(cookies_list)} 条")
        has_sessdata = any(c.name == "SESSDATA" and "bilibili.com" in c.domain for c in cookies_list)
        print(f"[调试] SESSDATA: {'存在' if has_sessdata else '❌ 缺失'}")
        if not has_sessdata:
            print("[警告] cookies.txt 中未找到 SESSDATA，请确认导出的是 B站 cookies")
            return None
    except Exception as e:
        print(f"[警告] 加载 cookies 失败: {e}")
        return None

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    })

    # 验证登录态
    try:
        nav = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        is_login = nav.get("data", {}).get("isLogin", False)
        uname = nav.get("data", {}).get("uname", "未知")
        mid = nav.get("data", {}).get("mid", 0)
        print(f"[调试] B站登录校验: {uname} (mid={mid}), isLogin={is_login}")
        if not is_login:
            print("[警告] cookies 已过期或无效，请重新登录 B站后导出 cookies.txt")
    except Exception as e:
        print(f"[警告] B站登录校验失败: {e}")

    return session


@app.route("/api/favorites")
def api_list_favorites():
    """获取当前用户的收藏夹列表（up_mid + Cookie 鉴权，无需 Wbi）"""
    session = _load_bilibili_session()
    if not session:
        return jsonify({"error": "未配置 B站 cookies，请在设置页上传 cookies.txt", "code": "no_cookie"}), 400

    try:
        # 从 nav 接口获取用户 mid
        nav = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        is_login = nav.get("data", {}).get("isLogin", False)
        uname = nav.get("data", {}).get("uname", "未知")
        mid = nav.get("data", {}).get("mid", 0)
        print(f"[调试] B站: {uname} (mid={mid}), isLogin={is_login}")
        if not is_login or not mid:
            return jsonify({"error": "B站 cookies 已过期，请重新导出 cookies.txt", "code": "login_expired"}), 401

        # 用 up_mid 参数获取收藏夹列表（2014年后接口，无需 Wbi）
        # type=2 为完整列表，包含默认收藏夹
        resp = session.get(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
            params={"up_mid": mid, "type": 2},
            timeout=10,
        )
        d = resp.json()
        print(f"[调试] list-all 响应: code={d.get('code')}")
        if d.get("code") != 0:
            return jsonify({"error": d.get("message", "B站 API 返回异常")}), 502

        folders = []
        for f in d.get("data", {}).get("list", []):
            folders.append({
                "id": f["id"],
                "title": f.get("title", ""),
                "count": f.get("media_count", 0),
            })
        print(f"[调试] 获取到 {len(folders)} 个收藏夹")

    except Exception as e:
        print(f"[错误] 获取收藏夹失败: {e}")
        return jsonify({"error": f"获取收藏夹失败: {e}"}), 502

    return jsonify({"folders": folders})


@app.route("/api/favorites/<int:folder_id>")
def api_list_favorite_videos(folder_id):
    """获取指定收藏夹内的所有视频（fav/resource/ids + view API，无需 Wbi）"""
    session = _load_bilibili_session()
    if not session:
        return jsonify({"error": "未配置 B站 cookies", "code": "no_cookie"}), 400

    videos = []
    try:
        # Step 1: 用 fav/resource/ids 获取 BV 列表（无需 Wbi）
        resp = session.get(
            "https://api.bilibili.com/x/v3/fav/resource/ids",
            params={"media_id": folder_id, "ps": 20},
            timeout=10,
        )
        d = resp.json()
        if d.get("code") != 0:
            return jsonify({"error": d.get("message", "获取视频列表失败")}), 502

        bv_ids = [item.get("bv_id", "") for item in d.get("data", []) if item.get("type") == 2]
        print(f"[调试] fav/resource/ids 获取到 {len(bv_ids)} 个 BV: {', '.join(bv_ids)}")

        # Step 2: 逐个获取视频详情（标题/封面/UP主/时长）
        for bv in bv_ids:
            try:
                view = session.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bv},
                    timeout=10,
                ).json()
                if view.get("code") == 0:
                    vdata = view["data"]
                    owner = vdata.get("owner", {}) or {}
                    videos.append({
                        "bv": bv,
                        "title": vdata.get("title", ""),
                        "cover": vdata.get("pic", ""),
                        "duration": vdata.get("duration", 0),
                        "uploader": owner.get("name", ""),
                    })
                else:
                    print(f"[警告] 获取视频详情失败 BV={bv}: {view.get('message')}")
                    videos.append({"bv": bv, "title": "", "cover": "", "duration": 0, "uploader": ""})
            except Exception as e:
                print(f"[警告] 获取视频详情异常 BV={bv}: {e}")
                videos.append({"bv": bv, "title": "", "cover": "", "duration": 0, "uploader": ""})

    except Exception as e:
        print(f"[错误] 获取收藏夹视频失败: {e}")
        return jsonify({"error": f"请求失败: {e}"}), 502

    return jsonify({"videos": videos, "total": len(videos)})


@app.route("/api/watchlater")
def api_list_watchlater():
    """获取当前登录用户的稍后再看列表（Cookie 鉴权，无需 Wbi）"""
    session = _load_bilibili_session()
    if not session:
        return jsonify({"error": "未配置 B站 cookies", "code": "no_cookie"}), 400

    try:
        resp = session.get(
            "https://api.bilibili.com/x/v2/history/toview",
            timeout=10,
        )
        d = resp.json()
        if d.get("code") != 0:
            return jsonify({"error": d.get("message", "获取稍后再看失败")}), 502

        videos = []
        for item in d.get("data", {}).get("list", []):
            owner = item.get("owner", {}) or {}
            videos.append({
                "bv": item.get("bvid", ""),
                "title": item.get("title", ""),
                "cover": item.get("pic", ""),
                "duration": item.get("duration", 0),
                "uploader": owner.get("name", ""),
            })
        print(f"[调试] 获取到 {len(videos)} 个稍后再看视频")

    except Exception as e:
        print(f"[错误] 获取稍后再看失败: {e}")
        return jsonify({"error": f"请求失败: {e}"}), 502

    return jsonify({"videos": videos, "total": len(videos)})


def _process_batch_sequential(job_ids: list[str], force_asr: bool = False):
    """逐个串行处理批量任务，上一个彻底完成才开始下一个"""
    total = len(job_ids)
    conn = db_conn()
    rows = {r["id"]: r["url"] for r in conn.execute(
        "SELECT id, url FROM jobs WHERE id IN ({})".format(
            ",".join("?" for _ in job_ids)
        ), job_ids
    ).fetchall()}
    conn.close()

    for idx, jid in enumerate(job_ids):
        url = rows.get(jid)
        if not url:
            print(f"[批处理] ({idx+1}/{total}) 跳过: {jid}（未找到）")
            continue
        print(f"[批处理] ({idx+1}/{total}) 开始: {jid}")
        try:
            process_job(jid, url, force_asr=force_asr)
            # process_job 内部会吞掉异常并写入 DB，所以这里需要查状态确认是否真的完成
            conn = db_conn()
            row = conn.execute("SELECT status, error FROM jobs WHERE id=?", (jid,)).fetchone()
            conn.close()
            if row and row["status"] == "completed":
                print(f"[批处理] ({idx+1}/{total}) 完成: {jid}")
            else:
                err = row["error"] if row and row["error"] else "（无错误信息）"
                print(f"[批处理] ({idx+1}/{total}) 失败: {jid} - {err}")
        except Exception as e:
            print(f"[批处理] ({idx+1}/{total}) 失败: {jid} - {e}")
            # process_job 自己会写 error 到 DB，这里只打日志


@app.route("/api/jobs/batch", methods=["POST"])
def create_jobs_batch():
    """批量创建任务，返回后逐个串行处理"""
    data = request.get_json()
    if not data or "videos" not in data:
        return jsonify({"error": "缺少 videos 参数"}), 400

    videos = data["videos"]  # [{bv, title, uploader, duration, cover}, ...]
    force_asr = data.get("force_asr", False)
    if not videos:
        return jsonify({"error": "videos 不能为空"}), 400

    # 批量创建前同样检查 API 配置
    config = load_config()
    err = _check_api_config(config, force_asr=force_asr)
    if err:
        return jsonify({"error": err}), 400

    conn = db_conn()
    now = datetime.now().isoformat()
    created = []
    try:
        for v in videos:
            bv = v.get("bv", "").strip()
            if not bv:
                continue
            job_id = str(uuid.uuid4())[:8]
            url = f"https://www.bilibili.com/video/{bv}"
            conn.execute(
                """INSERT INTO jobs
                   (id, url, video_id, title, thumbnail, uploader, duration,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, url, bv,
                 v.get("title", ""), v.get("cover", ""),
                 v.get("uploader", ""), v.get("duration"),
                 "pending", now, now),
            )
            created.append({"id": job_id, "url": url, "title": v.get("title", "")})
        conn.commit()
    finally:
        conn.close()

    if not created:
        return jsonify({"error": "没有有效的视频"}), 400

    # 后台串行处理
    threading.Thread(
        target=_process_batch_sequential,
        args=([c["id"] for c in created], force_asr),
        daemon=True,
    ).start()

    return jsonify({"jobs": created, "total": len(created)})


# ===================== 启动 =====================
if __name__ == "__main__":
    init_db()
    migrate_db()
    print("=" * 50)
    print("B站视频 AI 总结工具已启动")
    print("请打开浏览器访问: http://localhost:5000")
    print("=" * 50)
    try:
        # 使用 waitress 替代 Flask 默认服务器，SSE 更稳定
        from waitress import serve
        print("[服务器] 使用 waitress (生产级 WSGI 服务器)")
        serve(app, host="0.0.0.0", port=5000, threads=16, channel_timeout=300)
    except ImportError:
        print("[警告] 未安装 waitress，使用 Flask 默认服务器（SSE 可能不稳定）")
        print("建议执行: .\\venv\\Scripts\\pip install waitress")
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
