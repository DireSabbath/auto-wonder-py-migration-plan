"""在两个库上各放一套夹具，跑完 18 个定时任务后比较落库结果。

Java 用一次性 Spring 进程调用公开的任务方法，库仍是 33060。
Python 在本进程调用同一批任务，库是 33061。
"""

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_JAVA_PORT = 33060
_PYTHON_PORT = 33061
_JAVA_REDIS = 63790
_PYTHON_REDIS = 63791
# Java RedisManager 的连接池不读 REDIS_DATABASE，固定落在 0 号库。
_JAVA_REDIS_DB = 0
_PAUSE_ERROR = "PAUSE_CONFIRMATION_MISSING: 暂停确认超时，平台未收到有效暂停检查点"
_STUCK_ERROR = "session stuck (node may have crashed)"
_PENDING_TIMEOUT = "PENDING_TIMEOUT: object missing after 24h"
_READBACK = "readback unavailable for connector/event"
_BIND_MISSING = "binding not found"
_OWNER = "OWNER_INACTIVE"
_JAVA_HOME = Path("/tmp/alibabacloud-landing-zone/ai-sdlc/auto-wonder")
_ENV_FILE = Path("/tmp/java-aone.env")
_COPIED = (
    "OSS_ARTIFACT_BUCKET",
    "OSS_ENABLED",
    "S3_ENABLED",
    "S3_ENDPOINT",
    "S3_PUBLIC_ENDPOINT",
    "S3_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_ACCESS_KEY_SECRET",
    "S3_FORCE_PATH_STYLE",
    "AUTOWONDER_SECRET_MASTER_KEY",
)
_JOBS = (
    "com.aliyun.autowonder.scheduledtask.ScheduledTaskScheduler#scan",
    "com.aliyun.autowonder.scheduledtask.ScheduledTaskRunCompensationTask#sweep",
    "com.aliyun.autowonder.executor.ExecutorUpdateScheduler#scanUpgrades",
    "com.aliyun.autowonder.executor.ProviderModelCatalogScheduler#refreshDueCatalogs",
    "com.aliyun.autowonder.user.AccountDeactivationExpiryTask#sweep",
    "com.aliyun.autowonder.conversation.ConversationTurnEventCleanupTask#cleanup",
    "com.aliyun.autowonder.conversation.AgentConversationRecoveryTask#recoverStaleTurns",
    "com.aliyun.autowonder.conversation.ConversationElicitationExpiryTask#expire",
    "com.aliyun.autowonder.debuglog.DebugLogReconciliationTask#reconcile",
    "com.aliyun.autowonder.ai.AiCompensationTask#sweep",
    "com.aliyun.autowonder.workspace.WorkspaceCleanupTask#sweep",
    "com.aliyun.autowonder.dispatch.DispatchCompensationTask#sweep",
    "com.aliyun.autowonder.integration.AoneOutboxDispatcher#dispatchScheduled",
    "com.aliyun.autowonder.integration.AoneInboundPoller#pollScheduled",
    "com.aliyun.autowonder.integration.feishu.FeishuInbox#drain",
    "com.aliyun.autowonder.integration.dingtalk.DingTalkStreamLifecycle#reconcile",
    "com.aliyun.autowonder.integration.receipt.ExternalOperationRecoveryJob#recoverScheduled",
    "com.aliyun.autowonder.insights.participation.HumanAgentParticipationSnapshotScheduler#nightlyRebuild",
)


def jobs_dual() -> dict[str, object]:
    """插入成对夹具，两边各跑一轮，再比较可观察结果。"""
    _prepare_env()
    from autowonder.config import get_settings

    get_settings.cache_clear()
    token = "jd" + format(time.time_ns(), "x")[-8:]
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    _mysql(_JAVA_PORT, _seed(token, now))
    _mysql(_PYTHON_PORT, _seed(token, now))
    java_catalog = _catalog(_JAVA_REDIS, _JAVA_REDIS_DB)
    python_catalog = _catalog(_PYTHON_REDIS, 0)
    python_failures = asyncio.run(_run_python())
    python_done = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    java_log = _run_java()
    java_done = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    java = _facts(_JAVA_PORT, token, java_log, _JAVA_REDIS, _JAVA_REDIS_DB)
    python = _facts(_PYTHON_PORT, token, "", _PYTHON_REDIS, 0)
    java["catalogUnchanged"] = _catalog(_JAVA_REDIS, _JAVA_REDIS_DB) == java_catalog
    python["catalogUnchanged"] = _catalog(_PYTHON_REDIS, 0) == python_catalog
    python["jobFailures"] = python_failures
    checks = _checks(
        token,
        java,
        python,
        java_log,
        python_failures,
        java_done,
        python_done,
    )
    passed = 0
    failed = 0
    for item in checks:
        if item["pass"] is True:
            passed += 1
        else:
            failed += 1
    return {
        "command": "jobs-dual",
        "ok": failed == 0,
        "passCount": passed,
        "failCount": failed,
        "checks": checks,
        "facts": {"java": java, "python": python},
    }


