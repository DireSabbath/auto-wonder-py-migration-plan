"""洞察查询。语句与 ``InsightsDao.xml``、``MemberDeliveryDao.xml`` 一致。"""

from autowonder.dashboards.sql import END_TO_END_SUCCESSFUL_WORKITEMS, WORKITEM_DISPATCH_FENCE

RISK_CASE = """
CASE
    WHEN al.action IN ('DELETE', 'FORCE_PUBLISH', 'ROLE_CHANGE') THEN 'high'
    WHEN al.action IN ('UPDATE', 'REJECT', 'RETRY') THEN 'medium'
    ELSE 'low'
END
"""


def usage_since(column_sql: str, agent_sql: str) -> str:
    """用量聚合。数字员工条件只在传入 id 时出现。"""
    return (
        "SELECT "
        + column_sql
        + " FROM dispatch_ai_usage WHERE tenant_id = :tenant_id"
        + " AND usage_at >= :since"
        + agent_sql
    )


COUNT_WORKITEMS = """
SELECT COUNT(*) FROM workitem
WHERE tenant_id = :tenant_id AND is_deleted = 0 AND gmt_create >= :since
"""

COUNT_COMPLETED_WORKITEMS = """
SELECT COUNT(*) FROM workitem w
JOIN status_node sn ON w.status_node_id = sn.id
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0 AND w.gmt_create >= :since
  AND sn.category = 'DONE'
"""


def count_usage_workitems(agent_sql: str) -> str:
    """有用量记录的工单数。"""
    return (
        "SELECT COUNT(DISTINCT workitem_id) FROM dispatch_ai_usage"
        " WHERE tenant_id = :tenant_id AND usage_at >= :since" + agent_sql
    )


def avg_dispatch_minutes(agent_sql: str) -> str:
    """成功调度的平均分钟数。"""
    return (
        "SELECT COALESCE(AVG(TIMESTAMPDIFF(MINUTE, gmt_create, gmt_modified)), 0)"
        " FROM dispatch WHERE tenant_id = :tenant_id AND status = 'SUCCEEDED'"
        " AND gmt_create >= :since AND is_deleted = 0" + agent_sql
    )


def count_dispatches(extra_sql: str, agent_sql: str) -> str:
    """调度计数。额外状态条件来自固定 SQL 片段。"""
    return (
        "SELECT COUNT(*) FROM dispatch WHERE tenant_id = :tenant_id"
        " AND gmt_create >= :since AND is_deleted = 0" + extra_sql + agent_sql
    )


COUNT_HIGH_RISK_AUDITS = """
SELECT COUNT(*) FROM audit_log
WHERE tenant_id = :tenant_id AND gmt_create >= :since
  AND action IN ('DELETE', 'FORCE_PUBLISH', 'ROLE_CHANGE', 'PERMISSION_GRANT')
"""

COUNT_AUDIT_LOGS = """
SELECT COUNT(*) FROM audit_log
WHERE tenant_id = :tenant_id AND gmt_create >= :since
"""

COUNT_AUDIT_BLOCKS = """
SELECT COUNT(*) FROM audit_log
WHERE tenant_id = :tenant_id AND gmt_create >= :since
  AND action IN ('BLOCK', 'REJECT', 'DENY')
"""


def daily_trend(value_sql: str, alias: str, agent_sql: str) -> str:
    """最近 7 个有数据的日期，新的在前。"""
    return (
        "SELECT DATE(usage_at) AS day, "
        + value_sql
        + " AS "
        + alias
        + " FROM dispatch_ai_usage WHERE tenant_id = :tenant_id AND usage_at >= :since"
        + agent_sql
        + " GROUP BY DATE(usage_at) ORDER BY day DESC LIMIT 7"
    )


def audit_items(risk_sql: str, worker_sql: str) -> str:
    """审计明细。时间窗用数据库的 NOW()。"""
    return (
        "SELECT DATE_FORMAT(al.gmt_create, '%Y-%m-%d %H:%i:%s') AS timestamp,"
        " COALESCE(a.name, CONCAT('user-', al.actor_id)) AS worker,"
        " al.action AS eventType,"
        " COALESCE(JSON_UNQUOTE(JSON_EXTRACT(al.detail_json, '$.summary')),"
        " CONCAT(al.target_type, '#', al.target_id)) AS detail,"
        + RISK_CASE
        + " AS riskLevel"
        + " FROM audit_log al"
        + " LEFT JOIN agent a ON al.actor_id = a.id AND al.tenant_id = a.tenant_id"
        + " WHERE al.tenant_id = :tenant_id"
        + " AND al.gmt_create >= DATE_SUB(NOW(), INTERVAL :days DAY)"
        + risk_sql
        + worker_sql
        + " ORDER BY al.gmt_create DESC LIMIT :limit OFFSET :offset"
    )


