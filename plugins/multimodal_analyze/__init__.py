"""multimodal_analyze plugin — exposes audio_analyze and video_analyze_frames.

Pure addition to the wpu fork; touches zero bundled hermes files. The
extraction + LLM-call logic lives in `plugin.tool`; this module is just
the plugin entry point that hands the tools to the loader via
``ctx.register_tool``.
"""

from __future__ import annotations

from plugins.multimodal_analyze.tool import (
    AUDIO_ANALYZE_SCHEMA,
    VIDEO_ANALYZE_FRAMES_SCHEMA,
    check_multimodal_available,
    handle_audio_analyze,
    handle_video_analyze_frames,
)


def register(ctx) -> None:
    """Register both multimodal tools. Called once by the plugin loader."""
    ctx.register_tool(
        name="audio_analyze",
        toolset="vision",
        schema=AUDIO_ANALYZE_SCHEMA,
        handler=handle_audio_analyze,
        check_fn=check_multimodal_available,
        emoji="🔊",
        is_async=True,
    )
    ctx.register_tool(
        name="video_analyze_frames",
        toolset="vision",
        schema=VIDEO_ANALYZE_FRAMES_SCHEMA,
        handler=handle_video_analyze_frames,
        check_fn=check_multimodal_available,
        emoji="🎞️",
        is_async=True,
    )
