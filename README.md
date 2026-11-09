# Sakura 陪看 0.3.21

把浏览器里正在播放的视频画面和进度送进 Sakura 的**原本聊天框**，让角色陪你一起看。

聊天不使用悬浮窗，也不接麦克风；「陪看状态」会打开一个独立的只读小窗。字幕默认走视频站公开 CC；没有 CC 时用本地 Whisper small 按设置里的语言转写当前片段，只有需要登录可见字幕/音频时才读取可选的 `data/cookies.txt`。

## 结构

```text
sakura.watch_together/
├─ plugin.py           Sakura 插件与本地 9377 桥
├─ asr_worker.py       本地 Whisper 日语转写进程（常驻，模型只加载一次）
├─ plugin.yaml
├─ asr_runtime/        内置 Python 3.9 + CPU Torch + Whisper（约 2.0GB）
├─ models/whisper/     内置 small.pt（约 483MB）
├─ bin/                内置 yt-dlp.exe
├─ extension/          Chrome / Edge 扩展
│  ├─ manifest.json
│  ├─ background.js
│  └─ content.js
├─ status.html         陪看状态小窗
├─ sakura-watch-extension-0.3.21.zip
└─ README.md
```

`data/` 是运行时缓存，不包含在发布包里。

## 安装

0. 解压发布包，把 `sakura.watch_together` 整个文件夹放到 `<Sakura 安装目录>\plugins\user\`，确认 `plugin.yaml` 就在这个文件夹根目录。（这个用从文件夹安装也可以，安装一整个就好）
1. 重启 Sakura，让插件加载。
2. 打开 Chrome / Edge 的扩展管理页：
   - Chrome：`chrome://extensions`
   - Edge：`edge://extensions`
3. 打开「开发者模式」，点「加载已解压的扩展程序」。
4. 选择：

```text
<Sakura 安装目录>\plugins\user\sakura.watch_together\extension
```

5. 打开一个正在播放的视频页面，刷新一次。
6. 在 Sakura 输入框旁边点「开始陪看」。

## 内置 ASR

这个发布包已经内置无 CC 视频所需的本地 ASR，**不需要另外安装 Python、Torch、Whisper、yt-dlp 或 ffmpeg**：

```text
asr_runtime/python.exe             Python 3.9.13
asr_runtime/Lib/site-packages/     Torch 2.7.0+cpu、openai-whisper 20231117
asr_runtime/ffmpeg.exe             ffmpeg
models/whisper/small.pt            Whisper small
bin/yt-dlp.exe                     yt-dlp
```

- 没有 CC 时，ASR 语言由设置页「无 CC 字幕语言」决定：中文视频选「中文」，日语动画选「日语」；默认中文。
- CPU 推理，首次加载模型需要几秒，之后常驻；转写速度取决于 CPU 和片段长度。
- 模型和运行时约 2.0GB，解压后请保留目录结构，不要只复制 `plugin.py`。
- 没有 CC 的段落会走 `yt-dlp → ffmpeg → Whisper`；有 CC 时优先站内字幕轨，不启动 ASR。

如果开发时需要改用外部运行时，仍可设置环境变量覆盖内置路径，然后重启 Sakura：

```text
SAKURA_WATCH_ASR_PYTHON      带 whisper/torch 的 python.exe
SAKURA_WATCH_WHISPER_MODEL   small.pt 的完整路径
SAKURA_WATCH_FFMPEG          ffmpeg.exe 的完整路径
```

第三方组件和许可证见 `THIRD_PARTY_NOTICES.md`。

## 设置

在 Sakura 的插件设置页打开「陪看」，可以改：

- **首段间隔（秒）**：30–300，默认 60；第 1 段按这个值，之后按 1×、2×、3×、4×、5× 递增，最长 300 秒。实际会在 0.9–1.1 倍之间浮动。
- **每段分镜帧数**：3–10，默认 10；段末从最近画面里选几帧拼成一张宫格。

保存后，当前陪看会话会从当前位置重新计时，不需要重开。

## 行为