def _prepare_env() -> None:
    for line in _ENV_FILE.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.startswith("export "):
            key = key[len("export ") :]
        if key in _COPIED:
            os.environ[key] = value
    os.environ["AUTOWONDER_AONE_ENABLED"] = "true"
    os.environ["REDIS_HOST"] = "127.0.0.1"
    os.environ["REDIS_PORT"] = "63791"
    os.environ["REDIS_DATABASE"] = "0"
    os.environ["SPRING_DATASOURCE_URL"] = ""
    os.environ["AUTOWONDER_DATABASE_URL"] = (
        "mysql+asyncmy://root:autowonder@127.0.0.1:33061/autowonder"
    )


def _stamp(now: datetime, delta: timedelta) -> str:
    return (now + delta).strftime("%Y-%m-%d %H:%M:%S")


def _seed(token: str, now: datetime) -> str:
    base = 7_000_000_000 + int(token[2:], 16)
    old_source = base
    new_source = base + 1
    template_id = base + 2
    past = _stamp(now, timedelta(days=-2))
    future = _stamp(now, timedelta(days=7))
    # Java 把 epoch 毫秒当成 INTERVAL 秒数，只有几十年前的行才会被选中。
    old_ai = "1900-01-01 00:00:00"
    recent = _stamp(now, timedelta(minutes=-1))
    old_event = _stamp(now, timedelta(days=-31))
    kept_event = _stamp(now, timedelta(days=-1))
    old_card = _stamp(now, timedelta(minutes=-40))
    old_turn = _stamp(now, timedelta(minutes=-6))
    old_debug = _stamp(now, timedelta(hours=-25))
    old_dispatch = _stamp(now, timedelta(minutes=-5))
    old_run = _stamp(now, timedelta(hours=-2))
    old_cleanup = _stamp(now, timedelta(days=-4))
    old_mod = _stamp(now, timedelta(minutes=-2))
    # next_fire_at 在 Python 里按 UTC 墙钟比较，在 Java 里按上海墙钟比较。
    # 写成 UTC 现在之前，两边都会把它当成已经到期，并且不早于 gmt_create。
    due = _stamp(now, timedelta(hours=-8, minutes=-2))
    return f"""
UPDATE scheduled_task SET status='PAUSED' WHERE name LIKE 'jd%-once' AND status='ACTIVE';
INSERT INTO user (username, password_hash, nickname, status, deactivated_at, cooling_off_expires_at, is_deleted)
VALUES ('{token}-expired', 'hash-expired', 'keep', 0, '{past}', '{past}', 0);
INSERT INTO user (username, password_hash, nickname, status, deactivated_at, cooling_off_expires_at, is_deleted)
VALUES ('{token}-cooling', 'hash-cooling', 'keep', 0, '{past}', '{future}', 0);
INSERT INTO agent_conversation (tenant_id, agent_id, channel, channel_conversation_id, executor_id, status)
VALUES (1, 1, 'WEB', '{token}-conv', 880003, 'ACTIVE');
SET @conv = LAST_INSERT_ID();
INSERT INTO agent_conversation_turn (tenant_id, conversation_id, direction, status, dispatch_attempt, last_dispatch_at, gmt_create)
VALUES (1, @conv, 'IN', 'PROCESSING', 1, '{old_turn}', '{old_turn}');
SET @turn = LAST_INSERT_ID();
INSERT INTO agent_conversation_turn_event (tenant_id, conversation_id, turn_id, dispatch_attempt, event_seq, event_type, payload_fragment, gmt_create)
VALUES (1, @conv, @turn, 1, 1, 'TEXT', '{token}-old', '{old_event}');
INSERT INTO agent_conversation_turn_event (tenant_id, conversation_id, turn_id, dispatch_attempt, event_seq, event_type, payload_fragment, gmt_create)
VALUES (1, @conv, @turn, 1, 2, 'TEXT', '{token}-recent', '{kept_event}');
INSERT INTO agent_conversation_elicitation (tenant_id, conversation_id, turn_id, request_id, status, gmt_create)
VALUES (1, @conv, @turn, '{token}-old', 'PENDING', '{old_card}');
INSERT INTO agent_conversation_elicitation (tenant_id, conversation_id, turn_id, request_id, status, gmt_create)
VALUES (1, @conv, @turn, '{token}-recent', 'PENDING', '{recent}');
INSERT INTO ai_session (tenant_id, scene, status, cli_session_ref, gmt_modified, is_deleted, version)
VALUES (1, 'CLARIFICATION', 'RUNNING', '{token}-old', '{old_ai}', 0, 0);
INSERT INTO ai_session (tenant_id, scene, status, cli_session_ref, gmt_modified, is_deleted, version)
VALUES (1, 'CLARIFICATION', 'RUNNING', '{token}-recent', '{recent}', 0, 0);
INSERT INTO debug_log (tenant_id, source_type, source_id, dispatch_id, agent_id, run_no, dispatch_status, object_key, status, gmt_modified)
VALUES (1, 'DISPATCH', {old_source}, {old_source}, 1, 1, 'RUNNING', '{token}/old', 'PENDING', '{old_debug}');
INSERT INTO debug_log (tenant_id, source_type, source_id, dispatch_id, agent_id, run_no, dispatch_status, object_key, status, gmt_modified)
VALUES (1, 'DISPATCH', {new_source}, {new_source}, 1, 2, 'RUNNING', '{token}/recent', 'PENDING', '{recent}');
INSERT INTO integration_outbox (tenant_id, provider, binding_id, workitem_id, event_type, payload_json, operation_key, lock_version, status)
VALUES (1, 'JOBDUAL', 990000003, 1, 'COMMENT_CREATE', '{{}}', '{token}-dispatch', 0, 'PENDING');
INSERT INTO integration_outbox (tenant_id, provider, binding_id, workitem_id, event_type, payload_json, operation_key, lock_version, status, gmt_modified)
VALUES (1, 'JOBDUAL', 990000003, 1, 'COMMENT_CREATE', '{{}}', '{token}-recovery', 0, 'SENDING', '{old_mod}');
INSERT INTO feishu_message_inbox (binding_id, tenant_id, agent_id, message_id, payload, status, attempts, available_at)
VALUES (990000004, 1, 1, '{token}-msg', '{{}}', 'PENDING', 0, '{recent}');
INSERT INTO dingtalk_robot_binding (tenant_id, app_key, credential_ref, robot_code, agent_id, transport_mode, status, is_deleted, version)
VALUES (1, '{token}-app', 'x', '{token}-robot', 1, 'STREAM', 'ENABLED', 0, 0);
INSERT INTO external_project_binding (tenant_id, provider, external_project_id, base_url, client_key, credential_ref, enabled, is_deleted, version)
VALUES (1, 'AONE', '{token}', 'http://127.0.0.1:1', 'jobdual', 'x', 1, 0, 0);
INSERT INTO squad (tenant_id, name) VALUES (1, '{token}-squad');
SET @squad = LAST_INSERT_ID();
INSERT INTO agent (tenant_id, name, kind) VALUES (1, '{token}-agent', 'STANDARD');
SET @agent = LAST_INSERT_ID();
INSERT INTO agent_version (tenant_id, agent_id, version_no) VALUES (1, @agent, 1);
SET @version = LAST_INSERT_ID();
UPDATE agent SET online_version_id = @version WHERE id = @agent;
INSERT INTO squad_member (tenant_id, squad_id, agent_id) VALUES (1, @squad, @agent);
INSERT INTO scheduled_task (workspace_id, name, instruction_md, squad_id, initial_agent_id, schedule_type, timezone, status, next_fire_at, creator_id, is_deleted, version, gmt_create, start_deadline_seconds)
VALUES (1, '{token}-once', 'jobdual', @squad, @agent, 'ONCE', 'Asia/Shanghai', 'ACTIVE', '{due}', 990000001, 0, 0, '{due}', 864000);
SET @task = LAST_INSERT_ID();
INSERT INTO scheduled_task (workspace_id, name, instruction_md, squad_id, initial_agent_id, schedule_type, timezone, status, next_fire_at, creator_id, is_deleted, version)
VALUES (1, '{token}-holder', 'jobdual', @squad, @agent, 'ONCE', 'Asia/Shanghai', 'ACTIVE', '{future}', 990000002, 0, 0);
SET @holder = LAST_INSERT_ID();
INSERT INTO scheduled_task_run (workspace_id, scheduled_task_id, trigger_key, trigger_type, scheduled_at, status, squad_id, initial_agent_id, session_mode, execution_snapshot_json, owner_id, creator_id, gmt_modified, version)
VALUES (1, @holder, '{token}-starting', 'SCHEDULED', '{due}', 'STARTING', @squad, @agent, 'ISOLATED', '{{}}', 990000002, 990000002, '{old_mod}', 0);
INSERT INTO executor_update_task (tenant_id, executor_id, request_id, target_version, status, next_attempt_at, is_deleted)
VALUES (1, 880002, '{token}-upd', '9.9.9', 'PENDING', '{old_mod}', 0);
INSERT INTO status_node (tenant_id, template_id, code, name, category)
VALUES (1, {template_id}, 'DONE', '完成', 'DONE');
SET @node = LAST_INSERT_ID();
INSERT INTO workitem (tenant_id, work_type, title, status_node_id, is_deleted, version)
VALUES (1, 'TASK', '{token}-cleanup', @node, 0, 0);
SET @item = LAST_INSERT_ID();
INSERT INTO workitem_event (tenant_id, workitem_id, event_type, to_val, gmt_create)
VALUES (1, @item, 'STATUS_CHANGE', 'DONE', '{old_cleanup}');
INSERT INTO dispatch (tenant_id, workitem_id, agent_id, executor_id, status, idempotency_key, is_deleted, version, gmt_modified)
VALUES (1, @item, 1, 880004, 'SUCCEEDED', '{token}-clean', 0, 0, '{recent}');
INSERT INTO dispatch (tenant_id, workitem_id, agent_id, executor_id, status, idempotency_key, is_deleted, version, gmt_modified)
VALUES (1, 880010, 1, 880005, 'PAUSING', '{token}-pause', 0, 0, '{old_dispatch}');
INSERT INTO dispatch (tenant_id, workitem_id, agent_id, executor_id, status, idempotency_key, is_deleted, version, gmt_modified)
VALUES (1, 880011, 1, 880006, 'RUNNING', '{token}-run', 0, 0, '{old_run}');
INSERT INTO org (name, active_name_key, owner_id, status, is_deleted)
VALUES ('{token}-disabled', '{token}-disabled', 1, 1, 0);
INSERT INTO org (name, active_name_key, owner_id, status, is_deleted)
VALUES ('{token}-deleted', NULL, 1, 0, 1);
"""


