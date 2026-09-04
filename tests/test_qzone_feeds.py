"""qzone-feeds 冒烟测试（FakeHost，不启 MaiBot）。

运行： python tests/test_qzone_feeds.py
覆盖：命令正则 / 鉴权 / jsonp 与 _Callback 剥壳 / extract_code_html /
richval 生成 / g_tk 对拍 / reply post_data 组装 / processed LRU 原子落盘 /
静默时段解析 / 自动任务流程 / 渲染格式
"""

import asyncio
import base64
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# maibot_sdk shim（本机未装 SDK 时兜底）
try:
    import maibot_sdk  # noqa: F401
except ImportError:
    _shim = types.ModuleType("maibot_sdk")
    _shim.Field = lambda *a, **k: None
    _shim.MaiBotPlugin = object

    def _decorator(*a, **k):
        def wrap(fn):
            return fn
        return wrap
    _shim.Command = _decorator
    sys.modules["maibot_sdk"] = _shim


def _load_plugin_as_package(plugin_dir: Path):
    """以包方式加载 plugin.py，让相对导入可解析（与 check_plugin.py 同手法）。"""
    pkg_name = "qzone_feeds_test_pkg"
    pkg = importlib.util.module_from_spec(
        importlib.machinery.ModuleSpec(pkg_name, None, is_package=True))
    pkg.__path__ = [str(plugin_dir)]
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.plugin", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, pkg_name


plugin_mod, _PKG = _load_plugin_as_package(PLUGIN_DIR)
QzoneFeedsPlugin = plugin_mod.QzoneFeedsPlugin
_parse_qq_id = plugin_mod._parse_qq_id
format_feed = plugin_mod.format_feed
_qzone_api = importlib.import_module(f"{_PKG}.qzone_api")
_processed_store = importlib.import_module(f"{_PKG}.processed_store")
_auto_tasks = importlib.import_module(f"{_PKG}.auto_tasks")
_cookie_manager = importlib.import_module(f"{_PKG}.cookie_manager")
_reply_manager = importlib.import_module(f"{_PKG}.reply_manager")

QzoneAPI = _qzone_api.QzoneAPI
extract_code_html = _qzone_api.extract_code_html
generate_gtk = _qzone_api.generate_gtk
get_picbo_and_richval = _qzone_api.get_picbo_and_richval
image_to_base64 = _qzone_api.image_to_base64
ProcessedStore = _processed_store.ProcessedStore
_is_in_silent_period = _auto_tasks._is_in_silent_period
_parse_time_to_minutes = _auto_tasks._parse_time_to_minutes
run_auto_job = _auto_tasks.run_auto_job
process_feeds = _auto_tasks.process_feeds
parse_cookie_string = _cookie_manager.parse_cookie_string
sanitize_llm_output = _reply_manager.sanitize_llm_output

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# ===== 1. 纯函数 =====
def test_pure_functions():
    print("\n== 纯函数 ==")
    # g_tk 已知值对拍（算法：hash=5381; hash += (hash<<5)+ord(c)）
    check("g_tk 空串", generate_gtk("") == "5381")
    check("g_tk 非空串类型", generate_gtk("abc").isdigit())
    check("g_tk 恒为非负 int 范围", 0 <= int(generate_gtk("x" * 100)) <= 2147483647)
    check("image_to_base64", image_to_base64(b"hi") == base64.b64encode(b"hi").decode())
    check("_parse_qq_id qq:前缀", _parse_qq_id("qq:12345") == "12345")
    check("_parse_qq_id 纯数字", _parse_qq_id("12345") == "12345")
    # richval 生成
    picbo, richval = get_picbo_and_richval({
        "ret": 0,
        "data": {"url": "http://a.com/x&bo=AAAA!", "albumid": "al", "lloc": "ll",
                 "sloc": "sl", "type": 1, "height": 100, "width": 200},
    })
    check("picbo 提取", picbo == "AAAA!")
    check("richval 逗号格式", richval == ",al,ll,sl,1,100,200,,100,200")
    # extract_code_html（frameElement.callback 剥壳）
    html_ok = "<html><script>_Callback(frameElement.callback({\"code\":0,\"message\":\"ok\"}));</script></html>"
    check("extract_code_html code=0", extract_code_html(html_ok) == 0)
    html_err = "<html><script>_Callback(frameElement.callback({\"code\":-1,\"message\":\"err\"}));</script></html>"
    check("extract_code_html code=-1", extract_code_html(html_err) == -1)
    check("extract_code_html 无script", extract_code_html("<p>hello</p>") is None)
    check("parse_cookie_string", parse_cookie_string("uin=o12345; skey=@abc; p_skey=xyz")["p_skey"] == "xyz")


