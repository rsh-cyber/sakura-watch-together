"""Sakura watch-together bridge.

The browser extension reports the currently playing <video> position and sends
cropped screenshots.  This plugin buffers recent frames, asks the extension for a
multi-frame storyboard at each segment boundary, and hands one storyboard plus a
short prompt to Sakura's normal chat window through sakura.host.conversation.

Design notes:
- no separate chat window (only a small status popup) and no microphone;
- the browser is the only source of playback truth;
- frames are cropped by the extension, so this process needs no image library;
- delivery is best-effort: if Sakura is busy, the segment is skipped, not queued
  indefinitely.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9377
DEFAULT_SEGMENT_SECONDS = 60.0
MIN_SEGMENT_SECONDS = 30.0
MAX_SEGMENT_SECONDS = 300.0
DEFAULT_STORYBOARD_FRAMES = 10
MIN_STORYBOARD_FRAMES = 3
MAX_STORYBOARD_FRAMES = 10
SETTINGS_SECTION_ID = "watch_together"
MAX_BODY_BYTES = 8 * 1024 * 1024
FRAME_FRESH_SECONDS = 15.0
POSITION_FRESH_SECONDS = 12.0
CONTEXT_PROVIDER_ID = "watch_together.current"


def _clock_text(seconds: float) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "0:00"
    return f"{total // 60}:{total % 60:02d}"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        # Keep the query: Bilibili uses ?p=N for multi-part episodes.  Drop
        # only the fragment, which changes during SPA navigation.
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return (base + (f"?{parsed.query}" if parsed.query else ""))[:1000]
    except Exception:
        return text[:500]


def _is_playable_url(value: Any) -> bool:
    """True only for pages that can actually yield an episode audio stream."""
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if "bilibili.com" not in host:
        return parsed.scheme in ("http", "https")
    # Bilibili index/season pages are not playable media.  Episode pages and
    # ordinary BV videos are.
    if path.startswith("/video/") and path[7:].strip("/"):
        return True
    if "/bangumi/play/ep" in path:
        return True
    return False


BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_PLAYER_API = "https://api.bilibili.com/x/player/v2"
BILIBILI_SEASON_API = "https://api.bilibili.com/pgc/view/web/season"
SUBTITLE_CACHE_TTL = 6 * 60 * 60
SUBTITLE_MAX_CUES = 4000
ASR_CACHE_TTL = 24 * 60 * 60
ASR_DOWNLOAD_TIMEOUT = 120.0
ASR_FFMPEG_TIMEOUT = 20.0
ASR_WORKER_TIMEOUT = 45.0
ASR_ENGINE_ID = "whisper-small-multi-v2"
_ASR_LANGUAGE = os.environ.get("SAKURA_WATCH_ASR_LANGUAGE", "zh").strip().lower()
ASR_LANGUAGE = _ASR_LANGUAGE if _ASR_LANGUAGE in ("zh", "ja") else "zh"
ASR_WORKER = Path(__file__).resolve().with_name("asr_worker.py")
ASR_DISABLED = Path(__file__).resolve().with_name("ASR-DISABLED").exists()


def _first_file(*candidates: Any) -> Path:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    for candidate in candidates:
        if candidate:
            return Path(candidate)
    return Path("")


# Works on the common Sakura + GPT-SoVITS layout, while allowing any external
# runtime/model through environment variables.  If these are absent the plugin
# still works; only the optional no-CC ASR fallback stays unavailable.
_PLUGIN_DIR = Path(__file__).resolve().parent
_SAKURA_ROOT = Path(__file__).resolve().parents[3]
ASR_PYTHON = _first_file(
    os.environ.get("SAKURA_WATCH_ASR_PYTHON"),
    _PLUGIN_DIR / "asr_runtime" / "python.exe",
    _SAKURA_ROOT / "tts" / "g50" / "runtime" / "python.exe",
    sys.executable,
)
ASR_WHISPER_MODEL = _first_file(
    os.environ.get("SAKURA_WATCH_WHISPER_MODEL"),
    _PLUGIN_DIR / "models" / "whisper" / "small.pt",
    _SAKURA_ROOT / "tts" / "g50" / "tools" / "asr" / "models" / "whisper" / "small.pt",
)
FFMPEG = _first_file(
    os.environ.get("SAKURA_WATCH_FFMPEG"),
    _PLUGIN_DIR / "asr_runtime" / "ffmpeg.exe",
    shutil.which("ffmpeg"),
    _SAKURA_ROOT / "tts" / "g50" / "runtime" / "ffmpeg.exe",
)


def _bilibili_key(url: str) -> Optional[Tuple[str, int]]:
    """Return (bvid, page) for a Bilibili watch URL, else None."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return None
    if "bilibili.com" not in parsed.netloc.lower():
        return None
    found = re.search(r"(BV[0-9A-Za-z]{10})", parsed.path, re.IGNORECASE)
    if not found:
        return None
    page = 1
    try:
        values = parse_qs(parsed.query).get("p") or []
        if values:
            page = max(1, int(values[0]))
    except (TypeError, ValueError):
        page = 1
    return found.group(1), page


def _bilibili_ep(url: str) -> Optional[int]:
    """Return the ep id from a bangumi URL, else None."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return None
    if "bilibili.com" not in parsed.netloc.lower():
        return None
    found = re.search(r"/bangumi/play/ep(\d+)", parsed.path, re.IGNORECASE)
    return int(found.group(1)) if found else None


def _bilibili_season(url: str) -> Optional[int]:
    """Return the season id from a bangumi season URL, else None."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return None
    if "bilibili.com" not in parsed.netloc.lower():
        return None
    found = re.search(r"/bangumi/play/ss(\d+)", parsed.path, re.IGNORECASE)
    return int(found.group(1)) if found else None


