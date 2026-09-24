"""用最新后验的 90% 可信上界判断要不要继续调查。"""

import math

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.evolution.jsontext import blank
from autowonder.evolution.models import EvolutionEvidence
from autowonder.evolution.store import find_latest_evidence

_Z_90 = 1.2815515655446004


async def check_trigger(
    session: AsyncSession,
    tenant_id: int,
    asset_type: str | None,
    asset_id: int | None,
    posterior_type: str | None,
    context_key: str | None,
    min_effective_sample_size: float | None,
    credible_upper_bound_below: float | None,
) -> dict[str, object]:
    """样本不足时不调查。缺省最少样本 5，上界阈值 0.30。"""
    if blank(asset_type) or asset_id is None or blank(posterior_type) or blank(context_key):
        raise BizError(ErrorCode.PARAM_INVALID)
    if asset_type is None or posterior_type is None or context_key is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    min_sample = 5.0
    if min_effective_sample_size is not None:
        min_sample = min_effective_sample_size
    threshold = 0.30
    if credible_upper_bound_below is not None:
        threshold = credible_upper_bound_below
    latest = await find_latest_evidence(
        session,
        tenant_id,
        asset_type,
        asset_id,
        posterior_type,
        context_key,
    )
    return _decision(latest, min_sample, threshold)


def credible_upper_bound_90(alpha: float, beta: float) -> float:
    """正态近似的 90% 可信上界，并截断到 1。"""
    total = alpha + beta
    if total <= 0:
        return 1.0
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1.0))
    spread = variance
    if spread < 0.0:
        spread = 0.0
    bound = mean + _Z_90 * math.sqrt(spread)
    if bound > 1.0:
        return 1.0
    return bound


def _decision(
    latest: EvolutionEvidence | None,
    min_sample: float,
    threshold: float,
) -> dict[str, object]:
    posterior_mean = None
    effective_sample_size = None
    upper = None
    if latest is not None:
        posterior_mean = latest.posterior_mean
        effective_sample_size = latest.effective_sample_size
        upper = credible_upper_bound_90(latest.alpha, latest.beta)
    return {
        "shouldInvestigate": _should_investigate(latest, min_sample, threshold),
        "posteriorMean": posterior_mean,
        "effectiveSampleSize": effective_sample_size,
        "credibleUpperBound90": upper,
    }


def _should_investigate(
    latest: EvolutionEvidence | None,
    min_sample: float,
    threshold: float,
) -> bool:
    if latest is None:
        return False
    if latest.effective_sample_size < min_sample:
        return False
    return credible_upper_bound_90(latest.alpha, latest.beta) < threshold
