# 给 Lite 版安装 ASR

Lite 版本默认不带语音识别。没有 CC 的视频不会产生字幕；装好本组件后才会启用本地 Whisper。

## 1. 下载

同目录下的：

```text
sakura.watch_together-0.3.21-asr-addon.zip
```

```text
大小：806,529,577 字节（约 769 MiB）
SHA256：DB6E7F53D32C2D72D2E73B2C01B86A0A77297F31CC7D2BC3EE1295EC482A1E39
```

解压后约 2GB，需要至少 2GB 可用空间。

## 2. 解压到插件目录

把压缩包里的这两个文件夹：

```text
asr_runtime\
models\
```

整个放到插件根目录，和 `plugin.py` 并排：

```text
D:\Sakura\plugins\user\sakura.watch_together\
├─ plugin.py
├─ asr_runtime\
│  ├─ python.exe
│  └─ ffmpeg.exe
├─ models\
│  └─ whisper\
│     └─ small.pt
├─ bin\
│  └─ yt-dlp.exe
└─ ASR-DISABLED
```

不要只复制 `python.exe` 或 `small.pt`，目录结构必须保留。

## 3. 删除禁用标记

Lite 包根部有一个：

```text
ASR-DISABLED
```

安装完 ASR 后，把它删掉。

这个文件存在时，插件会强制关闭 ASR，即使本机其他位置有可用运行时也不会用。

## 4. 重启 Sakura

完全退出 Sakura（包括托盘），再启动。

打开「陪看状态」，语音识别一栏应显示：

```text
Whisper 中文 · 待命
```

或：

```text
Whisper 日语 · 待命
```

第一次转写时模型加载需要几秒，之后会变成「已加载」。

## 5. 选择语言

在 Sakura 设置 →「陪看」→「无 CC 字幕语言」：

- 中文视频：中文
- 日语动画：日语

不会自动识别语言；选错会明显降低准确率。

## 6. 验证

找一个没有 CC 的中文或日语视频，开始陪看，等第一段投递。

- 有 CC：仍然优先使用 CC，不启动 ASR；
- 无 CC：才会走 `yt-dlp → ffmpeg → Whisper`；
- 如果状态显示「ASR 未安装」：检查 `ASR-DISABLED` 是否已删；
- 如果显示「Whisper 模型不可用」：检查 `asr_runtime\python.exe`、`asr_runtime\ffmpeg.exe`、`models\whisper\small.pt` 是否都在；
- 如果下载音频失败：检查 `bin\yt-dlp.exe` 是否存在，或视频是否要登录才能取流。

## 可选：使用外部运行时

如果不想用内置包，也可以让插件指向已有的 Python / Whisper：

```text
SAKURA_WATCH_ASR_PYTHON      带 torch 和 openai-whisper 的 python.exe
SAKURA_WATCH_WHISPER_MODEL   small.pt 的完整路径
SAKURA_WATCH_FFMPEG          ffmpeg.exe 的完整路径
```

设置环境变量后重启 Sakura，并删除 `ASR-DISABLED`。

注意：外部 Python 必须是 3.9–3.11 的 Windows x64 环境，且已安装 `torch`、`openai-whisper`、`numpy`；不推荐手动拼环境，内置 addon 更稳。
