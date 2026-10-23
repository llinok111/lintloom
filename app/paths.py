"""路径与参数校验。

这一层是整个应用的信任边界：任何来自 HTTP 的参数在落到 subprocess 之前都必须过这里。

防御的威胁：
- 目录穿越（`../../etc/passwd`、`a/../../../x`）
- 绝对路径 / 盘符路径绕过根目录
- 符号链接逃逸（link 指向根目录之外）
- 控制字符与 NUL 注入（在 Windows 上会被截断成另一条路径）
- 空字节、超长路径
- 方言 / 规则参数被当成命令行选项（例如 `-d "--config=/evil"` 或 `ansi; rm -rf /`）
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from . import config

RULE_CODE_RE = re.compile(r"^[A-Z]{2}[0-9]{2}$")
RULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,4}$")
DIALECT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ROOT_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

MAX_PATH_LEN = 512


class ValidationError(ValueError):
    """参数非法。上层统一转成 HTTP 400。"""


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def clean_relative_path(rel: object, *, allow_root: bool = False) -> str:
    """把用户给的相对路径规范化，并拒绝一切可疑形态。

    allow_root=True 时，「根目录本身」用 ".", "" 或 "/" 表示，返回 "."
    （其余任何输入都不允许退化成根，避免 `a/..` 这类写法被当成根）。
    """
    if not isinstance(rel, str):
        raise ValidationError("路径必须是字符串")
    rel = rel.strip()
    if allow_root and rel in ("", ".", "/"):
        return "."
    if len(rel) > MAX_PATH_LEN:
        raise ValidationError(f"路径过长（>{MAX_PATH_LEN} 字符）")
    if "\x00" in rel:
        raise ValidationError("路径包含 NUL 字节")
    if _has_control_chars(rel):
        raise ValidationError("路径包含控制字符")
    # 绝对路径 / 盘符 / UNC
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
        raise ValidationError("只接受相对于根目录的路径，不接受绝对路径")
    parts: list[str] = []
    for raw in re.split(r"[\\/]+", rel):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise ValidationError("路径不能包含 '..'")
        if raw.endswith(" ") or raw.endswith("."):
            # Windows 会静默去掉尾部的空格/点，导致校验路径 != 实际路径
            raise ValidationError("路径片段不能以空格或点结尾")
        parts.append(raw)
    if not parts:
        raise ValidationError("路径不能为空")
    return "/".join(parts)


def check_root_label(settings: config.Settings, label: object) -> tuple[str, Path]:
    if not isinstance(label, str) or not ROOT_LABEL_RE.match(label):
        raise ValidationError("根目录标识非法")
    roots = settings.normalised_roots()
    if label not in roots:
        raise ValidationError(f"未知根目录: {label}")
    return label, roots[label]


def resolve_under_root(
    settings: config.Settings,
    label: object,
    rel: object,
    *,
    must_exist: bool = True,
    kind: str = "file",
    writable: bool = False,
    allow_root: bool = False,
) -> tuple[str, Path]:
    """解析并校验 (root, rel) -> 绝对路径。任何越界都会抛 ValidationError。"""
    root_label, root = check_root_label(settings, label)
    rel_clean = clean_relative_path(rel, allow_root=allow_root)
    candidate = Path(os.path.realpath(root / rel_clean))

    # realpath 之后再比对，才能挡住 symlink 指向根外
    try:
        common = os.path.commonpath([str(root), str(candidate)])
    except ValueError:  # 不同盘符
        raise ValidationError("路径越界：不在允许的根目录内") from None
    if os.path.normcase(common) != os.path.normcase(str(root)):
        raise ValidationError("路径越界：不在允许的根目录内")

    if must_exist and not candidate.exists():
        raise ValidationError(f"目标不存在: {rel_clean}")

    if candidate.exists():
        if kind == "file" and not candidate.is_file():
            raise ValidationError("目标不是文件")
        if kind == "dir" and not candidate.is_dir():
            raise ValidationError("目标不是目录")
        if kind == "file":
            ext = candidate.suffix.lower()
            allowed = config.WRITABLE_EXT if writable else config.ALLOWED_EXT
            if ext not in allowed:
                raise ValidationError(
                    f"不支持的文件类型 {ext or '(无扩展名)'}，仅允许 {sorted(allowed)}"
                )
            if writable and candidate.stat().st_size > config.MAX_FILE_BYTES:
                raise ValidationError("文件过大，拒绝写入")
    return root_label, candidate


def validate_dialect(value: object, allowed: Iterable[str]) -> str:
    """方言必须命中运行期从 sqlfluff 拿到的真实清单，且形如标识符。"""
    if not isinstance(value, str):
        raise ValidationError("方言必须是字符串")
    value = value.strip()
    if not DIALECT_RE.match(value):
        raise ValidationError(f"方言名称非法: {value!r}")
    allowed_set = {a for a in allowed}
    if allowed_set and value not in allowed_set:
        raise ValidationError(f"未知方言: {value}（可用方言由 sqlfluff 运行时提供）")
    return value


def validate_rule_list(value: object, allowed_refs: set[str], field: str) -> list[str]:
    """规则引用校验：只允许真实存在的 code / name / group。

    先做形态校验（挡注入），再用运行时目录做白名单（挡拼写错误）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = [p for p in re.split(r"[,\s]+", value) if p]
    elif isinstance(value, list):
        raw: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValidationError(f"{field} 只能包含字符串")
            raw.extend(p for p in re.split(r"[,\s]+", item) if p)
    else:
        raise ValidationError(f"{field} 必须是字符串或字符串数组")

    out: list[str] = []
    for ref in raw:
        if len(ref) > 64:
            raise ValidationError(f"{field} 项过长: {ref[:16]}…")
        if not (RULE_CODE_RE.match(ref) or RULE_NAME_RE.match(ref)):
            raise ValidationError(f"{field} 含非法字符: {ref!r}")
        if allowed_refs and ref not in allowed_refs:
            raise ValidationError(f"未知规则引用: {ref}")
        if ref not in out:
            out.append(ref)
    if len(out) > 200:
        raise ValidationError(f"{field} 数量过多")
    return out


def validate_timeout(value: object, cap: float | None = None) -> float:
    cap = cap or config.TIMEOUT_MAX
    if value is None:
        return config.TIMEOUT_DEFAULT
    if isinstance(value, bool):
        raise ValidationError("超时必须是数字")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValidationError("超时必须是数字") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise ValidationError("超时数值非法")
    if num < config.TIMEOUT_MIN:
        raise ValidationError(f"超时不能小于 {config.TIMEOUT_MIN} 秒")
    if num > cap:
        raise ValidationError(f"超时不能大于 {cap} 秒")
    return round(num, 2)


def validate_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValidationError("布尔参数必须是 true/false")


def validate_int(value: object, lo: int, hi: int, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是整数")
    if isinstance(value, int):
        num = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValidationError(f"{field} 必须是整数")
        num = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValidationError(f"{field} 必须是整数")
        num = int(text)
    else:
        raise ValidationError(f"{field} 必须是整数")
    if not lo <= num <= hi:
        raise ValidationError(f"{field} 必须在 {lo}-{hi} 之间")
    return num
