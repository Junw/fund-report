from __future__ import annotations

import json
import re
from typing import Any


def loads_json_array(content: str) -> list[Any]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("["):
        match = re.search(r"\[.*\]", text, flags=re.S)
        text = match.group(0) if match else "[]"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []
