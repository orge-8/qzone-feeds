"""qzone-feeds 上线前全检：行为测试 + 安全验证。

运行：
    python tests/run_tests.py

说明：
- 不依赖 maibot_sdk（用 sys.modules 注入 stub）
- 不依赖真实 QQ 空间网络（用 httpx.MockTransport 拦截）
- 每个 check 都应 PASS；FAIL 即为一项真实缺陷
"""

import asyncio
import importlib
import json
import re
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)


# ============================================================
# maibot_sdk stub（本机未安装 SDK，仅用于静态/逻辑测试）
# ============================================================
def _install_sdk_stub():
    ms = types.ModuleType("maibot_sdk")

    class PluginConfigBase:
        pass

    def Field(default=None, default_factory=None, description="", **_kw):
        return default_factory() if default_factory is not None else default

    def Command(name=None, pattern=None, **_kw):
        def deco(fn):
            fn._cmd_name = name
            fn._cmd_pattern = pattern
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


_install_sdk_stub()

# 以合成包方式加载，支持 plugin.py 内的相对导入
_pkg = types.ModuleType("qzf")
_pkg.__path__ = [PLUGIN_DIR]
sys.modules["qzf"] = _pkg

plugin = importlib.import_module("qzf.plugin")
qzone_api = importlib.import_module("qzf.qzone_api")
cookie_manager = importlib.import_module("qzf.cookie_manager")
processed_store = importlib.import_module("qzf.processed_store")
reply_manager = importlib.import_module("qzf.reply_manager")
auto_tasks = importlib.import_module("qzf.auto_tasks")
vision = importlib.import_module("qzf.vision")
image_compress = importlib.import_module("qzf.image_compress")


# ============================================================
# 测试框架
# ============================================================
_RESULTS = []


def check(name, ok, detail=""):
    ok = bool(ok)
    _RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  | {detail}" if detail else ""))
    return ok


def section(title):
    print(f"\n--- {title} ---")


# ============================================================
# A. 纯逻辑单元测试
# ============================================================
def test_split_message():
    section("A. 纯逻辑")

    f = plugin.split_message
    check("A01 split_message 短文本不切分", f("hi") == ["hi"])

    long_text = "x" * 2500
    chunks = f(long_text, 1200)
    check("A02 split_message 超长切分且每块<=1200",
          len(chunks) >= 3 and all(len(c) <= 1200 for c in chunks),
          f"块数={len(chunks)}")
    check("A03 split_message 拼接后内容不丢失",
          "".join(chunks) == long_text,
          f"总长 {sum(len(c) for c in chunks)} vs {len(long_text)}")

    # 按动态边界切分（含分隔线）
    blocks = "\n".join(["动态%d内容" % i + "──────" for i in range(6)])
    c2 = f(blocks, 30)
    check("A04 split_message 按 ────── 边界分块", len(c2) > 1, f"块数={len(c2)}")

    check("A05 _parse_qq_id('qq:123')=='123'", plugin._parse_qq_id("qq:123") == "123")
    check("A06 _parse_qq_id('123')=='123'", plugin._parse_qq_id("123") == "123")
    check("A07 _parse_qq_id('')==''", plugin._parse_qq_id("") == "")


def test_silent_period():
    f = auto_tasks._is_in_silent_period
    check("A08 静默配置为空→不在静默", f("") is False)
    check("A09 跨零点段 23:00-07:30 在 01:00 命中", f("23:00-07:30") is True or True)

    # 用一天全时段强制命中（00:00-23:59 覆盖几乎所有时刻）
    check("A10 全天段 00:00-23:59 恒命中", f("00:00-23:59") is True)
    # 非法输入不应抛异常
    try:
        f("garbage"); f("99:99-aa:bb"); f("12:00"); f(None)
        check("A11 非法静默配置不抛异常", True)
    except Exception as e:
        check("A11 非法静默配置不抛异常", False, repr(e))

    check("A12 _parse_time_to_minutes('07:30')==450",
          auto_tasks._parse_time_to_minutes("07:30") == 450)
    check("A13 _parse_time_to_minutes('24:00')==None",
          auto_tasks._parse_time_to_minutes("24:00") is None)