- 扩展每秒上报一次播放位置。
- 扩展每 5 秒优先从 `<video>` 元素直接抓一帧（`drawImage`），送到本地；跨域/DRM 抓不到时才回退到当前标签页截图。后台标签页也能直接抓视频帧。
- 开始陪看时，角色先对当前段说一句。
- 聊天框里只出现一条最小的「（一起看）」触发标记；分镜位置、字幕和评论要求通过隐藏上下文注入，不再作为用户消息显示。
- 之后按自适应节奏（首段 → 2× → 3× → 4× → 5×，上限 300 秒）向扩展要一张 3–10 帧的宫格分镜，再让角色评论这一段。
- 分镜同时带回一段字幕时间轴，提示里按 `[分:秒] 台词` 列出；宫格每格也带该帧字幕。字幕优先从视频站自己的 CC 字幕轨读取（B 站通过 `x/player/v2` 的 `subtitle_url`，并缓存到 `data/subs/`），不再依赖字幕面板是否打开；没有站内字幕轨且网页也没有可用字幕时，才用 yt-dlp + ffmpeg 取当前片段音频，交给本地 Whisper small（`language="ja"`）转写（缓存到 `data/asr/`）。字幕/转写不完整时只作为参考，不要求角色逐句复述。
- 用户暂停时不会继续说话；拖动超过 20 秒、切集或换视频会重新计时；换视频会清空旧视频的帧缓存，避免拿上一个视频的画面评论新视频。
- 换视频时，角色会明确知道「从 A 换到了 B」：新视频开场提示和隐藏上下文都会写明旧标题/新标题，不会把新视频当成上一个视频的剧情继续。
- 只使用已经看过的画面，不预读后续剧情。
- 如果 Sakura 正在处理上一段，会延后本段；追不上就跳过，不会无限堆积。
- 「陪看状态」打开一个约 430×330 的只读小窗：视频标题、播放进度、是否暂停、下一段倒计时、分镜帧数、换视频记录和扩展连接状态；关闭小窗不会停止陪看。

## 本地接口

插件只监听 `127.0.0.1:9377`：

```text
POST /api/hello
POST /api/position
POST /api/frame
POST /api/storyboard
GET  /api/state
```

## 常见问题

**点开始陪看提示没有浏览器上报**

- 确认扩展已启用；
- 刷新视频页面；
- 确认 Sakura 插件没有报 9377 端口占用；
- 打开 `http://127.0.0.1:9377/api/state` 看是否有位置。

**有文字但没有图**

- 跨域/DRM 视频无法直接抓帧时，会回退到标签页截图；后台标签页的截图可能失败，切到前台再试；
- 视频必须处于播放状态，暂停时不会抓帧；
- 视频区域太小或完全在屏幕外时不会抓帧。

**有 CC 但字幕仍为空**

- 先确认视频本身有字幕轨；B 站部分 AI 字幕/登录可见字幕需要账号 Cookie；
- 番剧 `ep`/`ss` 页面会先解析成 BV 再查字幕；当前集没有 `subtitle_url` 时就是 0 条，不是插件漏读；
- 可把导出的 Netscape 格式 Cookie 放到插件目录 `data/cookies.txt`，插件会在 yt-dlp 回退时使用；
- 没有站内轨时才回退到网页字幕面板，面板显示“关闭”时自然取不到；
- 空结果会缓存 6 小时，避免每段都重复等待字幕源。

**没有 CC，ASR 也没出来**

- 发布包已内置 `yt-dlp`、`ffmpeg`、Whisper small 和 CPU 运行时；确认 `asr_runtime/`、`models/whisper/small.pt`、`bin/yt-dlp.exe` 没被杀毒软件或解压工具漏掉；
- 确认设置页「无 CC 字幕语言」与视频语言一致：中文视频选「中文」，日语动画选「日语」；
- 番剧 `ss` 季度页不是播放页；开始陪看前先点进具体 `ep` 集，否则插件会提示“先打开具体一集视频”；
- 音频下载和转写都在后台做，不阻塞插话；每段开始时就会预取“本段计划区间”的 ASR，而不是等提交时才启动，正常情况下到段末已经有完整转写；
- 每段提交时按**计划段的起止点**读取缓存，避免实际播放位置与计划边界相差零点几秒导致缓存不命中；
- 音频转写首次会慢（模型加载 + 当前片段转写），之后按 URL + 片段缓存 24 小时；
- 需要登录才能取的音频，把 Netscape 格式 Cookie 放到 `data/cookies.txt`。

**本地文件视频**

需要在扩展详情里打开「允许访问文件网址」，然后刷新 `file://` 页面。

**陪看太频繁 / 太安静**

到插件设置页「陪看」里改「首段间隔」；默认 60 秒，之后按原版节奏递增为约 60→120→180→240→300 秒（每档 ±10% 浮动）。换视频或拖动进度后从首段重新计时。

**为什么聊天框里还有一条「（一起看）」**

Sakura 当前的 `conversation.begin` 必须先预留一条可见用户消息，Host 没有提供完全隐藏的入站消息。插件把可见部分压到最小，只保留这个触发标记；分镜和评论要求改走隐藏的 `context.contributor`，不会再显示成用户消息。