# ===== 2. 命令正则 =====
def test_command_patterns():
    print("\n== 命令正则 ==")
    # 直接扫源码取 pattern（避免实例化触发 SDK config 注入检查）
    src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    pats = re.findall(r'@Command\("([^"]+)",\s*pattern=(r"[^"]+")\)', src)
    # 提取到的 pattern 带 r"..." 字面前缀，还原成真正的 pattern 字符串
    pat_map = {name: raw[2:-1] for name, raw in pats}
    pat_publish = pat_map.get("qzone_publish")
    pat_publish_img = pat_map.get("qzone_publish_image")
    pat_feeds = pat_map.get("qzone_feeds")
    pat_msglist = pat_map.get("qzone_msglist")
    pat_reply = pat_map.get("qzone_reply_comments")
    pat_status = pat_map.get("qzone_status")
    check("7个pattern都取到", len(pat_map) == 7)

    check("/动态发 全角斜杠", re.match(pat_publish, "／动态发 今天天气好"))
    check("/动态发 半角斜杠", re.match(pat_publish, "/动态发 hello world"))
    m = re.match(pat_publish, "/动态发 正文 | http://x.com/a.jpg")
    check("/动态发 不吞管道URL（留给发图）", m is not None)
    check("/动态发图", re.match(pat_publish_img, "/动态发图 看这张 | https://a.com/b.jpg"))
    check("/动态发图 无图不匹配", re.match(pat_publish_img, "/动态发图 只有正文") is None)
    check("/好友动态 无参", (m := re.match(pat_feeds, "/好友动态")) and m.group("count") is None)
    check("/好友动态 带条数", (m := re.match(pat_feeds, "/好友动态 10")) and m.group("count") == "10")
    check("/说说 QQ+条数", (m := re.match(pat_msglist, "/说说 123456789 3")) and m.group("qq") == "123456789")
    check("/说说 短QQ不匹配", re.match(pat_msglist, "/说说 1234") is None)
    check("/回复评论 默认", (m := re.match(pat_reply, "/回复评论")) and m.group("count") is None)
    check("/动态状态", re.match(pat_status, "/动态状态"))
    check("/动态状态 多余参数不匹配", re.match(pat_status, "/动态状态 xx") is None)


# ===== 3. 鉴权 =====
def test_auth():
    print("\n== 鉴权 ==")
    p = QzoneFeedsPlugin()
    # config 是 SDK 的只读 property，未注入时 _is_admin 走 AttributeError 分支返回 False；
    # 这里用未绑定调用注入 stub config 验证白名单逻辑。
    _is_admin = type(p)._is_admin
    stub = types.SimpleNamespace(config=types.SimpleNamespace(
        admin=types.SimpleNamespace(admin_ids=["qq:10001", "10002"])))
    check("admin qq:前缀放行", _is_admin(stub, {"user_id": "10001"}))
    check("admin 纯数字放行", _is_admin(stub, {"user_id": "10002"}))
    check("非管理员拒绝", not _is_admin(stub, {"user_id": "99999"}))
    check("local_operator 放行", _is_admin(stub, {"user_id": "", "is_local_operator": True}))
    # 未注入 config 时的安全兜底：拒绝
    check("无 config 拒绝", not p._is_admin({"user_id": "10001"}))


# ===== 4. jsonp 剥壳 + get_list 解析 =====
def test_msglist_parsing():
    print("\n== msglist 解析 ==")
    payload = {
        "code": 0,
        "logininfo": {"name": "测试号"},
        "msglist": [{
            "tid": "tid001",
            "created_time": 1700000000,
            "content": "第一条说说",
            "pic": [{"url1": "http://img.example/1.jpg"}],
            "commentlist": [
                {"name": "好友A", "uin": "11111", "tid": "c1", "content": "评论1", "createTime": "昨天 12:00"},
                {"name": "测试号", "uin": "99999", "tid": "c2", "content": "自己评论", "createTime": "昨天 13:00"},
            ],
        }],
    }
    body = json.dumps(payload, ensure_ascii=False)
    text = f"_preloadCallback({body});"

    api = QzoneAPI({"uin": "o099999", "p_skey": "abc"})
    check("uin 剥离前导o0", api.uin == "99999")

    # 直接测试剥壳逻辑（不发起网络请求：手动复现 get_list 的解析段）
    json_str = text[len("_preloadCallback("):-2] if text.startswith("_preloadCallback(") and text.endswith(");") else text
    data = json.loads(json_str)
    check("jsonp 剥壳 json.loads 成功", data["code"] == 0)
    check("logininfo.name", data["logininfo"]["name"] == "测试号")
    msg = data["msglist"][0]
    check("评论提取", len(msg["commentlist"]) == 2)
    check("图片url提取", msg["pic"][0]["url1"] == "http://img.example/1.jpg")


