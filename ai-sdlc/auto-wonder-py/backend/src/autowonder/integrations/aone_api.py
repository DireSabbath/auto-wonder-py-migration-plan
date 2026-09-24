"""Aone OpenAPI 客户端，以及项目、工单查询。每次请求都先核对开关。"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from autowonder.integrations.aone_codec import (
    AoneOpenApiError,
    aone_web_base_url,
    require_enabled,
    sign_aone,
    to_url_encoded_query,
)

logger = logging.getLogger(__name__)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERMITS_PER_SECOND = 1.5
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0
_PER_PAGE = 200
_ID_LIST_MAX = 50
_WINDOW_MAX_ITEMS = 5000
_MAX_PAGES = 26
_DEFAULT_EPOCH_MILLIS = 946656000000


@dataclass
class AoneConfig:
    """一次调用的主机、客户端和区域。"""

    base_url: str
    client_key: str
    access_secret: str
    region_id: str | None


@dataclass
class ExternalProject:
    """外部项目。"""

    external_id: str | None = None
    name: str | None = None
    raw_json: str | None = None


@dataclass
class ExternalProjectMember:
    """外部项目成员。"""

    external_user_id: str | None = None
    staff_id: str | None = None
    display_name: str | None = None
    role_name: str | None = None
    raw_json: str | None = None


@dataclass
class PageResult[Item]:
    """分页。``total_count`` 来自 Aone 信封。"""

    items: list[Item]
    page: int
    page_size: int
    total_count: int


@dataclass
class ExternalIssueType:
    """启用的工作项类型。"""

    external_id: str | None = None
    stamp: str | None = None
    name: str | None = None


@dataclass
class ExternalStatusOption:
    """工作流状态。"""

    external_id: str | None = None
    name: str | None = None


@dataclass
class ExternalWorkitemDetail:
    """搜索或详情接口映射出的工单。"""

    external_id: str | None = None
    external_project_id: str | None = None
    external_issue_type_id: str | None = None
    work_type: str | None = None
    title: str | None = None
    content_md: str | None = None
    status_id: str | None = None
    status_name: str | None = None
    priority: int | None = None
    external_url: str | None = None
    source_lifecycle: str = "ACTIVE"
    updated_at: datetime | None = None
    created_at: datetime | None = None
    raw_json: str | None = None


@dataclass
class ExternalComment:
    """外部评论。"""

    external_id: str | None = None
    external_workitem_id: str | None = None
    author_staff_id: str | None = None
    content_md: str | None = None
    source_status: str | None = None
    updated_at: datetime | None = None


@dataclass
class _Limiter:
    """每秒 1.5 次，低于 Aone 每分钟 100 次的服务端配额。"""

    permits_per_second: float = _PERMITS_PER_SECOND
    next_at: float = field(default_factory=time.monotonic)

    def acquire(self) -> None:
        """等到下一个许可。"""
        now = time.monotonic()
        wait = self.next_at - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        self.next_at = max(self.next_at, now) + (1.0 / self.permits_per_second)


_LIMITER = _Limiter()


class AoneClient:
    """带签名的 GET / 表单 POST。"""

    def get(self, config: AoneConfig, path: str, query: dict[str, object]) -> dict[str, object]:
        """查询。开关关闭时在发请求前失败。"""
        require_enabled()
        timestamp = int(time.time() * 1000)
        signature = sign_aone(config.client_key, config.access_secret, timestamp)
        qs = to_url_encoded_query(query)
        url = config.base_url + path + ("" if qs == "" else "?" + qs)
        headers = _headers(config, timestamp, signature)
        return self._execute("GET", url, headers, None, path)

    def post_form(
        self,
        config: AoneConfig,
        path: str,
        form: dict[str, object],
    ) -> dict[str, object]:
        """表单写操作。正文按百分号编码，避免评论里的 ``&`` 破坏解析。"""
        require_enabled()
        timestamp = int(time.time() * 1000)
        signature = sign_aone(config.client_key, config.access_secret, timestamp)
        url = config.base_url + path
        headers = _headers(config, timestamp, signature)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self._execute("POST", url, headers, to_url_encoded_query(form), path)

    def _execute(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
        path: str,
    ) -> dict[str, object]:
        _LIMITER.acquire()
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                content=body,
                timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            )
        except httpx.HTTPError as error:
            raise AoneOpenApiError("Aone request failed: " + str(error)) from error
        text = response.text
        try:
            payload = response.json()
        except ValueError as error:
            raise AoneOpenApiError(
                "Aone returned non-JSON response: HTTP "
                + str(response.status_code)
                + " "
                + text
            ) from error
        if not isinstance(payload, dict):
            raise AoneOpenApiError(
                "Aone returned non-JSON response: HTTP "
                + str(response.status_code)
                + " "
                + text
            )
        _log_read(path, response.status_code, payload)
        success = bool(payload.get("success"))
        if response.status_code >= 400 or not success:
            message = payload.get("message")
            detail = text if not isinstance(message, str) or message.strip() == "" else message
            raise AoneOpenApiError(detail, _terminal(response.status_code, detail))
        result = payload.get("result")
        if isinstance(result, dict):
            _copy_envelope(payload, result)
            return result
        wrapped: dict[str, object] = {"result": result}
        _copy_envelope(payload, wrapped)
        return wrapped


def search_projects(
    client: AoneClient,
    config: AoneConfig,
    query: str,
    page: int,
    page_size: int,
) -> PageResult[ExternalProject]:
    """按名称搜索项目。"""
    import json

    query_body = json.dumps(
        {"region": "alibaba", "name": query, "page": page, "perPage": page_size},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = client.get(
        config,
        "/ak/project/openapi/ProjectApiFacade/searchByQuery",
        {"region": "alibaba", "query": query_body},
    )
    items = [_to_project(item) for item in _array_from(result) if isinstance(item, dict)]
    return PageResult(items, page, page_size, _int_value(result.get("totalCount")))


def get_project(client: AoneClient, config: AoneConfig, project_id: str) -> ExternalProject:
    """读取单个项目。"""
    result = client.get(
        config,
        "/ak/project/openapi/ProjectApiFacade/getProjectInfo",
        {"projectId": project_id, "region": "alibaba"},
    )
    return _to_project(result)


def list_members(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
) -> list[ExternalProjectMember]:
    """展开角色下的用户；没有 users 数组时把角色行本身当作成员。"""
    result = client.get(
        config,
        "/ak/project/openapi/ProjectApiFacade/getProjectMembers",
        {"targetType": "AKProject", "targetIds": project_id, "region": "alibaba"},
    )
    members: list[ExternalProjectMember] = []
    for item in _array_from(result):
        if not isinstance(item, dict):
            continue
        role_name = _first(_str(item, "roleName"), _str(item, "role"), _str(item, "name"))
        users = item.get("users")
        if isinstance(users, list):
            for user in users:
                if isinstance(user, dict):
                    members.append(_to_member(user, role_name))
        else:
            members.append(_to_member(item, role_name))
    return members


def search_project_first_page(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
) -> PageResult[ExternalWorkitemDetail]:
    """连接测试只拉第一页。"""
    result = _search_page(client, config, project_id, [], None, None, 1, _PER_PAGE)
    items = _to_workitems(result)
    return PageResult(items, 1, _PER_PAGE, _int_value(result.get("totalCount")))


def search_project(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
) -> PageResult[ExternalWorkitemDetail]:
    """按创建时间窗口扫描。超过偏移上限时对半切分窗口。"""
    collected: dict[str, ExternalWorkitemDetail] = {}
    state = {"first_done": False}
    _scan_window(
        client,
        config,
        project_id,
        _DEFAULT_EPOCH_MILLIS,
        int(time.time() * 1000),
        collected,
        state,
    )
    return PageResult(list(collected.values()), 1, _PER_PAGE, len(collected))


def search_by_ids(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
    ids: list[str],
) -> PageResult[ExternalWorkitemDetail]:
    """idList 每批最多 50 个。"""
    if len(ids) == 0:
        return PageResult([], 1, _PER_PAGE, 0)
    collected: dict[str, ExternalWorkitemDetail] = {}
    total = 0
    start = 0
    while start < len(ids):
        batch = ids[start : start + _ID_LIST_MAX]
        result = _search_page(client, config, project_id, batch, None, None, 1, _PER_PAGE)
        total += _int_value(result.get("totalCount"))
        for item in _to_workitems(result):
            if item.external_id is not None:
                collected.setdefault(item.external_id, item)
        start += _ID_LIST_MAX
    return PageResult(list(collected.values()), 1, _PER_PAGE, total)


def get_workitem(
    client: AoneClient,
    config: AoneConfig,
    workitem_id: str,
) -> ExternalWorkitemDetail:
    """按 id 拉详情。"""
    result = client.get(
        config,
        "/issue/openapi/IssueTopService/getById",
        {"id": workitem_id},
    )
    return to_detail(result)


def list_enabled_issue_types(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
    staff_id: str | None,
    stamp: str,
) -> list[ExternalIssueType]:
    """某个 stamp 下启用的类型。"""
    result = client.get(
        config,
        "/issue/openapi/IssueTopService/getEnabledIssueTypes",
        {"akProjectId": project_id, "stamp": stamp, "staffId": staff_id},
    )
    issue_types: list[ExternalIssueType] = []
    for item in _array_from(result):
        if isinstance(item, dict):
            issue_types.append(
                ExternalIssueType(_str(item, "id"), _str(item, "stamp"), _str(item, "name"))
            )
    return issue_types


def list_status_rules(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
    issue_type_id: int,
) -> list[ExternalStatusOption]:
    """先拿 workflowId，再按 position 排序状态。"""
    workflow = client.get(
        config,
        "/issue/openapi/IssueTopService/getTemplateAndWorkflowInfo",
        {"akProjectId": project_id, "issueTypeId": issue_type_id},
    )
    workflow_id = workflow.get("workflowId")
    if not isinstance(workflow_id, int):
        return []
    result = client.get(
        config,
        "/issue/openapi/IssueTopService/getWorkflowStatusDetail",
        {"akProjectId": project_id, "workflowId": workflow_id},
    )
    statuses = [item for item in _array_from(result) if isinstance(item, dict)]
    statuses.sort(key=lambda item: _int_value(item.get("position")))
    return [ExternalStatusOption(_str(item, "id"), _str(item, "name")) for item in statuses]


def create_comment(
    client: AoneClient,
    config: AoneConfig,
    workitem_id: str | None,
    staff_id: str | None,
    content: str,
) -> ExternalComment:
    """评论走表单 POST，避免长正文撑爆 URL。"""
    result = client.post_form(
        config,
        "/issue/openapi/IssueTopService/createComment",
        {
            "targetType": "Issue",
            "targetId": workitem_id,
            "user": staff_id,
            "content": content,
        },
    )
    external_id = _first(_str(result, "id"), _str(result, "commentId"))
    if external_id is None:
        external_id = _comment_id_from_message(_str(result, "message"))
    source_status = "ACTIVE"
    if result.get("isDeleted") is True:
        source_status = "DELETED"
    comment = ExternalComment(
        external_id=external_id,
        content_md=content,
        author_staff_id=staff_id,
        external_workitem_id=workitem_id,
        source_status=source_status,
        updated_at=_date(_first(_str(result, "updatedAt"), _str(result, "gmtModified"))),
    )
    return comment


def update_status(
    client: AoneClient,
    config: AoneConfig,
    workitem_id: str | None,
    staff_id: str | None,
    status_name: str | None,
) -> None:
    """回写状态。"""
    client.post_form(
        config,
        "/issue/openapi/IssueTopService/update",
        {"issueId": workitem_id, "modifier": staff_id, "status": status_name},
    )


def update_content(
    client: AoneClient,
    config: AoneConfig,
    workitem_id: str | None,
    staff_id: str | None,
    title: str | None,
    content_md: str | None,
) -> None:
    """回写标题和描述。"""
    client.post_form(
        config,
        "/issue/openapi/IssueTopService/update",
        {
            "issueId": workitem_id,
            "modifier": staff_id,
            "subject": title,
            "description": content_md,
        },
    )


def to_detail(issue: dict[str, object]) -> ExternalWorkitemDetail:
    """把 Aone issue 对象收成同步用的详情。"""
    import json

    work_type = _work_type(_str(issue, "stamp"))
    external_id = _str(issue, "id")
    project_id = _str(issue, "akProjectId")
    return ExternalWorkitemDetail(
        external_id=external_id,
        external_project_id=project_id,
        external_issue_type_id=_first(_str(issue, "issueTypeId"), _str(issue, "issueTypeID")),
        work_type=work_type,
        title=_str(issue, "subject"),
        content_md=_first(_str(issue, "description"), _str(issue, "content")),
        status_id=_first(_str(issue, "statusId"), _str(issue, "statusID")),
        status_name=_str(issue, "status"),
        priority=_priority(_str(issue, "priorityId"), _str(issue, "priority")),
        external_url=_first(
            _str(issue, "webUrl"),
            _str(issue, "url"),
            _web_url(project_id, work_type, external_id),
        ),
        source_lifecycle=_lifecycle(issue),
        updated_at=_date(
            _first(
                _str(issue, "modifiedAt"),
                _str(issue, "gmtModified"),
                _str(issue, "updatedAt"),
                _str(issue, "updateStatusAt"),
            )
        ),
        created_at=_date(_str(issue, "createdAt")),
        raw_json=json.dumps(issue, ensure_ascii=False, separators=(",", ":")),
    )


def _search_page(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
    ids: list[str],
    created_from: datetime | None,
    created_to: datetime | None,
    page_no: int,
    per_page: int,
) -> dict[str, object]:
    params: dict[str, object] = {"akProjectId": project_id}
    if len(ids) > 0:
        params["idList"] = ids
    params["stamp"] = "Req,Bug,Task"
    if created_from is not None:
        params["createdAtFrom"] = _format_time(created_from)
    if created_to is not None:
        params["createdAtTo"] = _format_time(created_to)
    params["page"] = page_no
    params["perPage"] = per_page
    return client.post_form(config, "/issue/openapi/IssueTopService/searchV4", params)


def _scan_window(
    client: AoneClient,
    config: AoneConfig,
    project_id: str,
    start_ms: int,
    end_ms: int,
    collected: dict[str, ExternalWorkitemDetail],
    state: dict[str, bool],
) -> None:
    from_date = None if start_ms <= _DEFAULT_EPOCH_MILLIS else _from_millis(start_ms)
    to_date = _from_millis(end_ms)
    try:
        first_page = _search_page(
            client, config, project_id, [], from_date, to_date, 1, _PER_PAGE
        )
    except AoneOpenApiError as error:
        if not state["first_done"]:
            raise
        logger.warning(
            "Aone window scan failed, returning partial result from=%s to=%s error=%s",
            from_date,
            to_date,
            error,
        )
        return
    state["first_done"] = True
    total_count = _int_value(first_page.get("totalCount"))
    if total_count == 0:
        return
    can_split = end_ms - start_ms > 1
    if total_count > _WINDOW_MAX_ITEMS and can_split:
        mid = start_ms + (end_ms - start_ms) // 2
        _scan_window(client, config, project_id, start_ms, mid, collected, state)
        _scan_window(client, config, project_id, mid + 1, end_ms, collected, state)
        return
    _add_all(first_page, collected)
    total_pages = (total_count + _PER_PAGE - 1) // _PER_PAGE
    page_no = 2
    while page_no <= total_pages and page_no <= _MAX_PAGES:
        try:
            page = _search_page(
                client, config, project_id, [], from_date, to_date, page_no, _PER_PAGE
            )
        except AoneOpenApiError as error:
            logger.warning(
                "Aone window page fetch failed, returning partial result page=%s error=%s",
                page_no,
                error,
            )
            return
        _add_all(page, collected)
        page_no += 1


def _add_all(result: dict[str, object], collected: dict[str, ExternalWorkitemDetail]) -> None:
    for item in _to_workitems(result):
        if item.external_id is not None:
            collected.setdefault(item.external_id, item)


def _to_workitems(result: dict[str, object]) -> list[ExternalWorkitemDetail]:
    return [to_detail(item) for item in _array_from(result) if isinstance(item, dict)]


def _headers(config: AoneConfig, timestamp: int, signature: str) -> dict[str, str]:
    region = "1" if config.region_id is None else config.region_id
    return {
        "clientKey": config.client_key,
        "timestamp": str(timestamp),
        "signature": signature,
        "Ao-Region-Id": region,
    }


def _copy_envelope(source: dict[str, object], target: dict[str, object]) -> None:
    for key in ("success", "message", "totalCount", "pageSize"):
        if key in source:
            target[key] = source[key]


def _terminal(status: int, detail: str) -> bool:
    if 400 <= status < 500 and status != 429:
        return True
    lowered = detail.lower()
    return "not found" in lowered or "invalid" in lowered


def _log_read(path: str, status: int, payload: dict[str, object]) -> None:
    if path not in {
        "/issue/openapi/IssueTopService/searchV4",
        "/issue/openapi/IssueTopService/getById",
        "/issue/openapi/CommentTopService/get",
    }:
        return
    result = payload.get("result")
    count = len(result) if isinstance(result, list) else None
    logger.info("Aone API response endpoint=%s httpStatus=%s itemCount=%s", path, status, count)


def _array_from(result: dict[str, object]) -> list[object]:
    for key in ("result", "data", "list"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    if len(result) == 0:
        return []
    return [result]


def _to_project(obj: dict[str, object]) -> ExternalProject:
    import json

    return ExternalProject(
        _first(_str(obj, "id"), _str(obj, "akProjectId")),
        _first(_str(obj, "name"), _str(obj, "displayName")),
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
    )


def _to_member(obj: dict[str, object], role_name: str | None) -> ExternalProjectMember:
    import json

    return ExternalProjectMember(
        _str(obj, "id"),
        _first(_str(obj, "staffId"), _str(obj, "userId")),
        _first(
            _str(obj, "nickName"),
            _str(obj, "realName"),
            _str(obj, "name"),
            _str(obj, "displayName"),
        ),
        role_name,
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
    )


def _work_type(stamp: str | None) -> str | None:
    if stamp is None:
        return None
    lowered = stamp.lower()
    if lowered == "req":
        return "REQ"
    if lowered == "bug":
        return "BUG"
    if lowered == "task":
        return "TASK"
    return None


def _priority(priority_id: str | None, priority: str | None) -> int | None:
    raw = priority_id if priority_id is not None and priority_id.strip() != "" else priority
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _lifecycle(issue: dict[str, object]) -> str:
    if issue.get("isDeleted") is True or issue.get("deleted") is True:
        return "DELETED"
    if issue.get("isClosed") is True or issue.get("closed") is True:
        return "CLOSED"
    return "ACTIVE"


def _web_url(project_id: str | None, work_type: str | None, external_id: str | None) -> str | None:
    base = aone_web_base_url()
    if (
        base is None
        or project_id is None
        or project_id.strip() == ""
        or external_id is None
        or external_id.strip() == ""
    ):
        return None
    kind = "req" if work_type == "REQ" else "issue"
    return base + "/" + kind + "/" + project_id + "/" + external_id


def _date(value: str | None) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    text = value.strip()
    if text.isdigit():
        return _from_millis(int(text)).replace(tzinfo=None)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(_SHANGHAI).replace(tzinfo=None)
    return parsed


def _from_millis(millis: int) -> datetime:
    return datetime.fromtimestamp(millis / 1000, _SHANGHAI)


def _format_time(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=_SHANGHAI)
    return aware.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _comment_id_from_message(message: str | None) -> str | None:
    if message is None or message.strip() == "":
        return None
    match = re.search(r"comment\s+id\s*:?\s*(\d+)", message, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1)


def _str(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    return str(value)


def _first(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value.strip() != "":
            return value
    return None


def _int_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)
