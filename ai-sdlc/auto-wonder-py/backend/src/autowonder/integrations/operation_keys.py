"""外部操作的语义键、标记和摘要。算法与 Java ``ExternalOperationKeys`` 一致。"""

import hashlib
import json


def text_digest(value: str | None) -> str:
    """正文摘要。换行先收成 ``\\n``。"""
    text = ""
    if value is not None:
        text = value.replace("\r\n", "\n")
    return sha256_text(text)


def payload_digest(payload_json: str | None) -> str:
    """把 JSON 对象的键排序后再做摘要，用来识别同一操作的载荷是否变了。"""
    raw = "{}"
    if payload_json is not None and payload_json.strip() != "":
        raw = payload_json
    parsed = json.loads(raw)
    canonical = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256_text(canonical)


def sha256_text(value: str | None) -> str:
    """UTF-8 的小写十六进制 SHA-256。"""
    raw = ""
    if value is not None:
        raw = value
    return hashlib.sha256(raw.encode()).hexdigest()


def aone_comment_key(workitem_id: int, comment_id: int) -> str:
    """一条本地评论对应一个稳定的 Aone 写回键。"""
    material = str(workitem_id).strip() + "\u0000" + str(comment_id).strip()
    return "aone.comment:" + sha256_text(material)


def operation_marker(operation_key: str) -> str:
    """写进外部评论末尾的隐藏标记，便于回读时认出这次操作。"""
    return "<!-- aw-op:" + sha256_text(operation_key)[:24] + " -->"