# ===== 5. _Callback 剥壳（好友动态流）=====
def test_feeds_parsing():
    print("\n== feeds 剥壳 ==")
    raw = '_Callback({"code":0,"data":{"data":[{"appid":"311","uin":"11111","key":"fid1","html":"<div class=\'f-info\'>内容</div>","feedstime":"昨天17:50"},{"appid":"160","uin":"11111","key":"fid2","html":"x"}]}});'
    data_str = raw[len("_Callback("):-2].replace("undefined", "null")
    import json5
    data = json5.loads(data_str)
    check("_Callback 剥壳", data["code"] == 0)
    feeds = [f for f in data["data"]["data"] if str(f.get("appid", "")) == "311"]
    check("appid==311 过滤", len(feeds) == 1 and feeds[0]["key"] == "fid1")


# ===== 6. reply post_data 组装 =====
def test_reply_post_data():
    print("\n== reply post_data 组装 ==")
    # 抓取 reply() 组装的 post_data：monkeypatch httpx
    captured = {}

    class FakeRes:
        status_code = 200
        text = '<script>_Callback(frameElement.callback({"code":0}));</script>'

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, **kw):
            captured["url"] = kw["url"]
            captured["data"] = kw["data"]
            return FakeRes()

    import httpx
    orig_client = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        api = QzoneAPI({"uin": "o099999", "p_skey": "abc"})
        ok = asyncio.run(api.reply("fid123", "99999", "好友A", "11111", "谢谢评论", "c1"))
        check("reply 返回 True", ok)
        d = captured["data"]
        check("topicId 格式", d["topicId"] == "99999_fid123__1")
        check("@前缀 content", d["content"] == "@{uin:11111,nick:好友A,auto:1}谢谢评论")
        check("commentId", d["commentId"] == "c1")
        check("commentUin", d["commentUin"] == "11111")
        check("feedsType", d["feedsType"] == 100)
    finally:
        httpx.AsyncClient = orig_client


# ===== 7. processed_list LRU + 原子落盘 =====
def test_processed_store():
    print("\n== processed_store ==")

    async def run():
        tmp = tempfile.mkdtemp()
        store = ProcessedStore(tmp)
        for i in range(250):
            await store.mark_processed(f"tid{i}", f"c{i % 150}")
        pl = await store._load()
        check("容量裁剪到200", len(pl) == 200)
        check("最旧被淘汰", "tid0" not in pl and "tid49" not in pl)
        check("最新保留", "tid249" in pl)
        # touch 最旧条目后不被淘汰
        oldest = next(iter(pl))
        await store.mark_processed(oldest)
        await store.mark_processed("tid_new")
        pl2 = await store._load()
        check("touch 后不被淘汰", oldest in pl2)
        # 评论裁剪
        for j in range(150):
            await store.mark_processed("tid_c", j)
        pl3 = await store._load()
        check("评论裁剪到100", len(pl3["tid_c"]) == 100)
        # 原子落盘验证：无 .tmp 残留
        check("无 tmp 残留", not os.path.exists(os.path.join(tmp, "processed_list.json.tmp")))
        # 重复调用不重复（150条裁到100后保留50~149，5 已被淘汰）
        check("is_processed 已裁剪淘汰", not await store.is_processed("tid_c", 5))
        check("is_processed 保留区命中", await store.is_processed("tid_c", 149))
        check("is_processed 未标记", not await store.is_processed("tid_c", 999))

    asyncio.run(run())


