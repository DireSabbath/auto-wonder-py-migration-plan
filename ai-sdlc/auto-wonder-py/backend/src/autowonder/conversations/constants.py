"""会话渠道、状态和协议能力名，与 Java 常量逐字一致。"""

PLATFORM_CHANNEL = "PLATFORM_ASSISTANT"
CLARIFICATION_CHANNEL = "WORKITEM_CLARIFICATION"
INTERNAL_CHANNEL = "PLATFORM_INTERNAL"
BIZ_REF_WORKITEM = "WORKITEM"
PERMISSION_READ = "READ"

STATUS_ACTIVE = "ACTIVE"
STATUS_PROCESSING = "PROCESSING"
STATUS_QUEUED = "QUEUED"
STATUS_CANCELED = "CANCELED"
DIRECTION_IN = "IN"
DIRECTION_OUT = "OUT"

TITLE_SOURCE_USER = "USER"
TITLE_SOURCE_AUTO = "AUTO"
MAX_TITLE_LENGTH = 255
AUTO_TITLE_LENGTH = 30
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

TURN_EVENT = "CONVERSATION_TURN_EVENT"
TURN_CANCEL = "CONVERSATION_TURN_CANCEL"
ACP_INTERACTION = "CONVERSATION_ACP_INTERACTION_V1"
ATTACHMENT_MANIFEST_V1 = "ATTACHMENT_MANIFEST_V1"
ARTIFACT_OUTPUT_V1 = "ARTIFACT_OUTPUT_V1"
ACTION_PLAN_V1 = "ACTION_PLAN_V1"
AGENT_ENVIRONMENT_VARIABLES_V1 = "AGENT_ENVIRONMENT_VARIABLES_V1"

EVENT_PAGE_LIMIT = 200
MAX_TURN_EVENTS = 5000
MAX_TURN_ERROR_LENGTH = 1024
MAX_ELICITATION_MESSAGE_LENGTH = 1024
CANCELED_FALLBACK_CONTENT = "响应已终止"
LOCK_TIMEOUT_SECONDS = 10

SNAPSHOT_KEY_PREFIX = "autowonder:clarification:commands:v1:"
PROBE_LOCK_PREFIX = "autowonder:clarification:commands:lock:v1:"
SNAPSHOT_TTL_SEC = 3600
PROBE_LOCK_TTL_SEC = 30
ROUND_ROBIN_TTL_SEC = 7 * 24 * 60 * 60

API_MODE_SUFFIX = (
    "\n\n重要:你在API模式下运行,不能使用AskUserQuestion等交互工具。"
    "直接用文字提问和回复。"
)
PLATFORM_PROMPT_SUFFIX = (
    "\n\n当前是平台管家对话，与你交谈的就是这个会话的唯一 Owner。"
    "查询、读取、汇总类请求直接完成，不要反复确认。"
    "任何会创建、修改、删除平台数据的操作，必须先调用"
    " autowonder.propose_platform_actions 产出参数冻结的一次性行动计划，"
    "把目标、参数、影响面和执行步骤讲清楚，等 Owner 显式确认后再调用"
    " autowonder.execute_platform_action_plan；未确认前不得执行任何写操作，"
    "也不得自行放宽或替换计划里的参数。"
    "Owner 选中的文件读取失败时必须如实报告，禁止静默忽略后继续作答。"
    "只做工程可视化配图，不做通用艺术文生图。"
)
CLARIFICATION_PROMPT_SUFFIX = (
    "\n\n当前是工单需求澄清会话。遵循身份配置完成澄清；仅在用户明确确认最终方案后上传产物。"
    "用户要求重写时，先清理本次澄清上传的旧产物，再上传新版。"
)
