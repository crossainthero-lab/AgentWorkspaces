"""Image-to-text fallback for agents whose model cannot consume images.

`describe_screen` sends the workspace screenshot to a vision-capable model behind an OpenAI-compatible
`/chat/completions` endpoint and returns TEXT. It is honest about its limits: when no backend is configured
it says so; it never invents a description.

Configuration (environment of the MCP server process; nothing is stored):
  AW_VISION_URL        base URL, e.g. http://host:8080/v1
  AW_VISION_MODEL      model id (must accept image input)
  AW_VISION_KEY_ENV    NAME of the environment variable holding the API key (optional)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

PROMPT = ("You are the eyes of a computer-use agent. Describe this screenshot of a graphical desktop for a "
          "model that cannot see it: list the visible windows, their titles, all readable text, buttons, "
          "input fields and menus with their approximate pixel coordinates (x,y of the centre; the image "
          "is {w}x{h}), and which element has focus. Be concrete and brief.")


class VisionUnavailable(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("AW_VISION_URL") and os.environ.get("AW_VISION_MODEL"))


def describe(png_b64: str, width: int, height: int, question: Optional[str] = None, timeout: float = 120.0) -> str:
    url, model = os.environ.get("AW_VISION_URL"), os.environ.get("AW_VISION_MODEL")
    if not url or not model:
        raise VisionUnavailable("no vision backend is configured (AW_VISION_URL / AW_VISION_MODEL). This agent's "
                                "own model must read the screenshot image itself.")
    headers = {"Content-Type": "application/json"}
    key_env = os.environ.get("AW_VISION_KEY_ENV")
    if key_env and os.environ.get(key_env):
        headers["Authorization"] = "Bearer " + os.environ[key_env]
    text = (question.strip() + "\n\n" if question else "") + PROMPT.format(w=width, h=height)
    body = {"model": model, "max_tokens": 700, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64}}]}]}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise VisionUnavailable(f"vision backend answered HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise VisionUnavailable(f"vision backend unreachable: {type(e).__name__}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        raise VisionUnavailable("vision backend returned an unexpected response")
