"""Per-provider rights metadata and the deployment policy that decides which providers run.

The metadata records what fairserp's maintainers read in each provider's published terms
on the review date. It is not legal advice and does not override your own agreement with a
provider (an order form can grant or remove rights). Operators can tighten it per
deployment, for example by lowering storage limits.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum


class Provenance(StrEnum):
    OWN_INDEX = "own-index"  # the provider crawls and ranks its own index and says so
    OPEN_DATA = "open-data"  # openly licensed dataset with a published API
    AGGREGATOR = "aggregator"  # names the source of each item it returns
    UNDISCLOSED = "undisclosed"  # does not say where its results come from
    USER_OPERATED = "user-operated"  # an instance the operator runs; status follows its config


class CommercialUse(StrEnum):
    ALLOWED = "allowed"
    PER_AGREEMENT = "per-agreement"  # depends on the plan or order form you hold
    NONCOMMERCIAL = "noncommercial-only"
    UNSPECIFIED = "unspecified"


# Provenance classes the default "strict" profile allows without an explicit opt-in.
DISCLOSED = {Provenance.OWN_INDEX, Provenance.OPEN_DATA, Provenance.AGGREGATOR}


@dataclass(frozen=True)
class Rights:
    provenance: Provenance
    commercial_use: CommercialUse
    # Longest any copy of a result may be kept, in seconds. None: the terms set no limit.
    # 0: transient use only, never cached or archived.
    max_storage_seconds: int | None
    attribution: str | None
    terms_url: str
    terms_reviewed: str
    notes: str = ""

    def as_dict(self) -> dict:
        data = asdict(self)
        data["provenance"] = self.provenance.value
        data["commercial_use"] = self.commercial_use.value
        return data


class Profile(StrEnum):
    STRICT = "strict"  # disclosed provenance only, plus explicit opt-ins
    OPEN = "open"  # every configured engine


def policy_block(
    rights: Rights,
    engine: str,
    profile: Profile,
    opt_in: set[str],
    noncommercial_deployment: bool,
) -> str | None:
    """Why policy keeps this engine from running, or None when it may run."""
    if engine in opt_in or profile == Profile.OPEN:
        if rights.commercial_use == CommercialUse.NONCOMMERCIAL and not noncommercial_deployment:
            return (
                f"{engine} results are licensed for noncommercial use only; set "
                "FAIRSERP_NONCOMMERCIAL_DEPLOYMENT=true if this deployment is noncommercial"
            )
        return None
    if rights.provenance not in DISCLOSED:
        return (
            f"{engine} has {rights.provenance.value} provenance and the strict profile "
            f"excludes it; opt in with FAIRSERP_OPT_IN={engine}"
        )
    if rights.commercial_use == CommercialUse.NONCOMMERCIAL and not noncommercial_deployment:
        return (
            f"{engine} results are licensed for noncommercial use only; set "
            "FAIRSERP_NONCOMMERCIAL_DEPLOYMENT=true if this deployment is noncommercial"
        )
    return None