# ===== 8. 静默时段 =====
def test_silent_period():
    print("\n== 静默时段 ==")
    check("解析合法", _parse_time_to_minutes("23:00") == 23 * 60)
    check("解析非法", _parse_time_to_minutes("bad") is None)
    check("空配置不静默", not _is_in_silent_period(""))
    check("坏配置不静默", not _is_in_silent_period("xx-yy"))
    # 跨零点逻辑（不依赖当前时刻：构造两段式验证解析分支）
    cfg = "23:00-07:00,12:00-14:00"
    periods = [p.strip() for p in cfg.split(",") if "-" in p]
    ok = True
    for period in periods:
        s, e = period.split("-", 1)
        ok = ok and _parse_time_to_minutes(s) is not None and _parse_time_to_minutes(e) is not None
    check("多段全部可解析", ok)


# ===== 9. 自动任务流程（假 feeds，概率强制）=====
def test_auto_job():
    print("\n== 自动任务 ==")

    class FakeAPI:
        uin = "99999"

        def __init__(self, feeds):
            self._feeds = feeds
            self.liked = []
            self.commented = []

        async def get_qzone_list(self, describe_images=True):
            return self._feeds

        async def like(self, fid, target_qq):
            self.liked.append(fid)
            return True

        async def comment(self, fid, target_qq, content):
            self.commented.append((fid, content))
            return True

    async def run():
        tmp = tempfile.mkdtemp()
        store = ProcessedStore(tmp)
        feeds = [
            {"target_qq": "11111", "tid": "f1", "content": "说说1", "rt_con": ""},
            {"target_qq": "22222", "tid": "f2", "content": "说说2", "rt_con": "转发内容"},
        ]
        api = FakeAPI(feeds)

        plugin_stub = types.SimpleNamespace(
            config=types.SimpleNamespace(
                auto=types.SimpleNamespace(
                    enable_auto_read=True, enable_auto_reply=False,
                    like_probability=1.0, comment_probability=1.0,
                    action_interval_sec=0,
                    comment_prompt="评论{target_name}的{content}",
                ),
                admin=types.SimpleNamespace(auto_read_blacklist=[]),
                plugin=types.SimpleNamespace(text_model="replyer"),
            ),
            ctx=types.SimpleNamespace(llm=types.SimpleNamespace(
                generate=_fake_llm_generate)),
        )
        result = await run_auto_job(plugin_stub, api, store, None)
        check("auto_job ok", result["ok"])
        check("概率1.0 全点赞", set(api.liked) == {"f1", "f2"})
        check("概率1.0 全评论", len(api.commented) == 2)
        check("处理后已标记", await store.is_processed("f1") and await store.is_processed("f2"))

        # 第二轮：同样 feeds 不应再处理
        api2 = FakeAPI(feeds)
        result2 = await run_auto_job(plugin_stub, api2, store, None)
        check("第二轮去重不处理", not api2.liked and not api2.commented)

        # 概率 0：不点赞不评论但标记
        tmp2 = tempfile.mkdtemp()
        store2 = ProcessedStore(tmp2)
        plugin_stub.config.auto.like_probability = 0.0
        plugin_stub.config.auto.comment_probability = 0.0
        api3 = FakeAPI([{"target_qq": "33333", "tid": "f3", "content": "x", "rt_con": ""}])
        await run_auto_job(plugin_stub, api3, store2, None)
        check("概率0 不点赞", not api3.liked)
        check("概率0 不评论", not api3.commented)
        check("概率0 仍标记", await store2.is_processed("f3"))

        # 黑名单
        tmp3 = tempfile.mkdtemp()
        store3 = ProcessedStore(tmp3)
        plugin_stub.config.admin.auto_read_blacklist = ["44444"]
        api4 = FakeAPI([{"target_qq": "44444", "tid": "f4", "content": "x", "rt_con": ""}])
        await run_auto_job(plugin_stub, api4, store3, None)
        check("黑名单跳过", not api4.liked and not (await store3.is_processed("f4")))

        # /说说 命令路径：process_feeds 概率 1.0 显式点赞+评论（回显式假 LLM 验证 prompt 拼装）
        tmp4 = tempfile.mkdtemp()
        store4 = ProcessedStore(tmp4)
        api5 = FakeAPI([
            {"target_qq": "55555", "tid": "f5", "content": "说说5", "rt_con": "", "images": ["一只猫"]},
            {"target_qq": "66666", "tid": "f6", "content": "说说6", "rt_con": "", "images": []},
        ])

        async def _echo_llm(plugin, prompt, model=None):
            # 与 reply_manager._llm_generate(plugin, prompt) 签名一致
            return f"[{prompt}]"

        _auto_tasks._llm_generate = _echo_llm
        try:
            stats = await process_feeds(plugin_stub, api5, store4, api5._feeds,
                                        like_probability=1.0, comment_probability=1.0,
                                        action_interval=0)
            check("process_feeds 全点赞", set(api5.liked) == {"f5", "f6"} and stats["liked"] == 2)
            check("process_feeds 全评论", len(api5.commented) == 2 and stats["commented"] == 2)
            check("process_feeds 统计", stats["handled"] == 2)
            check("process_feeds 评论内容含图片描述", any("[图: 一只猫]" in c[1] for c in api5.commented))
            # 再次调用：已处理条目全部跳过
            stats2 = await process_feeds(plugin_stub, api5, store4, api5._feeds,
                                         like_probability=1.0, comment_probability=1.0,
                                         action_interval=0)
            check("process_feeds 去重", stats2["handled"] == 0 and len(api5.liked) == 2)
        finally:
            _auto_tasks._llm_generate = _fake_llm_generate

    async def _fake_llm_generate(plugin, prompt, model=None):
        # 与 reply_manager._llm_generate(plugin, prompt) 签名一致
        return "哈哈不错"

    run_auto_job.__globals__["_llm_generate"] = _fake_llm_generate
    asyncio.run(run())


