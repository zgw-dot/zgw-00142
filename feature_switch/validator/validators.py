from __future__ import annotations

import json
from typing import Any, Optional, TYPE_CHECKING

from ..core.enums import VersionStatus, VALID_TRANSITIONS

if TYPE_CHECKING:
    from ..storage.repository import SwitchRepository


class ValidationError(Exception):
    """Raised when a validation check fails; message is human-readable."""

    def __init__(self, message: str, *, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def __str__(self) -> str:
        if self.field:
            return f"[{self.field}] {self.message}"
        return self.message


# ---------------------------------------------------------------------------
# Field-level validations
# ---------------------------------------------------------------------------

def validate_ratio(ratio: Any) -> int:
    """Ensure rollout_ratio is an int within [0, 100]."""
    if isinstance(ratio, bool) or not isinstance(ratio, int):
        raise ValidationError(
            f"灰度比例必须是整数，收到 {ratio!r} (类型 {type(ratio).__name__})",
            field="rollout_ratio",
        )
    if ratio < 0 or ratio > 100:
        raise ValidationError(
            f"灰度比例必须在 0 到 100 之间，收到 {ratio}",
            field="rollout_ratio",
        )
    return ratio


def validate_not_self_approve(author: str, approver: str) -> None:
    """Block authors from approving their own draft."""
    if author == approver:
        raise ValidationError(
            f"作者 '{author}' 不能审批自己的草稿，必须由其他人审批",
            field="approver",
        )


def validate_transition(
    current: VersionStatus, target: VersionStatus
) -> None:
    """Ensure the state machine allows current -> target."""
    allowed = VALID_TRANSITIONS.get(current, [])
    if target not in allowed:
        raise ValidationError(
            f"不允许的状态流转: {current.value} -> {target.value}。"
            f"允许的目标状态: {[s.value for s in allowed] or '(无)'}",
            field="status",
        )


def validate_dependencies(
    env: str,
    dependencies: list[str],
    repo: "SwitchRepository",
    *,
    extra_available: Optional[set[str]] = None,
) -> None:
    """Ensure every referenced dependency exists as a published switch
    (or is being imported in the same batch, passed via `extra_available`).
    """
    if not dependencies:
        return
    available = {s.name for s in repo.list_published_switches(env=env)}
    if extra_available:
        available = available | set(extra_available)
    missing = [dep for dep in dependencies if dep not in available]
    if missing:
        raise ValidationError(
            f"依赖开关在环境 '{env}' 中不存在（未发布或未创建）: {missing}",
            field="dependencies",
        )


# ---------------------------------------------------------------------------
# Payload validation (for import / create / edit)
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = ("env", "name", "author", "rollout_ratio", "default_value")


def validate_switch_payload(data: Any) -> dict[str, Any]:
    """Validate the shape and types of a switch spec dict.

    Does NOT touch DB; call validate_dependencies separately if needed.
    """
    if not isinstance(data, dict):
        raise ValidationError(f"开关配置必须是对象/mapping，收到 {type(data).__name__}")

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ValidationError(f"缺少必填字段: {missing}")

    env = data["env"]
    name = data["name"]
    author = data["author"]
    default_value = data["default_value"]

    if not isinstance(env, str) or not env.strip():
        raise ValidationError("env 必须是非空字符串", field="env")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name 必须是非空字符串", field="name")
    if not isinstance(author, str) or not author.strip():
        raise ValidationError("author 必须是非空字符串", field="author")
    if not isinstance(default_value, bool):
        raise ValidationError(
            f"default_value 必须是布尔值，收到 {default_value!r}",
            field="default_value",
        )

    ratio = validate_ratio(data["rollout_ratio"])

    whitelist = data.get("whitelist", [])
    if not isinstance(whitelist, list) or any(not isinstance(x, str) for x in whitelist):
        raise ValidationError("whitelist 必须是字符串列表", field="whitelist")

    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(x, str) or not x for x in dependencies
    ):
        raise ValidationError("dependencies 必须是非空字符串列表", field="dependencies")

    return {
        "env": env.strip(),
        "name": name.strip(),
        "author": author.strip(),
        "rollout_ratio": ratio,
        "whitelist": list(whitelist),
        "dependencies": list(dependencies),
        "default_value": default_value,
    }


