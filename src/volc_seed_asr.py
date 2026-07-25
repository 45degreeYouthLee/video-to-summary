"""
volc_seed_asr.py
火山方舟 SeedASR WebSocket 客户端（单流 / 双流）
基于火山方舟《接入语音模型》文档中的二进制协议实现。
"""

import asyncio
import gzip
import json
import struct
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import aiohttp


DEFAULT_SAMPLE_RATE = 16000


class ProtocolVersion:
    V1 = 0b0001


class MessageType:
    CLIENT_FULL_REQUEST = 0b0001
    CLIENT_AUDIO_ONLY_REQUEST = 0b0010
    SERVER_FULL_RESPONSE = 0b1001
    SERVER_ERROR_RESPONSE = 0b1111


class MessageTypeSpecificFlags:
    NO_SEQUENCE = 0b0000
    POS_SEQUENCE = 0b0001
    NEG_SEQUENCE = 0b0010
    NEG_WITH_SEQUENCE = 0b0011


class SerializationType:
    NO_SERIALIZATION = 0b0000
    JSON = 0b0001


class CompressionType:
    GZIP = 0b0001


class CommonUtils:
    @staticmethod
    def gzip_compress(data: bytes) -> bytes:
        return gzip.compress(data)

    @staticmethod
    def gzip_decompress(data: bytes) -> bytes:
        return gzip.decompress(data)

    @staticmethod
    def judge_wav(data: bytes) -> bool:
        return len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    @staticmethod
    def convert_wav_with_path(audio_path: str, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
        cmd = [
            "ffmpeg", "-v", "quiet", "-y", "-i", audio_path,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sample_rate),
            "-f", "wav", "-",
        ]
        try:
            result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"音频转码失败: {e.stderr.decode()}")

    @staticmethod
    def read_wav_info(data: bytes):
        if len(data) < 44:
            raise ValueError("WAV 文件过短")
        if data[:4] != b"RIFF":
            raise ValueError("非 RIFF 格式")
        if data[8:12] != b"WAVE":
            raise ValueError("非 WAVE 格式")
        num_channels = struct.unpack("<H", data[22:24])[0]
        sample_rate = struct.unpack("<I", data[24:28])[0]
        bits_per_sample = struct.unpack("<H", data[34:36])[0]
        pos = 36
        while pos < len(data) - 8:
            subchunk_id = data[pos : pos + 4]
            subchunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
            if subchunk_id == b"data":
                wave_data = data[pos + 8 : pos + 8 + subchunk_size]
                return num_channels, bits_per_sample // 8, sample_rate, wave_data
            pos += 8 + subchunk_size
        raise ValueError("WAV 文件未找到 data 块")


class AsrRequestHeader:
    def __init__(self):
        self.message_type = MessageType.CLIENT_FULL_REQUEST
        self.message_type_specific_flags = MessageTypeSpecificFlags.POS_SEQUENCE
        self.serialization_type = SerializationType.JSON
        self.compression_type = CompressionType.GZIP
        self.reserved_data = bytes([0x00])

    def with_message_type(self, message_type: int):
        self.message_type = message_type
        return self

    def with_message_type_specific_flags(self, flags: int):
        self.message_type_specific_flags = flags
        return self

    def to_bytes(self) -> bytes:
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        header.append((self.message_type << 4) | self.message_type_specific_flags)
        header.append((self.serialization_type << 4) | self.compression_type)
        header.extend(self.reserved_data)
        return bytes(header)

    @staticmethod
    def default_header() -> "AsrRequestHeader":
        return AsrRequestHeader()


class RequestBuilder:
    def __init__(self, api_key: str, resource_id: str, enable_nonstream: bool = False):
        self.api_key = api_key
        self.resource_id = resource_id
        self.enable_nonstream = enable_nonstream

    def new_auth_headers(self) -> dict:
        reqid = str(uuid.uuid4())
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": reqid,
            "X-Api-Connect-Id": reqid,
            "X-Api-Sequence": "-1",
        }

    def new_full_client_request(self, seq: int) -> bytes:
        header = AsrRequestHeader.default_header()
        payload = {
            "user": {"uid": "demo_uid"},
            "audio": {
                "format": "wav",
                "codec": "raw",
                "rate": DEFAULT_SAMPLE_RATE,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
                "enable_nonstream": self.enable_nonstream,
            },
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        compressed = CommonUtils.gzip_compress(payload_bytes)
        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack(">i", seq))
        request.extend(struct.pack(">I", len(compressed)))
        request.extend(compressed)
        return bytes(request)

    def new_audio_only_request(self, seq: int, segment: bytes, is_last: bool = False) -> bytes:
        header = AsrRequestHeader.default_header()
        if is_last:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.NEG_WITH_SEQUENCE)
            seq = -seq
        else:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)
        header.with_message_type(MessageType.CLIENT_AUDIO_ONLY_REQUEST)
        compressed = CommonUtils.gzip_compress(segment)
        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack(">i", seq))
        request.extend(struct.pack(">I", len(compressed)))
        request.extend(compressed)
        return bytes(request)