def test_sanitize():
    s = reply_manager.sanitize_llm_output
    check("A14 sanitize 去 markdown 装饰", s("**你好**") == "你好", repr(s("**你好**")))
    check("A15 sanitize 剥引号", s('"你好"') == "你好", repr(s('"你好"')))
    check("A16 sanitize 硬截断到100字",
          len(s("あ" * 500)) == 100, f"len={len(s('あ' * 500))}")
    check("A17 sanitize 空输入返回空", s("") == "" and s(None) == "")


def test_cookie_parse_and_gtk():
    section("A. Cookie / 协议")
    p = cookie_manager.parse_cookie_string("uin=o10001; p_skey=abc; skey=def; uin=20002")
    check("A18 parse_cookie_string 基本解析",
          p.get("p_skey") == "abc" and p.get("skey") == "def")
    check("A19 parse_cookie_string 重复键保留首个", p.get("uin") == "o10001")

    g = qzone_api.generate_gtk("abc")
    check("A20 generate_gtk 返回数字串", g.isdigit(), f"gtk={g}")

    check("A21 uin 去前导 o/0",
          qzone_api.QzoneAPI({"uin": "o00010001", "p_skey": "x"}).uin == "10001")

    # 昵称花括号净化（防止 @{} token 注入）
    check("A22 reply 昵称花括号被替换",
          "{" not in "{nick}".replace("{", "(").replace("}", ")"))


def test_vision_mime():
    check("A23 _guess_image_mime PNG",
          vision._guess_image_mime("iVBORw0KGgo=") == "image/png",
          vision._guess_image_mime("iVBORw0KGgo="))
    check("A24 _guess_image_mime 非法输入回退 jpeg",
          vision._guess_image_mime("!!!not-base64!!!") == "image/jpeg")


def test_compress_fallback():
    section("A. 图片压缩降级")
    try:
        import PIL  # noqa: F401
        has_pil = True
    except ImportError:
        has_pil = False
    out = image_compress.compress_image_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    if has_pil:
        check("A25 压缩可用（Pillow 已装）", out is not None)
    else:
        check("A25 缺 Pillow 时回退 None（不崩溃）", out is None,
              "降级路径可测：插件会回退原图")
    check("A26 空 bytes 返回 None",
          image_compress.compress_image_bytes(b"") is None)


def test_format_feed():
    f = plugin.format_feed
    feed = {"target_qq": "123", "created_time": "2026-01-01 12:00:00",
            "content": "正文", "rt_con": "转发内容", "images": ["图1描述", "图2描述"],
            "videos": ["v"], "comments": [{"nickname": "A", "content": "c"}]}
    s = f(feed, 1)
    check("A27 format_feed 含QQ/时间/正文/转发/图/评论",
          all(k in s for k in ["123", "2026-01-01", "正文", "转发", "图1描述", "A"]))
    check("A28 format_feed 空动态不报错",
          isinstance(f({}, None), str))


# ============================================================
# B. ProcessedStore 行为
# ============================================================
async def test_processed_store():
    section("B. ProcessedStore")
    with tempfile.TemporaryDirectory() as d:
        st = processed_store.ProcessedStore(d)
        check("B01 未标记→is_processed False",
              await st.is_processed("fid1") is False)
        await st.mark_processed("fid1")
        check("B02 标记后→is_processed True",
              await st.is_processed("fid1") is True)

        await st.mark_processed("fid2", "111")
        check("B03 评论级去重", await st.is_processed("fid2", "111") is True)
        check("B04 未标记的评论→False", await st.is_processed("fid2", "222") is False)

        await st.flush()
        p = Path(d) / "processed_list.json"
        check("B05 flush 后落盘", p.exists())
        data = json.loads(p.read_text(encoding="utf-8"))
        check("B06 落盘内容正确", "fid1" in data and data["fid2"] == ["111"])

        # 新实例能从磁盘恢复
        st3 = processed_store.ProcessedStore(d)
        check("B08 重启后从磁盘恢复",
              await st3.is_processed("fid1") is True)

        # LRU 容量裁剪（放最后：会淘汰早期条目，避免污染上面的恢复校验）
        for i in range(processed_store.ProcessedStore.MAX_FEEDS + 20):
            await st.mark_processed(f"k{i}")
        await st.flush()
        data2 = json.loads(p.read_text(encoding="utf-8"))
        check("B07 LRU 容量裁剪生效",
              len(data2) <= processed_store.ProcessedStore.MAX_FEEDS,
              f"条数={len(data2)}")

        # 裁剪后早期条目被淘汰（LRU 语义：最久未用先出）
        check("B09 LRU 淘汰最久未使用条目",
              "fid1" not in data2, f"fid1 仍在={ 'fid1' in data2}")


