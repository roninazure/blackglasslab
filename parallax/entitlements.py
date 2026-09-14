from dataclasses import dataclass
from enum import StrEnum


class Plan(StrEnum):
    EXPLORER = "explorer"
    PRO = "pro"
    EDGE = "edge"
    API = "api"
    INSTITUTIONAL = "institutional"


class Feature(StrEnum):
    DETAILS = "details"
    ADVANCED_FILTERS = "advanced_filters"
    ALERTS = "alerts"
    API_ACCESS = "api_access"


@dataclass(frozen=True)
class Entitlement:
    plan: Plan
    play_limit: int
    features: frozenset[Feature]

    def permits(self, feature: Feature) -> bool:
        return feature in self.features


def entitlement(plan: Plan) -> Entitlement:
    if plan == Plan.EXPLORER:
        return Entitlement(plan, 5, frozenset())
    if plan == Plan.PRO:
        return Entitlement(
            plan,
            1000,
            frozenset({Feature.DETAILS, Feature.ADVANCED_FILTERS, Feature.ALERTS}),
        )
    if plan == Plan.API:
        return Entitlement(plan, 10000, frozenset(Feature))
    # Reserved identifiers do not silently grant unlaunched access.
    return Entitlement(plan, 0, frozenset())


PUBLIC_PLANS = (
    {"name": "EXPLORER", "price": "Free", "plan": "explorer"},
    {"name": "PRO", "price": "$49/month", "plan": "pro"},
    {"name": "ENTERPRISE / API", "price": "Contact Sales", "plan": "api"},
)
