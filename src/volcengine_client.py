"""
volcengine_client.py
封装火山方舟 Chat API 与 MiMo-V2.5-ASR（兼容 OpenAI SDK 格式）
"""

import base64
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from openai import OpenAI


class VolcengineClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

    def summarize(
        self,
        transcript: str,
        system_prompt: str,
        user_template: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> str:
        """
        调用火山方舟模型生成总结
        transcript: 清洗后的字幕全文
        user_template: 需包含 {transcript} 占位符
        """
        user_content = user_template.replace("{transcript}", transcript)

        # 火山方舟兼容 OpenAI 接口格式
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.choices[0].message.content

    def batch_summarize(
        self,
        transcripts: list[tuple[str, str]],
        system_prompt: str,
        user_template: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        delay_seconds: float = 1.0,
    ) -> list[tuple[str, str]]:
        """
        批量总结，返回 [(url, summary), ...]
        delay_seconds: 避免触发速率限制
        """
        results = []
        for url, text in transcripts:
            try:
                summary = self.summarize(
                    text, system_prompt, user_template, max_tokens, temperature
                )
                results.append((url, summary))
                time.sleep(delay_seconds)
            except Exception as e:
                results.append((url, f"[错误] 总结失败: {e}"))
        return results


class MimoChatClient:
    """
    MiMo-V2.5 文本模型客户端（用于总结）
    兼容 OpenAI Chat Completions API 格式
    """

    def __init__(self, api_key: str, api_url: str = "https://token-plan-cn.xiaomimimo.com/v1", model: str = "mimo-v2.5"):
        self.api_key = api_key
        # 兼容 base_url 或完整 chat/completions URL
        self.api_url = api_url.rstrip("/")
        if not self.api_url.endswith("/chat/completions"):
            self.api_url = f"{self.api_url}/chat/completions"
        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.api_url.replace("/chat/completions", ""),
        )

    def summarize(
        self,
        transcript: str,
        system_prompt: str,
        user_template: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> str:
        """
        调用 MiMo 模型生成总结
        """
        user_content = user_template.replace("{transcript}", transcript)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.choices[0].message.content

    def batch_summarize(
        self,
        transcripts: list[tuple[str, str]],
        system_prompt: str,
        user_template: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        delay_seconds: float = 1.0,
    ) -> list[tuple[str, str]]:
        """
        批量总结，返回 [(url, summary), ...]
        """
        results = []
        for url, text in transcripts:
            try:
                summary = self.summarize(
                    text, system_prompt, user_template, max_tokens, temperature
                )
                results.append((url, summary))
                time.sleep(delay_seconds)
            except Exception as e:
                results.append((url, f"[错误] 总结失败: {e}"))
        return results


class MimoASRClient:
    """
    MiMo-V2.5-ASR 客户端
    API 文档: https://mimo.mi.com/static/docs/api/audio/Speech-Recognition.md
    """

    def __init__(self, api_key: str, api_url: str = "https://token-plan-cn.xiaomimimo.com/v1", language: str = "auto"):
        self.api_key = api_key
        # 兼容用户填写 base_url 或完整 chat/completions URL
        self.api_url = api_url.rstrip("/")
        if not self.api_url.endswith("/chat/completions"):
            self.api_url = f"{self.api_url}/chat/completions"
        self.language = language

    def transcribe(self, audio_path: Path, max_retries: int = 3) -> str:
        """
        上传音频文件并返回识别文本。
        支持 mp3 / wav 格式。
        遇到网络错误自动重试。
        """
        audio_path = Path(audio_path)
        suffix = audio_path.suffix.lower()

        if suffix == ".mp3":
            mime_type = "audio/mpeg"
            fmt = "mp3"
        elif suffix == ".wav":
            mime_type = "audio/wav"
            fmt = "wav"
        else:
            raise ValueError(f"不支持的音频格式: {suffix}，仅支持 mp3 / wav")

        # Base64 编码
        audio_bytes = audio_path.read_bytes()
        b64_data = base64.b64encode(audio_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64_data}"

        payload = {
            "model": "mimo-v2.5-asr",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": data_url,
                                "format": fmt,
                            },
                        }
                    ],
                }
            ],
            "asr_options": {
                "language": self.language,
            },
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(self.api_url, headers=headers, json=payload, timeout=300)
                response.raise_for_status()
                result = response.json()

                # 提取识别文本
                choices = result.get("choices", [])
                if not choices:
                    raise RuntimeError(f"ASR 返回空 choices: {result}")

                text = choices[0].get("message", {}).get("content", "").strip()
                if not text:
                    raise RuntimeError(f"ASR 返回空文本: {result}")

                return text

            except requests.exceptions.ConnectionError as e:
                last_error = e
                print(f"[警告] MiMo ASR 连接失败，第 {attempt}/{max_retries} 次重试...")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)  # 指数退避
                continue
            except requests.exceptions.HTTPError as e:
                # HTTP 4xx/5xx 不重试
                raise RuntimeError(f"MiMo ASR 请求失败: {e}\n响应: {e.response.text[:500]}")
            except Exception as e:
                # 其他错误不重试
                raise RuntimeError(f"MiMo ASR 调用异常: {e}")

        raise RuntimeError(
            f"MiMo ASR 连续 {max_retries} 次连接失败。\n"
            f"最后错误: {last_error}\n"
            "可能原因：网络不稳定、音频文件过大、MiMo 服务繁忙。"
        )