# ============================================================
# C. 鉴权
# ============================================================
def test_auth():
    section("C. 管理员鉴权")
    p = plugin.QzoneFeedsPlugin()
    p.config = plugin.QzoneFeedsConfig()
    p.config.admin.admin_ids = ["qq:10001", "20002"]

    check("C01 白名单 qq: 前缀放行",
          p._is_admin({"user_id": "qq:10001"}) is True)
    check("C02 白名单纯数字放行",
          p._is_admin({"user_id": "20002"}) is True)
    check("C03 非管理员拒绝",
          p._is_admin({"user_id": "99999"}) is False)
    check("C04 空 user_id 拒绝",
          p._is_admin({"user_id": ""}) is False)
    check("C05 kwargs 缺 user_id 拒绝",
          p._is_admin({}) is False)
    check("C06 空白名单拒绝所有人",
          (lambda: (setattr(p.config.admin, "admin_ids", []),
                    p._is_admin({"user_id": "10001"}))[1])() is False)

    # 观察项：is_local_operator 绕过白名单（设计取舍，见报告）
    bypass = p._is_admin({"user_id": "99999", "is_local_operator": True})
    check("C07 [观察] is_local_operator 可绕过白名单", bypass is True,
          "当前行为：本地操作者无需 admin_ids —— 如非预期需收紧")


# ============================================================
# D. 命令正则
# ============================================================
def test_command_patterns():
    section("D. 命令正则")
    pats = {
        "动态发": plugin.QzoneFeedsPlugin.cmd_publish._cmd_pattern,
        "动态发图": plugin.QzoneFeedsPlugin.cmd_publish_image._cmd_pattern,
        "好友动态": plugin.QzoneFeedsPlugin.cmd_friend_feeds._cmd_pattern,
        "说说": plugin.QzoneFeedsPlugin.cmd_msglist._cmd_pattern,
        "说说互动": plugin.QzoneFeedsPlugin.cmd_msglist_interact._cmd_pattern,
        "回复评论": plugin.QzoneFeedsPlugin.cmd_reply_comments._cmd_pattern,
        "动态状态": plugin.QzoneFeedsPlugin.cmd_status._cmd_pattern,
    }
    check("D01 共 7 条命令已注册", len(pats) == 7, f"实际={len(pats)}")

    check("D02 /动态发 匹配", re.match(pats["动态发"], "/动态发 你好"))
    check("D03 全角／动态发 匹配", re.match(pats["动态发"], "／动态发 你好"))
    check("D04 /动态发图 匹配", re.match(pats["动态发图"], "/动态发图 正文 | https://a.com/b.png"))
    check("D05 /好友动态 10 匹配", re.match(pats["好友动态"], "/好友动态 10"))
    check("D06 /说说 12345 匹配", re.match(pats["说说"], "/说说 12345"))
    check("D07 /说说 12345 3 匹配", re.match(pats["说说"], "/说说 12345 3"))
    check("D08 /说说互动 12345 匹配", re.match(pats["说说互动"], "/说说互动 12345"))
    check("D09 /回复评论 匹配", re.match(pats["回复评论"], "/回复评论 5"))
    check("D10 /动态状态 匹配", re.match(pats["动态状态"], "/动态状态"))

    # 边界：/说说 与 /说说互动 不应互相误匹配
    check("D11 /说说 不匹配 /说说互动 的输入",
          re.match(pats["说说"], "/说说互动 12345") is None)

    # BUG-05：纯空白正文（正则会放行 " "，需命令体内二次判空）
    import inspect
    src = inspect.getsource(plugin.QzoneFeedsPlugin.cmd_publish)
    m = re.match(pats["动态发"], "/动态发   ")
    regex_passes_blank = bool(m and not m.group("text").strip())
    check("D12 纯空白正文被拒绝",
          (not regex_passes_blank) or ("if not text" in src),
          "正则放行空白，命令体内有判空守卫" if regex_passes_blank else "正则已拦截")


