"""基准沙箱：只读工具（read_file / list_dir / search_content）。

v15.6 改造：_calls 从全局 int 改 threading.local——
之前 3 worker 并发跑分时一个 case 的 reset_counts() 会把另一个 case 的计数清零，
导致 tool_calls 统计错乱（这是 race condition 的一种表现）。
threading.local 后每个 worker 线程独立计数，互不干扰。
"""
import threading
from pathlib import Path

MAX_FILE_BYTES = 200 * 1024
_tls = threading.local()


def reset_counts():
    _tls.calls = 0


def get_call_count() -> int:
    return getattr(_tls, "calls", 0)


def _bump():
    _tls.calls = getattr(_tls, "calls", 0) + 1

def _safe(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel).resolve()
    if not str(p).startswith(str(root)):
        raise ValueError(f"path outside sandbox: {rel}")
    return p

def read_file(root: Path, rel: str) -> str:
    """读取沙箱内文件（200KB 上限）。"""
    _bump()
    p = _safe(root, rel)
    if not p.is_file():
        raise ValueError(f"not a file: {rel}")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file too large: {rel}")
    return p.read_text(encoding="utf-8", errors="replace")

def list_dir(root: Path, rel: str = "") -> list:
    """列目录（相对路径 + 类型）。"""
    _bump()
    p = _safe(root, rel) if rel else root
    if not p.is_dir():
        raise ValueError(f"not a dir: {rel}")
    out = []
    for child in sorted(p.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        out.append({"name": child.name, "type": kind,
                    "size": child.stat().st_size if child.is_file() else None})
    return out

def search_content(root: Path, pattern: str, rel: str = "") -> list:
    """在沙箱内文件名与内容中搜索（大小写不敏感）。"""
    _bump()
    p = _safe(root, rel) if rel else root
    pat = pattern.lower()
    hits = []
    for child in sorted(p.rglob("*")):
        if child.is_file() and child.stat().st_size <= MAX_FILE_BYTES:
            if pat in child.name.lower():
                hits.append({"file": str(child.relative_to(root)), "match": "filename"})
                continue
            try:
                text = child.read_text(encoding="utf-8", errors="replace").lower()
            except Exception:
                continue
            idx = text.find(pat)
            if idx >= 0:
                snippet = text[max(0, idx - 30):idx + 60].replace("\n", " ")
                hits.append({"file": str(child.relative_to(root)), "match": "content",
                             "snippet": snippet})
    return hits[:20]
