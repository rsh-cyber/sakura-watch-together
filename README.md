# Sakura 陪看 0.3.21 Lite

让 Sakura 角色陪你一起看视频。浏览器扩展上报视频进度和画面，插件结合可获取的字幕与多帧分镜，在 Sakura 主聊天中与当前角色互动。

**Lite 轻量版**包含陪看插件、Chrome / Edge 扩展、站内 CC / 网页字幕读取、yt-dlp 和状态窗口。有字幕时结合字幕陪看，没有字幕时可以根据画面评论；本地语音转写可通过 ASR 组件补装。

[下载 Lite 轻量版](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-lite.zip) · [打开 v0.3.21 发布页](https://github.com/rsh-cyber/sakura-watch-together/releases/tag/v0.3.21)

## 三个下载包怎么选

在发布页的 Assets 附件区域，按文件名选择对应的包。

| 包         | 文件与下载链接                                                                                                                                                                   | 用途                                                           |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| ASR 增补组件包 | [sakura.watch_together-0.3.21-asr-addon.zip](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-asr-addon.zip)     | 已安装 Lite 后，补装 Python、Whisper small 模型及 ffmpeg，启用无 CC 视频的本地转写 |
| 完整版       | [sakura.watch_together-0.3.21-asr-bundled.zip](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-asr-bundled.zip) | 一次安装陪看插件、浏览器扩展和 ASR 环境                                       |
| Lite 轻量版  | [sakura.watch_together-0.3.21-lite.zip](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-lite.zip)               | 先使用画面与已有字幕陪看，之后按需补装 ASR                                      |

ASR 增补组件包安装在已有的陪看插件目录中。首次使用可以从 Lite 开始，或选择带齐转写环境的完整版。

## 运行环境

- Windows x64，Sakura Plugin API 4。
- Chrome 或 Edge 116 及以上版本。
- Sakura 中配置可处理图片输入的模型，用于画面陪看。

角色回复使用 Sakura 当前配置的模型。站点字幕、画面和音频的可获取情况会受网页结构、跨域及视频访问条件影响。

## 安装 Lite 轻量版

1. 下载上面的 Lite ZIP。
2. 完全退出 Sakura，包括系统托盘中的后台进程。
3. 解压 ZIP，找到包含 plugin.yaml 和 plugin.py 的 sakura.watch_together 文件夹。
4. 把该文件夹放到以下位置。（从zip或者文件夹也可以装）

```text
<Sakura 安装目录>\plugins\user\sakura.watch_together
```

安装后的结构应类似下面这样。

```text
<Sakura 安装目录>\plugins\user\sakura.watch_together\
├─ plugin.yaml
├─ plugin.py
├─ asr_worker.py
├─ status.html
├─ extension\
│  ├─ manifest.json
│  ├─ background.js
│  └─ content.js
├─ bin\
│  └─ yt-dlp.exe
└─ ASR-DISABLED
```

5. 重启 Sakura，在插件设置中确认「陪看」已启用。
6. 继续按照下一节加载浏览器扩展。

如果 Sakura 提供本地 ZIP 或文件夹安装入口，也可通过插件设置导入。选择文件夹时，应选择包含 plugin.yaml 的这一层。

更新已有安装时，先备份并保留原来的 data 目录和配置，再替换插件文件。

## 加载浏览器扩展

1. 在浏览器地址栏打开对应页面。

```text
Edge    edge://extensions
Chrome  chrome://extensions
```

2. 开启「开发者模式」。
3. 点击「加载解压缩的扩展」，选择插件内的 extension 文件夹。

```text
<Sakura 安装目录>\plugins\user\sakura.watch_together\extension
```

4. 打开要观看的视频页面，刷新一次。
5. 在 Sakura 输入框旁点击「开始陪看」。

也可以使用插件包内附带的浏览器扩展 ZIP，解压后选择其中包含 manifest.json 的目录。

## 使用

- 「开始陪看」从当前视频进度开始。
- 「停止陪看」停止后续评论投递。
- 「陪看状态」查看浏览器连接、播放进度、下一段分镜和 ASR 状态。

插件按播放进度整理多帧分镜，默认每段 10 帧。插话间隔会按 60、120、180、240、300 秒逐步调整。暂停、换视频或拖动进度会影响调度。

开始陪看后，视频画面和相关字幕会通过 Sakura 交给当前配置的模型。浏览器扩展与插件通过本机 127.0.0.1:9377 通信。

## 给 Lite 补装 ASR 组件

希望没有 CC 的视频也能产生转写字幕时，先安装 Lite，再按以下步骤补齐 ASR。

1. 下载 [ASR 增补组件包](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-asr-addon.zip)。
2. 完全退出 Sakura。
3. 解压增补包，将其中完整的 asr_runtime 和 models 两个文件夹放入陪看插件根目录，与 plugin.py 并排。

```text
<Sakura 安装目录>\plugins\user\sakura.watch_together\
├─ plugin.yaml
├─ plugin.py
├─ asr_runtime\
│  ├─ python.exe
│  ├─ ffmpeg.exe
│  └─ 其余运行环境文件
├─ models\
│  └─ whisper\
│     └─ small.pt
├─ bin\
│  └─ yt-dlp.exe
└─ extension\
```

4. **删除插件根目录中的 ASR-DISABLED 文件。** 这个标记会强制关闭 ASR，补齐环境后需要移除它才能启用。
5. 重启 Sakura。
6. 在 Sakura 设置的「陪看 → 无 CC 字幕语言」中，为中文视频选择中文，为日语视频选择日语。当前流程按所选语言转写。
7. 打开「陪看状态」，确认语音识别显示「Whisper 中文 · 待命」或「Whisper 日语 · 待命」。

选择一个没有 CC 的视频开始陪看，等待音频获取、模型加载和转写完成。有可用 CC 时优先使用 CC；无 CC 时再尝试 yt-dlp → ffmpeg → Whisper。本地转写与评论调度分别进行，转写结果可能稍后进入字幕缓存。

## 安装完整版

直接下载 [asr-bundled 完整包](https://github.com/rsh-cyber/sakura-watch-together/releases/download/v0.3.21/sakura.watch_together-0.3.21-asr-bundled.zip)，按照上面的插件安装步骤放入 sakura.watch_together 目录，再加载浏览器扩展、选择转写语言。

完整版已包含 asr_runtime 和 Whisper small 模型。若从 Lite 覆盖升级，安装完成后要删除旧目录中遗留的 ASR-DISABLED，再重启 Sakura。

## 常见问题

### Lite 状态显示「ASR 未安装」

这是 Lite 初始状态。画面与可获取的 CC 字幕仍可用于陪看。需要转写时，按上面的步骤补装 ASR。

### 补装后仍然显示「ASR 未安装」

先检查 ASR-DISABLED 是否已经删除，再确认这三个文件的位置，并完全重启 Sakura。

```text
asr_runtime\python.exe
asr_runtime\ffmpeg.exe
models\whisper\small.pt
```

运行环境需要保留完整目录结构。

### 有 CC，但 Whisper 一直待命

插件优先使用可获取的 CC，只有需要无 CC 字幕回退时才尝试 ASR。

### 没有字幕或取流失败

Lite 使用视频可提供的 CC 或网页字幕。安装 ASR 后，还需能获取音频并成功转写。检查转写语言、bin/yt-dlp.exe、视频访问条件和状态窗中的错误。部分视频需要登录后才可取得字幕或音频。

### 浏览器扩展无法加载，或 Sakura 一直等不到视频

确认选择的是含 manifest.json 的 extension 目录，使用包内原文件。加载后刷新视频页面，再点击「开始陪看」。不同站点、跨域或受保护的视频可能影响画面采集。

### 更新后状态窗口仍是旧内容

状态页会在插件启动时加载。更新完成后完全退出并重新启动 Sakura，再打开「陪看状态」。

## 反馈问题

在仓库 Issues 中说明 Sakura 版本、插件版本、浏览器版本、使用 Lite 还是完整版，以及状态窗信息和复现步骤。反馈日志前移除 Cookie、令牌和私人对话。

[GitHub 仓库](https://github.com/rsh-cyber/sakura-watch-together) · [完整 Release 附件列表](https://github.com/rsh-cyber/sakura-watch-together/releases/tag/v0.3.21)
