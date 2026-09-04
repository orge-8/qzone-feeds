"""读说说时的图片识别（VisionManager，自 Maizone vision.py 移植）。

把说说配图（base64）交给视觉模型生成文字描述。
vision_model 为空或调用失败时回退占位文本。
"""
import asyncio


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


class VisionManager:
    """基于 Host llm.generate 的图片描述生成器。

    模型名取自配置 read.vision_model；为空时退化为占位符。
    """

    def __init__(self, plugin):
        self._plugin = plugin

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

    def _build_messages(self, image_base64: str) -> list[dict]:
        data_url = f"data:image/jpeg;base64,{image_base64}"
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

    async def get_image_description(self, image_base64: str) -> str:
        """生成图片描述文本。任何失败都返回占位文本，不抛异常。"""
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
        return description
