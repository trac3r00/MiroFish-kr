"""
LLM客户端封装
统一使用OpenAI格式调用；请求、路由与用量统计委托给 app.llm
"""

from typing import Optional, Dict, Any, List

from ..config import Config
from ..llm.client import (
    LLMResponseError,
    clean_chat_text as _clean_chat_text,
    create_completion,
    get_openai_client,
    is_response_format_unsupported as _is_response_format_unsupported,
    parse_json_response,
    run_json_loop,
)
from ..llm.profiles import LLMProfile, load_settings
from ..llm.router import DEFAULT_STAGE, resolve_profile
from .openai_chat_compat import extract_chat_completion_text
from .locale import t

__all__ = [
    "LLMClient",
    "LLMResponseError",
    "_clean_chat_text",
    "_is_response_format_unsupported",
]

CUSTOM_PROFILE_ID = "custom"


def _custom_profile(api_key: Optional[str], base_url: Optional[str], model: Optional[str]) -> LLMProfile:
    return LLMProfile(
        id=CUSTOM_PROFILE_ID,
        kind="openai_compatible",
        label="Custom",
        model=model or Config.LLM_MODEL_NAME,
        base_url=base_url or Config.LLM_BASE_URL,
        api_key=api_key or Config.LLM_API_KEY,
    )


class LLMClient:
    """LLM客户端（stage 决定使用哪个 profile；显式参数创建临时 openai_compatible profile）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        stage: str = DEFAULT_STAGE,
    ):
        self.stage = stage
        if api_key or base_url or model:
            profile = _custom_profile(api_key, base_url, model)
        else:
            settings = load_settings()
            if settings.default_profile_id is None:
                raise ValueError(t("config.llm_api_key_missing"))
            profile = resolve_profile(stage, settings)

        if profile.kind == "openai_compatible" and not profile.api_key:
            raise ValueError(t("config.llm_api_key_missing"))

        self.profile = profile
        self.api_key = profile.api_key
        self.base_url = profile.base_url
        self.model = profile.model
        self.client = get_openai_client(profile) if profile.kind == "openai_compatible" else None

    def _create_completion(
        self,
        *,
        messages: List[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[Dict[str, Any]],
    ) -> Any:
        """Send one raw Chat Completions request through the LLM seam."""

        profile = getattr(self, "profile", None) or LLMProfile(
            id=CUSTOM_PROFILE_ID,
            kind="openai_compatible",
            label="Custom",
            model=self.model,
            base_url=getattr(self, "base_url", None),
        )
        return create_completion(
            profile,
            stage=getattr(self, "stage", DEFAULT_STAGE),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            openai_client=getattr(self, "client", None),
        )

    def _for_stage(self, stage: Optional[str]) -> "LLMClient":
        """Client routed for a per-call stage; explicit (custom) clients keep their profile."""
        profile = getattr(self, "profile", None)
        if stage is None or stage == getattr(self, "stage", DEFAULT_STAGE) or profile is None or profile.id == CUSTOM_PROFILE_ID:
            return self
        return LLMClient(stage=stage)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict] = None,
        stage: Optional[str] = None,
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        client = self._for_stage(stage)
        if client is not self:
            return client.chat(messages, temperature=temperature, max_tokens=max_tokens, response_format=response_format)
        response = self._create_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return _clean_chat_text(extract_chat_completion_text(response))

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        max_attempts: int = 1,
        stage: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            max_attempts: 内容生成尝试次数（不含一次明确的JSON模式能力降级）
            
        Returns:
            解析后的JSON对象
        """
        client = self._for_stage(stage)
        if client is not self:
            return client.chat_json(messages, temperature=temperature, max_tokens=max_tokens, max_attempts=max_attempts)
        return run_json_loop(
            self._create_completion,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            max_attempts=max_attempts,
        )

    _parse_json_response = staticmethod(parse_json_response)
