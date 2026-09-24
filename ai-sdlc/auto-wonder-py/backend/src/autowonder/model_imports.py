"""导入全部领域模型，使 SQLAlchemy 元数据与 tenant 注册完整。"""

from autowonder.agents import models as agents_models
from autowonder.ai import models as ai_models
from autowonder.aiusage import models as aiusage_models
from autowonder.artifacts import models as artifacts_models
from autowonder.audits import models as audits_models
from autowonder.backups import models as backups_models
from autowonder.categories import models as categories_models
from autowonder.clarifications import models as clarifications_models
from autowonder.conversations import models as conversations_models
from autowonder.debuglogs import models as debuglogs_models
from autowonder.dispatch import models as dispatch_models
from autowonder.environments import models as environments_models
from autowonder.evolution import models as evolution_models
from autowonder.executors import models as executors_models
from autowonder.im import models as im_models
from autowonder.integrations import models as integrations_models
from autowonder.mcp import models as mcp_models
from autowonder.memories import models as memories_models
from autowonder.notifications import models as notifications_models
from autowonder.platform import models as platform_models
from autowonder.repos import models as repos_models
from autowonder.scheduledtasks import models as scheduledtasks_models
from autowonder.sdlcs import models as sdlcs_models
from autowonder.settings import models as settings_models
from autowonder.skills import models as skills_models
from autowonder.squads import models as squads_models
from autowonder.statemachines import models as statemachines_models
from autowonder.users import models as users_models
from autowonder.workitems import models as workitems_models
from autowonder.workspaces import models as workspaces_models

MODEL_MODULES = (
    agents_models,
    ai_models,
    aiusage_models,
    artifacts_models,
    audits_models,
    backups_models,
    categories_models,
    clarifications_models,
    conversations_models,
    debuglogs_models,
    dispatch_models,
    environments_models,
    evolution_models,
    executors_models,
    im_models,
    integrations_models,
    mcp_models,
    memories_models,
    notifications_models,
    platform_models,
    repos_models,
    scheduledtasks_models,
    sdlcs_models,
    settings_models,
    skills_models,
    squads_models,
    statemachines_models,
    users_models,
    workitems_models,
    workspaces_models,
)
