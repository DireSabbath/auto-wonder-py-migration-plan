"""仪表盘查询。与 ``DashboardDao.xml`` 的 source-aware 语句一致。

当前 schema 含 ``dispatch.source_type``，因此工单语义查询带上
``d.source_type = 'WORKITEM'``。全局调度计数不按来源过滤。
"""

FEED_LIMIT = 10

WORKITEM_DISPATCH_FENCE = "AND d.source_type = 'WORKITEM'"

END_TO_END_SUCCESSFUL_WORKITEMS = f"""
SELECT
    w.id,
    w.gmt_create,
    CASE
        WHEN sn.category = 'DONE' THEN w.gmt_modified
        ELSE latest_dispatch.gmt_modified
    END AS success_at
FROM workitem w
LEFT JOIN status_node sn
    ON w.status_node_id = sn.id AND sn.tenant_id = w.tenant_id
LEFT JOIN dispatch latest_dispatch ON latest_dispatch.id = (
    SELECT MAX(d.id)
    FROM dispatch d
    WHERE d.tenant_id = w.tenant_id
      {WORKITEM_DISPATCH_FENCE}
      AND d.workitem_id = w.id
      AND d.is_deleted = 0
)
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0
  AND (
      sn.category = 'DONE'
      OR (
          w.assignee_type = 'HUMAN'
          AND COALESCE(sn.category, '') <> 'DONE'
          AND latest_dispatch.status = 'SUCCEEDED'
      )
  )
"""

COUNT_RUNNING_DISPATCHES = """
SELECT COUNT(*) FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0 AND status = 'RUNNING'
"""

COUNT_TODAY_COMPLETED_TASKS = f"""
SELECT COUNT(*)
FROM (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) successful_workitem
WHERE successful_workitem.success_at >= CURDATE()
"""

COUNT_WEEK_COMPLETED_TASKS = f"""
SELECT COUNT(*)
FROM (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) successful_workitem
WHERE successful_workitem.success_at >= DATE_SUB(
    CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY
)
"""

AVG_TODAY_COMPLETED_TASK_DURATION = f"""
SELECT COALESCE(ROUND(AVG(TIMESTAMPDIFF(
    MINUTE, successful_workitem.gmt_create, successful_workitem.success_at
))), 0)
FROM (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) successful_workitem
WHERE successful_workitem.success_at >= CURDATE()
"""

COUNT_IN_PROGRESS_WORKITEMS = """
SELECT COUNT(*) FROM workitem w
JOIN status_node sn ON w.status_node_id = sn.id
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0
  AND sn.category = 'IN_PROGRESS'
"""

COUNT_QUEUED_DISPATCHES = """
SELECT COUNT(*) FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0
  AND status IN ('PENDING','PACKAGING','DISPATCHED','ACKED')
"""

COUNT_ONLINE_AGENTS = """
SELECT COUNT(*) FROM agent
WHERE tenant_id = :tenant_id AND is_deleted = 0 AND status = 'ONLINE'
"""

COUNT_ACTIVE_SQUADS = """
SELECT COUNT(DISTINCT sm.squad_id)
FROM squad_member sm
JOIN squad s ON s.id = sm.squad_id AND s.tenant_id = sm.tenant_id
JOIN dispatch d ON d.agent_id = sm.agent_id AND d.tenant_id = sm.tenant_id
WHERE sm.tenant_id = :tenant_id
  AND s.is_deleted = 0
  AND s.status = 0
  AND d.is_deleted = 0
  AND d.status = 'RUNNING'
"""

COUNT_WORKITEMS_BY_LIFECYCLE = """
SELECT sn.category AS category, COUNT(*) AS cnt
FROM workitem w
JOIN status_node sn ON w.status_node_id = sn.id
WHERE w.tenant_id = :tenant_id AND w.is_deleted = 0
GROUP BY sn.category
"""

COUNT_WORKITEMS_BY_TYPE = """
SELECT work_type AS workType, COUNT(*) AS cnt
FROM workitem
WHERE tenant_id = :tenant_id AND is_deleted = 0
GROUP BY work_type
"""

