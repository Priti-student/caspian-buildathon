"""Small, channel-independent input normalization helpers."""

import re


def normalize_message(text: str) -> str:
    """Make repeated punctuation and whitespace semantically neutral."""
    text = re.sub(r"[.]{2,}", ".", text)
    text = re.sub(r"[?]{2,}", "?", text)
    text = re.sub(r"[!]{2,}", "!", text)
    return re.sub(r"\s+", " ", text).strip()
