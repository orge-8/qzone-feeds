"""诊断：转发类动态生成评论时，实际送给 LLM 的 prompt 长什么样。

用于定位「哈哈转发了个寂寞，原内容是啥呀」这类凭空评论的根因。
运行：python tests/diag_transfer_comment.py
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)

# ---- maibot_sdk stub ----
ms = types.ModuleType("maibot_sdk")


class PluginConfigBase:
    pass


def Field(default=None, default_factory=None, description="", **_kw):
    return default_factory() if default_factory is not None else default


def Command(name=None, pattern=None, **_kw):
    def deco(fn):
        fn._cmd_name, fn._cmd_pattern = name, pattern
        return fn
    return deco


class MaiBotPlugin:
    config_model = None

    def __init__(self):
        pass


ms.PluginConfigBase = PluginConfigBase
ms.Field = Field
ms.Command = Command
ms.MaiBotPlugin = MaiBotPlugin
sys.modules["maibot_sdk"] = ms

pkg = types.ModuleType("qzf")
pkg.__path__ = [PLUGIN_DIR]
sys.modules["qzf"] = pkg

auto_tasks = importlib.import_module("qzf.auto_tasks")
qzone_api = importlib.import_module("qzf.qzone_api")

# ---- 捕获 prompt ----
CAPTURED = {}


async def fake_llm(plugin, prompt):
    CAPTURED["prompt"] = prompt
    return "（模拟 LLM 输出）"


auto_tasks._llm_generate = fake_llm

DEFAULT_TPL = (
    "好友{target_name}发了说说：{content}。"
    "请以 bot 身份写一条自然的评论，口语化、不超过40字、只输出评论内容。"
)


class FakeStore:
    def __init__(self):
        self.seen = set()

    async def is_processed(self, fid, tid=None):
        return fid in self.seen

    async def mark_processed(self, fid, tid=None):
        self.seen.add(fid)
        return True


class FakeApi:
    def __init__(self):
        self.comments = []

    async def comment(self, fid, qq, text):
        self.comments.append((fid, qq, text))
        return True

    async def like(self, fid, qq):
        return True


async def run_case(name, feed):
    store, api = FakeStore(), FakeApi()
    CAPTURED.clear()
    await auto_tasks.process_feeds(
        None, api, store, [feed],
        like_probability=0.0, comment_probability=1.0,
        action_interval=0, comment_prompt_tpl=DEFAULT_TPL,
    )
    prompt = CAPTURED.get("prompt")
    print(f"\n{'=' * 66}")
    print(f"场景：{name}")
    print(f"  feed.content = {feed.get('content')!r}")
    print(f"  feed.rt_con  = {feed.get('rt_con')!r}")
    print(f"  feed.images  = {feed.get('images')!r}")
    if prompt is None:
        print("  → 未调用 LLM（跳过评论）")
    else:
        print(f"  实际 prompt：\n    {prompt}")
    print(f"  是否发出评论：{'是' if api.comments else '否'}"
          f"{'  ' + repr(api.comments[0][2]) if api.comments else ''}")
    return prompt, bool(api.comments)


async def main():
    base = {"target_qq": "20001", "tid": "t", "videos": [], "comments": []}

    await run_case("A 正常纯文本动态", {**base, "content": "今天天气真好", "rt_con": "", "images": []})

    await run_case("B 转发动态：正文空 + rt_con 空（抓取失败）",
                   {**base, "content": "", "rt_con": "", "images": []})

    await run_case("C 转发动态：正文空 + rt_con 抓到了转发标记文字",
                   {**base, "content": "", "rt_con": "转发了说说", "images": []})

    await run_case("D 纯图片动态：无文字无转发", {**base, "content": "", "rt_con": "", "images": []})

    await run_case("E 转发动态：只有图，无正文无转发文字",
                   {**base, "content": "", "rt_con": "", "images": ["[图片]"]})

    print(f"\n{'=' * 66}")
    print("结论：见上方各场景 '是否发出评论' 行")


if __name__ == "__main__":
    asyncio.run(main())