class AsrResponse:
    def __init__(self):
        self.code = 0
        self.event = 0
        self.is_last_package = False
        self.payload_sequence = 0
        self.payload_size = 0
        self.payload_msg = None


class ResponseParser:
    @staticmethod
    def parse_response(msg: bytes) -> AsrResponse:
        response = AsrResponse()
        header_size = msg[0] & 0x0F
        message_type = msg[1] >> 4
        message_type_specific_flags = msg[1] & 0x0F
        serialization_method = msg[2] >> 4
        message_compression = msg[2] & 0x0F
        payload = msg[header_size * 4 :]

        if message_type_specific_flags & 0x01:
            response.payload_sequence = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]
        if message_type_specific_flags & 0x02:
            response.is_last_package = True
        if message_type_specific_flags & 0x04:
            response.event = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]

        if message_type == MessageType.SERVER_FULL_RESPONSE:
            response.payload_size = struct.unpack(">I", payload[:4])[0]
            payload = payload[4:]
        elif message_type == MessageType.SERVER_ERROR_RESPONSE:
            response.code = struct.unpack(">i", payload[:4])[0]
            response.payload_size = struct.unpack(">I", payload[4:8])[0]
            payload = payload[8:]

        if not payload:
            return response

        if message_compression == CompressionType.GZIP:
            try:
                payload = CommonUtils.gzip_decompress(payload)
            except Exception as e:
                raise RuntimeError(f"解压缩响应失败: {e}")

        if serialization_method == SerializationType.JSON:
            try:
                response.payload_msg = json.loads(payload.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"解析响应 JSON 失败: {e}")

        return response


class VolcSeedASRClient:
    """
    火山方舟 SeedASR WebSocket 客户端（同步包装）
    支持 bigmodel_async（双流）和 bigmodel_nostream（单流）

    长音频优化：
    - segment_duration_ms: 默认 1000ms，减少小段数量与 WebSocket 开销
    - total_timeout: 默认按音频时长自动计算（audio_length * multiplier + buffer），
      避免 26 分钟音频在 180s 硬超时内被强制中断。
    """

    def __init__(
        self,
        api_key: str,
        resource_id: str,
        base_url: str,
        segment_duration_ms: int = 1000,
        total_timeout: int | None = None,
        timeout_min_seconds: int = 180,
        timeout_audio_multiplier: float = 2.0,
        timeout_buffer_seconds: int = 120,
    ):
        self.api_key = api_key
        self.resource_id = resource_id
        # base_url 通常已包含 bigmodel_nostream 或 bigmodel_async
        self.url = base_url.rstrip("/")
        self.segment_duration_ms = max(200, segment_duration_ms)
        self.total_timeout = total_timeout
        self.timeout_min_seconds = timeout_min_seconds
        self.timeout_audio_multiplier = timeout_audio_multiplier
        self.timeout_buffer_seconds = timeout_buffer_seconds
        # 单流接口可快速发送，双流接口按实时速率发送
        self.simulate_realtime = "bigmodel_async" in self.url
        self.enable_nonstream = "bigmodel_nostream" in self.url

    def _estimate_timeout(self, duration_sec: float) -> int:
        """根据音频时长估算所需总超时（秒）"""
        if self.total_timeout is not None and self.total_timeout > 0:
            return self.total_timeout
        estimated = int(duration_sec * self.timeout_audio_multiplier) + self.timeout_buffer_seconds
        return max(self.timeout_min_seconds, estimated)

    def transcribe(self, audio_path: Path, on_retry=None) -> str:
        """火山 SeedASR 同步入口；on_retry 目前不用于 WebSocket 模式，但保留兼容接口。"""
        audio_path = Path(audio_path)
        print(f"[ASR] 开始提交音频到火山 SeedASR: {audio_path}", flush=True)
        print(f"[ASR] 接口: {self.url} (nonstream={self.enable_nonstream}, realtime={self.simulate_realtime})", flush=True)

        # 先读取并估算时长，用于动态超时
        content = self._read_audio_data(str(audio_path))
        try:
            _, _, sample_rate, wave_data = CommonUtils.read_wav_info(content)
            duration_sec = len(wave_data) / (2 * sample_rate)  # 16bit mono
        except Exception:
            duration_sec = 0.0
        timeout = self._estimate_timeout(duration_sec)
        print(
            f"[ASR] 音频时长约 {duration_sec/60:.1f} 分钟，"
            f"segment_duration_ms={self.segment_duration_ms}ms，"
            f"总超时 {timeout}s",
            flush=True,
        )

        try:
            return asyncio.run(asyncio.wait_for(self._transcribe_async(audio_path, content=content), timeout=timeout))
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"ASR 识别超时（{timeout}秒）。音频时长约 {duration_sec/60:.1f} 分钟，"
                f"已发送完全部音频片段，但服务端未在超时内返回最终结果。\n"
                f"建议：增大 config.yaml 中 asr.timeout_audio_multiplier / timeout_buffer_seconds，"
                f"或对超过 10 分钟的视频启用分片 ASR。"
            ) from e

    async def _transcribe_async(self, audio_path: Path, content: Optional[bytes] = None) -> str:
        audio_path = Path(audio_path)
        if content is None:
            content = self._read_audio_data(str(audio_path))
        segment_size = self._get_segment_size(content)
        print(f"[ASR] 音频已读取/转换，总大小 {len(content)} bytes, segment_size={segment_size}", flush=True)

        builder = RequestBuilder(self.api_key, self.resource_id, enable_nonstream=self.enable_nonstream)
        session = aiohttp.ClientSession()
        try:
            print(f"[ASR] 连接 WebSocket...", flush=True)
            ws = await session.ws_connect(self.url, headers=builder.new_auth_headers())
            print(f"[ASR] WebSocket 已连接", flush=True)
            try:
                # 发送完整客户端请求
                print(f"[ASR] 发送握手请求...", flush=True)
                await ws.send_bytes(builder.new_full_client_request(1))
                msg = await ws.receive()
                if msg.type != aiohttp.WSMsgType.BINARY:
                    raise RuntimeError(f"ASR 握手失败: {msg.type}")
                resp = ResponseParser.parse_response(msg.data)
                if resp.code != 0:
                    raise RuntimeError(f"ASR 握手失败: {resp.payload_msg}")
                print(f"[ASR] 握手成功", flush=True)

                # 发送音频并接收结果
                last_text = ""
                segments = [content[i : i + segment_size] for i in range(0, len(content), segment_size)]
                print(f"[ASR] 准备发送 {len(segments)} 个音频片段...", flush=True)
                sender_task = asyncio.create_task(
                    self._send_audio_segments(ws, builder, content, segment_size)
                )
                msg_count = 0
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            msg_count += 1
                            resp = ResponseParser.parse_response(msg.data)
                            if resp.code != 0:
                                raise RuntimeError(f"ASR 识别错误: {resp.payload_msg}")
                            msg_data = resp.payload_msg or {}
                            text = self._extract_text(msg_data)
                            if text:
                                last_text = text
                            if resp.is_last_package:
                                print(f"[ASR] 收到最后一个包 (共 {msg_count} 条消息)", flush=True)
                                break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            err = msg.data
                            err_str = str(err) if err else "WebSocket 连接异常（可能为服务端主动断开或网络不稳定）"
                            raise RuntimeError(f"WebSocket 错误: {err_str}")
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            if not last_text:
                                raise RuntimeError(
                                    "WebSocket 在收到识别结果前被关闭。"
                                    "可能原因：音频过长/发送过快被服务端断开，或识别超时。"
                                )
                            print(f"[ASR] WebSocket 已关闭 (共 {msg_count} 条消息)", flush=True)
                            break
                finally:
                    sender_task.cancel()
                    try:
                        await sender_task
                    except asyncio.CancelledError:
                        pass

                full_text = last_text.strip()
                if not full_text:
                    raise RuntimeError("ASR 返回空文本")
                print(f"[ASR] 识别完成，文本长度: {len(full_text)} 字符", flush=True)
                return full_text
            finally:
                await ws.close()
        finally:
            await session.close()

    async def _send_audio_segments(self, ws, builder: RequestBuilder, content: bytes, segment_size: int):
        segments = [content[i : i + segment_size] for i in range(0, len(content), segment_size)]
        seq = 2
        for i, segment in enumerate(segments):
            is_last = i == len(segments) - 1
            request = builder.new_audio_only_request(seq, segment, is_last=is_last)
            await ws.send_bytes(request)
            if not is_last:
                seq += 1
            if self.simulate_realtime:
                await asyncio.sleep(self.segment_duration_ms / 1000)
            # 每 10 段或最后一段打印进度
            if (i + 1) % 10 == 0 or is_last:
                print(f"[ASR] 已发送 {i + 1}/{len(segments)} 段音频", flush=True)

    def _read_audio_data(self, file_path: str) -> bytes:
        with open(file_path, "rb") as f:
            content = f.read()
        if not CommonUtils.judge_wav(content):
            content = CommonUtils.convert_wav_with_path(file_path, DEFAULT_SAMPLE_RATE)
        return content

    def _get_segment_size(self, content: bytes) -> int:
        channel_num, samp_width, sample_rate, _ = CommonUtils.read_wav_info(content)
        size_per_sec = channel_num * samp_width * sample_rate
        return size_per_sec * self.segment_duration_ms // 1000

    @staticmethod
    def _extract_text(msg_data: dict) -> str:
        # 不同返回格式兼容
        # 优先取 utterances（已按说话片段/句子分段，更利于后续展示），
        # 其次取 result.text（完整累计文本）。
        if not isinstance(msg_data, dict):
            return ""
        result = msg_data.get("result") or {}
        if not isinstance(result, dict):
            return ""

        # 1. 尝试使用 utterances（通常包含 text 与 timing）
        utterances = result.get("utterances") or []
        if utterances:
            seen = set()
            parts = []
            for u in utterances:
                utt_text = u.get("text", "") if isinstance(u, dict) else ""
                if not utt_text:
                    continue
                key = utt_text.strip()
                if key and key not in seen:
                    seen.add(key)
                    parts.append(key)
            if parts:
                return "\n".join(parts)

        # 2. 退回到 result.text
        text = result.get("text") or ""
        return text.strip()