class GenericChatClient:
    """
    通用 OpenAI 兼容 Chat Completions 客户端
    适配任意支持 OpenAI API 格式的服务商
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.model = model
        # 兼容用户填写 base_url 或完整 chat/completions URL
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url.replace("/chat/completions", "")
        self.client = OpenAI(api_key=api_key, base_url=self.base_url)

    def summarize(
        self,
        transcript: str,
        system_prompt: str,
        user_template: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> str:
        user_content = user_template.replace("{transcript}", transcript)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content


class GenericASRClient:
    """
    通用 OpenAI 兼容音频识别客户端
    适配任意支持 input_audio 消息格式的 ASR 服务
    """

    def __init__(self, api_key: str, base_url: str, model: str, language: str = "auto"):
        self.api_key = api_key
        self.model = model
        self.language = language
        # 兼容用户填写 base_url 或完整 chat/completions URL
        self.api_url = base_url.rstrip("/")
        if not self.api_url.endswith("/chat/completions"):
            self.api_url = f"{self.api_url}/chat/completions"

    def transcribe(self, audio_path: Path, max_retries: int = 3) -> str:
        audio_path = Path(audio_path)
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            mime_type = "audio/mpeg"
            fmt = "mp3"
        elif suffix == ".wav":
            mime_type = "audio/wav"
            fmt = "wav"
        else:
            raise ValueError(f"不支持的音频格式: {suffix}，仅支持 mp3 / wav")

        audio_bytes = audio_path.read_bytes()
        b64_data = base64.b64encode(audio_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64_data}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": data_url,
                                "format": fmt,
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": self.language},
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(self.api_url, headers=headers, json=payload, timeout=300)
                response.raise_for_status()
                result = response.json()
                choices = result.get("choices", [])
                if not choices:
                    raise RuntimeError(f"ASR 返回空 choices: {result}")
                text = choices[0].get("message", {}).get("content", "").strip()
                if not text:
                    raise RuntimeError(f"ASR 返回空文本: {result}")
                return text
            except requests.exceptions.ConnectionError as e:
                last_error = e
                print(f"[警告] ASR 连接失败，第 {attempt}/{max_retries} 次重试...")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue
            except requests.exceptions.HTTPError as e:
                raise RuntimeError(f"ASR 请求失败: {e}\n响应: {e.response.text[:500]}")
            except Exception as e:
                raise RuntimeError(f"ASR 调用异常: {e}")

        raise RuntimeError(
            f"ASR 连续 {max_retries} 次连接失败。\n"
            f"最后错误: {last_error}\n"
            "可能原因：网络不稳定、音频文件过大、ASR 服务繁忙。"
        )