class SubtitleTrack:
    """Fetch and cache the site's own CC track.

    This is deliberately the same source the original watch-together uses:
    Bilibili's subtitle metadata, not the rendered DOM.  The browser extension
    still supplies playback position and frames; subtitles come from the site's
    player API and are cached per BVID + CID.
    """

    def __init__(self, cache_dir: Path, log: Any) -> None:
        self.cache_dir = cache_dir
        self.log = log
        self._lock = threading.RLock()
        self._memory: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _http_json(self, url: str) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        with urllib.request.urlopen(request, timeout=7) as response:
            return json.loads(response.read().decode("utf-8"))

    def _resolve_cid(self, bvid: str, page: int) -> Optional[int]:
        payload = self._http_json(f"{BILIBILI_VIEW_API}?bvid={bvid}")
        if int(payload.get("code") or 0) != 0:
            return None
        data = payload.get("data") or {}
        pages = data.get("pages")
        if isinstance(pages, list) and pages:
            chosen = pages[max(0, min(len(pages) - 1, page - 1))]
            cid = chosen.get("cid")
            if cid is not None:
                return int(cid)
        cid = data.get("cid")
        return int(cid) if cid is not None else None

    def _resolve_episode_bvid(self, ep_id: int) -> Optional[str]:
        payload = self._http_json(f"{BILIBILI_SEASON_API}?ep_id={ep_id}")
        result = payload.get("result") or {}
        episodes = result.get("episodes")
        if not isinstance(episodes, list):
            return None
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            if int(episode.get("id") or 0) != ep_id:
                continue
            bvid = str(episode.get("bvid") or "").strip()
            return bvid or None
        return None

    def _resolve_season_bvid(self, season_id: int) -> Optional[str]:
        payload = self._http_json(f"{BILIBILI_SEASON_API}?season_id={season_id}")
        result = payload.get("result") or {}
        episodes = result.get("episodes")
        if not isinstance(episodes, list):
            return None
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            bvid = str(episode.get("bvid") or "").strip()
            if bvid:
                return bvid
        return None

    @staticmethod
    def _parse_srt(text: str) -> List[Dict[str, Any]]:
        cues: List[Dict[str, Any]] = []
        pattern = re.compile(
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
        )
        for block in re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", "")):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            match = pattern.search("\n".join(lines))
            if not match:
                continue
            def stamp(value: str) -> float:
                parts = value.replace(",", ".").split(":")
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            content = " ".join(
                line for line in lines if not pattern.search(line) and not line.isdigit()
            )
            content = re.sub(r"<[^>]+>", "", content).strip()
            if not content:
                continue
            cues.append(
                {
                    "from": round(stamp(match.group(1)), 2),
                    "to": round(stamp(match.group(2)), 2),
                    "text": content[:240],
                }
            )
        return cues

    @staticmethod
    def _ytdlp_path() -> Optional[str]:
        candidates = [Path(__file__).resolve().parent / "bin" / "yt-dlp.exe"]
        found = shutil.which("yt-dlp")
        if found:
            candidates.append(Path(found))
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(Path(local) / "Microsoft" / "WinGet" / "Links" / "yt-dlp.exe")
            packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
            try:
                candidates.extend(packages.glob("yt-dlp.yt-dlp_*/yt-dlp.exe"))
                candidates.extend(packages.glob("yt-dlp.yt-dlp_*/*/yt-dlp.exe"))
            except Exception:
                pass
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_size > 1_000_000:
                    return str(candidate)
            except Exception:
                continue
        return None

    def _fetch_with_ytdlp(self, bvid: str, page: int) -> List[Dict[str, Any]]:
        executable = self._ytdlp_path()
        if not executable:
            return []
        ffmpeg = Path(r"D:\Sakura\tts\g50\runtime\ffmpeg.exe")
        target_url = f"https://www.bilibili.com/video/{bvid}?p={max(1, page)}"
        temp_dir = Path(tempfile.mkdtemp(prefix="subs_", dir=str(self.cache_dir)))
        output = str(temp_dir / "%(id)s.%(ext)s")
        base = [
            str(executable),
            "--no-warnings",
            "--no-playlist",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "zh-Hans,zh-CN,zh,ai-zh",
            "--sub-format",
            "srt",
            "--convert-subs",
            "srt",
            "--socket-timeout",
            "15",
            "-o",
            output,
        ]
        if ffmpeg.exists():
            base.extend(["--ffmpeg-location", str(ffmpeg)])
        cookie_file = self.cache_dir.parent / "cookies.txt"
        if cookie_file.is_file() and cookie_file.stat().st_size > 0:
            base.extend(["--cookies", str(cookie_file)])
        try:
            for browser in ("edge", None):
                command = list(base)
                if browser:
                    command.extend(["--cookies-from-browser", browser])
                command.append(target_url)
                try:
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    continue
                if result.returncode != 0:
                    continue
                files = sorted(temp_dir.glob("*.srt"))
                if not files:
                    continue
                text = files[0].read_text(encoding="utf-8", errors="replace")
                cues = self._parse_srt(text)
                if cues:
                    return cues
            return []
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _fetch(self, bvid: str, page: int) -> List[Dict[str, Any]]:
        # The player API is fast and needs no external binary.  Only fall back
        # to yt-dlp when the site has no usable subtitle metadata.
        cues = self._fetch_api(bvid, page)
        if cues:
            return cues
        return self._fetch_with_ytdlp(bvid, page)

    def _fetch_api(self, bvid: str, page: int) -> List[Dict[str, Any]]:
        cid = self._resolve_cid(bvid, page)
        if cid is None:
            return []
        payload = self._http_json(
            f"{BILIBILI_PLAYER_API}?bvid={bvid}&cid={cid}"
        )
        if int(payload.get("code") or 0) != 0:
            return []
        data = payload.get("data") or {}
        subtitle = data.get("subtitle") or {}
        tracks = subtitle.get("subtitles") or []
        if not isinstance(tracks, list) or not tracks:
            return []
        preferred = None
        for track in tracks:
            if not isinstance(track, dict):
                continue
            url = str(track.get("subtitle_url") or "").strip()
            if not url:
                continue
            language = str(track.get("lan") or "").lower()
            label = str(track.get("lan_doc") or "")
            if preferred is None or language.startswith("zh") or "中文" in label:
                preferred = url
                if language.startswith("zh") or "中文" in label:
                    break
        if not preferred:
            return []
        if preferred.startswith("//"):
            preferred = "https:" + preferred
        elif preferred.startswith("/"):
            preferred = "https://www.bilibili.com" + preferred
        subtitle_payload = self._http_json(preferred)
        body = subtitle_payload.get("body")
        if not isinstance(body, list):
            return []
        cues: List[Dict[str, Any]] = []
        for item in body[:SUBTITLE_MAX_CUES]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            start = _as_float(item.get("from"), -1.0)
            end = _as_float(item.get("to"), start + 1.0)
            if start < 0:
                continue
            cues.append({"from": round(start, 2), "to": round(end, 2), "text": text[:240]})
        return cues

    def cues(self, url: str, start: float, end: float) -> List[Dict[str, Any]]:
        key = _bilibili_key(url)
        if key is None:
            ep_id = _bilibili_ep(url)
            if ep_id is not None:
                bvid = self._resolve_episode_bvid(ep_id)
                if not bvid:
                    self.log.warning("番剧字幕无法解析 BV", fields={"ep_id": ep_id})
                    return []
            else:
                season_id = _bilibili_season(url)
                if season_id is None:
                    return []
                bvid = self._resolve_season_bvid(season_id)
                if not bvid:
                    self.log.warning(
                        "番剧季度字幕无法解析 BV", fields={"season_id": season_id}
                    )
                    return []
            page = 1
        else:
            bvid, page = key
        cache_key = f"{bvid}_p{page}"
        now = time.time()
        with self._lock:
            memory = self._memory.get(cache_key)
            if memory and now - memory[0] < SUBTITLE_CACHE_TTL:
                return self._slice(memory[1], start, end)
        cache_file = self.cache_dir / f"{cache_key}.json"
        cues: List[Dict[str, Any]] = []
        cached = False
        try:
            if cache_file.exists() and now - cache_file.stat().st_mtime < SUBTITLE_CACHE_TTL:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(raw.get("cues"), list):
                    cues = raw["cues"]
                    cached = True
        except Exception:
            cues = []
            cached = False
        if not cached:
            try:
                cues = self._fetch(bvid, page)
            except Exception as error:
                self.log.warning(
                    "站内字幕获取失败",
                    fields={"bvid": bvid, "error": str(error)[:200]},
                )
                cues = []
            try:
                temp = cache_file.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(
                        {"bvid": bvid, "page": page, "cues": cues},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                temp.replace(cache_file)
            except Exception:
                pass
        self.log.info(
            "字幕轨读取",
            fields={
                "bvid": bvid,
                "page": page,
                "cues": len(cues),
                "cached": cached,
            },
        )
        with self._lock:
            self._memory[cache_key] = (now, cues)
        return self._slice(cues, start, end)

    @staticmethod
    def _slice(cues: List[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
        if not cues:
            return []
        lo = float(start) - 0.5
        hi = float(end) + 0.5
        return [
            cue
            for cue in cues
            if float(cue.get("from", -1)) >= lo and float(cue.get("from", -1)) <= hi
        ][:14]


class AsrTrack:
    """Transcribe the current audio segment when the site has no CC track.

    The heavy model lives in the GPT-SoVITS runtime, so it is loaded once in a
    persistent worker process.  Audio is fetched through yt-dlp and sliced with
    ffmpeg; the result is cached per URL + segment.
    """

    def __init__(self, cache_dir: Path, log: Any, language: str = "zh") -> None:
        self.cache_dir = cache_dir
        self.log = log
        self.language = language if language in ("zh", "ja") else "zh"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._prepare_url = ""
        self._prefetch_lock = threading.Lock()
        self._prefetching: set = set()
        self._proc: Optional[subprocess.Popen] = None
        self._queue: Optional[queue.Queue] = None
        self._reader: Optional[threading.Thread] = None
        self._last_error = ""

    def available(self) -> bool:
        return bool(
            not ASR_DISABLED
            and ASR_PYTHON.is_file()
            and ASR_WORKER.is_file()
            and FFMPEG.is_file()
            and ASR_WHISPER_MODEL.is_file()
            and ASR_WHISPER_MODEL.stat().st_size > 10_000_000
            and SubtitleTrack._ytdlp_path() is not None
        )

    def status(self) -> dict:
        return {
            "available": self.available(),
            "disabled": ASR_DISABLED,
            "worker": bool(self._proc is not None and self._proc.poll() is None),
            "language": self.language,
            "error": self._last_error[:200],
        }

    def _cached_audio(self, url: str) -> Optional[Path]:
        digest = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:20]
        for path in self.cache_dir.glob(digest + ".*"):
            if path.suffix.lower() in (".part", ".wav", ".json"):
                continue
            try:
                if path.stat().st_size > 10_000:
                    return path
            except Exception:
                continue
        return None

    def prepare(self, url: str) -> None:
        """Fetch the episode audio in the background so a segment never waits."""
        if not url or not _is_playable_url(url):
            return
        if not self.available() or self._cached_audio(url):
            return
        self._last_error = ""
        with self._prepare_lock:
            if self._prepare_url == url:
                return
            self._prepare_url = url

        def run() -> None:
            try:
                self._audio_for(url)
            except Exception as error:
                self.log.warning(
                    "ASR 预取音频异常", fields={"error": str(error)[:200]}
                )
            finally:
                with self._prepare_lock:
                    if self._prepare_url == url:
                        self._prepare_url = ""

        threading.Thread(target=run, name="WatchTogetherAsrPrefetch", daemon=True).start()

    def _audio_for(self, url: str) -> Optional[Path]:
        if not _is_playable_url(url):
            return None
        digest = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:20]
        with self._download_lock:
            cached = self._cached_audio(url)
            if cached is not None:
                return cached
            executable = SubtitleTrack._ytdlp_path()
            if not executable:
                return None
            target = self.cache_dir / f"{digest}.%(ext)s"
            command = [
                executable,
                "--no-warnings",
                "--no-playlist",
                "--playlist-items",
                "1",
                "-f",
                "bestaudio/best",
                "--socket-timeout",
                "20",
                "-o",
                str(target),
            ]
            if FFMPEG.is_file():
                command.extend(["--ffmpeg-location", str(FFMPEG)])
            cookie_file = self.cache_dir.parent / "cookies.txt"
            if cookie_file.is_file() and cookie_file.stat().st_size > 0:
                command.extend(["--cookies", str(cookie_file)])
            command.append(str(url))
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=ASR_DOWNLOAD_TIMEOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as error:
                self._last_error = f"下载音频失败: {error}"
                self.log.warning("ASR 音频下载异常", fields={"error": str(error)[:200]})
                return None
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                self._last_error = "下载音频失败: " + (detail[-1][:200] if detail else f"exit {result.returncode}")
                self.log.warning("ASR 音频下载失败", fields={"error": self._last_error})
                return None
            for path in self.cache_dir.glob(digest + ".*"):
                if path.suffix.lower() in (".part", ".wav", ".json"):
                    continue
                try:
                    if path.stat().st_size > 10_000:
                        return path
                except Exception:
                    continue
            self._last_error = "下载音频失败: 没有找到音频文件"
            return None

    def _stop_worker(self) -> None:
        proc = self._proc
        self._proc = None
        self._queue = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _start_worker(self) -> None:
        self._stop_worker()
        runtime = ASR_PYTHON
        if not runtime.is_file() or not ASR_WORKER.is_file():
            raise RuntimeError("ASR runtime is missing")
        q: queue.Queue = queue.Queue()
        environment = os.environ.copy()
        environment["SAKURA_WATCH_WHISPER_MODEL"] = str(ASR_WHISPER_MODEL)
        proc = subprocess.Popen(
            [str(runtime), str(ASR_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=environment,
        )
        self._proc = proc
        self._queue = q

        def read() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        self._reader = threading.Thread(target=read, name="WatchTogetherAsr", daemon=True)
        self._reader.start()

    def _transcribe(self, wav: Path) -> str:
        with self._lock:
            last_error = ""
            for attempt in range(2):
                if self._proc is None or self._proc.poll() is not None:
                    self._start_worker()
                proc = self._proc
                q = self._queue
                if proc is None or proc.stdin is None or q is None:
                    last_error = "ASR worker 未启动"
                    continue
                try:
                    proc.stdin.write(
                        json.dumps(
                            {"path": str(wav), "language": self.language},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    proc.stdin.flush()
                    line = q.get(timeout=ASR_WORKER_TIMEOUT)
                except Exception as error:
                    last_error = str(error)
                    self._stop_worker()
                    continue
                if line is None:
                    last_error = "ASR worker 已退出"
                    self._stop_worker()
                    continue
                try:
                    payload = json.loads(line)
                except Exception as error:
                    last_error = f"ASR 返回格式错误: {error}"
                    self._stop_worker()
                    continue
                if not payload.get("ok"):
                    last_error = str(payload.get("error") or "ASR 失败")
                    # A model-load failure is permanent; a bad audio file is not.
                    if "MODEL_LOAD_FAILED" in last_error:
                        self._stop_worker()
                        break
                    continue
                self._last_error = ""
                cues = payload.get("cues")
                return {
                    "text": str(payload.get("text") or "").strip(),
                    "cues": cues if isinstance(cues, list) else [],
                }
            self._last_error = last_error or "ASR 失败"
            self.log.warning("ASR 识别失败", fields={"error": self._last_error[:200]})
            return {"text": "", "cues": []}

    def _normalize_cues(
        self, raw: Any, start: float, end: float
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        cues: List[Dict[str, Any]] = []
        span = max(0.1, float(end) - float(start))
        for item in raw[:16]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            local_from = max(0.0, min(span, _as_float(item.get("from"))))
            local_to = max(local_from + 0.2, min(span, _as_float(item.get("to"))))
            cues.append(
                {
                    "from": round(float(start) + local_from, 2),
                    "to": round(float(start) + local_to, 2),
                    "text": text[:240],
                }
            )
        return cues

    def _slice_text(self, text: str, start: float, end: float) -> List[Dict[str, Any]]:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return []
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;])", text) if p.strip()]
        if not parts:
            parts = [text]
        parts = parts[:12]
        total = sum(len(part) for part in parts) or 1
        span = max(0.1, float(end) - float(start))
        cursor = float(start)
        cues: List[Dict[str, Any]] = []
        for part in parts:
            share = span * (len(part) / total)
            cues.append(
                {
                    "from": round(cursor, 2),
                    "to": round(cursor + share, 2),
                    "text": part[:240],
                }
            )
            cursor += share
        return cues

    def _cache_key(self, url: str, start: float, end: float) -> str:
        return hashlib.sha1(
            f"{ASR_ENGINE_ID}|{self.language}|{url}|{int(start)}|{int(end)}".encode("utf-8")
        ).hexdigest()[:24]

    def cached_cues(
        self, url: str, start: float, end: float
    ) -> List[Dict[str, Any]]:
        """Return a finished transcript only; never download or transcribe."""
        if not url or end <= start:
            return []
        cache_file = self.cache_dir / f"{self._cache_key(url, start, end)}.json"
        try:
            if cache_file.is_file() and time.time() - cache_file.stat().st_mtime < ASR_CACHE_TTL:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                cues = raw.get("cues")
                if isinstance(cues, list):
                    return cues
        except Exception:
            pass
        return []

    def prefetch_cues(self, url: str, start: float, end: float) -> None:
        """Warm the ASR cache in the background without blocking delivery."""
        if not url or end <= start or not _is_playable_url(url):
            return
        if not self.available() or self.cached_cues(url, start, end):
            return
        key = self._cache_key(url, start, end)
        with self._prefetch_lock:
            if key in self._prefetching:
                return
            self._prefetching.add(key)

        def run() -> None:
            try:
                if self._audio_for(url) is not None:
                    self.cues(url, start, end)
            except Exception as error:
                self.log.warning(
                    "ASR 预转写异常", fields={"error": str(error)[:200]}
                )
            finally:
                with self._prefetch_lock:
                    self._prefetching.discard(key)

        threading.Thread(
            target=run, name="WatchTogetherAsrCues", daemon=True
        ).start()

    def cues(self, url: str, start: float, end: float) -> List[Dict[str, Any]]:
        if not url or end <= start:
            return []
        if not self.available():
            return []
        cache_key = self._cache_key(url, start, end)
        cache_file = self.cache_dir / f"{cache_key}.json"
        now = time.time()
        try:
            if cache_file.is_file() and now - cache_file.stat().st_mtime < ASR_CACHE_TTL:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                cues = raw.get("cues")
                if isinstance(cues, list):
                    return cues
        except Exception:
            pass
        if float(end) - float(start) < 2.0:
            # A sub-two-second opening fragment is mostly noise/music.
            return []
        with self._prepare_lock:
            if self._prepare_url == url:
                # The prefetch is still writing; do not read a half file.
                return []
        audio = self._cached_audio(url)
        if audio is None:
            self.prepare(url)
            return []
        wav = self.cache_dir / f"{cache_key}.wav"
        try:
            result = subprocess.run(
                [
                    str(FFMPEG),
                    "-y",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{float(start):.3f}",
                    "-t",
                    f"{max(1.0, float(end) - float(start)):.3f}",
                    "-i",
                    str(audio),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    str(wav),
                ],
                capture_output=True,
                text=True,
                timeout=ASR_FFMPEG_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not wav.is_file() or wav.stat().st_size <= 44:
                self._last_error = "音频切片失败: " + (result.stderr or "")[:200]
                self.log.warning("ASR 音频切片失败", fields={"error": self._last_error})
                return []
            result = self._transcribe(wav)
            cues = self._normalize_cues(result.get("cues"), start, end)
            if not cues:
                cues = self._slice_text(str(result.get("text") or ""), start, end)
        except Exception as error:
            self._last_error = str(error)[:200]
            self.log.warning("ASR 字幕异常", fields={"error": self._last_error})
            cues = []
        finally:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass
        if cues:
            try:
                temp = cache_file.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(
                        {"url": url, "from": start, "to": end, "cues": cues},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                temp.replace(cache_file)
            except Exception:
                pass
        self.log.info(
            "ASR 字幕读取",
            fields={"cues": len(cues), "chars": sum(len(c.get("text", "")) for c in cues)},
        )
        return cues

    def close(self) -> None:
        with self._lock:
            self._stop_worker()


@dataclass
class Frame:
    data: bytes
    pos: float
    title: str
    url: str
    caption: str
    captured_at: float


@dataclass
class Snapshot:
    pos: float
    duration: float
    title: str
    url: str
    paused: bool
    ended: bool
    at: float
    frame: Optional[Frame]


class BrowserBridge:
    """Thread-safe state shared by the HTTP handler and the watch loop."""

    def __init__(self, log: Any) -> None:
        self.log = log
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._position: Optional[dict] = None
        self._frame: Optional[Frame] = None
        self._frames: List[Frame] = []
        self._storyboard_request: Optional[dict] = None
        self._storyboard_result: Optional[tuple[str, bytes, Dict[str, Any]]] = None
        self._hello_at = 0.0
        self._hello_version = ""
        self._status_extra: Optional[Any] = None

    def hello(self, version: str) -> None:
        with self._lock:
            self._hello_at = time.monotonic()
            self._hello_version = str(version or "")[:80]

    def update_position(self, payload: dict) -> None:
        with self._lock:
            self._position = {
                "pos": _as_float(payload.get("pos")),
                "duration": _as_float(payload.get("dur")),
                "title": str(payload.get("title") or "")[:300],
                "url": str(payload.get("url") or "")[:1000],
                "paused": bool(payload.get("paused")),
                "ended": bool(payload.get("ended")),
                "at": time.monotonic(),
            }

    def update_frame(self, payload: dict) -> bool:
        raw = payload.get("image")
        if not isinstance(raw, str):
            return False
        if raw.startswith("data:"):
            try:
                _head, encoded = raw.split(",", 1)
            except ValueError:
                return False
        else:
            encoded = raw
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            return False
        # PNG signature.  Sakura's tested artifact path is image/png; reject
        # anything else so the model never receives a broken/HTML payload.
        if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) > 6 * 1024 * 1024:
            return False

        now = time.monotonic()
        with self._lock:
            self._position = {
                "pos": _as_float(payload.get("pos")),
                "duration": _as_float(payload.get("dur")),
                "title": str(payload.get("title") or "")[:300],
                "url": str(payload.get("url") or "")[:1000],
                "paused": bool(payload.get("paused")),
                "ended": bool(payload.get("ended")),
                "at": now,
            }
            self._frame = Frame(
                data=data,
                pos=_as_float(payload.get("pos")),
                title=str(payload.get("title") or "")[:300],
                url=str(payload.get("url") or "")[:1000],
                caption=str(payload.get("caption") or "")[:500],
                captured_at=now,
            )
            self._frames.append(self._frame)
            # Keep enough history for a long segment without growing forever.
            cutoff = now - 12 * 60
            self._frames = [item for item in self._frames if item.captured_at >= cutoff][-120:]
        return True

    def snapshot(self) -> Optional[Snapshot]:
        now = time.monotonic()
        with self._lock:
            if self._position is None:
                return None
            position = dict(self._position)
            frame = self._frame
            if frame is not None and now - frame.captured_at > 8.0:
                # Keep an old frame for status, but do not let the model comment
                # on a scene the user has already left.
                pass
            return Snapshot(
                pos=float(position["pos"]),
                duration=float(position["duration"]),
                title=str(position["title"]),
                url=str(position["url"]),
                paused=bool(position["paused"]),
                ended=bool(position["ended"]),
                at=float(position["at"]),
                frame=frame,
            )

    def set_status_extra(self, callback: Any) -> None:
        """Attach a callback that adds session/settings fields to /api/state."""
        self._status_extra = callback

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            position = dict(self._position) if self._position else None
            frame = self._frame
            payload = {
                "ok": True,
                "extension_seen_ago": round(now - self._hello_at, 1) if self._hello_at else None,
                "extension_version": self._hello_version,
                "position": position,
                "frames_cached": len(self._frames),
                "storyboard_request": dict(self._storyboard_request)
                if self._storyboard_request
                else None,
                "frame": {
                    "pos": frame.pos,
                    "title": frame.title,
                    "caption": frame.caption,
                    "age": round(now - frame.captured_at, 1),
                    "bytes": len(frame.data),
                }
                if frame
                else None,
            }
        callback = self._status_extra
        if callback is not None:
            try:
                extra = callback()
                if isinstance(extra, dict):
                    payload.update(extra)
            except Exception:
                pass
        return payload

    def request_storyboard(
        self, from_pos: float, to_pos: float, limit: int, timeout: float = 2.5
    ) -> Optional[Tuple[bytes, Dict[str, Any]]]:
        """Ask the extension for a contact sheet covering one segment.

        The extension keeps the last few minutes of frames in its own memory;
        only when a segment is due does it draw those frames into one PNG.  If
        the page is gone or too slow, the caller falls back to the latest frame.
        """
        token = f"{time.time_ns()}"
        request = {
            "token": token,
            "from": round(float(from_pos), 2),
            "to": round(float(to_pos), 2),
            "limit": max(3, min(6, int(limit))),
        }
        with self._condition:
            self._storyboard_request = request
            self._storyboard_result = None
            self._condition.notify_all()
            deadline = time.monotonic() + max(0.5, float(timeout))
            data: Optional[bytes] = None
            meta: Dict[str, Any] = {}
            while time.monotonic() < deadline:
                if self._storyboard_result and self._storyboard_result[0] == token:
                    data = self._storyboard_result[1]
                    meta = self._storyboard_result[2]
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.25, remaining))
            self._storyboard_request = None
            self._storyboard_result = None
            self._condition.notify_all()
        if data is None:
            return None
        return data, meta

    def update_storyboard(self, payload: dict) -> bool:
        token = str(payload.get("token") or "")
        raw = payload.get("image")
        if not token or not isinstance(raw, str):
            return False
        if raw.startswith("data:"):
            try:
                _head, encoded = raw.split(",", 1)
            except ValueError:
                return False
        else:
            encoded = raw
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            return False
        if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) > 6 * 1024 * 1024:
            return False

        captions: List[Dict[str, Any]] = []
        raw_captions = payload.get("captions")
        if isinstance(raw_captions, list):
            for item in raw_captions[-80:]:
                if not isinstance(item, dict):
                    continue
                try:
                    pos = float(item.get("pos"))
                except (TypeError, ValueError):
                    continue
                text = str(item.get("text") or "").strip()
                if not text or pos < 0:
                    continue
                captions.append({"pos": round(pos, 2), "text": text[:240]})

        frames: List[Dict[str, Any]] = []
        raw_frames = payload.get("frames")
        if isinstance(raw_frames, list):
            for item in raw_frames[-12:]:
                if not isinstance(item, dict):
                    continue
                try:
                    pos = float(item.get("pos"))
                except (TypeError, ValueError):
                    continue
                caption = str(item.get("caption") or "").strip()
                if pos < 0:
                    continue
                frames.append({"pos": round(pos, 2), "caption": caption[:240]})

        with self._condition:
            if not self._storyboard_request or self._storyboard_request.get("token") != token:
                return False
            self._storyboard_result = (
                token,
                data,
                {"captions": captions, "frames": frames},
            )
            self._condition.notify_all()
        return True



class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "SakuraWatch/0.3"

    @property
    def bridge(self) -> BrowserBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, self.bridge.status())
        elif path == "/status":
            html = getattr(self.server, "status_html", "")  # type: ignore[attr-defined]
            if html:
                self._html(html)
            else:
                self._html("<html><body>status page unavailable</body></html>")
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "body too large"})
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as error:
            self._json(400, {"ok": False, "error": f"bad json: {error}"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "object expected"})
            return

        if path == "/api/hello":
            self.bridge.hello(str(payload.get("version") or ""))
            self._json(200, {"ok": True, "service": "sakura.watch_together"})
            return
        if path == "/api/position":
            self.bridge.update_position(payload)
            self._json(200, {"ok": True})
            return
        if path == "/api/frame":
            if not self.bridge.update_frame(payload):
                self._json(400, {"ok": False, "error": "bad image"})
                return
            self._json(200, {"ok": True})
            return
        if path == "/api/storyboard":
            if not self.bridge.update_storyboard(payload):
                self._json(400, {"ok": False, "error": "bad storyboard"})
                return
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, bridge: BrowserBridge, status_html: str = "") -> None:
        super().__init__(address, handler)
        self.bridge = bridge
        self.status_html = status_html


