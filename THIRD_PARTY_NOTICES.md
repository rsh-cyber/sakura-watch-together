# Third-party notices

This release bundles the following components so that the optional no-CC ASR
path works without a separate Python installation.

| Component | Version/path | License / source |
|---|---|---|
| CPython | 3.9.13, `asr_runtime/` | PSF License, https://www.python.org/ |
| PyTorch | 2.7.0+cpu | BSD-3-Clause, https://pytorch.org/ |
| OpenAI Whisper | 20231117 | MIT, https://github.com/openai/whisper |
| Whisper small model | `models/whisper/small.pt` | MIT (OpenAI Whisper release) |
| NumPy | 2.0.2 | BSD-3-Clause, https://numpy.org/ |
| Numba / llvmlite | 0.60.0 / 0.43.0 | BSD-2-Clause, https://numba.pydata.org/ |
| tiktoken | 0.14.0 | MIT, https://github.com/openai/tiktoken |
| regex | 2026.1.15 | Apache-2.0, https://github.com/mrabarnett/mrab-regex |
| tqdm | 4.70.1 | MPL-2.0 and MIT, https://github.com/tqdm/tqdm |
| more-itertools | 10.8.0 | MIT, https://github.com/more-itertools/more-itertools |
| requests / urllib3 / certifi / charset-normalizer / idna | bundled with tiktoken | Apache-2.0 / MIT / MPL-2.0 / MIT / BSD |
| SymPy / mpmath / networkx / Jinja2 / MarkupSafe / filelock / fsspec | bundled with Torch | BSD / BSD / BSD / BSD / BSD / Unlicense / BSD |
| FFmpeg | n4.3.2 build, `asr_runtime/ffmpeg.exe` | GPL-3.0-or-later (build uses `--enable-version3`), https://ffmpeg.org/ |
| yt-dlp | `bin/yt-dlp.exe` | Unlicense, https://github.com/yt-dlp/yt-dlp |

The bundle is intended to be distributed together with this notice. Source
code and license texts for each project are available from the upstream links
above. FFmpeg is distributed under GPLv3; redistribution must comply with the
GPL, including providing the corresponding source or a written offer where
required.
