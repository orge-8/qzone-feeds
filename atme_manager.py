"""被@检测与回复：「与我相关」接口轮询 → 去重 → 定位评论 → LLM 生成回复。

v1.3.0 新增。与 reply_manager（自己说说被评论）互补：
- mention（正文@我）     → 直接评论该说说
- comment_mention（评论@我）→ 定位那条评论回评；定位失败降级为评论说说
- other（赞/评论/回复/访问）→ 一律忽略（评论我自己的说说由 reply_manager 负责）

去重 key：atme:{post_uin}:{post_tid}（走 ProcessedStore，同一说说只处理一次，
多次 @/赞/评论合并为一次唤醒）。定位评论复用 get_list（msglist_v6，读他人
空间不需额外接口），定位不到就降级为评论说说——宁可回复位置粗一点，
也不丢互动。

已知盲区（真机探测 2026-09-26）：好友在**别人的说说**下回复 bot 的评论，
动作文案是「回复」而非「回复提到我」，会被归 other 忽略——该场景 v1.3.0
不覆盖，避免在第三方说说下自作主张接话。
"""

import asyncio
import datetime
import random

from .reply_manager import (_llm_generate, format_comment_time,
                            looks_like_identity_confusion,
                            looks_like_refusal, sanitize_llm_output)


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


def set_atme_manager_logger(custom_logger):
    global logger
    logger = custom_logger


_DEFAULT_ATME_PROMPT = (
    "你是{bot_name}，你在QQ空间被好友@了。"
    "说说内容：{post}；互动者：{nickname}；相关内容：{mention_content}；"
    "互动时间：{created_time}；当前时间：{current_time}。"
    "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"
    "留意互动时间与当前时间的间隔，别把几天前的@当成刚发的。"
)