# ---------------------------------------------------------------------------
# Import file parsers (fail fast on malformed syntax, before any write)
# ---------------------------------------------------------------------------

def parse_yaml(raw: str) -> Any:
    """Parse YAML and return the native structure.

    Raises ValidationError with a clear message on any syntax error.
    Uses PyYAML if available, otherwise falls back to a lightweight
    pure-Python parser that supports the subset we document.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return _parse_yaml_mini(raw)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationError(f"YAML 语法错误: {exc}")
    return data


def parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"JSON 语法错误 (行 {exc.lineno}, 列 {exc.colno}): {exc.msg}"
        )


# ---------------------------------------------------------------------------
# Mini YAML parser (subset) — used when PyYAML is not installed.
# Supports: mappings, sequences, scalar strings/int/bool/null, "- " list items.
# ---------------------------------------------------------------------------

def _parse_yaml_mini(raw: str) -> Any:
    """Tiny YAML subset parser. Good enough for our example configs."""
    lines = raw.splitlines()
    tokens: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2 != 0:
            raise ValidationError(
                f"YAML 缩进必须是 2 的倍数 (第 {idx} 行)"
            )
        tokens.append((indent // 2, stripped.strip()))
    if not tokens:
        return None

    pos = [0]

    def parse_block(level: int) -> Any:
        if pos[0] >= len(tokens):
            return None
        cur_indent, content = tokens[pos[0]]
        if cur_indent < level:
            return None
        if content.startswith("- "):
            return parse_sequence(level)
        if ":" in content:
            return parse_mapping(level)
        return _parse_scalar(content)

    def parse_mapping(level: int) -> dict:
        result: dict = {}
        while pos[0] < len(tokens):
            indent, content = tokens[pos[0]]
            if indent != level:
                break
            if content.startswith("- "):
                break
            if ":" not in content:
                raise ValidationError(f"YAML 解析失败: 期望 key: value (行内容 {content!r})")
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            pos[0] += 1
            if val == "":
                child = parse_block(level + 1)
                result[key] = child
            elif val.startswith("[") and val.endswith("]"):
                result[key] = _parse_inline_list(val)
            elif val.startswith("{") and val.endswith("}"):
                result[key] = _parse_inline_dict(val)
            else:
                result[key] = _parse_scalar(val)
        return result

    def parse_sequence(level: int) -> list:
        result: list = []
        while pos[0] < len(tokens):
            indent, content = tokens[pos[0]]
            if indent != level or not content.startswith("- "):
                break
            item_text = content[2:].strip()
            pos[0] += 1
            if ":" in item_text:
                mini_key, _, mini_val = item_text.partition(":")
                mini_dict = {mini_key.strip(): _parse_scalar(mini_val.strip()) if mini_val.strip() else parse_block(level + 1)}
                while pos[0] < len(tokens):
                    ni, nc = tokens[pos[0]]
                    if ni == level + 1 and not nc.startswith("- ") and ":" in nc:
                        k2, _, v2 = nc.partition(":")
                        pos[0] += 1
                        mini_dict[k2.strip()] = _parse_scalar(v2.strip()) if v2.strip() else parse_block(level + 1)
                    else:
                        break
                result.append(mini_dict)
            elif item_text == "":
                child = parse_block(level + 1)
                result.append(child)
            else:
                result.append(_parse_scalar(item_text))
        return result

    return parse_block(0)


def _parse_scalar(val: str) -> Any:
    if val in ("null", "~", ""):
        return None
    if val == "true":
        return True
    if val == "false":
        return False
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _parse_inline_list(val: str) -> list:
    inner = val[1:-1].strip()
    if not inner:
        return []
    parts = _split_top_level(inner, ",")
    return [_parse_scalar(p.strip()) for p in parts]


def _parse_inline_dict(val: str) -> dict:
    inner = val[1:-1].strip()
    if not inner:
        return {}
    result: dict = {}
    for pair in _split_top_level(inner, ","):
        if ":" not in pair:
            raise ValidationError(f"内联字典语法错误: {pair!r}")
        k, _, v = pair.partition(":")
        result[k.strip()] = _parse_scalar(v.strip())
    return result


def _split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    in_str: Optional[str] = None
    for ch in text:
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
            cur.append(ch)
            continue
        if ch in "[{(":
            depth += 1
            cur.append(ch)
        elif ch in "]})":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts
