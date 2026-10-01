"""把 AI 判讀的提示詞與回答記到 Langfuse，讓人查得到 AI 每次看到什麼、回答什麼。

.env 有 LANGFUSE_PUBLIC_KEY／LANGFUSE_SECRET_KEY（與 LANGFUSE_HOST）才啟用。
沒設定、套件沒裝、Langfuse 服務沒開或任何錯誤，都只是不留紀錄，不影響判讀與寄信。
"""

import os
import re
import sys
from contextlib import nullcontext

from common import ROOT

_client = False      # False＝還沒初始化；None＝不啟用


class _Noop:
    def update(self, **kwargs):
        pass


def _load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(LANGFUSE_[A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if m and m.group(2) and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2).strip('"').strip("'")


def client():
    global _client
    if _client is False:
        _client = None
        try:
            _load_env()
            if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
                from langfuse import get_client
                _client = get_client()
        except Exception as exc:
            print(f"  ！Langfuse 無法啟用，這次不留紀錄：{exc}", file=sys.stderr)
    return _client


def observe(name, as_type="span", **fields):
    """with observe("名稱", as_type="generation", model=..., input=...) as obs: obs.update(output=...)"""
    c = client()
    if c is None:
        return nullcontext(_Noop())
    try:
        return c.start_as_current_observation(as_type=as_type, name=name, **fields)
    except Exception as exc:
        print(f"  ！Langfuse 紀錄失敗：{exc}", file=sys.stderr)
        return nullcontext(_Noop())


def update(obs, **fields):
    try:
        obs.update(**fields)
    except Exception:
        pass


def flush():
    c = client()
    if c is not None:
        try:
            c.flush()
        except Exception as exc:
            print(f"  ！Langfuse 紀錄送出失敗：{exc}", file=sys.stderr)