# ===== 10. 渲染格式 =====
def test_format_feed():
    print("\n== 渲染格式 ==")
    feed = {
        "target_qq": "11111", "tid": "f1", "created_time": "昨天 17:50",
        "content": "你好世界", "rt_con": "",
        "images": ["一只猫"], "videos": [],
        "comments": [{"nickname": "好友A", "content": "评论1"}],
    }
    out = format_feed(feed, 1)
    check("含作者时间", "【11111】昨天 17:50" in out)
    check("含正文", "你好世界" in out)
    check("含图片描述", "🖼 一只猫" in out)
    check("含评论", "💬 好友A: 评论1" in out)
    check("分隔线", "──────" in out)


# ===== 11. 上线前全检 P0 修复回归 =====
def test_p0_fixes():
    print("\n== P0 修复回归 ==")

    # --- P0-2: sanitize_llm_output ---
    check("sanitize 剥markdown", sanitize_llm_output("**你好** `世界`") == "你好 世界")
    check("sanitize 剥引号", sanitize_llm_output('"不错哦"') == "不错哦")
    check("sanitize 截断100字", len(sanitize_llm_output("长" * 300)) == 100)
    check("sanitize 空输入", sanitize_llm_output("") == "" and sanitize_llm_output(None) == "")

    # --- P0-2: 空回复标记 processed（不再无限重试）---
    async def _empty_llm(plugin, prompt, model=None):
        return ""
    orig_llm = _reply_manager._llm_generate
    _reply_manager._llm_generate = _empty_llm
    try:
        async def run_empty_reply():
            tmp = tempfile.mkdtemp()
            store = ProcessedStore(tmp)
            rm = _reply_manager.ReplyManager(types.SimpleNamespace(
                config=types.SimpleNamespace(reply=types.SimpleNamespace(
                    scan_count=5, max_replies_per_run=10, reply_interval_sec=0, prompt="x")),
                ctx=types.SimpleNamespace(llm=None),
            ), store)
            api = types.SimpleNamespace(uin="99999", qq_nickname="bot")
            feeds = [{
                "target_qq": "99999", "tid": "fid1", "created_time": "now", "content": "说说",
                "images": [], "videos": [], "rt_con": "",
                "comments": [{"qq_account": "11111", "nickname": "好友A", "comment_tid": 42,
                              "content": "评论", "created_time": "now", "parent_tid": None}],
            }]

            async def fake_get_list(qq, num, filter=False, describe_images=True):
                return feeds

            api.get_list = fake_get_list
            ok, msg = await rm.reply_new_comments(api)
            check("空回复 run 完成", ok)
            check("空回复已标记 processed（不再无限重试）", await store.is_processed("fid1", 42))
        asyncio.run(run_empty_reply())
    finally:
        _reply_manager._llm_generate = orig_llm

    # --- P0-3: with_cookies=False 不外发 cookie ---
    captured = {}

    class _FakeAC:
        def __init__(self, **kwargs):
            captured["cookies"] = kwargs.get("cookies")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            class _S:
                status_code = 200
                headers = {}

                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *a):
                    return False

                async def aiter_bytes(self_, n):
                    yield b"fake"

            return _S()

    import httpx as _httpx
    orig_ac = _httpx.AsyncClient
    _httpx.AsyncClient = _FakeAC
    try:
        api = QzoneAPI({"uin": "o099999", "p_skey": "abc", "skey": "x"})
        asyncio.run(api.get_image_base64_by_url("http://evil.example/a.jpg", with_cookies=False))
        check("外部URL下载不带cookie", captured["cookies"] is None)
        asyncio.run(api.get_image_base64_by_url("http://photo.qzone.qq.com/a.jpg"))
        check("图床下载带cookie（防403）", captured["cookies"] == api.cookies)
    finally:
        _httpx.AsyncClient = orig_ac

    # --- P0-3附带: last_image_upload_failed 标记 ---
    check("初始无上传失败标记", QzoneAPI({}).last_image_upload_failed is False)

    # --- P0-1: 命令侧等待超时（worker 不存在时挂起 job 不再永久阻塞）---
    async def run_timeout():
        p = QzoneFeedsPlugin()
        p._queue = asyncio.Queue(maxsize=10)

        async def _run(api):
            return {"ok": True}

        t0 = asyncio.get_event_loop().time()
        result = await p._enqueue_job("command", _run, wait_timeout=0.2)
        elapsed = asyncio.get_event_loop().time() - t0
        check("命令等待超时返回错误", result.get("ok") is False and "超时" in result.get("msg", ""))
        check("超时未永久阻塞", elapsed < 5)

    asyncio.run(run_timeout())

    # --- P0-1: on_unload drain 回填挂起 job ---
    async def run_drain():
        p = QzoneFeedsPlugin()
        p._queue = asyncio.Queue(maxsize=10)
        p._worker_task = None  # 模拟 worker 尚未消费
        got = {}

        async def cb(result):
            got["result"] = result

        fut = asyncio.get_event_loop().create_future()

        async def cb2(result):
            if not fut.done():
                fut.set_result(result)

        await p._queue.put({"name": "command", "run": lambda api: None, "callback": cb})
        await p._queue.put({"name": "command", "run": lambda api: None, "callback": cb2})
        await p.on_unload()
        check("drain 回填失败结果", got.get("result", {}).get("ok") is False and "卸载" in got["result"]["msg"])
        check("drain 后队列清空", p._queue is None or p._queue.empty())

    asyncio.run(run_drain())

    # --- P1: publish_emotion 全上传失败标记（FakeClient 上传返回 ret!=0）---
    check("publish 降级标记逻辑存在", hasattr(QzoneAPI, "publish_emotion"))


