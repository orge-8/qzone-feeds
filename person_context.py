"""人物上下文注入：从 MaiBot 自带人物数据库读取昵称与印象（只读）。

- person_id = ctx.person.get_id(platform, user_id)
- name  = ctx.person.get_value(person_id, name_field)   → 评论人称化
- state = ctx.person.get_value(person_id, state_field)  → 印象注入 prompt

Host 侧实现（MaiBot 1.2.5 data.py 实测）：
- get_value = getattr(Person, field_name)，无此属性即返回
  {"success": False, "error": ...}（SDK 归一化后**没有 value 键**）
- 昵称属性为 person_name（加载时回退 nickname）；不存在 "state" 属性，
  印象存于 memory_points 列表（元素 "分类:内容:权重"）
- 未认识用户 is_known=False，person_name="未知用户XXXX"——不可当昵称

设计约束：
- **只读不写**：印象维护归 MaiBot 主框架，本插件不产生写入。
- **全链路防御式**：能力不存在/超时/失败 dict/空值一律静默降级——
  昵称回退 QQ 号，印象传空串，绝不阻塞评论主流程。
- 失败只打 debug 日志：好友未与 bot 聊过天是常态而非异常。
"""

import asyncio

# 单次画像查询超时（秒）：person capability 走 Host RPC，挂死时不能拖垮评论循环
_QUERY_TIMEOUT_SEC = 5.0
# 未认识用户的占位名前缀（Host 侧 person_name = "未知用户XXXX"）
_UNKNOWN_NAME_PREFIX = "未知用户"


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


def set_person_context_logger(custom_logger):
    global logger
    logger = custom_logger


def _extract_value(result) -> str:
    """从 capability 返回值提取纯文本。

    成功且归一化后为标量 → 文本；失败 dict（{"success": False, "error": ...}）
    或其他容器 → 空串。**绝不能把 dict 字符串化进 prompt**。
    """
    if isinstance(result, dict):
        # 归一化失败返回 {"success": False, "error": ...}（无 value 键）
        if result.get("success") is False:
            return ""
        if "value" in result:
            result = result["value"]
        else:
            return ""
    if isinstance(result, (dict, list, tuple, set)):
        return ""
    text = str(result or "").strip()
    if text.lower() == "none":
        return ""
    return text


async def fetch_person_context(plugin, user_id: str,
                               name_field: str = "person_name",
                               state_field: str = "memory_points") -> dict:
    """查询指定 QQ 的人物上下文。

    Args:
        plugin: 插件实例（用 plugin.ctx.person）
        user_id: QQ 号字符串
        name_field: 昵称属性名（Host Person 对象属性，默认 "person_name"）
        state_field: 印象属性名（默认 "memory_points"，列表 → 逐行文本）

    Returns:
        {"name": str, "state": str}——查不到的字段为空串/回退，永不抛异常。
        name 未查到时回退为 user_id 本身。
    """
    result = {"name": str(user_id or ""), "state": ""}
    uid = str(user_id or "").strip()
    if not uid:
        return result

    try:
        person = plugin.ctx.person
    except AttributeError:
        # SDK 无 person 能力（旧版 Host / stub 环境）
        logger.debug("ctx.person 不可用，跳过人物上下文")
        return result

    try:
        person_id = await asyncio.wait_for(
            person.get_id(platform="qq", user_id=uid),
            timeout=_QUERY_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.debug(f"person.get_id 超时（>{_QUERY_TIMEOUT_SEC}s）: {uid}")
        return result
    except Exception as e:
        logger.debug(f"person.get_id 失败: {e}（uid={uid}）")
        return result

    pid = str(person_id or "").strip()
    if not pid or pid == "None":
        # 该用户不在人物库（未与 bot 互动过）——常态，静默
        return result

    # 昵称
    try:
        raw_name = await asyncio.wait_for(
            person.get_value(person_id=pid, field_name=name_field),
            timeout=_QUERY_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.debug(f"person.get_value({name_field}) 超时: uid={uid}")
        raw_name = None
    except Exception as e:
        logger.debug(f"person.get_value({name_field}) 失败: {e}（uid={uid}）")
        raw_name = None
    name_text = _extract_value(raw_name)
    # 未认识用户的占位名不可当昵称；host 侧未认识时 is_known=False 且名带前缀
    if name_text and not name_text.startswith(_UNKNOWN_NAME_PREFIX):
        result["name"] = name_text

    # 印象
    try:
        raw_state = await asyncio.wait_for(
            person.get_value(person_id=pid, field_name=state_field),
            timeout=_QUERY_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.debug(f"person.get_value({state_field}) 超时: uid={uid}")
        raw_state = None
    except Exception as e:
        logger.debug(f"person.get_value({state_field}) 失败: {e}（uid={uid}）")
        raw_state = None

    if isinstance(raw_state, (list, tuple)):
        # memory_points: ["分类:内容:权重", ...] → 每行取"内容"段，去权重噪声
        lines = []
        for item in raw_state:
            s = str(item or "").strip()
            if not s:
                continue
            parts = s.split(":", 2)
            # 完整格式取中间段；两段及以下原样保留
            lines.append(parts[1].strip() if len(parts) == 3 else s)
        result["state"] = "\n".join(lines[:5])  # 最多 5 条，防 prompt 膨胀
    else:
        result["state"] = _extract_value(raw_state)

    return result
