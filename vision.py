"""读说说时的图片识别（VisionManager，自 Maizone vision.py 移植）。

把说说配图（base64）交给视觉模型生成文字描述。
vision_model 为空或调用失败时回退占位文本。
"""
import asyncio
import base64


# ===== logger =====
class NoLogger:
    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass

    def debug(self, msg):
        pass


logger = NoLogger()


def set_vision_logger(custom_logger):
    global logger
    logger = custom_logger


# 占位文本（与 NoImageManager 保持一致）
PLACEHOLDER = "[图片]"
PLACEHOLDER_FAILED = "[图片（识别失败）]"
# 描述长度上限，防止 prompt 膨胀
MAX_DESC_CHARS = 200
# 单图识别超时（秒）
DESC_TIMEOUT_SEC = 30

_DESCRIBE_PROMPT = (
    "请客观描述这张图片的内容，不超过100字。"
    "包括主要人物/物体/场景/文字内容，不要评价，不要输出多余内容。"
)


def _guess_image_mime(image_base64: str) -> str:
    """按 magic bytes 探测图片 MIME（只解 base64 头部片段），失败默认 jpeg。"""
    try:
        head = base64.b64decode(image_base64[:32])
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if head[:3] == b"GIF":
            return "image/gif"
        if head[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
    except Exception:
        pass
    return "image/jpeg"


class VisionManager:
    """基于 Host llm.generate 的图片描述生成器。

    模型名取自配置 read.vision_model；为空时退化为占位符。
    按图片 URL 做内存 LRU 缓存（read.enable_desc_cache / read.desc_cache_size），
    同一图片重复出现时不再下载与识别。缓存只驻内存，重启清空。
    """

    def __init__(self, plugin):
        self._plugin = plugin
        self._desc_cache: dict[str, str] = {}  # url -> 描述（插入序 LRU）

    def _get_vision_model(self) -> str:
        try:
            return str(self._plugin.config.read.vision_model or "").strip()
        except AttributeError:
            return ""

    def _get_enabled(self) -> bool:
        try:
            return bool(self._plugin.config.read.enable_image_description)
        except AttributeError:
            return True

    def _get_cache_enabled(self) -> bool:
        try:
            return bool(self._plugin.config.read.enable_desc_cache)
        except AttributeError:
            return True

    def _get_cache_size(self) -> int:
        try:
            return max(1, int(self._plugin.config.read.desc_cache_size or 200))
        except (AttributeError, TypeError, ValueError):
            return 200

    def is_cached(self, url: str) -> bool:
        """URL 是否已有缓存描述（命中即无需下载图片）。"""
        if not url or not self._get_cache_enabled():
            return False
        return url in self._desc_cache

    def _cache_get(self, url: str) -> str | None:
        if not self._get_cache_enabled():
            return None
        desc = self._desc_cache.pop(url, None)
        if desc is not None:
            self._desc_cache[url] = desc  # LRU touch
        return desc

    def _cache_put(self, url: str, desc: str) -> None:
        if not url or not self._get_cache_enabled():
            return
        self._desc_cache.pop(url, None)
        self._desc_cache[url] = desc
        limit = self._get_cache_size()
        while len(self._desc_cache) > limit:
            self._desc_cache.pop(next(iter(self._desc_cache)))

    def _build_messages(self, image_base64: str) -> list[dict]:
        data_url = f"data:{_guess_image_mime(image_base64)};base64,{image_base64}"
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

    async def get_image_description(self, url: str, image_base64: str) -> str:
        """生成图片描述文本。任何失败都返回占位文本，不抛异常。

        url 用于缓存键；缓存命中时 image_base64 可为空串（无需下载）。
        仅真实识别成功的描述写入缓存（占位符不入缓存，避免坏结果固化）。
        """
        if url:
            cached = self._cache_get(url)
            if cached is not None:
                logger.info(f"图片描述命中缓存: {url[:80]}")
                return cached
        if not self._get_enabled():
            return PLACEHOLDER
        vision_model = self._get_vision_model()
        if not vision_model:
            return PLACEHOLDER
        if not image_base64:
            return PLACEHOLDER_FAILED
        try:
            ctx = self._plugin.ctx
            result = await asyncio.wait_for(
                ctx.llm.generate(
                    prompt=self._build_messages(image_base64),
                    model=vision_model,
                ),
                timeout=DESC_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(f"图片描述生成超时（>{DESC_TIMEOUT_SEC}s）")
            return PLACEHOLDER_FAILED
        except Exception as e:
            logger.warning(f"图片描述生成失败: {e}")
            return PLACEHOLDER_FAILED

        if not isinstance(result, dict):
            logger.warning(f"图片描述返回格式异常: {type(result)}")
            return PLACEHOLDER_FAILED
        description = str(result.get("response") or "").strip()
        if not description:
            logger.warning("图片描述返回为空")
            return PLACEHOLDER_FAILED
        if len(description) > MAX_DESC_CHARS:
            description = description[:MAX_DESC_CHARS]
        logger.info(f"图片描述生成成功（模型={vision_model}，长度={len(description)}）")
        if url:
            self._cache_put(url, description)
        return description
