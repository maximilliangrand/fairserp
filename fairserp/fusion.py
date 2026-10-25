"""Merging result lists from several providers.

Reciprocal Rank Fusion (Cormack et al., 2009): score(url) = sum over providers of
1 / (k + rank). It needs no score calibration across providers, which matters because
their scores are not comparable. Results are deduplicated by canonical URL; every
provider's own rank is kept in ``sources``.
"""

from fairserp.canonical import canonical_url
from fairserp.model import RankedResult, SourceRef, WebResult

RRF_K = 60


def rank_single(engine: str, provenance: str, results: list[WebResult],
                offset: int = 0) -> list[RankedResult]:
    """One provider's list as ranked results, dropping duplicate URLs within the list."""
    ranked: list[RankedResult] = []
    seen: set[str] = set()
    for position, result in enumerate(results, start=1):
        canonical = canonical_url(result.url)
        if canonical in seen:
            continue
        seen.add(canonical)
        ranked.append(RankedResult(
            **result.model_dump(), rank=offset + len(ranked) + 1, canonical_url=canonical,
            sources=[SourceRef(engine=engine, rank=offset + position, provenance=provenance)],
        ))
    return ranked


def rrf(lists: dict[str, tuple[str, list[WebResult]]], limit: int) -> list[RankedResult]:
    """Fuse ``{engine: (provenance, results)}`` into one deduplicated, ranked list."""
    scores: dict[str, float] = {}
    best: dict[str, WebResult] = {}
    sources: dict[str, list[SourceRef]] = {}
    for engine, (provenance, results) in lists.items():
        seen: set[str] = set()
        for rank, result in enumerate(results, start=1):
            canonical = canonical_url(result.url)
            if canonical in seen:
                continue
            seen.add(canonical)
            scores[canonical] = scores.get(canonical, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(canonical, []).append(
                SourceRef(engine=engine, rank=rank, provenance=provenance))
            current = best.get(canonical)
            if current is None or _richer(result, current):
                best[canonical] = result
    order = sorted(scores, key=lambda c: (-scores[c], min(s.rank for s in sources[c])))
    return [
        RankedResult(**best[c].model_dump(), rank=position, canonical_url=c, sources=sources[c])
        for position, c in enumerate(order[:limit], start=1)
    ]


def _richer(candidate: WebResult, current: WebResult) -> bool:
    """Prefer the copy with a longer snippet and a date."""
    return (len(candidate.snippet), bool(candidate.published_at)) > (
        len(current.snippet), bool(current.published_at))
