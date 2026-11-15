# qzone-feeds（QQ空间动态）

MaiBot 插件：发 QQ 动态、读好友动态、VLM 识别动态配图、回复自己动态下的评论、定时自动读好友动态并点赞评论。所有命令需管理员鉴权。

基于 [Maizone](https://github.com/internetsb/Maizone) 协议层移植重构，参考 [qzone-toolkit](https://github.com/gfhdhytghd/qzone-toolkit) 的风控思路。

> **版本细节见 [CHANGELOG.md](CHANGELOG.md)** —— 本文件只写「怎么用、怎么配、怎么排障」。

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
- 评论内容由 LLM 生成（使用 `[auto] comment_prompt` 模板），发布前经过净化（剥 markdown、截断 100 字）与发言纪律检查；最终发布文本按 `[auto] max_comment_chars` 钳制（v1.8.1 起默认 50，超长切最近句子边界）
- 去重走 `processed_list.json`：已处理过的动态不会再点赞评论
- 评论文本包含动态的正文、转发内容与图片的文字描述（`[图: ...]`）
- **评论素材守卫**（v1.1.2）：详见 [CHANGELOG.md](CHANGELOG.md)。

## 已知限制

- **转发型动态的原内容未被解析**：`get_qzone_list` 只解析正文容器 `div.f-info` 与 `div.txt-box`，
  转发卡片里的原说说内容没有对应选择器，因此转发动态会落入「无可评论素材」分支被跳过评论。
  待拿到真机转发动态的原始 HTML 后可补上选择器。
- 转发语含中文冒号时，`txt-box` 的 `split("：", 1)` 会截断冒号前的部分。
- **被@回复的盲区（v1.3.0）**：好友在**别人的说说**下回复 bot 的评论，动作文案是「回复」
  而非「回复提到我」，会被归 other 忽略——不覆盖该场景，避免 bot 在第三方说说下自作主张接话。

## 变更记录

完整版本历史见 [CHANGELOG.md](CHANGELOG.md)（含 v1.5.0 表达风格、v1.4.0 宿主人设、v1.3.3 / v1.3.2 起的发言纪律、
时间上下文注入等）。近期变更：

- **评论时读宿主表达风格**（v1.5.0）：人设之外再注入 `reply_style`，两段均只作语气参考。详见 [CHANGELOG.md](CHANGELOG.md)。
- **评论时读宿主人设**（v1.4.0）：三条发言路径统一注入，只作语气参考，纪律优先。详见 [CHANGELOG.md](CHANGELOG.md)。
- **评论发言纪律下沉到代码层**（v1.3.3）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **人物画像注入加自证日志**（v1.3.3）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **回复评论注入时间上下文**（v1.3.2）：详见 [CHANGELOG.md](CHANGELOG.md)。

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
vision_task = "vlm"           # 视觉任务名（MaiBot 1.2.5+）。**必填**：留空则图片不会被识别，
                              # 日志会提示「视觉任务名与模型名均为空」；此时也不会下载图片（省流量）
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
enable_atme_reply = false     # 被@检测与回复（「与我相关」接口，v1.3.0）
atme_poll_count = 10          # 每轮拉取「与我相关」条数（1~20）
atme_max_replies_per_run = 5  # 单轮被@回复上限
interval_min = 30             # 循环间隔（分钟）
interval_jitter_min = 5       # v1.8.0：间隔随机抖动±N分钟（代码级默认5，钳到interval一半，0=关闭）
silent_hours = "23:00-07:30"  # 静默时段（支持跨零点，逗号分隔多段）
like_probability = 0.9
comment_probability = 0.6
lazy_vision_enabled = true    # v1.8.0：自动任务延迟识图（先决策后识图，省VLM；仅自动任务，命令不变）
max_comment_chars = 50        # v1.8.1：发布评论字数上限（默认/硬上限50；超长切最近句子边界，不再盲切）

[persona]                       # v1.4.0：评论/回评/被@ 统一注入宿主人设；v1.5.0 加表达风格
enable_host_persona = true      # 读 config.toml [personality] 的 personality + reply_style（只读）
persona_prompt = ""             # 人设覆盖项。留空=读宿主；填了=整体接管（人设+风格都不读宿主）
reply_style_prompt = ""         # 表达风格覆盖项。留空=读宿主；填了=只换风格，人设仍读宿主
persona_max_chars = 600         # 人设注入上限（按句截断）
reply_style_max_chars = 300     # 表达风格注入上限（按句截断）
persona_cache_ttl_sec = 600     # 宿主读取缓存秒数（0=每次重读）
```

### 被@检测与回复（v1.3.0，`enable_atme_reply = true` 开启）

每轮自动任务调「与我相关」接口（`feeds2_html_pav_all`，g_tk 用 **p_skey 版**），
按官方渲染文案分类动作：

- **mention（正文@我）** → 评论该说说
- **comment_mention（评论@我）** → 经 msglist_v6 定位互动者在说说下含 `@`/bot昵称
  的最新评论并回评；说说不在对方最近 10 条动态内或定位不到评论时，降级为评论说说
- **other（赞/评论/回复/访问）** → 忽略（自己说说被评论由「自动回评」负责）

去重 key `atme:{post_uin}:{post_tid}` 走 `processed_list.json`：同一说说上的
多次 @/赞/评论合并为一次唤醒；先标记后处理，回复失败不重试（防风控优先于送达率）。
回复内容走与自动回评相同的 LLM 净化链（剥 markdown、拒答拦截、身份错位拦截、语域拦截）。

### 评论时的人设与表达风格（v1.4.0 / v1.5.0）

三条发言路径（自动评论 / 回评 / 被@回复）在 `generate_guarded_comment` 入口**统一**注入
宿主的 `[personality]` 两段配置（`ctx.config.get`，manifest 已声明 `config.get`）：

```
【说话风格参考】<personality>       # 是谁、什么性格
【表达风格】<reply_style>           # 平时怎么措辞（v1.5.0）
（这段只说口气和用词习惯，不改变你在跟谁说话；
  与后文的【发言纪律】冲突时，一律以发言纪律为准。）
```

注入形态刻意**不是**「你是 X」，而是风格参考：自称句会让模型进入自我指涉状态，
产出的正是要拦的官方通稿腔（见 [CHANGELOG.md](CHANGELOG.md) v1.3.3 / v1.4.0）。
两段都排在模板之后、纪律段之前——纪律必须留在最末。

- 分别按句截断：人设 600 字 / 风格 300 字，都是语气锚而非知识库
- 任一段读不到 / 非字符串 / 超时（>5s）只是少一段，另一段照常注入，评论照发
- `persona_prompt` 填了 = 整体接管；只想换风格用 `reply_style_prompt`
- 不收宿主 `behavior_style`（Planner 行动准则）与 `multiple_reply_style`（随机换风格）：
  前者与发评论无关，后者会让评论风格不可复现
- `person_context`（好友印象）与本注入（bot 自己的口吻）是两件事，互不替代

## 依赖与登录态

- 硬依赖 **maibot-team.snowluma-adapter >= 1.0.0**（v1.3.0 起 SnowLuma 连接器合并 NapCat 适配器，`adapter.napcat.*` API 前缀不变）：cookie 通过 `adapter.napcat.account.get_cookies` 自动获取，无需扫码。注意 QZone API 组仅 `client_type=snowluma` 支持，但本插件只依赖账号组 `get_cookies`，napcat 画像下同样可用
- python 依赖：httpx / bs4 / json5 / pillow（manifest 已声明）

## 架构

```
plugin.py            生命周期 + 7 命令 + 鉴权 + 串行队列 worker + 自动任务启停
qzone_api.py         QQ空间协议层（移植自 Maizone，5 处缺陷修正；共享 httpx client）
cookie_manager.py    adapter 取 cookie + 节流 + data_dir 落盘
vision.py            VLM 图片描述（llm.generate 多模态 + URL LRU 缓存）
reply_manager.py     回评流程：扫描/去重/LLM/调用 reply + generate_guarded_comment（发布前判定总入口）
comment_style.py     评论语域纪律：代码级发言纪律段 + 书面套话特征 + 主动弃评出口（纯函数零依赖）
person_context.py    人物画像只读注入（ctx.person，失败静默降级）
host_persona.py      宿主人设/表达风格只读注入（ctx.config personality + reply_style，语气参考 + TTL 缓存）
atme_manager.py      「与我相关」被@检测与回复（正文@ / 评论@）
auto_tasks.py        自动任务循环 + 静默时段解析 + 自动读动态点赞评论
processed_store.py   已处理记录（LRU 200 feeds / 100 comments，防抖批量落盘）
```

- **串行队列**：所有 QQ 空间写操作（发/评/赞/回）走单 worker 队列，自动任务与手动命令同队列，防并发风控
- **自动重登**：cookie 失效（登录类错误码）自动强制刷新重试（默认 1 次）
- **去重**：`processed_list.json`（`ctx.paths.data_dir`）记录已处理动态/评论，手动与自动共享
- **反风控**：逐条操作间 `3~4s` 随机间隔；静默时段自动任务全停
- **凭据与出站防护**（v1.1.1、v1.2.2）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **取图失败不写进评论素材**（v1.2.2）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **拒答文本绝不发布**（v1.2.4）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **视觉未配置时不下载图片**（v1.2.4）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **取图失败可诊断**（v1.2.3）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **自动评论能看到图了**（v1.2.0）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **发言纪律走代码层，不受用户模板影响**（v1.3.3）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **身份错位拦截：bot 不再对好友称「主人」**（v1.2.9）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **图片重复描述修复**（v1.2.8）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **LLM RPC 超时抬升，修复 replyer 30s 误杀**（v1.2.7）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **评论注入人物画像**（v1.2.6）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **适配 MaiBot 1.2.5 的任务名/模型名拆分**（v1.2.1）：详见 [CHANGELOG.md](CHANGELOG.md)。
- **性能优化**（v1.1.0）：详见 [CHANGELOG.md](CHANGELOG.md)。

## 相对上游 Maizone 的修正

1. 图片下载带 cookies（相册图床 403 规避）
2. 上传响应 `eval()` → `json.loads`
3. cookies/processed_list 落盘到 `ctx.paths.data_dir`（不再污染代码目录）
4. capabilities 只声明实际使用的 3 项（`send.text` / `llm.generate` / `api.call`）
5. 注入真实 VisionManager（上游 `set_image_manager` 从未被调用，图片恒为 `[图片]`）

## 测试

```bash
# 行为测试：逻辑 + 鉴权 + 命令正则 + 安全验证 + 副作用守卫 + 图床降级 + LLM参数解析
#          + 失败诊断 + 拒答拦截 + 身份错位 + 语域纪律/弃评出口 + 生命周期/Manifest
python tests/run_tests.py

# WebUI 配置 Schema 显示层（需 maibot_sdk，走默认 Python 环境）
python -m pytest tests/test_webui_display.py -q
```

不依赖 MaiBot 与真实网络（maibot_sdk 用 stub 注入，出站请求用 `httpx.MockTransport` 拦截），
可直接在插件目录下运行。上线前需 **235/235 全过** + `pytest tests/test_webui_display.py` 6/6 全过。

## 部署

1. 整目录复制到 MaiBot 的 `plugins/qzone-feeds/`
2. **完整重启 MaiBot**（热重载对含 manifest 变更的插件不生效）
3. 改 `config.toml` 配置 `admin_ids`（不配则命令全拒）
4. 自动任务默认关闭，按需开 `enable_auto_read` / `enable_auto_reply`