SQUAD_LINE_AGGREGATES = """
SELECT
    s.id AS squadId,
    s.name AS name,
    COUNT(DISTINCT sm.agent_id) AS members,
    COUNT(DISTINCT CASE WHEN a.status = 'ONLINE' THEN a.id END) AS online,
    COUNT(DISTINCT CASE WHEN d.status = 'RUNNING' THEN d.agent_id END) AS busy,
    COUNT(DISTINCT CASE WHEN d.status = 'RUNNING' THEN d.id END) AS runningTasks
FROM squad s
LEFT JOIN squad_member sm
    ON sm.squad_id = s.id AND sm.tenant_id = s.tenant_id
LEFT JOIN agent a
    ON a.id = sm.agent_id AND a.tenant_id = s.tenant_id AND a.is_deleted = 0
LEFT JOIN dispatch d
    ON d.agent_id = sm.agent_id AND d.tenant_id = s.tenant_id AND d.is_deleted = 0
WHERE s.tenant_id = :tenant_id AND s.is_deleted = 0 AND s.status = 0
GROUP BY s.id, s.name
ORDER BY runningTasks DESC, members DESC
"""

SQUAD_IN_PROGRESS_WORKITEMS = f"""
SELECT sm.squad_id AS squadId, COUNT(DISTINCT d.workitem_id) AS cnt
FROM squad_member sm
JOIN dispatch d
    ON d.agent_id = sm.agent_id AND d.tenant_id = sm.tenant_id AND d.is_deleted = 0
JOIN workitem w
    ON w.id = d.workitem_id AND w.tenant_id = sm.tenant_id AND w.is_deleted = 0
JOIN status_node sn ON w.status_node_id = sn.id
WHERE sm.tenant_id = :tenant_id
  {WORKITEM_DISPATCH_FENCE}
  AND sn.category = 'IN_PROGRESS'
GROUP BY sm.squad_id
"""

ONLINE_WORKSTATIONS = """
SELECT
    a.id AS agentId,
    a.name AS name,
    a.avatar_url AS avatarUrl,
    COUNT(DISTINCT CASE WHEN d.status = 'RUNNING' THEN d.id END) AS runningTasks
FROM agent a
LEFT JOIN dispatch d
    ON d.agent_id = a.id AND d.tenant_id = a.tenant_id AND d.is_deleted = 0
WHERE a.tenant_id = :tenant_id AND a.is_deleted = 0 AND a.status = 'ONLINE'
GROUP BY a.id, a.name, a.avatar_url
ORDER BY runningTasks DESC, a.name
"""

COUNT_TODAY_SUCCEEDED = """
SELECT COUNT(*) FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0
  AND status = 'SUCCEEDED' AND gmt_create >= CURDATE()
"""

COUNT_TODAY_FAILED_OR_TIMEOUT = """
SELECT COUNT(*) FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0
  AND status IN ('FAILED','TIMEOUT') AND gmt_create >= CURDATE()
"""

COUNT_TODAY_RETRIES = """
SELECT COUNT(*) FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0
  AND attempt > 0 AND gmt_create >= CURDATE()
"""

AVG_TODAY_SUCCESS_DURATION = """
SELECT COALESCE(AVG(TIMESTAMPDIFF(MINUTE, gmt_create, gmt_modified)), 0)
FROM dispatch
WHERE tenant_id = :tenant_id AND is_deleted = 0
  AND status = 'SUCCEEDED' AND gmt_create >= CURDATE()
"""

LIST_RUNNING_FEED = f"""
SELECT
    d.id AS dispatchId,
    d.agent_id AS agentId,
    a.name AS agentName,
    d.workitem_id AS workitemId,
    w.title AS workitemTitle,
    st.name AS stepName,
    TIMESTAMPDIFF(MINUTE, d.gmt_modified, NOW()) AS runningMinutes
FROM dispatch d
LEFT JOIN agent a ON a.id = d.agent_id AND a.tenant_id = d.tenant_id
LEFT JOIN workitem w ON w.id = d.workitem_id AND w.tenant_id = d.tenant_id
LEFT JOIN sdlc_step st ON st.id = d.sdlc_step_id
WHERE d.tenant_id = :tenant_id
  {WORKITEM_DISPATCH_FENCE}
  AND d.is_deleted = 0 AND d.status = 'RUNNING'
ORDER BY d.gmt_modified ASC
LIMIT :limit
"""

