"""LLM Proxy — the security core of the multi-tenant platform.

Receives OpenAI-compatible requests from user containers (authenticated
by container token), injects the real API key, records usage, enforces
quotas, and forwards to the actual LLM provider.

Design: pass-through proxy. We only extract `model` for routing and `stream`
for response handling. All other parameters (messages, tools, temperature,
max_tokens, max_completion_tokens, reasoning_effort, thinking, top_p,
response_format, etc.) are forwarded as-is to litellm/provider.
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from litellm import acompletion
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit_log
from app.auth.service import decode_token
from app.config import settings
from app.db.models import Container, UsageRecord, User

logger = logging.getLogger("platform.llm_proxy")


# ---------------------------------------------------------------------------
# Quota caching
# ---------------------------------------------------------------------------

class QuotaCache:
    """In-memory cache for daily quota sums with TTL."""

    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[int, float]] = {}  # {user_id: (tokens, timestamp)}

    def get(self, user_id: str) -> int | None:
        """Get cached quota, or None if expired."""
        if user_id not in self._cache:
            return None
        tokens, cached_at = self._cache[user_id]
        if time.time() - cached_at > self.ttl_seconds:
            del self._cache[user_id]
            return None
        return tokens

    def set(self, user_id: str, tokens: int):
        """Cache quota with current timestamp."""
        self._cache[user_id] = (tokens, time.time())

    def invalidate(self, user_id: str):
        """Force cache miss."""
        self._cache.pop(user_id, None)


_quota_cache = QuotaCache(ttl_seconds=60)


# ---------------------------------------------------------------------------
# Model → provider mapping
# ---------------------------------------------------------------------------

_PROVIDERS: list[dict] = [
    {
        "prefix": "claude",
        "key_attr": "anthropic_api_key",
        "litellm_fmt": "{model}",
        "api_base": None,
        "keywords": ["claude"],
    },
    {
        "prefix": "openai",
        "key_attr": "openai_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base_attr": "openai_api_base",
        "keywords": ["gpt", "o1", "o3", "o4"],
    },
    {
        "prefix": "deepseek",
        "key_attr": "deepseek_api_key",
        "litellm_fmt": "deepseek/{model}",
        "api_base": None,
        "keywords": ["deepseek"],
    },
    {
        "prefix": "dashscope",
        "key_attr": "dashscope_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "keywords": ["qwen"],
    },
    {
        "prefix": "minimax",
        "key_attr": "minimax_api_key",
        "litellm_fmt": "minimax/{model}",
        "api_base_attr": "minimax_api_base",
        "keywords": ["minimax"],
    },
    {
        "prefix": "kimi",
        "key_attr": "kimi_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base": "https://api.moonshot.cn/v1",
        "keywords": ["kimi", "moonshot"],
    },
    {
        "prefix": "zhipu",
        "key_attr": "zhipu_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "keywords": ["glm"],
    },
    {
        "prefix": "doubao",
        "key_attr": "doubao_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "keywords": ["doubao"],
    },
    {
        "prefix": "aihubmix",
        "key_attr": "aihubmix_api_key",
        "litellm_fmt": "openai/{model}",
        "api_base": "https://aihubmix.com/v1",
        "keywords": ["aihubmix"],
    },
    {
        "prefix": "vllm",
        "key_attr": "hosted_vllm_api_key",
        "litellm_fmt": "hosted_vllm/{model}",
        "api_base_attr": "hosted_vllm_api_base",
        "keywords": [],
    },
]

_PREFIX_MAP: dict[str, dict] = {p["prefix"]: p for p in _PROVIDERS}

_KEYWORD_MAP: dict[str, dict] = {}
for _p in _PROVIDERS:
    for _kw in _p.get("keywords", []):
        _KEYWORD_MAP[_kw] = _p


def _normalize_minimax_api_base(api_base: str | None) -> str | None:
    if not api_base:
        return api_base
    normalized = api_base.strip().rstrip("/")
    if normalized.endswith("/anthropic/v1/messages"):
        return normalized[: -len("/anthropic/v1/messages")] + "/v1"
    if normalized.endswith("/anthropic/v1"):
        return normalized[: -len("/anthropic/v1")] + "/v1"
    if normalized.endswith("/anthropic"):
        return normalized[: -len("/anthropic")] + "/v1"
    return normalized


def _get_provider_key_base_and_headers(provider: dict) -> tuple[str, str | None, dict[str, str] | None]:
    api_key = getattr(settings, provider["key_attr"], "") or ""
    if "api_base_attr" in provider:
        api_base = getattr(settings, provider["api_base_attr"], "") or None
    else:
        api_base = provider.get("api_base")
    if provider["prefix"] == "minimax":
        api_base = _normalize_minimax_api_base(api_base)
    if not api_key and provider["prefix"] == "vllm":
        api_key = "dummy"
    return api_key, api_base, None


def _resolve_provider(model: str) -> tuple[str, str, str | None, dict[str, str] | None]:
    """Return (litellm_model_name, api_key, api_base_or_None, extra_headers_or_None)."""
    model_lower = model.lower()

    # 1. Explicit prefix
    if "/" in model:
        prefix = model_lower.split("/", 1)[0]
        if prefix in _PREFIX_MAP:
            provider = _PREFIX_MAP[prefix]
            actual_model = model.split("/", 1)[1]
            api_key, api_base, extra_headers = _get_provider_key_base_and_headers(provider)
            if api_key or extra_headers:
                litellm_model = provider["litellm_fmt"].format(model=actual_model)
                logger.info("模型路由: %s → %s (litellm=%s)", model, prefix, litellm_model)
                return litellm_model, api_key, api_base, extra_headers

    # 2. Keyword match
    for keyword, provider in _KEYWORD_MAP.items():
        if keyword in model_lower:
            api_key, api_base, extra_headers = _get_provider_key_base_and_headers(provider)
            if api_key or extra_headers:
                actual_model = model.split("/", 1)[1] if "/" in model else model
                litellm_model = provider["litellm_fmt"].format(model=actual_model)
                logger.info("模型路由: %s → %s (keyword=%r, litellm=%s)", model, provider["prefix"], keyword, litellm_model)
                return litellm_model, api_key, api_base, extra_headers

    # 3. Fallback: vLLM
    if settings.hosted_vllm_api_base:
        return f"hosted_vllm/{model}", settings.hosted_vllm_api_key or "dummy", settings.hosted_vllm_api_base, None

    # 4. Fallback: OpenRouter
    if settings.openrouter_api_key:
        return f"openrouter/{model}", settings.openrouter_api_key, None, None

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"No provider configured for model '{model}'",
    )


def _resolve_route_model(requested_model: str) -> str:
    route_model = settings.route_model.strip()
    if not route_model:
        return requested_model
    if route_model != requested_model:
        logger.info("强制模型路由: requested=%s → route_model=%s", requested_model, route_model)
    return route_model


# ---------------------------------------------------------------------------
# Quota check
# ---------------------------------------------------------------------------

_TIER_LIMITS = {
    "free": settings.quota_free,
    "basic": settings.quota_basic,
    "pro": settings.quota_pro,
}


async def _check_quota(db: AsyncSession, user: User) -> None:
    # 1. Check cache first
    cached_used = _quota_cache.get(user.id)
    if cached_used is not None:
        used_today = cached_used
    else:
        # 2. Cache miss: query database
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db.execute(
            select(func.coalesce(func.sum(UsageRecord.total_tokens), 0)).where(
                UsageRecord.user_id == user.id,
                UsageRecord.created_at >= today_start,
            )
        )
        used_today: int = result.scalar_one()
        _quota_cache.set(user.id, used_today)

    # 3. Check limit
    limit = _TIER_LIMITS.get(user.quota_tier, _TIER_LIMITS["free"])

    if used_today >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily token quota exceeded ({used_today:,}/{limit:,}). Resets at midnight UTC.",
        )


# ---------------------------------------------------------------------------
# Keys that litellm accepts directly from OpenAI-compatible requests.
# We pass these through as-is. Keys NOT in this set are stripped to avoid
# litellm errors on unknown parameters.
# ---------------------------------------------------------------------------

_LITELLM_PASSTHROUGH_KEYS = {
    "messages",
    "temperature",
    "top_p",
    "n",
    "stop",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "response_format",
    "seed",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "user",
    "reasoning_effort",
    "thinking",
    "service_tier",
    "store",
    "metadata",
    "logprobs",
    "top_logprobs",
    "stream_options",
}


async def proxy_chat_completion(
    db: AsyncSession,
    container_token: str,
    raw_request: dict,
):
    """Pass-through proxy: authenticate, check quota, forward to LLM, record usage.

    All OpenAI-compatible parameters are forwarded as-is. We only modify:
    - model: routed to the correct litellm model name
    - api_key: injected from platform settings
    - api_base: injected for providers that need it
    - stream_options: injected for streaming usage tracking
    """
    requested_model = raw_request.get("model", "")
    stream = raw_request.get("stream", False)
    messages = raw_request.get("messages", [])
    model = _resolve_route_model(requested_model)

    logger.info(
        "收到 LLM 请求: requested_model=%s, routed_model=%s, stream=%s, 消息数=%d",
        requested_model,
        model,
        stream,
        len(messages),
    )

    # 1. Authenticate
    container = None
    user = None

    if settings.dev_openclaw_url:
        pass  # Skip auth in dev mode
    else:
        # Try JWT API token first
        jwt_payload = decode_token(container_token)
        if jwt_payload and jwt_payload.get("type") == "access":
            user_id = jwt_payload.get("sub")
            if user_id:
                user_result = await db.execute(select(User).where(User.id == user_id))
                user = user_result.scalar_one_or_none()

        # Dedicated per-user container token
        if user is None:
            result = await db.execute(
                select(Container).where(Container.container_token == container_token)
            )
            container = result.scalar_one_or_none()
            if container is not None:
                user_result = await db.execute(select(User).where(User.id == container.user_id))
                user = user_result.scalar_one_or_none()

        # Shared runtime service token: allowed, but usage/quota is enforced at higher layers.
        if user is None and settings.shared_openclaw_system_token and container_token == settings.shared_openclaw_system_token:
            logger.info("Accepted shared OpenClaw system token for LLM proxy request")

        elif user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

        if user is not None:
            if not user.is_active:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account disabled")
            await _check_quota(db, user)

    # 2. Resolve provider
    litellm_model, api_key, api_base, extra_headers = _resolve_provider(model)

    # 3. Build kwargs — pass through all known OpenAI-compatible params
    kwargs: dict = {
        "model": litellm_model,
        "api_key": api_key,
        "stream": stream,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if extra_headers:
        kwargs["extra_headers"] = extra_headers

    # Pass through all supported parameters from the original request
    for key in _LITELLM_PASSTHROUGH_KEYS:
        if key in raw_request:
            kwargs[key] = raw_request[key]

    # Ensure streaming usage is reported
    if stream:
        if "stream_options" not in kwargs:
            kwargs["stream_options"] = {"include_usage": True}
        elif isinstance(kwargs["stream_options"], dict):
            kwargs["stream_options"].setdefault("include_usage", True)

    # Log extra keys we're NOT forwarding (for debugging)
    ignored_keys = set(raw_request.keys()) - _LITELLM_PASSTHROUGH_KEYS - {"model", "stream"}
    if ignored_keys:
        logger.debug("未转发的请求字段: %s", ignored_keys)

    # 4. Call LLM
    try:
        response = await acompletion(**kwargs)
    except Exception as e:
        msg_count = len(messages)
        logger.error("LLM 调用失败: model=%s, litellm=%s, 错误=%s, 消息数=%d", model, litellm_model, e, msg_count)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM provider error: {e}",
        )

    # 5. Streaming response
    if stream:
        import json

        async def _stream_generator():
            total_input = 0
            total_output = 0
            try:
                async for chunk in response:
                    data = chunk.model_dump()
                    chunk_usage = data.get("usage")
                    if chunk_usage:
                        total_input = chunk_usage.get("prompt_tokens") or 0
                        total_output = chunk_usage.get("completion_tokens") or 0
                    yield f"data: {json.dumps(data)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.error(
                    "Streaming LLM response interrupted: model=%s, litellm=%s, error=%s",
                    model, litellm_model, e, exc_info=True,
                )
                err_text = f"LLM provider stream error: {str(e)}"
                if len(err_text) > 300:
                    err_text = err_text[:300] + "..."
                error_chunk = {
                    "id": f"platform-error-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f"\n\n[Error] {err_text}"},
                        "finish_reason": "stop",
                    }],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                total = total_input + total_output
                if user is not None and total > 0:
                    try:
                        db.add(UsageRecord(
                            user_id=user.id, model=model,
                            input_tokens=total_input, output_tokens=total_output,
                            total_tokens=total,
                        ))
                        await write_audit_log(
                            db,
                            action="llm_call",
                            user_id=user.id,
                            resource=model,
                            detail={
                                "stream": True,
                                "requested_model": requested_model,
                                "routed_model": model,
                                "input_tokens": total_input,
                                "output_tokens": total_output,
                                "total_tokens": total,
                            },
                        )
                        await db.commit()
                    except Exception as e:
                        logger.warning("Failed to record streaming usage: %s", e)

        return _stream_generator()

    # 6. Record usage (non-streaming)
    usage = getattr(response, "usage", None)
    if user is not None and usage:
        db.add(UsageRecord(
            user_id=user.id, model=model,
            input_tokens=usage.prompt_tokens or 0,
            output_tokens=usage.completion_tokens or 0,
            total_tokens=usage.total_tokens or 0,
        ))
        await write_audit_log(
            db,
            action="llm_call",
            user_id=user.id,
            resource=model,
            detail={
                "stream": False,
                "requested_model": requested_model,
                "routed_model": model,
                "input_tokens": usage.prompt_tokens or 0,
                "output_tokens": usage.completion_tokens or 0,
                "total_tokens": usage.total_tokens or 0,
            },
        )
        await db.commit()

    # 7. Update container last_active_at
    if container is not None:
        container.last_active_at = datetime.utcnow()
        await db.commit()

    return response.model_dump()
