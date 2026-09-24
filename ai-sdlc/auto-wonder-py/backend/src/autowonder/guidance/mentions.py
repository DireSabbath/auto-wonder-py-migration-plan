"""从评论正文里抽出 @ 名称。规则与 GuidanceService 的正则一致。"""

import html
import re
import unicodedata

from autowonder.debuglogs.sanitizer import java_is_blank, java_is_whitespace, java_strip

_HTML_TAG = re.compile(r"(?s)<[^>]*>")
_HTML_MENTION = re.compile(
    r"(?is)<span\b(?=[^>]*\bdata-type\s*=\s*['\"]mention['\"])[^>]*>(.*?)</span>"
)
_AONE_WORKER = re.compile(r"\(WORKER_[^)]+\)$")
_TEXT_MENTION = re.compile(r"@([^\s\u00a0@]+)")
_TRAILING_PUNCT = re.compile(r"[,，。.!！?？;；:：]+$")


def html_text(content: str | None) -> str:
    """去掉标签并还原实体。不换行空格改成普通空格。"""
    raw = ""
    if content is not None:
        raw = content
    without_tags = _HTML_TAG.sub("", raw)
    return html.unescape(without_tags).replace("\u00a0", " ")


def mention_names(content_md: str | None) -> list[str]:
    """富文本 mention 优先。没有时再扫纯文本 @。"""
    if content_md is None:
        return []
    names: list[str] = []
    for match in _HTML_MENTION.finditer(content_md):
        name = normalize_mention_name(html_text(match.group(1)))
        if name is not None:
            names.append(name)
    if len(names) > 0:
        return names
    for match in _TEXT_MENTION.finditer(html_text(content_md)):
        name = normalize_mention_name("@" + match.group(1))
        if name is not None:
            names.append(name)
    return names


def normalize_mention_name(mention: str | None) -> str | None:
    """去掉 @、Aone worker 后缀和句尾标点。"""
    if mention is None:
        return None
    name = java_strip(mention)
    if not name.startswith("@"):
        return None
    name = java_strip(_AONE_WORKER.sub("", java_strip(name[1:])))
    name = java_strip(_TRAILING_PUNCT.sub("", name))
    if java_is_blank(name):
        return None
    return name


def mention_comparable_content(content_md: str | None) -> str:
    """富文本开头的 mention 收成 ``@名称`` 加后续正文，用来判断是不是只点名。"""
    if content_md is None:
        return ""
    content = content_md.lstrip()
    if not content.startswith("<"):
        return content
    mention = _HTML_MENTION.search(content)
    if mention is None or not java_is_blank(html_text(content[: mention.start()])):
        return html_text(content).lstrip()
    display_name = java_strip(html_text(mention.group(1)))
    if not display_name.startswith("@"):
        return html_text(content).lstrip()
    agent_name = java_strip(_AONE_WORKER.sub("", java_strip(display_name[1:])))
    if java_is_blank(agent_name):
        return html_text(content).lstrip()
    trailing = java_strip(html_text(content[mention.end() :]))
    if java_is_blank(trailing):
        return "@" + agent_name
    return "@" + agent_name + " " + trailing


def text_mention_index(content: str, agent_name: str) -> int:
    """纯文本 @ 名称。右侧是边界才算，中文可以紧跟在名称后面。"""
    token = "@" + agent_name
    start = 0
    while start <= len(content):
        index = content.find(token, start)
        if index < 0:
            return -1
        end = index + len(token)
        if end >= len(content) or is_text_mention_boundary(content[end]):
            return index
        start = index + 1
    return -1


def is_text_mention_boundary(char: str) -> bool:
    """空白、汉字或非字母数字都是纯文本 mention 的右边界。"""
    code = ord(char)
    if java_is_whitespace(code) or unicodedata.category(char) == "Zs":
        return True
    if _is_han(code):
        return True
    category = unicodedata.category(char)
    if category.startswith("L") or category == "Nd":
        return False
    return True


def _is_han(code: int) -> bool:
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF:
        return True
    if 0xF900 <= code <= 0xFAFF:
        return True
    if 0x20000 <= code <= 0x2A6DF or 0x2A700 <= code <= 0x2B73F:
        return True
    if 0x2B740 <= code <= 0x2B81F or 0x2B820 <= code <= 0x2CEAF:
        return True
    if 0x2CEB0 <= code <= 0x2EBEF or 0x30000 <= code <= 0x3134F:
        return True
    return 0x2F800 <= code <= 0x2FA1F
