"""Deterministic context for indexing, separate from verifiable unit text."""


def context_header(filename: str, section_path: str) -> str:
    return f"{filename} > {section_path}" if section_path else filename
