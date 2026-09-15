# qzone-feeds（QQ空间动态）

MaiBot 插件：发 QQ 动态、读好友动态、VLM 识别动态配图、回复自己动态下的评论、定时自动读好友动态并点赞评论。所有命令需管理员鉴权。

基于 [Maizone](https://github.com/internetsb/Maizone) 协议层移植重构，参考 [qzone-toolkit](https://github.com/gfhdhytghd/qzone-toolkit) 的风控思路。

## 命令（全部仅管理员）

| 命令 | 说明 |
| --- | --- |
| `/动态发 <正文>` | 发纯文本说说 |
| `/动态发图 <正文> \| <图片URL>` | 发带图说说（先传图再发） |
| `/好友动态 [条数]` | 拉好友动态流（默认5，上限15），图片经 VLM 转文字描述 |
| `/说说 <QQ号> [条数]` | 读指定 QQ 的说说列表（**纯读**，不点赞不评论） |
| `/说说互动 <QQ号> [条数]` | 读指定 QQ 的说说列表，并对未处理过的动态逐条点赞+评论（概率 1.0，已处理条目自动跳过） |
| `/回复评论 [条数]` | 扫自己最新动态的新评论，LLM 生成回复并发送 |
| `/动态状态` | cookie 年龄、队列深度、自动任务状态、最近发布/回评结果 |

支持全角 `/`。未授权统一回复「该命令仅管理员可用」。

### /说说互动 命令的点赞评论说明

`/说说互动 <QQ号>` 读到动态后会逐条处理未处理过的条目（**注意：这是写操作，会真实点赞评论**）：

- 点赞 + 评论概率固定 1.0（显式指令即显式意图，不走配置概率）
- 评论内容由 LLM 生成（使用 `[auto] comment_prompt` 模板），发布前经过净化（剥 markdown、截断 100 字）
- 去重走 `processed_list.json`：已处理过的动态不会再点赞评论
- 评论文本包含动态的正文、转发内容与图片的文字描述（`[图: ...]`）
- **评论素材守卫（v1.1.2）**：正文、转发内容、图片描述三者全为空时**跳过评论**（点赞照常）。
  否则 LLM 会拿到一个空白内容并凭空发挥 —— 真机曾因此产出「哈哈转发了个寂寞，原内容是啥呀」
  这类无意义评论（转发型动态的原内容当前尚未被解析，见下方「已知限制」）

## 已知限制

- **转发型动态的原内容未被解析**：`get_qzone_list` 只解析正文容器 `div.f-info` 与 `div.txt-box`，
  转发卡片里的原说说内容没有对应选择器，因此转发动态会落入「无可评论素材」分支被跳过评论。
  待拿到真机转发动态的原始 HTML 后可补上选择器。
- 转发语含中文冒号时，`txt-box` 的 `split("：", 1)` 会截断冒号前的部分。

## 配置

`config.toml`（首次运行自动生成默认值，需手动改）：

```toml
[admin]
admin_ids = ["qq:你的QQ号"]   # 必填，否则所有命令都会被拒

[plugin]
text_task = "replyer"         # LLM 任务名（MaiBot 1.2.5+：model_task_config 的键）
text_model_name = ""          # 具体模型名（可选；留空用任务默认模型）
                              # 旧版 text_model 字段仍兼容：其值会自动作为任务名迁移

[read]
vision_task = "vlm"           # 视觉任务名（MaiBot 1.2.5+）；留空则图片显示为 [图片]
vision_model_name = ""        # 视觉具体模型名（可选；留空用任务默认模型）
                              # 旧版 vision_model 字段仍兼容：其值会自动作为任务名迁移
max_images_per_feed = 9       # 单条动态最多识别几张图（QQ空间上限9，越多越耗时）
image_concurrency = 3         # 图片识别并发数
enable_image_compress = true  # 送 VLM 前压缩图片（省 token/加速；false=用原图）
image_max_edge = 1024         # 压缩后长边像素上限（256~4096）
image_quality = 80            # JPEG 压缩质量 10~95
enable_desc_cache = true      # 缓存图片VLM描述（同一图片URL不重复识别）
desc_cache_size = 200         # VLM描述缓存容量（LRU条数，重启清空）

[auto]
enable_auto_read = false      # 定时自动读好友动态并点赞/评论
enable_auto_reply = false     # 自动回复自己动态的新评论
interval_min = 30             # 循环间隔（分钟）
silent_hours = "23:00-07:30"  # 静默时段（支持跨零点，逗号分隔多段）
like_probability = 0.9
comment_probability = 0.6
```

## 依赖与登录态

- 硬依赖 **napcat-adapter**：cookie 通过 `adapter.napcat.account.get_cookies` 自动获取，无需扫码
- python 依赖：httpx / bs4 / json5 / pillow（manifest 已声明）

## 架构

```
plugin.py            生命周期 + 7 命令 + 鉴权 + 串行队列 worker + 自动任务启停
qzone_api.py         QQ空间协议层（移植自 Maizone，5 处缺陷修正；共享 httpx client）
cookie_manager.py    adapter 取 cookie + 节流 + data_dir 落盘
vision.py            VLM 图片描述（llm.generate 多模态 + URL LRU 缓存）
reply_manager.py     回评流程：扫描/去重/LLM/调用 reply
auto_tasks.py        自动任务循环 + 静默时段解析
processed_store.py   已处理记录（LRU 200 feeds / 100 comments，防抖批量落盘）
```

- **串行队列**：所有 QQ 空间写操作（发/评/赞/回）走单 worker 队列，自动任务与手动命令同队列，防并发风控
- **自动重登**：cookie 失效（登录类错误码）自动强制刷新重试（默认 1 次）
- **去重**：`processed_list.json`（`ctx.paths.data_dir`）记录已处理动态/评论，手动与自动共享
- **反风控**：逐条操作间 `3~4s` 随机间隔；静默时段自动任务全停
- **凭据与出站防护**（v1.1.1，v1.2.2 修订）：
  - 读动态下载图片时，只对 QQ 图床域名（`.qzone.qq.com` / `.qzonestyle.gtimg.cn` / `.gtimg.cn` /
    `.qpic.cn` / `.qq.com`）携带 cookie；**不在白名单的域名不是拒绝下载，而是降级为不带 cookie 下载**
    ——白名单只决定"要不要带凭据"，不该决定"能不能取图"（v1.1.1 一律拒绝曾误杀
    `m.qpic.cn` 等正常图床，真机表现为 VLM 描述全成"加载失败"）
  - 带 cookie 时手动跟随重定向且**每一跳都重新校验域名**，跨域跳转直接中止，
    防止 `p_skey` 被一条恶意 `<img src>` 或 302 带到第三方主机
  - `/动态发图` 的用户可控地址做 SSRF 校验（拒绝内网 / 回环 / 链路本地 / 保留网段，主机名解析后逐条判定），且该链路下载时不带 Qzone cookie
- **取图失败不写进评论素材**（v1.2.2）：图片描述中的占位符与失败标记
  （`[图片]` / `[图片（识别失败）]` / `[图片（加载失败）]`）一律不进入 LLM prompt。
  真机曾因把「加载失败」喂给模型，导致它在好友动态下公开评论「图裂了求补图」——
  等于把自身取图故障当成对作者的吐槽。
- **取图失败可诊断**（v1.2.3）：占位符/失败描述会打出**具体 URL 与原因标记**；
  视觉任务名与模型名双空时显式提示 `请配置 [read] vision_task`。
  此前这两条路径静默返回占位符，真机日志里表现为"图都在、描述却没有"，无从定位。
- **自动评论能看到图了**（v1.2.0）：自动任务读好友动态时改为尊重 `[read] enable_image_description`
  与图片限额（`max_images_per_feed` / `image_concurrency` / 压缩参数）——旧版写死
  `describe_images=False`，自动评论完全"看不到"配图，只能凭正文发挥。担心耗时可用
  `max_images_per_feed` 调低单条识别张数；配置不可读时保守降级为不识别（等价旧行为）。
- **适配 MaiBot 1.2.5 的任务名/模型名拆分**（v1.2.1）：1.2.5 起 `llm.generate` 的 `model`
  参数按**具体模型名**解释，任务名拆到新参数 `task_name`（SDK 2.8.1 默认 `utils`）。
  本插件新增 `text_task` / `text_model_name` 与 `vision_task` / `vision_model_name` 字段；
  旧配置（`text_model="replyer"`、`vision_model="vlm"` 存的是任务名）**无需改动**，
  读取时自动迁移为任务名。LLM 失败日志会打出完整 kwargs 便于排查。
- **性能优化**（v1.1.0）：
  - 共享 httpx 连接池：每个 job 内所有请求复用同一 AsyncClient（省 TCP+TLS 握手）
  - 已处理列表防抖落盘：mark 只改内存，2s 防抖批量写盘 + job 边界兜底（写盘次数降一个数量级）
  - VLM 描述缓存：同一图片 URL 不重复下载/识别（单张省 20s+ 与全部 token，命中日志 `图片描述命中缓存`）
  - VLM 下载小图优先（`smallurl`），下载字节数降 5~10 倍
  - 图片压缩走内存 bytes 直传，只在送 VLM 边界做一次 base64 编码

## 相对上游 Maizone 的修正

1. 图片下载带 cookies（相册图床 403 规避）
2. 上传响应 `eval()` → `json.loads`
3. cookies/processed_list 落盘到 `ctx.paths.data_dir`（不再污染代码目录）
4. capabilities 只声明实际使用的 3 项（`send.text` / `llm.generate` / `api.call`）
5. 注入真实 VisionManager（上游 `set_image_manager` 从未被调用，图片恒为 `[图片]`）

## 测试

```bash
# 行为测试（123 项：逻辑 + 鉴权 + 命令正则 + 安全验证 + 副作用守卫 + 图床降级 + LLM参数解析 + 失败诊断 + 生命周期/Manifest）
python tests/run_tests.py
```

不依赖 MaiBot 与真实网络（maibot_sdk 用 stub 注入，出站请求用 `httpx.MockTransport` 拦截），
可直接在插件目录下运行。上线前需 **123/123 全过**。

## 部署

1. 整目录复制到 MaiBot 的 `plugins/qzone-feeds/`
2. **完整重启 MaiBot**（热重载对含 manifest 变更的插件不生效）
3. 改 `config.toml` 配置 `admin_ids`（不配则命令全拒）
4. 自动任务默认关闭，按需开 `enable_auto_read` / `enable_auto_reply`
