"""VLM 送图前的 JPEG 压缩（长边等比缩放 + 质量重编码）。

- 供 qzone_api._describe_images 调用；/动态发图 上传原图不走这里
- Pillow 未安装或解码失败 → 返回 None，调用方回退原图（本模块永不抛异常）
"""

import io

# 压缩结果大小目标：超过则降质二压（1024px q80 通常 100~400KB，极难触发）
_TARGET_MAX_BYTES = 1024 * 1024
_MIN_QUALITY = 40


def compress_image_bytes(data: bytes, max_edge: int = 1024, quality: int = 80) -> bytes | None:
    """JPEG 压缩：长边等比缩放至 max_edge、质量 quality 重编码。

    返回 None 表示压缩失败（缺 Pillow / 解码失败 / 编码失败），
    调用方应回退使用原图 bytes。
    """
    try:
        from PIL import Image, ImageOps  # lazy import：缺 Pillow 时回退而非崩溃
    except ImportError:
        return None
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # 手机照片 EXIF 旋转（Pillow>=9.1）
        if img.mode in ("RGBA", "LA", "P"):
            # 透明通道：白底合成再转 RGB（直接 convert 会黑底）
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")  # CMYK / I;16 等兜底
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)  # 只缩不放，保比例
        out = _encode_jpeg(img, quality)
        # 一次降质重试（防御极端噪声图）
        if out and len(out) > _TARGET_MAX_BYTES and quality - 20 >= _MIN_QUALITY:
            retry = _encode_jpeg(img, max(_MIN_QUALITY, quality - 20))
            if retry and len(retry) < len(out):
                out = retry
        # 负优化保护：压完反而更大（原图已是小 JPEG）则用原图
        if out and len(out) >= len(data):
            return data
        return out
    except Exception:
        # UnidentifiedImageError / DecompressionBombError / OSError ... 全部回退原图
        return None


def _encode_jpeg(img, quality: int) -> bytes | None:
    try:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return None
