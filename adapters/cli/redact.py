"""输出脱敏。

三层防线:
  1. 键名词序列规则: 键名按分隔符(_,-,空格)与 camelCase 边界拆成
     小写词序列, 任意位置命中凭据名词即视为敏感(覆盖 password_hash、
     token_value、AWS_SECRET_ACCESS_KEY 等组合形态);
  2. 误伤豁免: 仅完整匹配 primary_key/foreign_key 等明确的数据库
     术语时不脱敏, 不允许豁免前缀遮蔽后续敏感词;
  3. 字符串值模式匹配: 密钥赋值形态、Bearer 头形态与长十六进制。
所有规则对键与值幂等, 脱敏发生在完整行解析之后, 跨 chunk 拆分
的 secret 同样会被命中。
"""

from __future__ import annotations

import re
from typing import Any

from security.secret_policy import redact_secret_text

#: 凭据名词: 在词序列任意位置命中即敏感。
_CREDENTIAL_NOUNS = frozenset(
    {
        "key",
        "keys",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwd",
        "pwd",
        "pass",
        "credential",
        "credentials",
    }
)

#: 仅完整匹配时豁免的数据库/索引术语。
_SAFE_EXACT_WORDS = frozenset(
    {
        ("primary", "key"),
        ("foreign", "key"),
        ("unique", "key"),
        ("candidate", "key"),
        ("shard", "key"),
        ("sort", "key"),
        ("partition", "key"),
        ("token", "count"),
    }
)

#: 旧版精确名单: 不符合尾词规则的既有敏感键。
_LEGACY_EXACT = frozenset({"authorization", "cookie", "bearer", "session_id", "sessionid", "auth"})

#: 字符串内 key=value / key:value / shell export 形态: 键候选使用与
#: dict 键相同的分词与敏感性判定(下划线、短横线、空格、点、camelCase
#: 均由 is_sensitive_key 统一处理); 值支持双引号/单引号/无引号三形态。
_KV_RE = re.compile(
    r"(?i)(?:\bexport\s+)?[\"']?"
    r"(?P<key>[A-Za-z][A-Za-z0-9_\-]*(?:[ \t_\-\.]+[A-Za-z0-9]+)*)"
    r"[\"']?\s*[:=]\s*"
    r'(?:"(?P<dval>[^\"]*)"|\'(?P<sval>[^\']*)\'|(?P<uval>[^\s\"",}]+))'
)

#: Bearer / Basic 头形态。
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

#: 长十六进制(>=32 位)按疑似摘要/密钥处理。
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

#: 单个字符串值的截断上限。
MAX_VALUE_CHARS = 2_000

_REDACTED = "[REDACTED]"

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _key_words(key: str) -> list[str]:
    """键名 -> 小写词序列(分隔符 + camelCase 双重拆分)。"""

    spaced = _CAMEL_BOUNDARY_RE.sub("_", key)
    return [word.lower() for word in _WORD_SPLIT_RE.split(spaced) if word]


def is_sensitive_key(key: str) -> bool:
    """判断 dict 键是否为敏感凭据键。"""

    words = _key_words(key)
    if not words:
        return False
    word_tuple = tuple(words)
    if word_tuple in _SAFE_EXACT_WORDS:
        return False
    if "_".join(words) in _LEGACY_EXACT:
        return True
    return any(word in _CREDENTIAL_NOUNS for word in words)


def _kv_sub(match: re.Match[str]) -> str:
    """字符串 KV 命中的替换函数: 敏感键才脱敏, 否则原文保留。"""

    key = match.group("key")
    if not is_sensitive_key(key):
        return match.group(0)
    base = match.start(0)
    for name in ("dval", "sval", "uval"):
        value = match.group(name)
        if value is not None:
            if not value:
                return match.group(0)
            start, end = match.span(name)
            raw = match.group(0)
            return raw[: start - base] + _REDACTED + raw[end - base :]
    return match.group(0)


def redact_string(text: str) -> str:
    """对单个字符串做确定性脱敏。

    顺序敏感: 先处理 Bearer/Basic 头形态(避免键值对规则只吃掉
    scheme), 再以与 dict 键相同的判定处理 key=value / key:value /
    shell export 形态与长十六进制。
    """

    # Only the retained prefix can leave this boundary. Truncate before the
    # regex passes so a hostile oversized value cannot amplify regex work.
    truncated = len(text) > MAX_VALUE_CHARS
    text = text[:MAX_VALUE_CHARS]
    text = redact_secret_text(text)
    text = _BEARER_RE.sub(_REDACTED, text)
    text = _KV_RE.sub(_kv_sub, text)
    text = _LONG_HEX_RE.sub(_REDACTED, text)
    if truncated:
        text += "...<truncated>"
    return text


def sanitize_value(value: Any) -> Any:
    """深度遍历 JSON 形态数据并脱敏。

    规则:
      - dict: 敏感键名的值整体替换(含非字符串); 其余键递归;
      - str: 模式脱敏与限长;
      - list/tuple: 逐元素递归;
      - 其他标量原样保留。
    本函数幂等: 对已脱敏输出再次执行结果不变。
    """

    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive_key(key):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = sanitize_value(item)
        return cleaned
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v) for v in value]
    return value


__all__ = ["MAX_VALUE_CHARS", "is_sensitive_key", "redact_string", "sanitize_value"]