def _mysql(port: int, sql: str) -> list[list[str | None]]:
    defaults = _defaults_file()
    completed = subprocess.run(
        [
            "mysql",
            "--defaults-file=" + str(defaults),
            "-P",
            str(port),
            "autowonder",
            "-N",
            "-B",
            "-e",
            sql,
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    rows: list[list[str | None]] = []
    for line in completed.stdout.splitlines():
        if line != "":
            rows.append([_cell(item) for item in line.split("\t")])
    return rows


def _defaults_file() -> Path:
    path = Path("/tmp/jobdual-my.cnf")
    path.write_text("[client]\nuser=root\npassword=autowonder\nhost=127.0.0.1\n")
    path.chmod(0o600)
    return path


def _cell(value: str) -> str | None:
    if value == r"\N" or value == "NULL":
        return None
    return value


def _one(port: int, sql: str) -> list[str | None]:
    return _mysql(port, sql)[0]


def _facts(
    port: int,
    token: str,
    java_log: str,
    redis_port: int,
    redis_db: int,
) -> dict[str, object]:
    expired = _one(
        port,
        "SELECT status, password_hash, nickname FROM user WHERE username='" + token + "-expired'",
    )
    cooling = _one(
        port,
        "SELECT status, password_hash, nickname FROM user WHERE username='" + token + "-cooling'",
    )
    old_event = _mysql(
        port,
        "SELECT COUNT(*) FROM agent_conversation_turn_event WHERE payload_fragment='"
        + token
        + "-old'",
    )
    kept_event = _mysql(
        port,
        "SELECT COUNT(*) FROM agent_conversation_turn_event WHERE payload_fragment='"
        + token
        + "-recent'",
    )
    cards = _mysql(
        port,
        "SELECT request_id, status FROM agent_conversation_elicitation "
        "WHERE request_id IN ('" + token + "-old','" + token + "-recent') ORDER BY request_id",
    )
    sessions = _mysql(
        port,
        "SELECT cli_session_ref, status, error FROM ai_session WHERE cli_session_ref IN ('"
        + token
        + "-old','"
        + token
        + "-recent') ORDER BY cli_session_ref",
    )
    logs = _mysql(
        port,
        "SELECT object_key, status, error_message FROM debug_log WHERE object_key IN ('"
        + token
        + "/old','"
        + token
        + "/recent') ORDER BY object_key",
    )
    outbox = _mysql(
        port,
        "SELECT operation_key, status, last_error FROM integration_outbox "
        "WHERE operation_key IN ('" + token + "-dispatch','" + token + "-recovery') "
        "ORDER BY operation_key",
    )
    inbox = _one(
        port,
        "SELECT status, attempts, last_error FROM feishu_message_inbox WHERE message_id='"
        + token
        + "-msg'",
    )
    binding = _one(
        port,
        "SELECT id, status FROM dingtalk_robot_binding WHERE app_key='" + token + "-app'",
    )
    stream = _redis_get(redis_port, redis_db, "dingtalk:stream:status:" + binding[0])
    poll = _one(
        port,
        "SELECT last_success_at, last_error FROM external_project_binding "
        "WHERE external_project_id='" + token + "'",
    )
    task = _mysql(
        port,
        "SELECT t.status, r.status, r.error, r.trigger_type "
        "FROM scheduled_task t JOIN scheduled_task_run r ON r.scheduled_task_id=t.id "
        "WHERE t.name='" + token + "-once' AND r.trigger_key<>'" + token + "-starting'",
    )
    starting = _one(
        port,
        "SELECT status, error FROM scheduled_task_run WHERE trigger_key='" + token + "-starting'",
    )
    upgrade = _one(
        port,
        "SELECT status, next_attempt_at FROM executor_update_task WHERE request_id='"
        + token
        + "-upd'",
    )
    dispatches = _mysql(
        port,
        "SELECT idempotency_key, status, error FROM dispatch WHERE idempotency_key IN ('"
        + token
        + "-pause','"
        + token
        + "-run','"
        + token
        + "-clean') ORDER BY idempotency_key",
    )
    turn = _one(
        port,
        "SELECT t.status FROM agent_conversation_turn t "
        "JOIN agent_conversation c ON c.id=t.conversation_id "
        "WHERE c.channel_conversation_id='" + token + "-conv'",
    )
    cleanup = _mysql(
        port,
        "SELECT COUNT(*) FROM workitem w "
        "JOIN status_node sn ON sn.id=w.status_node_id "
        "JOIN workitem_event e ON e.workitem_id=w.id AND e.event_type='STATUS_CHANGE' "
        "AND UPPER(e.to_val)=UPPER(sn.code) "
        "JOIN dispatch d ON d.workitem_id=w.id AND d.is_deleted=0 AND d.executor_id IS NOT NULL "
        "WHERE w.title='" + token + "-cleanup' AND w.is_deleted=0 AND UPPER(sn.category)='DONE'",
    )
    workitem = _one(
        port,
        "SELECT w.id, w.version, d.executor_id FROM workitem w "
        "JOIN dispatch d ON d.workitem_id=w.id AND d.idempotency_key='"
        + token
        + "-clean' WHERE w.title='"
        + token
        + "-cleanup'",
    )
    marker = _redis_get(
        redis_port,
        redis_db,
        "workspace:cleanup:sent:"
        + str(workitem[2])
        + ":"
        + str(workitem[0])
        + ":"
        + str(workitem[1]),
    )
    active = _mysql(port, "SELECT COUNT(*) FROM org WHERE is_deleted=0")
    return {
        "expired": expired,
        "cooling": cooling,
        "oldEvent": old_event[0][0],
        "keptEvent": kept_event[0][0],
        "cards": cards,
        "sessions": sessions,
        "debugLogs": logs,
        "outbox": outbox,
        "inbox": inbox,
        "streamStatus": _stream_status(stream),
        "bindingStatus": binding[1],
        "poll": poll,
        "fired": task,
        "starting": starting,
        "upgrade": upgrade,
        "dispatches": dispatches,
        "turn": turn[0],
        "cleanupEligible": cleanup[0][0],
        "cleanupMarker": marker,
        "activeOrgs": active[0][0],
        "participationTenants": _tenants(java_log),
    }


def _stream_status(raw: str | None) -> str | None:
    if raw is None:
        return None
    marker = '"status":"'
    start = raw.find(marker)
    if start < 0:
        return raw
    start += len(marker)
    end = raw.find('"', start)
    return raw[start:end]


def _tenants(log: str) -> str | None:
    needle = "tenants="
    found = None
    for line in log.splitlines():
        at = line.find(needle)
        if at < 0:
            continue
        digits = []
        for char in line[at + len(needle) :]:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if digits:
            found = "".join(digits)
    return found


def _catalog(port: int, db: int) -> tuple[str | None, str | None]:
    return (
        _redis_get(port, db, "model-catalog:snapshot:qoder"),
        _redis_get(port, db, "model-catalog:snapshot:qodercn"),
    )


def _redis_get(port: int, db: int, key: str) -> str | None:
    completed = subprocess.run(
        ["redis-cli", "-p", str(port), "-n", str(db), "GET", key],
        capture_output=True,
        text=True,
    )
    text = completed.stdout.strip()
    if text == "" or text == "(nil)":
        return None
    return text


async def _run_python() -> list[str]:
    from autowonder.jobs.scheduler import RUNNERS

    handler = _Grab()
    logger = logging.getLogger("autowonder.jobs.sweeps")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    failures: list[str] = []
    for name, runner in RUNNERS.items():
        try:
            await runner()
        except Exception as error:
            failures.append(name + " " + type(error).__name__ + " " + str(error))
    _PYTHON_LOG.append("\n".join(handler.lines))
    return failures


_PYTHON_LOG: list[str] = []


class _Grab(logging.Handler):
    """留下参与度任务打出的租户数。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _run_java() -> str:
    specs = " ".join(_JOBS)
    log_path = Path("/tmp/jobdual-java.log")
    command = (
        "set -a; source /tmp/java-aone.env; set +a; "
        "export SERVER_PORT=7011; "
        "cd /tmp/alibabacloud-landing-zone/ai-sdlc/auto-wonder; "
        "exec java -Dloader.main=JobDual -Dloader.path=/tmp/jobdual "
        "-Dserver.port=7011 "
        "-Dautowonder.ai.worker-pool-size=1 "
        "-Dautowonder.workitem.scheduled-start.scanner-enabled=false "
        "-cp target/auto-wonder.jar org.springframework.boot.loader.PropertiesLauncher "
        + specs
        + " > /tmp/jobdual-java.log 2>&1"
    )
    try:
        subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=360,
        )
    except subprocess.TimeoutExpired:
        _stop_probe()
    return log_path.read_text(errors="replace")


def _stop_probe() -> None:
    completed = subprocess.run(
        ["ss", "-ltnp"],
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        if ":7011" not in line:
            continue
        marker = "pid="
        at = line.find(marker)
        if at < 0:
            continue
        digits = []
        for char in line[at + len(marker) :]:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if digits:
            subprocess.run(["kill", "".join(digits)], check=False)


def _checks(
    token: str,
    java: dict[str, object],
    python: dict[str, object],
    java_log: str,
    python_failures: list[str],
    java_done: datetime,
    python_done: datetime,
) -> list[dict[str, object]]:
    python["participationTenants"] = _tenants("\n".join(_PYTHON_LOG))
    java_view = _view(java, java_done)
    python_view = _view(python, python_done)
    same = java_view == python_view
    expected = java_view == _expected(token)
    java_jobs = _job_lines(java_log)
    checks: list[dict[str, object]] = [
        {"name": "outcomes", "pass": same, "note": "fixture rows match"},
        {"name": "expected", "pass": expected, "note": "rows show the job effects"},
        {
            "name": "pythonJobs",
            "pass": python_failures == [],
            "note": " ".join(python_failures),
        },
        {
            "name": "javaReady",
            "pass": "ENV server.port=7011" in java_log and "JOBDUAL CLOSED" in java_log,
            "note": "second context bound 7011 and exited",
        },
    ]
    for spec in _JOBS:
        ok = ("JOB " + spec + " OK") in java_jobs
        checks.append({"name": spec.rsplit(".", 1)[-1], "pass": ok, "note": spec})
    return checks


def _job_lines(log: str) -> str:
    lines = []
    for line in log.splitlines():
        if line.startswith("JOB ") or line.startswith("ENV ") or line.startswith("JOBDUAL"):
            lines.append(line)
    return "\n".join(lines)


def _view(facts: dict[str, object], done: datetime) -> dict[str, object]:
    upgrade = facts["upgrade"]
    upgrade_view = [upgrade[0], _soon(upgrade[1], done)]
    poll = facts["poll"]
    poll_view = [poll[0], _poll_failed(poll[1])]
    copied = {
        "expired": facts["expired"],
        "cooling": facts["cooling"],
        "oldEvent": facts["oldEvent"],
        "keptEvent": facts["keptEvent"],
        "cards": facts["cards"],
        "sessions": facts["sessions"],
        "debugLogs": facts["debugLogs"],
        "outbox": facts["outbox"],
        "inbox": facts["inbox"],
        "streamStatus": facts["streamStatus"],
        "bindingStatus": facts["bindingStatus"],
        "poll": poll_view,
        "fired": facts["fired"],
        "starting": facts["starting"],
        "upgrade": upgrade_view,
        "dispatches": facts["dispatches"],
        "turn": facts["turn"],
        "cleanupEligible": facts["cleanupEligible"],
        "cleanupMarker": facts["cleanupMarker"],
        "participationMatchesActive": facts["participationTenants"] == facts["activeOrgs"],
        "catalogUnchanged": facts["catalogUnchanged"],
    }
    return copied


def _soon(text: str | None, done: datetime) -> str:
    if text is None:
        return "missing"
    moment = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    delta = (moment - done).total_seconds()
    if 200 <= delta <= 400:
        return "later"
    return "delta=" + str(int(delta))


def _poll_failed(text: str | None) -> str:
    if text is None or text == "":
        return "empty"
    return "failed"


def _expected(token: str) -> dict[str, object]:
    """夹具跑完后应出现的效果。空错误在原始查询里是 \\N。"""
    return {
        "expired": ["1", "DEACTIVATED", None],
        "cooling": ["0", "hash-cooling", "keep"],
        "oldEvent": "0",
        "keptEvent": "1",
        "cards": [
            [token + "-old", "EXPIRED"],
            [token + "-recent", "PENDING"],
        ],
        "sessions": [
            [token + "-old", "FAILED", _STUCK_ERROR],
            [token + "-recent", "RUNNING", None],
        ],
        "debugLogs": [
            [token + "/old", "FAILED", _PENDING_TIMEOUT],
            [token + "/recent", "PENDING", None],
        ],
        "outbox": [
            [token + "-dispatch", "FAILED", _BIND_MISSING],
            [token + "-recovery", "UNKNOWN", _READBACK],
        ],
        "inbox": ["DONE", "1", None],
        "streamStatus": "FAILED",
        "bindingStatus": "ENABLED",
        "poll": [None, "failed"],
        "fired": [["PAUSED", "FAILED", _OWNER, "SCHEDULED"]],
        "starting": ["FAILED", _OWNER],
        "upgrade": ["PENDING", "later"],
        "dispatches": [
            [token + "-clean", "SUCCEEDED", None],
            [token + "-pause", "PAUSE_FAILED", _PAUSE_ERROR],
            [token + "-run", "TIMEOUT", "TIMEOUT"],
        ],
        "turn": "PROCESSING",
        "cleanupEligible": "1",
        "cleanupMarker": None,
        "participationMatchesActive": True,
        "catalogUnchanged": True,
    }