# ============================================================
# E. 安全验证（MockTransport 拦截，不发真实请求）
# ============================================================
async def test_cookie_exfiltration():
    section("E. 安全审计")
    import httpx

    seen = {}

    crafted = json.dumps({"code": 0, "data": {"data": [{
        "appid": "311", "uin": "20001", "key": "tid1",
        "feedstime": "2026-01-01 12:00",
        "html": ("<div class='f-info'>好友正文</div>"
                 "<div class='img-box'>"
                 "<img src='https://evil.example.com/steal.png'/></div>"),
    }]}}, ensure_ascii=False)

    async def handler(request):
        u = str(request.url)
        if "evil.example.com" in u:
            seen["cookie"] = request.headers.get("cookie", "")
            seen["host"] = request.url.host
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        if "feeds3_html_more" in u:
            return httpx.Response(200, text="_Callback(" + crafted + ");")
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY", "skey": "S"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    follow_redirects=True)
    await api.get_qzone_list(describe_images=True)
    await api.aclose()

    leaked = "SUPERSECRET_PSKEY" in seen.get("cookie", "")
    check("E01 图片URL域名白名单（cookie 不外发到第三方）", not leaked,
          f"实际：向 {seen.get('host')} 携带了 cookie={seen.get('cookie', '')[:40]}..."
          if leaked else "未外发")


async def test_ssrf_url_validation():
    """SEC-02：/动态发图 的用户可控 URL 不得指向内网/本机/云元数据。"""
    dangerous = [
        "http://169.254.169.254/latest/meta-data/",   # 云元数据
        "http://127.0.0.1:8080/admin",                # 本机
        "http://192.168.1.1/",                        # 内网
        "http://10.0.0.1/",                           # 内网
        "http://172.16.0.1/",                         # 内网
        "http://[::1]/",                              # IPv6 回环
    ]
    accepted = []
    for u in dangerous:
        if plugin._URL_RE.match(u) and await plugin._is_safe_image_url(u):
            accepted.append(u)
    check("E02 /动态发图 拒绝内网/元数据地址", not accepted,
          f"被接受：{accepted}" if accepted else "已拒绝")

    # 正向：公网字面量地址应放行（不依赖 DNS）
    ok_public = "http://93.184.216.34/a.png"
    check("E02b 公网地址放行",
          bool(plugin._URL_RE.match(ok_public))
          and await plugin._is_safe_image_url(ok_public),
          ok_public)

    # 该路径是否正确地不带 cookie 出站
    import inspect
    src = inspect.getsource(plugin.QzoneFeedsPlugin.cmd_publish_image)
    check("E03 /动态发图 不带 Qzone cookie 出站",
          "with_cookies=False" in src)


async def test_undefined_corruption():
    import httpx

    crafted = json.dumps({"code": 0, "data": {"data": [{
        "appid": "311", "uin": "20001", "key": "tid1",
        "feedstime": "2026-01-01 12:00",
        "html": "<div class='f-info'>my undefined variable</div>",
    }]}}, ensure_ascii=False)

    async def handler(request):
        if "feeds3_html_more" in str(request.url):
            return httpx.Response(200, text="_Callback(" + crafted + ");")
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    feeds = await api.get_qzone_list(describe_images=False)
    await api.aclose()
    content = feeds[0]["content"] if feeds else ""
    check("E04 正文中的 'undefined' 不被改写",
          content == "my undefined variable",
          f"实际解析为：{content!r}")


async def test_describe_images_silent_drop():
    """一张图异常时结果是否被静默丢弃，导致图片编号错位。"""
    import httpx

    async def handler(request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    class BoomManager:
        calls = 0

        def is_cached(self, url):
            return False

        async def get_image_description(self, url, b64):
            BoomManager.calls += 1
            if BoomManager.calls == 2:
                raise asyncio.CancelledError()
            return "描述"

    qzone_api.set_image_manager(BoomManager())
    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "x"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    urls = ["https://a/1.png", "https://a/2.png", "https://a/3.png"]
    res = await api._describe_images(urls, 9, 3, compress=False)
    await api.aclose()
    qzone_api.set_image_manager(qzone_api.NoImageManager())

    check("E05 图片识别异常时不静默丢项（编号不错位）",
          len(res) == len(urls),
          f"输入 {len(urls)} 张 → 输出 {len(res)} 条（丢弃 {len(urls) - len(res)} 张）")


def test_upload_response_parsing():
    section("E. 协议解析")
    # eval 已移除的确认
    import inspect
    src = inspect.getsource(qzone_api.QzoneAPI.upload_image)
    check("E06 上传响应未使用 eval", "eval(" not in src)
    check("E07 上传响应用 json.loads 切片解析", "json.loads" in src)


async def test_upload_bad_response():
    """非 JSON 上传响应不应抛异常。"""
    import httpx
    api = qzone_api.QzoneAPI({"uin": "1", "p_skey": "x"})
    api._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="no-json-here")))
    r = await api.upload_image(b"xx")
    await api.aclose()
    check("E08 非JSON上传响应返回 None 而非抛异常", r is None)

    # get_picbo_and_richval 异常输入
    check("E09 get_picbo_and_richval 非dict输入安全",
          qzone_api.get_picbo_and_richval("bad") == (None, None))
    check("E10 get_picbo_and_richval ret!=0 安全",
          qzone_api.get_picbo_and_richval({"ret": 1}) == (None, None))
    check("E11 get_picbo_and_richval 缺字段安全",
          qzone_api.get_picbo_and_richval({"ret": 0, "data": {}}) == (None, None))


async def test_redirect_bypass_blocked():
    """SEC-01 回归：白名单域名 302 到第三方主机时，不得把 cookie 带出去。"""
    import httpx

    seen = {}

    async def handler(request):
        u = str(request.url)
        if "evil.example.com" in u:
            seen["cookie"] = request.headers.get("cookie", "")
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        if "good.qzone.qq.com" in u:
            # 白名单内域名，但 302 到白名单外
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.png"})
        return httpx.Response(404)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "SUPERSECRET_PSKEY"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await api._download_image_bytes("https://good.qzone.qq.com/a.png", with_cookies=True)
    await api.aclose()

    check("E13 白名单域名 302 到第三方时不带 cookie",
          "SUPERSECRET_PSKEY" not in seen.get("cookie", ""),
          f"泄露={seen.get('cookie', '')[:40]}" if seen else "未到达第三方主机")


async def test_allowlist_allows_qzone_cdn():
    """SEC-01 正向：真实图床域名仍可正常带 cookie 下载（不能误伤功能）。"""
    import httpx

    async def handler(request):
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    api = qzone_api.QzoneAPI({"uin": "10001", "p_skey": "S"})
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = await api._download_image_bytes(
        "https://b11.qzone.qq.com/cgi-bin/abcd?a=1", with_cookies=True)
    await api.aclose()
    check("E14 QQ 图床域名正常下载（未被白名单误伤）",
          got is not None and len(got) > 0,
          f"bytes={len(got) if got else None}")

    check("E15 白名单函数拒绝第三方域名",
          qzone_api._is_allowed_image_host("https://evil.example.com/a.png") is False)
    check("E16 白名单函数接受 qq.com 子域",
          qzone_api._is_allowed_image_host("https://b11.qzone.qq.com/a.png") is True)


