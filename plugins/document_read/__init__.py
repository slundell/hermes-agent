"""document_read plugin — exposes the Tika-backed document reader tool.

Pure addition to the wpu fork; touches zero bundled hermes files. The
extraction logic lives in `plugin.tool`; this module is just the plugin
entry point that hands the tool to the plugin loader via ctx.register_tool.
"""

from __future__ import annotations

from plugins.document_read.tool import (
    DOCUMENT_READ_SCHEMA,
    check_document_read_available,
    handle_document_read,
)


def register(ctx) -> None:
    """Register the document_read tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="document_read",
        toolset="file",
        schema=DOCUMENT_READ_SCHEMA,
        handler=handle_document_read,
        check_fn=check_document_read_available,
        emoji="📄",
    )