@dataclass
class WatchSession:
    url: str
    title: str
    start_pos: float
    next_at: float
    segment_start_pos: float
    active: bool = True
    segment_index: int = 0
    immediate: bool = True
    created_at: float = 0.0
    last_pos: float = 0.0
    last_delivery_at: float = 0.0
    character_id: str = ""
    paused: bool = False
    last_snapshot_at: float = 0.0
    pending_hint: str = ""
    switched_from: str = ""
    switch_pending: bool = False


class WatchTogetherPlugin:
    def setup(self, context):
        self.context = context
        self.log = context.get("sakura.host.logging")
        self.chat = context.get("sakura.host.chat")
        self.conversation = context.get("sakura.host.conversation")
        self.artifacts = context.get("sakura.host.artifacts")
        self._context_service = context.get("sakura.host.context")

        self._data_dir = Path(__file__).resolve().parent / "data" / "frames"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._settings_path = Path(context.data_path("settings.json"))
        except Exception:
            self._settings_path = Path(__file__).resolve().parent / "data" / "settings.json"
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = self._load_settings_file()
        self._subtitle_track = SubtitleTrack(
            self._settings_path.parent / "subs", self.log
        )
        self._asr_track = AsrTrack(
            self._settings_path.parent / "asr",
            self.log,
            language=str(self._settings["asr_language"]),
        )
        self._bridge = BrowserBridge(self.log)
        self._server: Optional[_BridgeServer] = None
        self._server_error = ""
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._session: Optional[WatchSession] = None
        self._delivery_busy = False
        self._context_unregister = None
        self._status_html = self._load_status_html()
        self._bridge.set_status_extra(self._window_status)

        self._start_server()
        self._register_context_provider()
        self._register_settings()
        self._manager = threading.Thread(
            target=self._watch_loop, name="WatchTogetherManager", daemon=True
        )
        self._manager.start()

        composer = context.get("sakura.host.ui.composer-tools-v0")
        self._register_tool(composer, "watch_together.start", "开始陪看", "从当前播放位置开始一起看", "sparkles", 520, self._tool_start)
        self._register_tool(composer, "watch_together.stop", "停止陪看", "结束当前的陪看会话", "sparkles", 521, self._tool_stop)
        self._register_tool(composer, "watch_together.status", "陪看状态", "查看浏览器连接与当前片段", "sparkles", 522, self._tool_status)

        context.effect(self._shutdown)
        self.log.info(
            "陪看插件已启动",
            fields={
                "port": BRIDGE_PORT,
                "segment_seconds": self._settings["interval_seconds"],
                "storyboard_frames": self._settings["storyboard_frames"],
                "asr": self._asr_track.available(),
            },
        )

    def _start_server(self) -> None:
        try:
            self._server = _BridgeServer(
                (BRIDGE_HOST, BRIDGE_PORT), _BridgeHandler, self._bridge, self._status_html
            )
        except OSError as error:
            self._server_error = str(error)
            self.log.error("陪看端口启动失败", fields={"port": BRIDGE_PORT, "error": str(error)})
            return
        thread = threading.Thread(
            target=self._server.serve_forever,
            name="WatchTogetherBridge",
            daemon=True,
        )
        thread.start()

    def _register_context_provider(self) -> None:
        """Expose the active watch session as durable turn context.

        This is the difference between "a screenshot with a note" and the
        character actually knowing that the two of you are watching something
        together.  The fragment is collected on every turn, not only when a
        frame happens to be delivered.
        """
        service = self._context_service
        if service is None:
            self.log.warning("陪看上下文服务不可用")
            return
        try:
            cleanup = service.register(
                {
                    "providerId": CONTEXT_PROVIDER_ID,
                    "description": "当前是否正在和用户一起看视频，以及播放进度与暂停状态。",
                    "order": 88,
                    "scope": "turn",
                    "failurePolicy": "skip",
                },
                self._context_fragment,
            )
        except Exception as error:
            self.log.warning("陪看上下文注册失败", fields={"error": str(error)})
            return
        self._context_unregister = cleanup
        self.context.effect(cleanup)

    def _context_fragment(self, request: Any = None) -> List[Dict[str, Any]]:
        with self._lock:
            session = self._session
        if session is None:
            return []
        if isinstance(request, dict):
            requested_character = str(request.get("character_id") or "")
            if session.character_id and requested_character and requested_character != session.character_id:
                return []
        snapshot = self._bridge.snapshot()
        title = session.title or (snapshot.title if snapshot else "") or "当前视频"
        if snapshot is not None:
            pos = snapshot.pos
            duration = snapshot.duration
            paused = snapshot.paused
            stale = time.monotonic() - snapshot.at > POSITION_FRESH_SECONDS
        else:
            pos = session.last_pos
            duration = 0.0
            paused = session.paused
            stale = True
        if paused:
            state = "已暂停"
        elif stale:
            state = "浏览器暂时没有更新位置"
        else:
            state = "播放中"
        lines = [
            "【陪看·当前共同活动】",
            f"你和用户正在一起看《{title}》。",
            f"当前进度：{_clock_text(pos)}" + (f" / {_clock_text(duration)}" if duration > 0 else "") + f"（{state}）。",
        ]
        if session.switched_from:
            if session.switch_pending:
                lines.append(
                    f"刚刚换了视频：从《{session.switched_from}》切换到了《{title}》。"
                    "这是一个新视频，不是同一段内容；不要接着上一个视频的剧情说。"
                )
            else:
                lines.append(
                    f"这次陪看中途换过视频：之前是《{session.switched_from}》，现在是《{title}》。"
                )
        lines.extend([
            "这是你们正在共同进行的活动，不是用户单方面在看；用户问“我们在看什么”时可以自然回答。",
            "不要主动编造后续剧情，不要提截图、插件、工具或系统。",
        ])
        pending_hint = session.pending_hint.strip()
        if pending_hint:
            lines.extend(["", pending_hint])
        return [{
            "id": CONTEXT_PROVIDER_ID,
            "content": "\n".join(lines),
            "priority": 88,
            "budgetHint": 700,
            "sensitivity": "private",
        }]

    def _load_settings_file(self) -> Dict[str, Any]:
        defaults = {
            "interval_seconds": int(DEFAULT_SEGMENT_SECONDS),
            "storyboard_frames": int(DEFAULT_STORYBOARD_FRAMES),
            "asr_language": ASR_LANGUAGE,
        }
        try:
            raw = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        if not isinstance(raw, dict):
            return defaults
        interval = raw.get("interval_seconds", defaults["interval_seconds"])
        frames = raw.get("storyboard_frames", defaults["storyboard_frames"])
        language = str(raw.get("asr_language", defaults["asr_language"]) or "zh").lower()
        if language not in ("zh", "ja"):
            language = defaults["asr_language"]
        if isinstance(interval, bool) or not isinstance(interval, int):
            interval = defaults["interval_seconds"]
        if isinstance(frames, bool) or not isinstance(frames, int):
            frames = defaults["storyboard_frames"]
        return {
            "interval_seconds": max(
                int(MIN_SEGMENT_SECONDS), min(int(MAX_SEGMENT_SECONDS), interval)
            ),
            "storyboard_frames": max(
                int(MIN_STORYBOARD_FRAMES), min(int(MAX_STORYBOARD_FRAMES), frames)
            ),
            "asr_language": language,
        }

    def _save_settings_file(self) -> None:
        target = self._settings_path
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(target)

    def _register_settings(self) -> None:
        settings = self.context.get("sakura.host.settings")
        if settings is None:
            self.log.warning("陪看设置服务不可用")
            return
        descriptor = {
            "sectionId": SETTINGS_SECTION_ID,
            "title": "陪看",
            "order": 93,
            "fields": [
                {
                    "key": "status",
                    "label": "运行状态",
                    "type": "string",
                    "readonly": True,
                },
                {
                    "key": "interval_seconds",
                    "label": "首段间隔（秒）",
                    "type": "integer",
                    "default": int(DEFAULT_SEGMENT_SECONDS),
                    "minimum": int(MIN_SEGMENT_SECONDS),
                    "maximum": int(MAX_SEGMENT_SECONDS),
                    "step": 10,
                    "description": "开头第一段多久说一句；之后按 1×、2×、3×、4×、5× 递增，最长 300 秒。",
                },
                {
                    "key": "storyboard_frames",
                    "label": "每段分镜帧数",
                    "type": "integer",
                    "default": int(DEFAULT_STORYBOARD_FRAMES),
                    "minimum": int(MIN_STORYBOARD_FRAMES),
                    "maximum": int(MAX_STORYBOARD_FRAMES),
                    "step": 1,
                    "description": "每段从最近画面里选几帧拼成一张宫格，越多越完整，也越耗 token。",
                },
                {
                    "key": "asr_language",
                    "label": "无 CC 字幕语言",
                    "type": "select",
                    "default": "zh",
                    "options": [
                        {"value": "zh", "label": "中文"},
                        {"value": "ja", "label": "日语"},
                    ],
                    "description": "只在没有站内 CC 时生效；看中文视频选中文，看日语动画选日语。",
                },
            ],
        }
        try:
            settings.register(
                descriptor,
                load=self._load_settings,
                save=self._save_settings,
            )
        except Exception as error:
            self.log.warning("陪看设置注册失败", fields={"error": str(error)})

    def _load_settings(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self._status_text(),
                "interval_seconds": int(self._settings["interval_seconds"]),
                "storyboard_frames": int(self._settings["storyboard_frames"]),
                "asr_language": str(self._settings["asr_language"]),
            }

    def _save_settings(self, values: Any) -> None:
        if not isinstance(values, Mapping):
            return
        interval = values.get("interval_seconds")
        frames = values.get("storyboard_frames")
        language = str(values.get("asr_language") or "").lower()
        with self._lock:
            if isinstance(interval, int) and not isinstance(interval, bool):
                self._settings["interval_seconds"] = max(
                    int(MIN_SEGMENT_SECONDS), min(int(MAX_SEGMENT_SECONDS), interval)
                )
            if isinstance(frames, int) and not isinstance(frames, bool):
                self._settings["storyboard_frames"] = max(
                    int(MIN_STORYBOARD_FRAMES), min(int(MAX_STORYBOARD_FRAMES), frames)
                )
            if language in ("zh", "ja"):
                self._settings["asr_language"] = language
                self._asr_track.language = language
            try:
                self._save_settings_file()
            except Exception as error:
                self.log.warning("陪看设置保存失败", fields={"error": str(error)})
            session = self._session
            if session is not None and not session.immediate:
                snapshot = self._bridge.snapshot()
                current = snapshot.pos if snapshot is not None else session.last_pos
                session.segment_start_pos = current
                session.next_at = current + self._next_interval(session)
        self.log.info(
            "陪看设置已更新",
            fields={
                "interval_seconds": self._settings["interval_seconds"],
                "storyboard_frames": self._settings["storyboard_frames"],
                "asr_language": self._settings["asr_language"],
            },
        )

    def _interval_seconds(self) -> float:
        with self._lock:
            return float(self._settings["interval_seconds"])

    def _next_interval(self, session: Optional[WatchSession] = None) -> float:
        # Match the original watch-together segment ramp: the setting is the
        # first segment, then 2×, 3×, 4×, capped at 5× / 300 seconds.
        # A small jitter avoids a metronomic rhythm.
        factor = min(5, max(1, session.segment_index + 1)) if session else 1
        base = self._interval_seconds() * factor
        jittered = base * random.uniform(0.9, 1.1)
        return float(max(MIN_SEGMENT_SECONDS, min(MAX_SEGMENT_SECONDS, round(jittered))))

    def _load_status_html(self) -> str:
        path = Path(__file__).resolve().with_name("status.html")
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return (
                "<!doctype html><meta charset='utf-8'>"
                "<body style='background:#1c1d22;color:#eee;font:13px sans-serif;padding:16px'>"
                "<h3>陪看状态</h3><pre id='s'>读取中…</pre>"
                "<script>setInterval(async()=>{try{const r=await fetch('/api/state');"
                "document.getElementById('s').textContent=JSON.stringify(await r.json(),null,2)}"
                "catch(e){document.getElementById('s').textContent='桥未连接'}},1000)</script>"
            )

    def _window_status(self) -> Dict[str, Any]:
        now = time.monotonic()
        snapshot = self._bridge.snapshot()
        connected = bool(snapshot is not None and now - snapshot.at <= POSITION_FRESH_SECONDS)
        with self._lock:
            session = self._session
            session_data: Dict[str, Any] = {"active": session is not None}
            if session is not None:
                session_data.update({
                    "title": session.title,
                    "url": session.url,
                    "segment_index": session.segment_index,
                    "segment_start": session.segment_start_pos,
                    "next_in": max(0.0, session.next_at - snapshot.pos) if snapshot else None,
                    "paused": session.paused,
                    "switched_from": session.switched_from,
                    "switch_pending": session.switch_pending,
                    "last_delivery_ago": (
                        round(now - session.last_delivery_at, 1)
                        if session.last_delivery_at
                        else None
                    ),
                })
            return {
                "connected": connected,
                "session": session_data,
                "settings": {
                    "interval_seconds": int(self._settings["interval_seconds"]),
                    "storyboard_frames": int(self._settings["storyboard_frames"]),
                },
                "context": bool(self._context_unregister),
                "asr": self._asr_track.status(),
                "server_error": self._server_error,
            }

    def _status_text(self) -> str:
        with self._lock:
            session = self._session
        snapshot = self._bridge.snapshot()
        if session is None:
            state = "未开始"
        elif snapshot is None:
            state = "等待浏览器"
        elif time.monotonic() - snapshot.at > POSITION_FRESH_SECONDS:
            state = "浏览器无更新"
        else:
            state = f"进行中·{_clock_text(snapshot.pos)}"
        return (
            f"{state} | 首段间隔 {int(self._settings['interval_seconds'])} 秒（之后递增）"
            f" | 每段 {int(self._settings['storyboard_frames'])} 帧"
        )

    def _register_tool(self, composer, tool_id, label, description, icon, order, callback) -> None:
        handle, _ = self.context._register_callback("ui.composer_tool.invoke", callback)
        registered = composer.register(self.context.plugin_id, {
            "toolId": tool_id,
            "label": label,
            "description": description,
            "icon": icon,
            "order": order,
        }, handle)
        self.context.effect(lambda: composer.unregister(registered["registrationId"]))

    # -- composer tools -------------------------------------------------

    @staticmethod
    def _composer_text(text: str, limit: int = 190) -> str:
        value = " ".join(str(text or "").split())
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"

    def _tool_start(self, payload=None):
        facts = self.chat.current() if self.chat is not None else {}
        if not (facts or {}).get("sessionId") or not (facts or {}).get("characterId"):
            return {"status": "completed", "message": "还没开始陪看：当前没有打开的 Sakura 会话。"}
        snapshot = self._bridge.snapshot()
        if snapshot is None:
            return {
                "status": "completed",
                "message": "还没开始陪看：没有收到浏览器上报，请启用扩展并刷新视频页。",
            }
        if not snapshot.url:
            return {"status": "completed", "message": "还没开始陪看：浏览器没有上报视频地址。"}
        if not _is_playable_url(snapshot.url):
            return {
                "status": "completed",
                "message": "先打开具体一集视频再开始陪看；番剧索引页/季度页没有可分析的画面。",
            }

        character_id = str((facts or {}).get("characterId") or "")
        with self._lock:
            self._session = WatchSession(
                url=_normalize_url(snapshot.url),
                title=snapshot.title or "正在看的视频",
                start_pos=snapshot.pos,
                next_at=snapshot.pos + self._next_interval(),
                segment_start_pos=snapshot.pos,
                immediate=True,
                created_at=time.monotonic(),
                last_pos=snapshot.pos,
                character_id=character_id,
                last_snapshot_at=time.monotonic(),
            )
        self._asr_track.prepare(self._session.url)
        return {
            "status": "completed",
            "message": self._composer_text(
                f"开始陪看《{snapshot.title or '当前视频'}》。先看一小段，我会在合适的时候开口。"
            ),
        }

    def _tool_stop(self, payload=None):
        with self._lock:
            had = self._session is not None
            if self._session is not None:
                self._session.active = False
            self._session = None
        return {
            "status": "completed",
            "message": "陪看已停止。" if had else "当前没有进行中的陪看。",
        }

    @staticmethod
    def _find_browser() -> str:
        candidates = [
            shutil.which("msedge"),
            shutil.which("chrome"),
            shutil.which("chromium"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return ""

    def _open_status_window(self) -> bool:
        if self._server is None:
            return False
        url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/status"
        browser = self._find_browser()
        if browser:
            try:
                subprocess.Popen(
                    [browser, f"--app={url}", "--window-size=430,330", "--no-first-run"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                return True
            except Exception as error:
                self.log.warning("状态窗启动失败", fields={"error": str(error)})
        try:
            return bool(webbrowser.open(url))
        except Exception as error:
            self.log.warning("状态页打开失败", fields={"error": str(error)})
            return False

    def _tool_status(self, payload=None):
        if self._open_status_window():
            return {"status": "completed", "message": "陪看状态窗已打开。"}
        return {
            "status": "completed",
            "message": f"状态窗打不开；可手动访问 http://{BRIDGE_HOST}:{BRIDGE_PORT}/status",
        }

    # -- segment loop ---------------------------------------------------

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)
            with self._lock:
                session = self._session
            if session is None:
                continue
            snapshot = self._bridge.snapshot()
            if snapshot is None:
                continue
            session.last_snapshot_at = time.monotonic()
            if snapshot.ended:
                with self._lock:
                    self._session = None
                continue

            # Detect a video/episode change even while the new video is still
            # paused.  Otherwise the first tick after resuming would treat the
            # new position as a seek and lose the opening comment.
            current_url = _normalize_url(snapshot.url)
            if current_url and session.url and current_url != session.url:
                # User changed episode/video: restart the segment clock and
                # remember the old title so the character can acknowledge the
                # switch instead of treating it as the same programme.
                old_title = session.title or "上一个视频"
                session.switched_from = old_title
                session.switch_pending = True
                session.url = current_url
                session.title = snapshot.title or session.title
                session.start_pos = snapshot.pos
                session.segment_start_pos = snapshot.pos
                session.segment_index = 0
                session.next_at = snapshot.pos + self._next_interval(session)
                session.immediate = True
                session.last_pos = snapshot.pos
                session.paused = bool(snapshot.paused)
                self._asr_track.prepare(session.url)
                continue

            if snapshot.paused:
                session.paused = True
                session.last_pos = snapshot.pos
                continue
            session.paused = False

            # A large forward/backward jump means a seek; do not deliver a
            # stale frame from before the jump.
            if abs(snapshot.pos - session.last_pos) > 20.0:
                session.start_pos = snapshot.pos
                session.segment_start_pos = snapshot.pos
                session.segment_index = 0
                session.next_at = snapshot.pos + self._next_interval(session)
                session.immediate = True
                session.last_pos = snapshot.pos
                continue
            session.last_pos = snapshot.pos

            if session.immediate:
                if self._can_use_frame(snapshot, min_pos=session.start_pos - 3.0):
                    opening_reason = "换视频" if session.switch_pending else "开场"
                    if self._deliver(session, snapshot, opening_reason):
                        session.immediate = False
                        session.segment_index += 1
                        session.segment_start_pos = snapshot.pos
                        session.next_at = snapshot.pos + self._next_interval(session)
                        session.last_delivery_at = time.monotonic()
                        self._prefetch_next_segment(session)
                elif snapshot.frame is not None:
                    # The user started while the page was paused or the frame
                    # was stale.  Don't hold the opening forever.
                    session.immediate = False
                    session.segment_start_pos = snapshot.pos
                    session.next_at = snapshot.pos + self._next_interval(session)
                    self._prefetch_next_segment(session)
                continue

            if snapshot.pos < session.next_at:
                continue
            if not self._can_use_frame(snapshot, min_pos=session.next_at - 20.0):
                # No fresh frame at the boundary.  Move the boundary forward
                # rather than spamming the same old screenshot.  The segment
                # still advances so the ramp does not get stuck at 60s.
                session.segment_index += 1
                session.segment_start_pos = snapshot.pos
                session.next_at = snapshot.pos + self._next_interval(session)
                self._prefetch_next_segment(session)
                continue
            delivered = self._deliver(session, snapshot, "片段")
            session.segment_index += 1
            session.segment_start_pos = snapshot.pos
            session.next_at = snapshot.pos + self._next_interval(session)
            self._prefetch_next_segment(session)
            if delivered:
                session.last_delivery_at = time.monotonic()

    @staticmethod
    def _can_use_frame(snapshot: Snapshot, min_pos: float) -> bool:
        frame = snapshot.frame
        if frame is None:
            return False
        if time.monotonic() - frame.captured_at > FRAME_FRESH_SECONDS:
            return False
        if (
            frame.url
            and snapshot.url
            and _normalize_url(frame.url) != _normalize_url(snapshot.url)
        ):
            # Never use a frame from the previous video after a switch.
            return False
        return frame.pos >= min_pos

    def _prefetch_next_segment(self, session: WatchSession) -> None:
        """Start ASR at the *beginning* of a segment, not when it is delivered.

        Whisper needs a few seconds to download/transcribe.  Starting it after
        the previous comment gives the next segment its full interval (60s and
        increasing) to finish, so the prompt can read a completed transcript.
        """
        if self._session is not session or not session.active:
            return
        start = float(session.segment_start_pos)
        end = float(session.next_at)
        if end <= start + 1.0:
            return
        try:
            self._asr_track.prefetch_cues(session.url, start, end)
        except Exception as error:
            self.log.warning(
                "ASR 预取安排失败", fields={"error": str(error)[:200]}
            )

    def _deliver(self, session: WatchSession, snapshot: Snapshot, reason: str) -> bool:
        with self._lock:
            if self._delivery_busy:
                return False
            self._delivery_busy = True
        delivered = False
        try:
            frame = snapshot.frame
            if frame is None:
                return False
            facts = self.chat.current() if self.chat is not None else {}
            session_id = str((facts or {}).get("sessionId") or "")
            character_id = str((facts or {}).get("characterId") or "")
            if not session_id or not character_id:
                with self._lock:
                    self._session = None
                return False
            if session.character_id and character_id != session.character_id:
                with self._lock:
                    self._session = None
                return False
            if facts.get("idle") is False:
                # The user or another turn is currently using this chat.
                # Leave the segment pending; it will be retried while the
                # frame is still fresh, otherwise the loop moves on.
                return False

            target = self._data_dir / f"watch_{int(time.time() * 1000)}.png"
            segment_start = float(session.segment_start_pos)
            storyboard_meta: Dict[str, Any] = {}
            try:
                storyboard_result = self._bridge.request_storyboard(
                    segment_start,
                    snapshot.pos,
                    int(self._settings["storyboard_frames"]),
                    timeout=3.0,
                )
            except Exception as error:
                self.log.warning("陪看分镜请求失败", fields={"error": str(error)})
                storyboard_result = None
            use_storyboard = bool(storyboard_result)
            if storyboard_result:
                image_data, storyboard_meta = storyboard_result
            else:
                image_data = frame.data
            target.write_bytes(image_data)
            allocation = self.artifacts.allocate({"mediaType": "image/png", "suffix": ".png"})
            shutil.copyfile(target, allocation["path"])
            artifact = self.artifacts.commit(allocation["artifactId"])
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass

            caption = (frame.caption or "").strip()
            caption_lines: List[str] = []
            caption_source = "网页可见字幕"
            raw_timeline = storyboard_meta.get("captions")
            if isinstance(raw_timeline, list):
                for item in raw_timeline:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        at = float(item.get("pos"))
                    except (TypeError, ValueError):
                        continue
                    caption_lines.append(f"[{_clock_text(at)}] {text}")
                    if len(caption_lines) >= 14:
                        break
            # Normal segments have a planned end.  Use that exact boundary for
            # cc/ASR lookup so the track prefetched at the start of the segment
            # matches the turn that is delivered at the end of it.
            planned_end = (
                float(session.next_at)
                if reason == "片段" and session.next_at > segment_start + 1.0
                else float(snapshot.pos)
            )
            # Prefer the site's own CC track, like the original watch-together.
            # The DOM timeline remains a fallback for pages without a track.
            try:
                site_cues = self._subtitle_track.cues(
                    session.url, segment_start, planned_end
                )
            except Exception as error:
                site_cues = []
                self.log.warning(
                    "字幕轨读取异常", fields={"error": str(error)[:200]}
                )
            if site_cues:
                caption_source = "站点字幕轨"
                caption_lines = []
                for cue in site_cues:
                    text = str(cue.get("text") or "").strip()
                    if not text:
                        continue
                    caption_lines.append(
                        f"[{_clock_text(_as_float(cue.get('from')))}] {text}"
                    )
                    if len(caption_lines) >= 14:
                        break
            if not site_cues and not caption_lines:
                # The ASR for this segment was requested when the segment began
                # (see _prefetch_next_segment).  Here we only read the finished
                # transcript; never block this turn on download/Whisper.
                try:
                    asr_cues = self._asr_track.cached_cues(
                        session.url, segment_start, planned_end
                    )
                except Exception as error:
                    asr_cues = []
                    self.log.warning(
                        "ASR 字幕读取异常", fields={"error": str(error)[:200]}
                    )
                if asr_cues:
                    self.log.info(
                        "ASR 字幕已就绪",
                        fields={
                            "cues": len(asr_cues),
                            "from": round(segment_start, 1),
                            "to": round(planned_end, 1),
                        },
                    )
                    caption_source = "语音转写"
                    caption_lines = []
                    for cue in asr_cues:
                        text = str(cue.get("text") or "").strip()
                        if not text:
                            continue
                        caption_lines.append(
                            f"[{_clock_text(_as_float(cue.get('from')))}] {text}"
                        )
                        if len(caption_lines) >= 14:
                            break
            if caption_lines:
                caption_line = (
                    f"这一段{caption_source}时间轴（可能不完整，按时间顺序）：\n"
                    + "\n".join(caption_lines)
                    + "\n"
                )
            elif caption:
                caption_line = f"画面中可见的字幕（可能不完整）：{caption}\n"
            else:
                caption_line = ""
            if use_storyboard:
                visual_line = (
                    f"这是一段约 {max(1, int(snapshot.pos - segment_start))} 秒的多帧分镜，"
                    f"从 {_clock_text(segment_start)} 到 {_clock_text(snapshot.pos)}；"
                    "每格标了时间，按从左到右、从上到下的顺序看。\n"
                )
            else:
                visual_line = (
                    f"这是播放到 {_clock_text(snapshot.pos)} 时的一帧画面，不是预告；"
                    "你不知道后续剧情，不要猜测。\n"
                )
            switch_line = ""
            if session.switch_pending and session.switched_from:
                switch_line = (
                    f"注意：视频刚刚从《{session.switched_from}》切换到了"
                    f"《{session.title or '当前视频'}》。这是一个新视频，"
                    "不要接着上一个视频的剧情说，开场时自然提一句换了视频即可。\n"
                )
            hidden_hint = (
                f"你正在和用户一起看视频《{session.title or '当前视频'}》。"
                f"现在播放到大约 {_clock_text(snapshot.pos)}。\n"
                f"{switch_line}"
                f"{visual_line}"
                f"{caption_line}"
                "请用一到两句话，像坐在旁边一起看一样自然评论这一段。"
                "不要逐句复述字幕，不要复述画面细节，不要提截图、工具、AI 或系统，"
                "不要编造没出现的台词。"
                "无论画面内容如何，最终回复仍必须遵守 Sakura 的输出协议："
                "返回合法 JSON，segments 每项同时包含 ja、zh、tone、control；"
                "不要只输出纯文本或 Markdown 代码块。"
            )
            # conversation.begin reserves a visible human turn.  Keep that turn
            # minimal; the actual direction travels through the hidden context
            # contributor instead of being rendered as a user message.
            session.pending_hint = hidden_hint
            accepted = self.conversation.begin(
                character_id, "（一起看）", artifact, "queued"
            )
            job_id = str((accepted or {}).get("jobId") or "")
            if not job_id:
                session.pending_hint = ""
                self.log.warning("陪看提交未返回 jobId", fields={"reason": reason})
                return
            threading.Thread(
                target=self._poll_job,
                args=(job_id, session),
                name="WatchTogetherPoll",
                daemon=True,
            ).start()
            delivered = True
            self.log.info(
                "陪看片段已提交",
                fields={
                    "reason": reason,
                    "pos": round(snapshot.pos, 1),
                    "title": session.title[:80],
                    "storyboard": use_storyboard,
                    "image_bytes": len(image_data),
                },
            )
        except Exception as error:
            session.pending_hint = ""
            self.log.error("陪看片段提交失败", fields={"error": str(error)})
        finally:
            with self._lock:
                self._delivery_busy = False
        return delivered

    def _poll_job(self, job_id: str, session: WatchSession) -> None:
        deadline = time.monotonic() + 90.0
        try:
            while time.monotonic() < deadline:
                result = self.conversation.poll(job_id)
                status = str((result or {}).get("status") or "")
                if status == "completed":
                    reply = str(((result or {}).get("result") or {}).get("reply") or "")
                    with self._lock:
                        if self._session is session:
                            session.switch_pending = False
                    self.log.info("陪看回复完成", fields={"reply_chars": len(reply)})
                    return
                if status in ("failed", "cancelled"):
                    self.log.warning("陪看回复结束", fields={"status": status})
                    return
                time.sleep(0.25)
            self.log.warning("陪看回复超时", fields={"job_id": job_id})
        except Exception as error:
            self.log.error("陪看轮询失败", fields={"error": str(error)})
        finally:
            with self._lock:
                if self._session is session:
                    session.pending_hint = ""
            try:
                self.conversation.release(job_id)
            except Exception:
                pass

    def _shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self._session = None
        try:
            self._asr_track.close()
        except Exception:
            pass
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
