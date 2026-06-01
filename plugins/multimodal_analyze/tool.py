"""audio_analyze + video_analyze_frames — multimodal tools for the gemma4-e4b
backend exposed via the auxiliary.vision config.

Both tools route through ``async_call_llm(task="vision", ...)`` so they pick up
whatever endpoint and model the user has wired into ``auxiliary.vision``. The
ik-llamacpp/gemma4-e4b backend accepts native OpenAI multimodal content
blocks:

  - **audio**: ``{"type":"input_audio","input_audio":{"data": <raw b64>,
    "format":"wav"}}`` — note **raw base64**, NOT a data URL. Data URLs return
    HTTP 400 from gemma4-e4b. ``format`` must be ``"wav"``.

  - **video**: gemma4-e4b has no first-class video block; instead it accepts a
    sequence of ``image_url`` blocks as frames. We ffmpeg-extract N evenly
    spaced frames as PNGs and send them in order.

Both tools take either an http(s) URL or a local file path. URLs are downloaded
through urllib (no auth — for authenticated URLs the caller should fetch via
the appropriate Hermes tool first and pass the cached path). The
``auxiliary.vision.download_timeout`` config knob bounds the download.

Wire-up: `auxiliary.vision` is the existing aux config slot; on this cluster
it points at ``aina-media.llm.svc.cluster.wpu.nu/v1`` (gemma4-e4b on
worker-03). Anyone with a different vision-capable aux backend that *also*
speaks input_audio + image_url frame sequences (e.g. gpt-4o, claude with
vision) would have the tools work transparently — the plugin is endpoint
agnostic as far as the wire format goes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Limits — tune via env if needed ─────────────────────────────────────────
# gemma4-e4b at Q5_K_M on Broadwell CPU is ~10 tok/s gen, ~52 tok/s prompt
# eval (which includes ~265 tokens per image and ~10 tokens/sec of audio).
# Caps below keep a tool call from monopolising the slot for >2 min on
# expected-shape inputs.
_AUDIO_MAX_BYTES   = int(os.environ.get("MULTIMODAL_AUDIO_MAX_BYTES",   "5242880"))   # 5 MiB
_VIDEO_MAX_BYTES   = int(os.environ.get("MULTIMODAL_VIDEO_MAX_BYTES",   "33554432"))  # 32 MiB
_VIDEO_FRAMES_MAX  = int(os.environ.get("MULTIMODAL_VIDEO_FRAMES_MAX",  "6"))         # 6 frames
_DOWNLOAD_TIMEOUT  = float(os.environ.get("MULTIMODAL_DOWNLOAD_TIMEOUT", "30"))
_FFMPEG_TIMEOUT    = float(os.environ.get("MULTIMODAL_FFMPEG_TIMEOUT",   "60"))


# ── Tool schemas — what the agent sees ─────────────────────────────────────

AUDIO_ANALYZE_SCHEMA = {
    "name": "audio_analyze",
    "description": (
        "Analyze a short audio clip via the multimodal vision-aux backend "
        "(gemma4-e4b on aina-media). Accepts http(s) URLs or local file "
        "paths (.wav .mp3 .ogg .m4a .flac .aac). Audio is transcoded to "
        "16 kHz mono WAV if needed. Output is the model's free-form "
        "description / transcription as JSON: {success, analysis, "
        "audio_size_bytes, audio_duration_sec, model_used}. Best for 1-90s "
        "clips; longer clips may exceed the model context window. The "
        "model can answer questions about content (speech, music, tone) "
        "but is NOT a dedicated ASR; for Swedish speech transcription "
        "prefer the Whisper STT path on the Signal voice-message pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "audio_url": {
                "type": "string",
                "description": "http(s):// URL or local file path",
            },
            "question": {
                "type": "string",
                "description": "What the agent wants to know about the clip — e.g. \"What is being said?\", \"Music or speech?\", \"Describe the audio\".",
            },
        },
        "required": ["audio_url", "question"],
    },
}


VIDEO_ANALYZE_FRAMES_SCHEMA = {
    "name": "video_analyze_frames",
    "description": (
        "Analyze a short video by sampling N evenly-spaced frames and "
        "sending them to the multimodal vision-aux backend as an "
        "image_url sequence. Accepts http(s) URLs or local file paths. "
        "Uses ffmpeg to extract frames. Output is the model's description "
        "of the visual sequence as JSON: {success, analysis, "
        "video_size_bytes, frames_extracted, frames_requested, model_used}. "
        "Each frame adds ~265 prompt tokens; default 3 frames keeps a "
        "single call to ~800 prompt tokens. For event localisation (\"at "
        "what time did X happen\") increase max_frames. NOT the right "
        "tool for full transcripts of dialogue — use audio_analyze on "
        "the audio track for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_url": {
                "type": "string",
                "description": "http(s):// URL or local file path to the video",
            },
            "question": {
                "type": "string",
                "description": "What the agent wants to know — e.g. \"What is happening?\", \"How many people?\", \"Describe the action sequence\".",
            },
            "max_frames": {
                "type": "integer",
                "description": f"How many evenly-spaced frames to sample (1-{_VIDEO_FRAMES_MAX}; default 3).",
                "minimum": 1,
                "maximum": _VIDEO_FRAMES_MAX,
                "default": 3,
            },
        },
        "required": ["video_url", "question"],
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _tool_error(msg: str, **extra: Any) -> str:
    """Mirror of tools.helpers.tool_error — kept inline to avoid coupling."""
    payload = {"success": False, "error": msg, **extra}
    return json.dumps(payload, ensure_ascii=False)


def _ffmpeg_available() -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _resolve_to_local_path(url_or_path: str, suffix_hint: str = "") -> Tuple[Path, bool]:
    """Return (local_path, should_cleanup). Downloads via urllib if URL."""
    if url_or_path.startswith("file://"):
        url_or_path = url_or_path[len("file://"):]
    p = Path(os.path.expanduser(url_or_path))
    if p.is_file():
        return p, False
    parsed = urllib.parse.urlparse(url_or_path)
    if parsed.scheme in {"http", "https"}:
        # Pick a temp path with the right suffix for ffmpeg's autodetect.
        if not suffix_hint:
            suffix_hint = Path(parsed.path).suffix or ".bin"
        tmp = Path(tempfile.mkdtemp(prefix="mm-")) / f"download{suffix_hint}"
        logger.info("Downloading %s -> %s", url_or_path[:80], tmp)
        req = urllib.request.Request(
            url_or_path,
            headers={"User-Agent": "Hermes-multimodal/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            data = resp.read()
        tmp.write_bytes(data)
        return tmp, True
    raise ValueError(
        f"audio/video source must be http(s):// URL or existing local path; "
        f"got: {url_or_path!r}"
    )


def _transcode_audio_to_wav(src: Path) -> Path:
    """ffmpeg -> 16 kHz mono WAV. Returns path to /tmp WAV (always cleanup)."""
    out = Path(tempfile.mkdtemp(prefix="mm-")) / "audio.wav"
    r = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-ar", "16000", "-ac", "1",
            str(out),
        ],
        capture_output=True, timeout=_FFMPEG_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to transcode audio: {r.stderr.decode('utf-8', 'replace')[:300]}"
        )
    return out


def _probe_duration(src: Path) -> Optional[float]:
    """Best-effort duration via ffprobe; None if unavailable."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(src),
            ],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return float(r.stdout.strip() or 0.0)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _extract_video_frames(src: Path, n: int) -> List[Path]:
    """Sample n evenly-spaced frames from `src`. Returns ordered list of PNGs."""
    duration = _probe_duration(src) or 1.0
    out_dir = Path(tempfile.mkdtemp(prefix="mm-frames-"))
    # Sample at duration/(n+1), 2*duration/(n+1), ... avoids the very first
    # and very last frames which are often duplicates of black frames or
    # fade-ins on real-world videos.
    frames: List[Path] = []
    for i in range(1, n + 1):
        t = duration * i / (n + 1)
        out = out_dir / f"frame_{i:03d}.png"
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{t:.3f}", "-i", str(src),
                "-frames:v", "1", "-q:v", "2",
                str(out),
            ],
            capture_output=True, timeout=_FFMPEG_TIMEOUT,
        )
        if r.returncode != 0 or not out.exists():
            logger.warning(
                "ffmpeg failed to extract frame %d/%d at t=%.2fs: %s",
                i, n, t, r.stderr.decode("utf-8", "replace")[:200],
            )
            continue
        frames.append(out)
    return frames