def count_audit_items(risk_sql: str, worker_sql: str) -> str:
    """审计明细条数。"""
    return (
        "SELECT COUNT(*) FROM audit_log al"
        " LEFT JOIN agent a ON al.actor_id = a.id AND al.tenant_id = a.tenant_id"
        " WHERE al.tenant_id = :tenant_id"
        " AND al.gmt_create >= DATE_SUB(NOW(), INTERVAL :days DAY)" + risk_sql + worker_sql
    )


LIST_ACTIVE_WORKERS = """
SELECT DISTINCT a.id AS id, a.name AS name
FROM agent a
JOIN dispatch d ON a.id = d.agent_id AND a.tenant_id = d.tenant_id
WHERE a.tenant_id = :tenant_id AND a.is_deleted = 0 AND d.is_deleted = 0
ORDER BY a.name
"""

LIST_MEMBERS = """
SELECT m.user_id AS memberId,
       COALESCE(NULLIF(u.nickname, ''), u.username, CONCAT('成员 #', m.user_id)) AS memberName
FROM org_member m
LEFT JOIN user u ON u.id = m.user_id
WHERE m.tenant_id = :tenant_id AND m.is_deleted = 0 AND m.status = 0
"""

MEMBER_COUNTS = f"""
SELECT CASE WHEN w.assignee_type = 'HUMAN' THEN w.assignee_ref ELSE w.assign_operator_id END
         AS memberId,
       COUNT(*) AS total,
       SUM(CASE WHEN success.success_at >= :start AND success.success_at < :end THEN 1 ELSE 0 END)
         AS completed,
       SUM(CASE WHEN sn.category = 'IN_PROGRESS' AND success.id IS NULL THEN 1 ELSE 0 END)
         AS inProgress,
       SUM(CASE WHEN w.work_type = 'REQ' AND success.success_at >= :start
                 AND success.success_at < :end THEN 1 ELSE 0 END) AS requirements
FROM workitem w
LEFT JOIN status_node sn ON sn.id = w.status_node_id AND sn.tenant_id = w.tenant_id
LEFT JOIN (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) success ON success.id = w.id
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0 AND (
    (w.gmt_create >= :start AND w.gmt_create < :end)
    OR (success.success_at >= :start AND success.success_at < :end)
    OR EXISTS (
        SELECT 1 FROM workitem_event e
        WHERE e.tenant_id = w.tenant_id AND e.workitem_id = w.id
          AND e.gmt_create >= :start AND e.gmt_create < :end
    )
    OR EXISTS (
        SELECT 1 FROM dispatch d
        WHERE d.tenant_id = w.tenant_id AND d.workitem_id = w.id AND d.is_deleted = 0
          {WORKITEM_DISPATCH_FENCE}
          AND d.gmt_create < :end
          AND (
              d.gmt_modified >= :start
              OR d.status IN ('PENDING','PACKAGING','DISPATCHED','ACKED','RUNNING','PAUSING')
          )
    )
)
GROUP BY CASE WHEN w.assignee_type = 'HUMAN' THEN w.assignee_ref ELSE w.assign_operator_id END
"""

PARTICIPATION_EVENTS = """
SELECT w.id AS workitemId, w.title AS title, w.gmt_create AS workitemCreatedAt,
       e.id AS eventId, e.event_type AS eventType, e.to_val AS toVal,
       CASE
           WHEN e.event_type = 'ASSIGN' AND target_agent.id IS NOT NULL THEN 'AGENT'
           WHEN e.event_type = 'ASSIGN' AND e.to_val REGEXP '^[0-9]+$' THEN 'HUMAN'
           ELSE NULL
       END AS inferredToType,
       e.detail_json AS detailJson, e.gmt_create AS eventAt,
       COALESCE(sn.category = 'DONE', FALSE) AS terminal
FROM workitem w
JOIN workitem_event e ON e.tenant_id = w.tenant_id AND e.workitem_id = w.id
LEFT JOIN status_node sn ON sn.template_id = w.template_id AND sn.code = e.to_val
LEFT JOIN agent target_agent
  ON e.event_type = 'ASSIGN'
 AND e.to_val REGEXP '^[0-9]+$'
 AND target_agent.tenant_id = w.tenant_id
 AND target_agent.id = CAST(e.to_val AS UNSIGNED)
 AND target_agent.is_deleted = 0
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0
  AND e.gmt_create < :cutoff
  AND e.event_type IN ('CREATE', 'ASSIGN', 'STATUS_CHANGE')
ORDER BY w.id, e.gmt_create, e.id
LIMIT :limit OFFSET :offset
"""
