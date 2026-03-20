from datetime import date, timedelta
from decimal import Decimal

from src.shared.schemas.budget import EntityType, PeriodType

from .config import budget_config


def calculate_model_cost(model_name: str, tokens_in: int, tokens_out: int) -> tuple[Decimal, Decimal, Decimal]:
    """
    Calculate the cost for a model based on input and output tokens.

    Returns:
        Tuple of (total_cost, input_cost_per_1k, output_cost_per_1k)
    """
    pricing = budget_config.model_pricing.get(model_name, budget_config.model_pricing["default"])

    input_cost_per_1k = pricing["input"]
    output_cost_per_1k = pricing["output"]

    # Calculate costs (pricing is per 1000 tokens)
    input_cost = (Decimal(tokens_in) / Decimal("1000")) * input_cost_per_1k
    output_cost = (Decimal(tokens_out) / Decimal("1000")) * output_cost_per_1k
    total_cost = input_cost + output_cost

    # Round to 4 decimal places for precision
    return round(total_cost, 4), input_cost_per_1k, output_cost_per_1k


def get_period_start_end(period_type: PeriodType, reference_date: date = None) -> tuple[date, date]:
    """
    Get the start and end dates for a budget period.

    Args:
        period_type: The type of period (daily, weekly, monthly)
        reference_date: The reference date (defaults to today)

    Returns:
        Tuple of (period_start, period_end)
    """
    if reference_date is None:
        reference_date = date.today()

    if period_type == PeriodType.DAILY:
        return reference_date, reference_date

    elif period_type == PeriodType.WEEKLY:
        # Week starts on Monday (0) and ends on Sunday (6)
        days_since_monday = reference_date.weekday()
        week_start = reference_date - timedelta(days=days_since_monday)
        week_end = week_start + timedelta(days=6)
        return week_start, week_end

    elif period_type == PeriodType.MONTHLY:
        # Month starts on the 1st and ends on the last day
        month_start = reference_date.replace(day=1)
        # Get the last day of the month
        if reference_date.month == 12:
            next_month = reference_date.replace(year=reference_date.year + 1, month=1, day=1)
        else:
            next_month = reference_date.replace(month=reference_date.month + 1, day=1)
        month_end = next_month - timedelta(days=1)
        return month_start, month_end

    else:
        raise ValueError(f"Unsupported period type: {period_type}")


def calculate_budget_utilization(budget_amount: Decimal, current_spend: Decimal) -> float:
    """Calculate budget utilization percentage."""
    if budget_amount <= 0:
        return 0.0
    return float((current_spend / budget_amount) * 100)


def is_budget_exceeded(budget_amount: Decimal, current_spend: Decimal) -> bool:
    """Check if budget is exceeded."""
    return current_spend >= budget_amount


def generate_budget_warnings(budget_amount: Decimal, current_spend: Decimal, period_type: PeriodType, entity_type: EntityType) -> list[str]:
    """Generate warning messages based on budget status."""
    warnings = []
    utilization = calculate_budget_utilization(budget_amount, current_spend)

    if utilization >= 100:
        warnings.append(f"Budget exceeded: {utilization:.1f}% of {period_type.value} budget used")
    elif utilization >= budget_config.budget_critical_threshold_percent:
        warnings.append(f"Budget critical: {utilization:.1f}% of {period_type.value} budget used")
    elif utilization >= budget_config.budget_warning_threshold_percent:
        warnings.append(f"Budget warning: {utilization:.1f}% of {period_type.value} budget used")

    return warnings


def get_entity_hierarchy_order() -> list[EntityType]:
    """Get the entity types in hierarchical order (most specific to most general)."""
    return [
        EntityType.USER,
        EntityType.SERVICE_ACCOUNT,
        EntityType.TEAM,
        EntityType.DEPARTMENT,
        EntityType.ORGANIZATION,
    ]


def get_parent_entity_info(
    entity_type: EntityType, user_id: str = None, service_account_id: str = None, team_id: str = None, department_id: str = None, org_id: str = None
) -> list[tuple[EntityType, str]]:
    """
    Get parent entities for hierarchical budget checking.

    Returns a list of (entity_type, entity_id) tuples in order from child to parent.
    """
    entities = []

    if entity_type == EntityType.USER and user_id:
        entities.append((EntityType.USER, user_id))
        if team_id:
            entities.append((EntityType.TEAM, team_id))
        if department_id:
            entities.append((EntityType.DEPARTMENT, department_id))
        if org_id:
            entities.append((EntityType.ORGANIZATION, org_id))

    elif entity_type == EntityType.SERVICE_ACCOUNT and service_account_id:
        entities.append((EntityType.SERVICE_ACCOUNT, service_account_id))
        if team_id:
            entities.append((EntityType.TEAM, team_id))
        if department_id:
            entities.append((EntityType.DEPARTMENT, department_id))
        if org_id:
            entities.append((EntityType.ORGANIZATION, org_id))

    elif entity_type == EntityType.TEAM and team_id:
        entities.append((EntityType.TEAM, team_id))
        if department_id:
            entities.append((EntityType.DEPARTMENT, department_id))
        if org_id:
            entities.append((EntityType.ORGANIZATION, org_id))

    elif entity_type == EntityType.DEPARTMENT and department_id:
        entities.append((EntityType.DEPARTMENT, department_id))
        if org_id:
            entities.append((EntityType.ORGANIZATION, org_id))

    elif entity_type == EntityType.ORGANIZATION and org_id:
        entities.append((EntityType.ORGANIZATION, org_id))

    return entities


def format_currency(amount: Decimal) -> str:
    """Format a decimal amount as currency."""
    return f"${amount:.2f}"


def validate_budget_amount(amount: Decimal) -> bool:
    """Validate that a budget amount is reasonable."""
    # Budget must be positive and not exceed $1 million
    return Decimal("0") < amount <= Decimal("1000000")


def get_model_names() -> list[str]:
    """Get list of supported model names."""
    return [name for name in budget_config.model_pricing.keys() if name != "default"]