LIST_RECENT_FEED = f"""
SELECT
    d.id AS dispatchId,
    a.name AS agentName,
    w.title AS workitemTitle,
    d.status AS status,
    TIMESTAMPDIFF(MINUTE, d.gmt_create, d.gmt_modified) AS durationMinutes,
    DATE_FORMAT(d.gmt_modified, '%Y-%m-%d %H:%i:%s') AS finishedAt
FROM dispatch d
LEFT JOIN agent a ON a.id = d.agent_id AND a.tenant_id = d.tenant_id
LEFT JOIN workitem w ON w.id = d.workitem_id AND w.tenant_id = d.tenant_id
WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
  {WORKITEM_DISPATCH_FENCE}
  AND d.status IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELED')
ORDER BY d.gmt_modified DESC
LIMIT :limit
"""

LIST_TODAY_COMPLETED_WORKITEMS = f"""
SELECT
    successful_workitem.id AS workitemId,
    w.title AS title
FROM (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) successful_workitem
JOIN workitem w ON w.id = successful_workitem.id
WHERE successful_workitem.success_at >= CURDATE()
ORDER BY successful_workitem.success_at DESC
"""

LIST_WEEK_COMPLETED_WORKITEMS = f"""
SELECT
    successful_workitem.id AS workitemId,
    w.title AS title
FROM (
    {END_TO_END_SUCCESSFUL_WORKITEMS}
) successful_workitem
JOIN workitem w ON w.id = successful_workitem.id
WHERE successful_workitem.success_at >= DATE_SUB(
    CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY
)
ORDER BY successful_workitem.success_at DESC
"""

LIST_RUNNING_WORKITEMS = f"""
SELECT
    d.id AS dispatchId,
    d.agent_id AS agentId,
    a.name AS agentName,
    d.workitem_id AS workitemId,
    w.title AS workitemTitle,
    st.name AS stepName,
    TIMESTAMPDIFF(MINUTE, d.gmt_modified, NOW()) AS runningMinutes
FROM dispatch d
LEFT JOIN agent a ON a.id = d.agent_id AND a.tenant_id = d.tenant_id
LEFT JOIN workitem w ON w.id = d.workitem_id AND w.tenant_id = d.tenant_id
LEFT JOIN sdlc_step st ON st.id = d.sdlc_step_id
WHERE d.tenant_id = :tenant_id
  {WORKITEM_DISPATCH_FENCE}
  AND d.is_deleted = 0 AND d.status = 'RUNNING'
ORDER BY d.gmt_modified ASC
"""

LIST_AGENT_RUNNING = f"""
SELECT
    d.id AS dispatchId,
    d.agent_id AS agentId,
    a.name AS agentName,
    d.workitem_id AS workitemId,
    w.title AS workitemTitle,
    st.name AS stepName,
    TIMESTAMPDIFF(MINUTE, d.gmt_modified, NOW()) AS runningMinutes
FROM dispatch d
LEFT JOIN agent a ON a.id = d.agent_id AND a.tenant_id = d.tenant_id
LEFT JOIN workitem w ON w.id = d.workitem_id AND w.tenant_id = d.tenant_id
LEFT JOIN sdlc_step st ON st.id = d.sdlc_step_id
WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
  {WORKITEM_DISPATCH_FENCE}
  AND d.status = 'RUNNING' AND d.agent_id = :agent_id
ORDER BY d.gmt_modified ASC
"""

AGENT_EXISTS = """
SELECT COUNT(*) FROM agent
WHERE tenant_id = :tenant_id AND id = :agent_id AND is_deleted = 0
"""

WORKITEM_FENCED_SQL = (
    END_TO_END_SUCCESSFUL_WORKITEMS,
    COUNT_TODAY_COMPLETED_TASKS,
    COUNT_WEEK_COMPLETED_TASKS,
    AVG_TODAY_COMPLETED_TASK_DURATION,
    SQUAD_IN_PROGRESS_WORKITEMS,
    LIST_RUNNING_FEED,
    LIST_RECENT_FEED,
    LIST_TODAY_COMPLETED_WORKITEMS,
    LIST_WEEK_COMPLETED_WORKITEMS,
    LIST_RUNNING_WORKITEMS,
    LIST_AGENT_RUNNING,
)

SOURCE_AGNOSTIC_SQL = (
    COUNT_RUNNING_DISPATCHES,
    COUNT_QUEUED_DISPATCHES,
    COUNT_TODAY_SUCCEEDED,
    COUNT_TODAY_FAILED_OR_TIMEOUT,
    COUNT_TODAY_RETRIES,
    AVG_TODAY_SUCCESS_DURATION,
    COUNT_ACTIVE_SQUADS,
    SQUAD_LINE_AGGREGATES,
    ONLINE_WORKSTATIONS,
)
