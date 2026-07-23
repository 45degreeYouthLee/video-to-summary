"""
subtitle_extractor.py
负责 yt-dlp 调用、字幕下载、SRT/VTT 清洗为纯文本、无字幕时提取音频
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class SubtitleExtractor:
    def __init__(self, yt_dlp_path="yt-dlp", temp_dir="./temp", cookies_options: dict | None = None):
        self.yt_dlp_path = yt_dlp_path
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_options = cookies_options or {}
        self._cookies_temp_file = None
        self._check_yt_dlp()

    def _prepare_cookies_file(self) -> str | None:
        """
        yt-dlp 使用 --cookies 时会尝试写回 cookies 文件，可能导致 PermissionError。
        这里把用户提供的 cookies.txt 复制到 temp 目录的可写副本，避免污染原文件。
        """
        cookies_file = self.cookies_options.get("cookies_file")
        if not cookies_file:
            return None

        source = Path(cookies_file)
        if not source.exists():
            print(f"[警告] cookies 文件不存在: {source}")
            return None

        # 复制到 temp 目录，确保可写
        dest = self.temp_dir / "cookies_working.txt"
        try:
            dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            # Windows 下去掉只读属性
            try:
                import stat
                os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
            return str(dest)
        except Exception as e:
            print(f"[警告] 复制 cookies 文件失败: {e}，回退到原文件")
            # 复制失败则回退到原文件
            return str(source)

    def _get_cookies_param(self) -> tuple[str, str] | None:
        """返回 (param_name, param_value)"""
        if self.cookies_options.get("cookies_from_browser"):
            return "--cookies-from-browser", self.cookies_options["cookies_from_browser"]

        cookies_file = self._prepare_cookies_file()
        if cookies_file:
            return "--cookies", cookies_file
        return None

    def _apply_cookies(self, cmd: list) -> list:
        """在命令行中加入 cookie 参数"""
        param = self._get_cookies_param()
        if param:
            return cmd + [param[0], param[1]]
        return cmd

    def _apply_cookies_to_ydl(self, ydl_opts: dict) -> dict:
        """在 yt-dlp Python API 选项中加入 cookie 参数"""
        if self.cookies_options.get("cookies_from_browser"):
            ydl_opts["cookiesfrombrowser"] = self.cookies_options["cookies_from_browser"]
        else:
            cookies_file = self._prepare_cookies_file()
            if cookies_file:
                ydl_opts["cookiefile"] = cookies_file
        return ydl_opts

    def _check_yt_dlp(self):
        try:
            subprocess.run(
                [self.yt_dlp_path, "--version"],
                capture_output=True,
                check=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError(
                f"yt-dlp 未找到: {self.yt_dlp_path}\n"
                "请安装: .\\venv\\Scripts\\pip install yt-dlp"
            )

    def get_video_info(self, url: str) -> dict:
        """获取视频元信息：标题、封面、简介、作者等，封面下载到本地避免跨域/防盗链"""
        cmd = self._apply_cookies([
            self.yt_dlp_path,
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            url,
        ])
        print(f"[调试] 获取视频信息命令: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[警告] 获取视频信息失败: {result.stderr}")
            return {}

        try:
            info = json.loads(result.stdout.splitlines()[0])

            # 从 URL 提取 video_id 用于命名封面文件
            import re
            video_id_match = re.search(r"/(BV\w+|av\d+)", url)
            video_id = video_id_match.group(1) if video_id_match else "unknown"

            # 下载封面到本地
            thumbnail_url = info.get("thumbnail", "")
            local_thumbnail = ""
            if thumbnail_url:
                try:
                    import requests
                    ext = ".jpg"
                    if "." in thumbnail_url.split("/")[-1]:
                        ext = "." + thumbnail_url.split("/")[-1].split(".")[-1].split("?")[0]
                    local_path = self.temp_dir / f"thumbnail_{video_id}{ext}"
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.bilibili.com",
                    }
                    resp = requests.get(thumbnail_url, headers=headers, timeout=30)
                    if resp.status_code == 200:
                        local_path.write_bytes(resp.content)
                        local_thumbnail = f"/thumbnail/{video_id}{ext}"
                        print(f"[调试] 封面已下载到本地: {local_path}")
                    else:
                        print(f"[警告] 封面下载失败，状态码: {resp.status_code}")
                except Exception as e:
                    print(f"[警告] 封面下载异常: {e}")

            video_info = {
                "title": info.get("title", ""),
                "thumbnail": local_thumbnail or thumbnail_url,
                "description": info.get("description", ""),
                "uploader": info.get("uploader", ""),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url", url),
            }
            print(f"[调试] 获取到视频信息: {video_info}")
            return video_info
        except Exception as e:
            print(f"[警告] 解析视频信息失败: {e}")
            print(f"[调试] yt-dlp 输出前 500 字: {result.stdout[:500]}")
            return {}

    def list_subtitles(self, url: str) -> list:
        """列出视频可用字幕语言，返回 [(lang, ext), ...]"""
        cmd = self._apply_cookies([
            self.yt_dlp_path,
            "--list-subs",
            "--dump-json",
            "--skip-download",
            url,
        ])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[警告] 获取字幕列表失败: {result.stderr}")
            return []

        try:
            info = json.loads(result.stdout.splitlines()[0])
            subs = info.get("subtitles", {})
            items = []
            for lang, formats in subs.items():
                for f in formats:
                    items.append((lang, f.get("ext", "srt")))
            # 去重
            return list(set(items))
        except Exception:
            return []

    def download_subtitle(self, url: str, lang="zh-CN", ext="srt") -> Path | None:
        """下载指定语言字幕，返回文件路径；无字幕返回 None"""
        outtmpl = self.temp_dir / "subtitle_%(id)s.%(ext)s"
        cmd = self._apply_cookies([
            self.yt_dlp_path,
            "--write-subs",
            "--sub-langs", lang,
            "--convert-subs", ext,
            "--skip-download",
            "--paths", str(self.temp_dir),
            "--output", str(outtmpl),
            url,
        ])
        result = subprocess.run(cmd, capture_output=True, text=True)
        # yt-dlp 会写入 subtitle_{video_id}.{ext}
        # 查找刚下载的文件
        files = sorted(self.temp_dir.glob(f"subtitle_*.{ext}"), key=os.path.getmtime)
        if files:
            return files[-1]
        return None

    def extract_audio(self, url: str) -> Path:
        """
        使用 yt-dlp Python API 提取音频，更可控、错误信息更完整。
        返回 mp3 文件路径。
        """
        import yt_dlp

        output_path = None
        errors = []

        def progress_hook(d):
            nonlocal output_path
            if d["status"] == "finished":
                output_path = d.get("filename")

        # 不再清理旧音频：失败重试（retry-asr）需要依赖历史音频文件。
        # 后续按修改时间取最新 audio_* 文件即可，避免混淆。

        ydl_opts = {
            "format": "ba",
            "outtmpl": str(self.temp_dir / "audio_%(id)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "quiet": False,
            "no_warnings": False,
            "progress_hooks": [progress_hook],
            "noprogress": True,
        }
        ydl_opts = self._apply_cookies_to_ydl(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            errors.append(str(e))

        # 策略 1 失败后，尝试 m4a 格式下载
        if not output_path:
            try:
                ydl_opts_m4a = {
                    "format": "ba[ext=m4a]/ba",
                    "outtmpl": str(self.temp_dir / "audio_%(id)s.%(ext)s"),
                    "quiet": False,
                    "no_warnings": False,
                    "progress_hooks": [progress_hook],
                    "noprogress": True,
                }
                ydl_opts_m4a = self._apply_cookies_to_ydl(ydl_opts_m4a)
                with yt_dlp.YoutubeDL(ydl_opts_m4a) as ydl:
                    ydl.download([url])
            except Exception as e:
                errors.append(str(e))

        # 查找下载到的文件
        files = sorted(
            self.temp_dir.glob("audio_*.*"),
            key=os.path.getmtime,
            reverse=True,
        )

        if not files:
            raise RuntimeError(
                "音频提取失败，无法找到音频文件。\n"
                "可能原因：\n"
                "1. B站视频需要登录 cookie（可在 config.yaml 配置 bilibili.cookies）\n"
                "2. 视频受地区/版权限制\n"
                "3. yt-dlp 版本过旧，尝试执行: .\\venv\\Scripts\\pip install -U yt-dlp\n"
                f"错误详情: {'; '.join(errors[:3])}"
            )

        audio_file = files[0]

        # 确保是 mp3，不是则尝试转换
        if audio_file.suffix.lower() != ".mp3":
            try:
                mp3_path = audio_file.with_suffix(".mp3")
                self._convert_to_mp3(audio_file, mp3_path)
                return mp3_path
            except Exception as e:
                errors.append(f"转码失败: {e}")
                # 如果转码失败，返回原文件（但 ASR 可能不支持）
                return audio_file

        return audio_file

    def _convert_to_mp3(self, input_path: Path, output_path: Path):
        """用 ffmpeg 将音频转为 mp3"""
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("未检测到 ffmpeg，无法转码音频")

        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vn", "-ar", "16000", "-ac", "1",
            "-b:a", "128k", str(output_path)
        ]
        subprocess.run(cmd, capture_output=True, check=True, text=True)

    @staticmethod
    def parse_srt(srt_path: Path) -> list[dict]:
        """解析 SRT 为 [{'start':str, 'end':str, 'text':str}, ...]"""
        content = srt_path.read_text(encoding="utf-8")
        # 处理可能的 BOM
        content = content.lstrip("﻿")

        blocks = re.split(r"\n\s*\n", content.strip())
        entries = []
        for block in blocks:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if len(lines) < 2:
                continue
            # 时间行格式: 00:00:01,000 --> 00:00:03,000
            time_line = lines[1] if "-->" in lines[1] else lines[0]
            if "-->" not in time_line:
                continue
            match = re.match(
                r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
                time_line,
            )
            if not match:
                continue
            text = " ".join(lines[2:]) if len(lines) > 2 else ""
            entries.append({
                "start": match.group(1),
                "end": match.group(2),
                "text": text,
            })
        return entries

    @staticmethod
    def clean_transcript(
        entries: list[dict],
        filler_patterns: list[str] | None = None,
        min_length: int = 8,
        merge_gap: int = 3,
    ) -> str:
        """
        清洗字幕为纯文本：
        1. 合并短句
        2. 过滤口癖
        3. 去除重复行
        """
        if filler_patterns is None:
            filler_patterns = []

        # 步骤1：合并时间间隔短的相邻字幕
        merged = []
        for e in entries:
            text = e["text"].strip()
            if not text:
                continue
            if merged and _time_diff(merged[-1]["end"], e["start"]) <= merge_gap:
                merged[-1]["text"] += " " + text
                merged[-1]["end"] = e["end"]
            else:
                merged.append({"start": e["start"], "end": e["end"], "text": text})

        # 步骤2：逐行清洗
        lines = []
        seen = set()
        for m in merged:
            text = m["text"]
            # 去除 HTML 标签（B 站有些字幕带 <font>）
            text = re.sub(r"<[^>]+>", "", text)
            # 去除口癖词
            for pat in filler_patterns:
                text = re.sub(pat, "", text)
            # 归一化空白
            text = re.sub(r"\s+", " ", text).strip()
            # 去重（忽略标点差异）
            key = re.sub(r"[^\w]", "", text)
            if len(text) >= min_length and key and key not in seen:
                seen.add(key)
                lines.append(text)

        return "\n".join(lines)

    @staticmethod
    def deduplicate_asr_text(text: str) -> str:
        """
        去除 ASR 识别结果中的重复内容。
        处理两种情况：
        1. 连续完全相同的句子
        2. 短时反复出现的相似短句
        """
        if not text:
            return text

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return text

        result = []
        prev_line = None
        repeat_count = 0
        max_repeat = 2  # 最多允许连续重复 2 次

        for line in lines:
            if line == prev_line:
                repeat_count += 1
                if repeat_count <= max_repeat:
                    result.append(line)
                # 超过 max_repeat 的不加入
            else:
                repeat_count = 1
                result.append(line)
                prev_line = line

        # 再过滤全文范围内重复率过高的短句
        final = []
        seen_short = {}
        for line in result:
            # 对短于 20 字的句子做全局去重
            if len(line) <= 20:
                norm = re.sub(r"[^\w]", "", line)
                if norm in seen_short:
                    continue
                seen_short[norm] = True
            final.append(line)

        return "\n".join(final)

    @staticmethod
    def segment_asr_text(text: str, max_line_chars: int = 80) -> str:
        """
        把 ASR 返回的连续文本按中文/英文句尾标点分段，提升字幕卡片阅读体验。
        对超长句子会进一步按逗号切分，并控制每行长度不超过 max_line_chars。
        """
        if not text:
            return text

        # 1. 在句尾标点后插入换行（保留标点）
        segmented = re.sub(r"([。！？；?!])\s*", r"\1\n", text)
        lines = [line.strip() for line in segmented.split("\n") if line.strip()]

        # 2. 对超长行按逗号进一步拆分，避免一行字过多
        result = []
        for line in lines:
            if len(line) <= max_line_chars:
                result.append(line)
                continue

            # 按逗号/顿号拆分，保留分隔符
            parts = re.split(r"(?<=[，,、])", line)
            current = ""
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if current and len(current) + len(part) > max_line_chars:
                    result.append(current)
                    current = part
                else:
                    current += part
            if current:
                result.append(current)

        return "\n".join(result)


def _time_diff(t1: str, t2: str) -> float:
    """计算两个 SRT 时间戳的秒差"""

    def _parse(t):
        t = t.replace(",", ".")
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    return abs(_parse(t2) - _parse(t1))


def extract_transcript(url: str, config: dict, force_audio: bool = False) -> tuple[str, str]:
    """
    对外接口：返回 (transcript_text, source_info)
    source_info: 'subtitle' 或 'audio' 或 'failed'
    force_audio: 为 True 时跳过字幕，直接提取音频（用于强制 ASR）
    """
    extractor = SubtitleExtractor(
        yt_dlp_path=config["paths"]["yt_dlp_path"],
        temp_dir=config["paths"]["temp_dir"],
        cookies_options=config.get("bilibili", {}),
    )

    # 步骤 1：尝试下载 CC 字幕
    subs = []
    if not force_audio:
        subs = extractor.list_subtitles(url)

    preferred_langs = ["zh-CN", "zh-Hans", "zh-TW", "zh-Hant", "zh", "en"]
    chosen = None
    for lang in preferred_langs:
        for l, e in subs:
            if l.startswith(lang):
                chosen = (l, e)
                break
        if chosen:
            break

    if chosen:
        lang, ext = chosen
        print(f"[信息] 发现 CC 字幕: {lang}, 格式: {ext}")
        srt_path = extractor.download_subtitle(url, lang=lang, ext="srt")
        if srt_path:
            entries = SubtitleExtractor.parse_srt(srt_path)
            filler = config.get("cleaning", {}).get("filler_words", [])
            min_len = config.get("cleaning", {}).get("min_sentence_length", 8)
            merge_gap = config.get("cleaning", {}).get("merge_gap_seconds", 3)
            text = SubtitleExtractor.clean_transcript(
                entries, filler_patterns=filler, min_length=min_len, merge_gap=merge_gap
            )
            return text, f"subtitle:{lang}"

    # 步骤 2：无 CC 字幕或强制音频 → 提取音频，后续交给 ASR 转字幕
    if force_audio:
        print("[信息] 强制使用音频识别，跳过字幕下载...")
    else:
        print("[信息] 未检测到 CC 字幕，准备提取音频交给 ASR 识别...")

    audio_path = extractor.extract_audio(url)
    # 返回空文本，source='audio'，由上层 ASR 模块处理
    return "", "audio"