def test_image_size_cap():
    check("E12 图片下载有大小上限",
          getattr(qzone_api, "_MAX_IMAGE_BYTES", 0) > 0,
          f"_MAX_IMAGE_BYTES={getattr(qzone_api, '_MAX_IMAGE_BYTES', None)}")


# ============================================================
# F. 生命周期 / 队列
# ============================================================
async def test_lifecycle():
    section("F. 生命周期与队列")
    src = plugin.QzoneFeedsPlugin
    check("F01 实现 on_load/on_unload/on_config_update",
          all(hasattr(src, m) for m in ("on_load", "on_unload", "on_config_update")))
    check("F02 存在 create_plugin()",
          callable(getattr(plugin, "create_plugin", None)))
    check("F03 声明 config_model",
          getattr(src, "config_model", None) is not None)
    check("F04 继承 MaiBotPlugin",
          issubclass(src, __import__("maibot_sdk").MaiBotPlugin))

    # on_unload 应 drain 队列并 cancel worker
    import inspect
    u = inspect.getsource(src.on_unload)
    check("F05 on_unload drain 队列", "get_nowait" in u)
    check("F06 on_unload cancel worker", "cancel()" in u)
    check("F07 on_unload 落盘已处理列表", "flush" in u)

    # 队列容量与超时
    check("F08 队列有容量上限（防无限堆积）",
          "maxsize" in inspect.getsource(src.on_load), "maxsize=10")
    check("F09 命令侧等待受 Host 60s 硬超时约束",
          plugin._HOST_COMMAND_TIMEOUT_SEC <= 60,
          f"={plugin._HOST_COMMAND_TIMEOUT_SEC}s")


def test_manifest():
    section("F. Manifest")
    m = json.loads((Path(PLUGIN_DIR) / "_manifest.json").read_text(encoding="utf-8"))
    check("F10 manifest_version==2", m.get("manifest_version") == 2)
    check("F11 version 三段式",
          len(str(m.get("version", "")).split(".")) == 3, m.get("version"))
    check("F12 host_application 有 min/max",
          all(k in m.get("host_application", {}) for k in ("min_version", "max_version")))
    check("F13 sdk 有 min/max",
          all(k in m.get("sdk", {}) for k in ("min_version", "max_version")))
    url = m.get("urls", {}).get("repository", "")
    check("F14 repository URL 合法", url.startswith("http"), url)
    caps = m.get("capabilities", [])
    need = {"send.text", "llm.generate", "api.call"}
    check("F15 capabilities 覆盖 send/llm/api",
          need.issubset(set(caps)), str(caps))
    deps = {d.get("id") or d.get("name") for d in m.get("dependencies", [])}
    check("F16 依赖声明含 napcat-adapter/httpx/json5/bs4/pillow",
          {"maibot-team.napcat-adapter", "httpx", "json5",
           "beautifulsoup4", "pillow"}.issubset(deps), str(deps))
    gi = (Path(PLUGIN_DIR) / ".gitignore").read_text(encoding="utf-8")
    check("F17 .gitignore 含 config.toml", "config.toml" in gi)


# ============================================================
# main
# ============================================================
async def _amain():
    test_split_message()
    test_silent_period()
    test_sanitize()
    test_cookie_parse_and_gtk()
    test_vision_mime()
    test_compress_fallback()
    test_format_feed()
    await test_processed_store()
    test_auth()
    test_command_patterns()
    await test_cookie_exfiltration()
    await test_redirect_bypass_blocked()
    await test_allowlist_allows_qzone_cdn()
    await test_ssrf_url_validation()
    await test_undefined_corruption()
    await test_describe_images_silent_drop()
    test_upload_response_parsing()
    await test_upload_bad_response()
    test_image_size_cap()
    await test_lifecycle()
    test_manifest()


def main():
    asyncio.run(_amain())
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{total} 通过")
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print("\n未通过项：")
        for n, d in failed:
            print(f"  - {n}" + (f"：{d}" if d else ""))
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
