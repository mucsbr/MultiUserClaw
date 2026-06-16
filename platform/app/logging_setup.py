"""Platform Gateway 统一日志配置。"""

import logging
import sys

from app.config import settings

# ANSI 颜色
_COLORS = {
    "DEBUG": "\033[36m",       # 青色
    "INFO": "\033[32m",        # 绿色
    "WARNING": "\033[33m",     # 黄色
    "ERROR": "\033[31m",       # 红色
    "CRITICAL": "\033[1;31m",  # 粗体红色
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """带颜色的日志格式化器。"""

    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname:<7}{_RESET}"
        return super().format(record)


def setup_logging() -> None:
    """配置全局日志。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 清除已有 handler，防止重复输出
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(ColorFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)

    # 降低第三方库的日志级别
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def log_settings_summary() -> None:
    """启动时打印关键配置摘要，方便排查问题。"""
    logger = logging.getLogger("platform.config")

    def _mask(key: str) -> str:
        """API Key 脱敏：只显示前 4 位。"""
        return f"{key[:4]}***" if len(key) > 4 else ("(已设置)" if key else "(空)")

    logger.info("========== Platform Gateway 配置 ==========")
    logger.info("  开发模式 openclaw : %s", settings.dev_openclaw_url or "(关闭)")
    logger.info("  开发模式 gateway  : %s", settings.dev_gateway_url or "(关闭)")
    logger.info("  默认模型          : %s", settings.default_model)
    logger.info("  强制路由模型      : %s", settings.route_model or "(关闭)")
    logger.info("  数据库            : %s", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)

    # LLM 供应商密钥
    providers = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "deepseek": settings.deepseek_api_key,
        "dashscope": settings.dashscope_api_key,
        "minimax": settings.minimax_api_key,
        "kimi": settings.kimi_api_key,
        "moonshot": settings.moonshot_api_key,
        "aihubmix": settings.aihubmix_api_key,
        "openrouter": settings.openrouter_api_key,
        "zhipu": settings.zhipu_api_key,
    }
    configured = {k: _mask(v) for k, v in providers.items() if v}
    unconfigured = [k for k, v in providers.items() if not v]

    if configured:
        logger.info("  已配置的 LLM 密钥 : %s", ", ".join(f"{k}={v}" for k, v in configured.items()))
    else:
        logger.warning("  已配置的 LLM 密钥 : 无 —— 所有 LLM 调用都会失败!")

    if unconfigured:
        logger.info("  未配置的 LLM 密钥 : %s", ", ".join(unconfigured))

    # vLLM
    if settings.hosted_vllm_api_base:
        logger.info("  vLLM 地址         : %s (key=%s)", settings.hosted_vllm_api_base, _mask(settings.hosted_vllm_api_key))
    logger.info("============================================")