class AtmeManager:
    """「与我相关」被@轮询与回复（配置走 plugin.config.auto.atme_*）。"""

    def __init__(self, plugin, store):
        self._plugin = plugin
        self._store = store

    def _cfg(self, name: str, default):
        try:
            return getattr(self._plugin.config.auto, name)
        except AttributeError:
            return default

    def _seen_key(self, post_uin, post_tid) -> str:
        return f"atme:{post_uin}:{post_tid}"

    async def reply_atme_mentions(self, api) -> tuple[bool, str]:
        """拉取「与我相关」并回复其中的被@条目。

        Returns:
            (success, summary)
        """
        poll_count = max(1, min(int(self._cfg("atme_poll_count", 10) or 10), 20))
        max_replies = max(1, int(self._cfg("atme_max_replies_per_run", 5) or 5))
        interval_base = float(self._cfg("reply_interval_sec", 3) or 3)
        prompt_tpl = str(self._cfg("atme_prompt", "") or _DEFAULT_ATME_PROMPT)

        items = await api.get_atme_list(count=poll_count)
        if not items:
            return False, "「与我相关」列表为空"
        if isinstance(items[0], dict) and items[0].get("error"):
            return False, str(items[0]["error"])

        my_uin = str(api.uin)
        bot_name = getattr(api, "qq_nickname", "") or "我"
        mention_count = 0
        reply_count = 0
        for item in items:
            action = item.get("action")
            if action not in ("mention", "comment_mention"):
                continue
            post_uin = item.get("post_uin")
            post_tid = item.get("post_tid")
            if not post_uin or not post_tid:
                continue  # 无说说归属（访问主页等）
            if str(post_uin) == my_uin:
                continue  # 自己的说说 → 被评论场景，reply_manager 负责
            key = self._seen_key(post_uin, post_tid)
            mention_count += 1
            if await self._store.is_processed(key):
                continue
            # 先标记后处理：同一说说上的多次 @/赞/评论合并为一次唤醒，
            # 即使本轮回复失败也不重试（防风控优先于送达率，与 reply_manager 同哲学）
            await self._store.mark_processed(key)
            if reply_count >= max_replies:
                logger.info(f"被@回复达到单轮上限({max_replies})，剩余条目仅标记已读: {key}")
                continue
            await asyncio.sleep(interval_base + random.random())
            try:
                ok = await self._reply_one(api, item, prompt_tpl, bot_name,
                                           action == "comment_mention")
                if ok:
                    reply_count += 1
            except Exception as e:
                logger.error(f"处理被@条目 {key} 出错: {e}")

        if mention_count == 0:
            return True, "「与我相关」无被@条目"
        return True, f"「与我相关」{mention_count}条被@，回复{reply_count}条"

    async def _reply_one(self, api, item: dict, prompt_tpl: str, bot_name: str,
                         is_comment_mention: bool) -> bool:
        """回复单个被@条目。评论@优先定位原评论回评，定位不到降级评论说说。"""
        post_uin = str(item["post_uin"])
        post_tid = str(item["post_tid"])
        actor_uin = str(item.get("uin") or "")
        nickname = item.get("nickname") or "好友"
        mention_content = item.get("content") or ""

        # comment_mention：定位该互动者在说说下含 @/bot昵称 的最新评论（倒序找）
        reply_target = None
        post_content = mention_content
        created_time_raw = item.get("time", "")
        if is_comment_mention:
            located = await self._locate_mention_comment(api, post_uin, post_tid,
                                                         actor_uin, bot_name)
            if located is not None:
                reply_target = located
                post_content = located["post_content"] or mention_content
                # 定位到的评论时间更精确，优先采用；缺失则退回条目 abstime
                created_time_raw = located.get("created_time") or created_time_raw

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_time_str = format_comment_time(created_time_raw)
        try:
            prompt = prompt_tpl.format(
                bot_name=bot_name,
                post=post_content,
                nickname=nickname,
                mention_content=mention_content,
                created_time=created_time_str,
                current_time=now_str,
                now_time=now_str,
            )
            # 旧版模板（config.toml 已落盘不随插件升级更新）缺时间占位符时补齐
            missing = []
            if "{created_time}" not in prompt_tpl:
                missing.append(f"互动时间：{created_time_str}")
            if "{current_time}" not in prompt_tpl and "{now_time}" not in prompt_tpl:
                missing.append(f"当前时间：{now_str}")
            if missing:
                prompt += ("（" + "；".join(missing)
                           + "。请留意互动时间与当前时间的间隔，别把几天前的@当成刚发的。）")
        except (KeyError, IndexError):
            # 模板占位符不匹配时回退默认模板（也带时间上下文）
            prompt = (
                f"你是{bot_name}，你在QQ空间被好友@了。"
                f"说说内容：{post_content}；互动者：{nickname}；相关内容：{mention_content}；"
                f"互动时间：{created_time_str}；当前时间：{now_str}。"
                "请直接输出回复内容，口语化、不超过50字、不要引号和多余说明。"
                "留意互动时间与当前时间的间隔，别把几天前的@当成刚发的。"
            )
        reply_message = sanitize_llm_output(await _llm_generate(self._plugin, prompt))
        if not reply_message:
            logger.warning("被@回复内容为空，跳过")
            return False
        if looks_like_refusal(reply_message):
            logger.warning(f"LLM 返回拒答内容，已跳过被@回复: {reply_message[:50]}")
            return False
        if looks_like_identity_confusion(reply_message):
            # @我的是好友不是主人，对好友称「主人」等于 bot 公开认错爹
            logger.warning(f"LLM 称呼好友为主人（身份错位），已跳过被@回复: {reply_message[:50]}")
            return False

        if reply_target is not None:
            ok = await api.reply(post_tid, post_uin, nickname, actor_uin,
                                 reply_message, reply_target["comment_tid"])
            if ok:
                logger.info(f"被@(评论)已回评: {post_uin}/{post_tid} ← {reply_message[:30]}")
                return True
            logger.warning("回评失败，降级为评论说说")
        ok = await api.comment(post_tid, post_uin, reply_message)
        if ok:
            logger.info(f"被@已评论说说: {post_uin}/{post_tid} ← {reply_message[:30]}")
        else:
            logger.error(f"被@评论说说失败: {post_uin}/{post_tid}")
        return ok

    async def _locate_mention_comment(self, api, post_uin: str, post_tid: str,
                                      actor_uin: str, bot_name: str) -> dict | None:
        """在目标说说评论中定位互动者最新一条含 @/bot昵称 的评论。

        经 get_list（msglist_v6）扫描对方最近动态找 tid 匹配的说说；
        找不到说说或评论时返回 None（调用方降级为评论说说）。
        """
        try:
            feeds = await api.get_list(post_uin, 10, filter=False, describe_images=False)
        except Exception as e:
            logger.warning(f"拉取 {post_uin} 动态定位评论失败: {e}")
            return None
        if not feeds or (isinstance(feeds[0], dict) and feeds[0].get("error")):
            return None
        feed = next((f for f in feeds if str(f.get("tid")) == post_tid), None)
        if feed is None:
            logger.info(f"目标说说 {post_uin}/{post_tid} 不在对方最近10条动态中，降级评论说说")
            return None
        bot_nick = (bot_name or "").strip()
        located = None  # 倒序找最新一条
        for comment in reversed(feed.get("comments") or []):
            if str(comment.get("qq_account", "")) != actor_uin:
                continue
            content = str(comment.get("content", "") or "")
            if "@" in content or (bot_nick and bot_nick in content):
                located = {
                    "comment_tid": comment.get("comment_tid"),
                    "post_content": str(feed.get("content", "") or ""),
                    # 定位到的评论自带时间（get_list 归一化字段），
                    # 优先于「与我相关」条目的 abstime（前者更精确）
                    "created_time": comment.get("created_time", ""),
                }
                break
        if located is None or not located["comment_tid"]:
            return None
        return located