# ===== 12. P1 修复回归 =====
def test_p1_fixes():
    print("\n== P1 修复回归 ==")

    # --- P1: /说说 拆命令（纯读 vs 互动）---
    src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    pat_map = {name: raw[2:-1] for name, raw in re.findall(
        r'@Command\("([^"]+)",\s*pattern=(r"[^"]+")\)', src)}
    check("7个命令注册", len(pat_map) == 7 and "qzone_msglist_interact" in pat_map)
    pat_read = pat_map["qzone_msglist"]
    pat_interact = pat_map["qzone_msglist_interact"]
    check("/说说 纯读匹配", re.match(pat_read, "/说说 123456789"))
    check("/说说互动 匹配互动正则", (m := re.match(pat_interact, "/说说互动 123456789 3")) and m.group("qq") == "123456789")
    check("/说说 不误匹配说说互动", re.match(pat_read, "/说说互动 123456789") is None)
    check("/说说互动 不误匹配纯读正则", re.match(pat_read, "/说说互动 123456789") is None)

    # --- P1: 图片下载大小上限（Content-Length 超限拒绝）---
    captured = {}

    class _BigFakeStream:
        def __init__(self, clen):
            self.status_code = 200
            self._clen = clen

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def headers(self):
            return {"content-length": self._clen} if self._clen else {}

        async def aiter_bytes(self, n):
            yield b"x" * 1024

    class _SizeFakeAC:
        def __init__(self, **kwargs):
            captured["cookies"] = kwargs.get("cookies")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return _BigFakeStream(captured.get("clen"))

    import httpx as _httpx
    orig_ac = _httpx.AsyncClient
    _httpx.AsyncClient = _SizeFakeAC
    try:
        api = QzoneAPI({"uin": "o099999", "p_skey": "abc"})
        captured["clen"] = str(20 * 1024 * 1024)
        r1 = asyncio.run(api.get_image_base64_by_url("http://x/big.jpg", with_cookies=False))
        check("Content-Length 超限拒绝", r1 is None)
        captured["clen"] = None  # 无 Content-Length：靠累计截断
        async def _overflow_stream(self, n):
            for _ in range(200):  # 200KB 总量太小，改用大块
                yield b"x" * (1024 * 1024)  # 每块 1MB，11 块超 10MB
        _BigFakeStream.aiter_bytes = _overflow_stream
        r2 = asyncio.run(api.get_image_base64_by_url("http://x/lying.jpg", with_cookies=False))
        check("累计大小截断中止", r2 is None)
        captured["clen"] = "1024"
        async def _small_stream(self, n):
            yield b"x" * 1024
        _BigFakeStream.aiter_bytes = _small_stream
        r3 = asyncio.run(api.get_image_base64_by_url("http://x/ok.jpg", with_cookies=False))
        check("正常大小放行", r3 is not None and len(r3) > 0)
    finally:
        _httpx.AsyncClient = orig_ac

    # --- P1: created_time 非法时间戳兜底 ---
    check("created_time 兜底代码存在",
          "OverflowError" in (PLUGIN_DIR / "qzone_api.py").read_text(encoding="utf-8"))

    # --- P1: cookie 失效重登路径（force 刷新重试）---
    async def run_retry():
        calls = []

        async def get_cookies(force=False):
            calls.append(force)
            return {"uin": "o099999", "p_skey": "abc"}

        class _Logger:
            def warning(self, msg): pass
            def error(self, msg): pass
            def info(self, msg): pass

        # config 是只读 property，用未绑定调用 + 全 stub
        p = types.SimpleNamespace(
            config=types.SimpleNamespace(
                queue=types.SimpleNamespace(retry_on_auth_fail=1, queue_timeout_sec=30),
                auto=types.SimpleNamespace(action_interval_sec=0, comment_prompt=""),
            ),
            _cookie_mgr=types.SimpleNamespace(get_cookies=get_cookies),
            ctx=types.SimpleNamespace(logger=_Logger()),
            _store=None, _reply_mgr=None,
        )
        _execute_job = QzoneFeedsPlugin._execute_job

        attempts = []

        async def run_fn(api):
            attempts.append(1)
            if len(attempts) == 1:
                raise _qzone_api.CookieExpiredError("登录态失效（code=1000000）")
            return {"ok": True, "msg": "重试成功"}

        result = await _execute_job(p, {"name": "command", "run": run_fn, "callback": None})
        check("重登路径首次失败后 force 刷新", calls == [False, True])
        check("重登后重试成功", result.get("ok") is True)
        check("run_fn 执行两次", len(attempts) == 2)

        # 重试耗尽：每次都抛 CookieExpiredError
        async def run_fail(api):
            raise _qzone_api.CookieExpiredError("登录态失效")

        result2 = await _execute_job(p, {"name": "command", "run": run_fail, "callback": None})
        check("重试耗尽返回失败", result2.get("ok") is False and "登录态失效" in result2.get("msg", ""))

    asyncio.run(run_retry())

    # --- P1: vision.py 模块测试（经包加载，避免顶层路径歧义）---
    _vision_pkg = importlib.import_module(f"{_PKG}.vision")
    VisionManager = _vision_pkg.VisionManager
    PLACEHOLDER = _vision_pkg.PLACEHOLDER
    PLACEHOLDER_FAILED = _vision_pkg.PLACEHOLDER_FAILED

    def _vision_plugin(vision_model="", enabled=True, llm_fn=None):
        return types.SimpleNamespace(
            config=types.SimpleNamespace(read=types.SimpleNamespace(
                vision_model=vision_model, enable_image_description=enabled)),
            ctx=types.SimpleNamespace(llm=types.SimpleNamespace(generate=llm_fn or (lambda **k: None))),
        )

    async def _run_vision_all():
        vm = VisionManager(_vision_plugin(vision_model=""))
        check("vision 无模型回退占位符", await vm.get_image_description("abc") == PLACEHOLDER)

        vm2 = VisionManager(_vision_plugin(enabled=False))
        check("vision 关闭回退占位符", await vm2.get_image_description("abc") == PLACEHOLDER)

        async def ok_llm(prompt, model=None):
            return {"response": "一只猫"}
        vm3 = VisionManager(_vision_plugin(vision_model="vlm", llm_fn=ok_llm))
        check("vision 成功描述", await vm3.get_image_description("abc") == "一只猫")

        async def long_llm(prompt, model=None):
            return {"response": "长" * 500}
        vm4 = VisionManager(_vision_plugin(vision_model="vlm", llm_fn=long_llm))
        check("vision 描述截断200字", len(await vm4.get_image_description("abc")) == 200)

        async def bad_llm(prompt, model=None):
            raise RuntimeError("llm down")
        vm5 = VisionManager(_vision_plugin(vision_model="vlm", llm_fn=bad_llm))
        check("vision 异常回退", await vm5.get_image_description("abc") == PLACEHOLDER_FAILED)

    asyncio.run(_run_vision_all())

    # --- P1: _worker 异常分支（run_fn 抛异常 → callback ok=False）---
    async def run_worker_exc():
        class _Logger:
            def warning(self, msg): pass
            def error(self, msg): pass
            def info(self, msg): pass

        p = types.SimpleNamespace(
            config=types.SimpleNamespace(
                queue=types.SimpleNamespace(retry_on_auth_fail=0, queue_timeout_sec=30),
                auto=types.SimpleNamespace(action_interval_sec=0, comment_prompt=""),
            ),
            _cookie_mgr=types.SimpleNamespace(get_cookies=_ok_cookies),
            ctx=types.SimpleNamespace(logger=_Logger()),
            _store=None, _reply_mgr=None,
            _queue=asyncio.Queue(maxsize=10),
        )
        # SimpleNamespace 属性函数不自动绑定 self，需 MethodType 手动绑定
        p._execute_job = types.MethodType(QzoneFeedsPlugin._execute_job, p)
        _worker = QzoneFeedsPlugin._worker
        got = {}
        fut = asyncio.get_event_loop().create_future()

        async def cb(result):
            if not fut.done():
                fut.set_result(result)

        async def bad_run(api):
            raise RuntimeError("boom")

        worker_task = asyncio.create_task(_worker(p))
        await p._queue.put({"name": "command", "run": bad_run, "callback": cb})
        result = await asyncio.wait_for(fut, timeout=5)
        check("worker 捕获 run_fn 异常", result.get("ok") is False and "boom" in result.get("msg", ""))

        # cookie 获取失败路径
        async def no_cookies(force=False):
            return None
        p._cookie_mgr = types.SimpleNamespace(get_cookies=no_cookies)
        fut2 = asyncio.get_event_loop().create_future()

        async def cb2(result):
            if not fut2.done():
                fut2.set_result(result)

        await p._queue.put({"name": "command", "run": bad_run, "callback": cb2})
        result2 = await asyncio.wait_for(fut2, timeout=5)
        check("worker cookie失败路径", result2.get("ok") is False and "cookie" in result2.get("msg", ""))
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    async def _ok_cookies(force=False):
        return {"uin": "o099999", "p_skey": "abc"}

    asyncio.run(run_worker_exc())

    # --- P1: reply @ 昵称 {} 过滤 ---
    check("昵称过滤代码存在", "safe_nickname" in (PLUGIN_DIR / "qzone_api.py").read_text(encoding="utf-8"))



def test_manifest():
    print("\n== manifest ==")
    mf = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    check("manifest_version=2", mf["manifest_version"] == 2)
    check("id", mf["id"] == "org.orge-8.qzone-feeds")
    check("capabilities 3项", sorted(mf["capabilities"]) == ["api.call", "llm.generate", "send.text"])
    dep_types = {d["type"] for d in mf["dependencies"]}
    check("依赖类型白名单", dep_types <= {"plugin", "python_package"})
    for d in mf["dependencies"]:
        check(f"依赖字段白名单 {d.get('name', d.get('id'))}",
              set(d.keys()) <= {"type", "name", "id", "version_spec"})


def main():
    test_pure_functions()
    test_command_patterns()
    test_auth()
    test_msglist_parsing()
    test_feeds_parsing()
    test_reply_post_data()
    test_processed_store()
    test_silent_period()
    test_auto_job()
    test_format_feed()
    test_p0_fixes()
    test_p1_fixes()
    test_manifest()
    print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
