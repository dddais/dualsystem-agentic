"""Helpers for stable, namespace-qualified tool names."""

from __future__ import annotations

CANONICAL_TOOL_SEPARATOR = "___"
LEGACY_TOOL_SEPARATOR = "/"


def make_canonical_tool_name(name: str, namespace: str | None = None) -> str:
    """Return the VLM-facing tool name for one namespace/name pair."""
    clean_name = str(name).strip()
    clean_namespace = str(namespace).strip() if namespace else ""
    if clean_namespace:
        return f"{clean_namespace}{CANONICAL_TOOL_SEPARATOR}{clean_name}"
    return clean_name


def split_qualified_tool_name(name: str) -> tuple[str | None, str]:
    """Split ``namespace___tool`` or legacy ``namespace/tool`` names."""
    text = str(name).strip()
    for separator in (CANONICAL_TOOL_SEPARATOR, LEGACY_TOOL_SEPARATOR):
        if separator not in text:
            continue
        namespace, tool_name = (part.strip() for part in text.split(separator, 1))
        if namespace and tool_name:
            return namespace, tool_name
    return None, text