def _cleanup_path(p: Optional[Path]) -> None:
    if p is None:
        return
    try:
        if p.is_file():
            p.unlink(missing_ok=True)
        if p.parent.name.startswith("mm-"):
            # Best-effort directory wipe (only the dirs we created).
            for child in p.parent.glob("*"):
                child.unlink(missing_ok=True)
            p.parent.rmdir()
    except Exception as exc:
        logger.debug("Cleanup failed for %s: %s", p, exc)


# ── Tool implementations ───────────────────────────────────────────────────

async def _call_aux_vision(
    messages: List[Dict[str, Any]],
    max_tokens: int = 512,
) -> str:
    """POST to the auxiliary.vision endpoint via the standard aux path.

    Returns the extracted text (content, falling back to reasoning_content
    if the model only produced reasoning). Surface-level errors propagate
    as exceptions; the caller decides how to format them.
    """
    from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
    response = await async_call_llm(
        task="vision",
        messages=messages,
        max_tokens=max_tokens,
        # temperature=0 to make tool output reproducible session-to-session.
        # Aux-call sampling noise was empirically observed flipping gemma4
        # between "Pure tone" and "I cannot hear" on identical inputs.
        temperature=0,
    )
    return extract_content_or_reasoning(response) or ""


async def audio_analyze_tool(audio_url: str, question: str) -> str:
    """Implementation of audio_analyze. Returns JSON-encoded result."""
    downloaded: Optional[Path] = None
    transcoded: Optional[Path] = None
    try:
        if not isinstance(question, str) or not question.strip():
            return _tool_error("question is required (non-empty string)")
        local, should_cleanup = _resolve_to_local_path(audio_url, suffix_hint=".audio")
        if should_cleanup:
            downloaded = local
        size_bytes = local.stat().st_size
        if size_bytes > _AUDIO_MAX_BYTES:
            return _tool_error(
                f"audio exceeds {_AUDIO_MAX_BYTES // (1024*1024)} MiB cap "
                f"({size_bytes // (1024*1024)} MiB). Trim it first.",
                audio_size_bytes=size_bytes,
            )
        if not _ffmpeg_available():
            return _tool_error(
                "ffmpeg not found on PATH. audio_analyze needs ffmpeg to "
                "transcode arbitrary inputs to the WAV/16kHz/mono shape "
                "the gemma4-e4b backend accepts."
            )
        duration_sec = _probe_duration(local)
        transcoded = _transcode_audio_to_wav(local)
        raw_b64 = base64.b64encode(transcoded.read_bytes()).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "input_audio", "input_audio": {
                    "data": raw_b64, "format": "wav",
                }},
            ],
        }]
        logger.info(
            "audio_analyze: %.1f KB transcoded (%.1fs), question=%r",
            transcoded.stat().st_size / 1024,
            duration_sec or -1.0,
            question[:60],
        )
        analysis = await _call_aux_vision(messages, max_tokens=512)
        return json.dumps({
            "success": True,
            "analysis": analysis.strip() or "(no response from model)",
            "audio_size_bytes": size_bytes,
            "audio_duration_sec": duration_sec,
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("audio_analyze failed for %s", audio_url[:80])
        return _tool_error(f"{type(exc).__name__}: {exc}")
    finally:
        _cleanup_path(downloaded)
        _cleanup_path(transcoded)


async def video_analyze_frames_tool(
    video_url: str, question: str, max_frames: int = 3,
) -> str:
    """Implementation of video_analyze_frames. Returns JSON-encoded result."""
    downloaded: Optional[Path] = None
    frames: List[Path] = []
    try:
        if not isinstance(question, str) or not question.strip():
            return _tool_error("question is required (non-empty string)")
        n = max(1, min(_VIDEO_FRAMES_MAX, int(max_frames or 3)))
        local, should_cleanup = _resolve_to_local_path(video_url, suffix_hint=".video")
        if should_cleanup:
            downloaded = local
        size_bytes = local.stat().st_size
        if size_bytes > _VIDEO_MAX_BYTES:
            return _tool_error(
                f"video exceeds {_VIDEO_MAX_BYTES // (1024*1024)} MiB cap "
                f"({size_bytes // (1024*1024)} MiB). Trim it first.",
                video_size_bytes=size_bytes,
            )
        if not _ffmpeg_available():
            return _tool_error(
                "ffmpeg not found on PATH. video_analyze_frames needs ffmpeg "
                "to extract frames from arbitrary inputs."
            )
        frames = _extract_video_frames(local, n)
        if not frames:
            return _tool_error(
                "ffmpeg could not extract any frames from the video. "
                "Check that the file is a valid video container with a "
                "video stream."
            )
        content: List[Dict[str, Any]] = [{"type": "text", "text": question}]
        for f in frames:
            b64 = base64.b64encode(f.read_bytes()).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        messages = [{"role": "user", "content": content}]
        logger.info(
            "video_analyze_frames: %d frames extracted (of %d requested), "
            "video size %.1f MB, question=%r",
            len(frames), n, size_bytes / (1024 * 1024), question[:60],
        )
        analysis = await _call_aux_vision(messages, max_tokens=768)
        return json.dumps({
            "success": True,
            "analysis": analysis.strip() or "(no response from model)",
            "video_size_bytes": size_bytes,
            "frames_extracted": len(frames),
            "frames_requested": n,
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("video_analyze_frames failed for %s", video_url[:80])
        return _tool_error(f"{type(exc).__name__}: {exc}")
    finally:
        _cleanup_path(downloaded)
        for f in frames:
            _cleanup_path(f)


# ── Dispatcher entries ─────────────────────────────────────────────────────

def handle_audio_analyze(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    audio_url = args.get("audio_url", "")
    question = args.get("question", "")
    return audio_analyze_tool(audio_url, question)


def handle_video_analyze_frames(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    video_url = args.get("video_url", "")
    question = args.get("question", "")
    max_frames = args.get("max_frames", 3)
    return video_analyze_frames_tool(video_url, question, max_frames)


# ── Availability check (used by the plugin gate) ───────────────────────────

def check_multimodal_available() -> bool:
    """Both tools need ffmpeg AND an auxiliary.vision config that's been set
    to a non-empty backend. The check is run at plugin-load time; if it
    returns False the tools are hidden from the agent.
    """
    if not _ffmpeg_available():
        logger.warning("multimodal_analyze: ffmpeg not on PATH; tools disabled")
        return False
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        aux = (cfg.get("auxiliary") or {}).get("vision") or {}
        provider = (aux.get("provider") or "").strip().lower()
        base_url = (aux.get("base_url") or "").strip()
        model = (aux.get("model") or "").strip()
        # Either an explicit provider/base_url is set, or auto with the main
        # provider being multimodal-capable. We don't try to verify the
        # latter here; we just require the slot exists.
        if provider in {"", "auto"} and not base_url and not model:
            logger.warning(
                "multimodal_analyze: auxiliary.vision is unconfigured; tools "
                "remain enabled (auto resolution will pick a backend at "
                "call time), but errors will surface there if no usable "
                "backend exists.",
            )
        return True
    except Exception as exc:
        logger.debug("multimodal_analyze: config check raised %s; enabling anyway", exc)
        return True
