"""Persistent Whisper worker for the watch-together plugin.

The plugin selects Chinese or Japanese per request (``language`` = ``zh`` or
``ja``); the same multilingual ``small`` model handles both.

The release bundles a portable Python runtime with CPU Torch and Whisper under
``asr_runtime/``.  Keep the model loaded there and exchange one JSON request
per line over stdin/stdout.  All human-readable logs go to stderr so stdout
stays a clean protocol channel.  A system Python remains usable through
SAKURA_WATCH_ASR_PYTHON.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
MODEL_PATH = os.environ.get("SAKURA_WATCH_WHISPER_MODEL", "").strip()
MODEL_ROOT = Path(
    os.environ.get("SAKURA_WATCH_WHISPER_ROOT")
    or (Path(MODEL_PATH).parent if MODEL_PATH else Path(""))
)
MODEL_NAME = os.environ.get("SAKURA_WATCH_WHISPER_MODEL_NAME", "small")
INITIAL_PROMPTS = {
    "ja": "これは日本語のアニメの会話です。句読点を付けて書き起こしてください。",
    "zh": "这是中文视频的对话，请加上标点并逐句转写。",
}


def _emit(payload: dict) -> None:
    sys.__stdout__.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.__stdout__.flush()


def _cues(segments: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    cues: List[Dict[str, Any]] = []
    for item in segments:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(item.get("start") or 0.0))
        end = min(duration, float(item.get("end") or start))
        if end <= start:
            continue
        cues.append(
            {
                "from": round(start, 2),
                "to": round(end, 2),
                "text": text[:240],
            }
        )
    return cues


def main() -> None:
    # Whisper/tqdm writes progress to stdout during import/inference.
    # Reserve the real stdout for JSON lines and divert everything else.
    protocol = sys.__stdout__
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout = sys.stderr
    try:
        import torch
        import whisper

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if MODEL_PATH and Path(MODEL_PATH).is_file():
            model = whisper.load_model(MODEL_PATH, device=device)
        else:
            model = whisper.load_model(
                MODEL_NAME,
                download_root=str(MODEL_ROOT) if str(MODEL_ROOT) else None,
                device=device,
            )
    except Exception as error:
        _emit({"ok": False, "error": f"ASR_MODEL_LOAD_FAILED: {error}"})
        traceback.print_exc(file=sys.stderr)
        return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            path = str(request.get("path") or "")
            if not path or not Path(path).is_file():
                _emit({"ok": False, "error": "ASR_AUDIO_MISSING"})
                continue
            import torch

            requested = str(request.get("language") or "auto").strip().lower()
            language = None if requested in ("", "auto") else requested
            prompt = INITIAL_PROMPTS.get(language or "", "")
            result = model.transcribe(
                path,
                language=language,
                task="transcribe",
                fp16=torch.cuda.is_available(),
                verbose=False,
                beam_size=5,
                temperature=0.0,
                condition_on_previous_text=False,
                initial_prompt=prompt or None,
            )
            segments = result.get("segments") or []
            duration = max(
                [float(item.get("end") or 0.0) for item in segments] or [0.0]
            )
            cues = _cues(segments, duration)
            _emit(
                {
                    "ok": True,
                    "text": str(result.get("text") or "").strip(),
                    "cues": cues,
                    "language": str(result.get("language") or language or "auto"),
                }
            )
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            _emit({"ok": False, "error": str(error)[:500]})


if __name__ == "__main__":
    main()
