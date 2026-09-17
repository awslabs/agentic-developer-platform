"""Analysis service for insight generation and research proposals (US-G3).

Reviews scanner findings, groups them into themes, and generates
actionable research proposals with cost estimates and experiment plans.
"""

import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_finding import ResearchFinding
from app.models.research_proposal import ResearchProposal, STATUS_TRANSITIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Theme definitions — maps tag clusters to research themes
# ---------------------------------------------------------------------------

THEME_DEFINITIONS: dict[str, dict[str, Any]] = {
    "new-model-deployment": {
        "tags": ["new-model", "inference"],
        "title_template": "Deploy {model_name} on {gpu_type}",
        "objective_template": (
            "Validate whether {gpu_type} can serve {model_name} at acceptable "
            "latency for production workloads"
        ),
        "hypothesis_template": (
            "{gpu_type} with {vram}GB VRAM should fit the {quant_type} variant "
            "of {model_name} and achieve <{latency_target}ms p99 latency"
        ),
        "resource_template": "1x {gpu_type}, 256GB disk",
        "base_cost_per_hour": 1.50,
        "default_duration_hours": 5,
    },
    "inference-optimization": {
        "tags": ["inference", "quantization"],
        "title_template": "Benchmark {technique} for {model_name}",
        "objective_template": (
            "Measure throughput and latency improvements from {technique} "
            "on {model_name}"
        ),
        "hypothesis_template": (
            "{technique} should improve throughput by {improvement_pct}% "
            "while maintaining output quality within acceptable limits"
        ),
        "resource_template": "1x {gpu_type}, 128GB disk",
        "base_cost_per_hour": 2.00,
        "default_duration_hours": 8,
    },
    "gpu-cost-comparison": {
        "tags": ["gpu-hardware", "pricing"],
        "title_template": "Cost-performance analysis: {gpu_a} vs {gpu_b}",
        "objective_template": (
            "Compare $/token cost between {gpu_a} and {gpu_b} for "
            "representative inference workloads"
        ),
        "hypothesis_template": (
            "{gpu_a} should offer better $/token for {workload_type} workloads "
            "despite higher per-hour pricing"
        ),
        "resource_template": "1x {gpu_a}, 1x {gpu_b}, 256GB disk each",
        "base_cost_per_hour": 4.00,
        "default_duration_hours": 10,
    },
    "competitor-analysis": {
        "tags": ["competitor"],
        "title_template": "Replicate {competitor} capability: {feature}",
        "objective_template": (
            "Determine if Superplane can match or exceed {competitor}'s "
            "{feature} capability using existing infrastructure"
        ),
        "hypothesis_template": (
            "Using SkyPilot + existing GPU fleet, we can achieve comparable "
            "performance to {competitor}'s {feature}"
        ),
        "resource_template": "Existing workspace resources",
        "base_cost_per_hour": 1.00,
        "default_duration_hours": 4,
    },
    "training-benchmark": {
        "tags": ["training"],
        "title_template": "Fine-tuning benchmark: {model_name} on {gpu_type}",
        "objective_template": (
            "Benchmark fine-tuning {model_name} on {gpu_type} to establish "
            "baseline throughput and cost metrics"
        ),
        "hypothesis_template": (
            "{gpu_type} should complete LoRA fine-tuning of {model_name} "
            "within {duration}h at estimated cost ${cost}"
        ),
        "resource_template": "1x {gpu_type}, 512GB disk, 128GB RAM",
        "base_cost_per_hour": 3.00,
        "default_duration_hours": 12,
    },
}

# Default GPU types for experiment proposals
DEFAULT_GPU_TYPES = ["L40S", "A100", "H100"]
DEFAULT_QUANT_TYPES = ["FP8", "GPTQ-4bit", "AWQ-4bit"]
DEFAULT_LATENCY_TARGETS = {"L40S": 200, "A100": 100, "H100": 50}
DEFAULT_VRAM = {"L40S": 48, "A100": 80, "H100": 80}


# ---------------------------------------------------------------------------
# Finding analysis — grouping and theme detection
# ---------------------------------------------------------------------------


def group_findings_by_theme(
    findings: list[ResearchFinding],
) -> dict[str, list[ResearchFinding]]:
    """Group findings into theme clusters based on their tags.

    Each finding can belong to multiple themes. Themes are identified by
    matching tag combinations from THEME_DEFINITIONS.

    Returns:
        Dict mapping theme name to list of findings in that theme.
    """
    theme_groups: dict[str, list[ResearchFinding]] = defaultdict(list)

    for finding in findings:
        finding_tags = set(finding.tags or [])
        if not finding_tags:
            continue

        for theme_name, theme_def in THEME_DEFINITIONS.items():
            theme_tags = set(theme_def["tags"])
            # Finding belongs to theme if it has at least one matching tag
            if finding_tags & theme_tags:
                theme_groups[theme_name].append(finding)

    return dict(theme_groups)


def extract_model_names(findings: list[ResearchFinding]) -> list[str]:
    """Extract model names mentioned in findings."""
    model_keywords = [
        "llama",
        "mistral",
        "qwen",
        "deepseek",
        "gemma",
        "phi",
        "falcon",
        "mixtral",
        "command-r",
        "claude",
        "gpt",
        "yi",
        "internlm",
        "codestral",
        "starcoder",
    ]
    models = set()

    for finding in findings:
        text = f"{finding.title} {finding.summary or ''}".lower()
        for kw in model_keywords:
            if kw in text:
                # Try to extract a more specific model name from title
                title_words = finding.title.split()
                for i, word in enumerate(title_words):
                    if kw in word.lower():
                        # Grab word + next word for version (e.g., "Llama 4")
                        model_name = word
                        if i + 1 < len(title_words):
                            next_w = title_words[i + 1]
                            # Check if next word is a version/number
                            if any(c.isdigit() for c in next_w) or next_w.startswith(
                                "v"
                            ):
                                model_name = f"{word} {next_w}"
                        models.add(model_name)
                        break

    return list(models) if models else ["latest trending model"]


def extract_gpu_types(findings: list[ResearchFinding]) -> list[str]:
    """Extract GPU types mentioned in findings."""
    gpu_keywords = {
        "h100": "H100",
        "h200": "H200",
        "b200": "B200",
        "gb200": "GB200",
        "a100": "A100",
        "l40s": "L40S",
        "l4": "L4",
        "t4": "T4",
    }
    gpus = set()

    for finding in findings:
        text = f"{finding.title} {finding.summary or ''}".lower()
        for kw, gpu_name in gpu_keywords.items():
            if kw in text:
                gpus.add(gpu_name)

    return list(gpus) if gpus else DEFAULT_GPU_TYPES[:1]


def extract_techniques(findings: list[ResearchFinding]) -> list[str]:
    """Extract optimization techniques mentioned in findings."""
    technique_keywords = {
        "speculative decoding": "speculative decoding",
        "kv cache": "KV cache optimization",
        "tensor parallelism": "tensor parallelism",
        "quantization": "quantization",
        "gptq": "GPTQ quantization",
        "awq": "AWQ quantization",
        "fp8": "FP8 quantization",
        "gguf": "GGUF conversion",
        "vllm": "vLLM serving",
        "sglang": "SGLang serving",
        "continuous batching": "continuous batching",
        "paged attention": "paged attention",
    }
    techniques = set()

    for finding in findings:
        text = f"{finding.title} {finding.summary or ''}".lower()
        for kw, technique in technique_keywords.items():
            if kw in text:
                techniques.add(technique)

    return list(techniques) if techniques else ["default optimization"]


def extract_competitors(findings: list[ResearchFinding]) -> list[str]:
    """Extract competitor names from findings."""
    competitor_keywords = [
        "anyscale",
        "modal",
        "runpod",
        "together",
        "lambda",
        "replicate",
        "fireworks",
        "groq",
        "cerebras",
    ]
    competitors = set()

    for finding in findings:
        text = f"{finding.title} {finding.summary or ''}".lower()
        for kw in competitor_keywords:
            if kw in text:
                competitors.add(kw.title())

    return list(competitors) if competitors else ["competitor"]


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------


def generate_experiment_plan(
    theme_name: str,
    context: dict[str, str],
) -> list[dict[str, object]]:
    """Generate a step-by-step experiment plan based on theme.

    Returns:
        List of step dicts with step_number, description, expected_output.
    """
    common_steps = [
        {
            "step_number": 1,
            "description": "Set up workspace and provision required GPU resources via SkyPilot",
            "expected_output": "Running instance with specified GPU type and disk",
        },
        {
            "step_number": 2,
            "description": "Install dependencies (vLLM/SGLang, CUDA toolkit, model weights)",
            "expected_output": "Environment ready with all tools and model downloaded",
        },
    ]

    theme_specific_steps = {
        "new-model-deployment": [
            {
                "step_number": 3,
                "description": f"Deploy {context.get('model_name', 'model')} with {context.get('quant_type', 'FP8')} quantization on vLLM",
                "expected_output": "Model loaded and serving on API endpoint",
            },
            {
                "step_number": 4,
                "description": "Run latency benchmark: 100 requests at concurrency 1, 10, 50",
                "expected_output": "P50/P95/P99 latency numbers for each concurrency level",
            },
            {
                "step_number": 5,
                "description": "Run throughput benchmark: max tokens/sec sustained over 10 minutes",
                "expected_output": "Sustained throughput in tokens/sec and requests/sec",
            },
            {
                "step_number": 6,
                "description": "Measure VRAM usage and validate model fits in GPU memory",
                "expected_output": "Peak VRAM usage in GB, OOM status",
            },
            {
                "step_number": 7,
                "description": "Record results and tear down resources",
                "expected_output": "Results document with all metrics, resources released",
            },
        ],
        "inference-optimization": [
            {
                "step_number": 3,
                "description": f"Deploy baseline model without {context.get('technique', 'optimization')}",
                "expected_output": "Baseline throughput and latency measurements",
            },
            {
                "step_number": 4,
                "description": f"Apply {context.get('technique', 'optimization')} and redeploy",
                "expected_output": "Optimized model serving on API endpoint",
            },
            {
                "step_number": 5,
                "description": "Run identical benchmark suite as baseline",
                "expected_output": "Optimized throughput and latency measurements",
            },
            {
                "step_number": 6,
                "description": "Compare output quality (perplexity, accuracy on test set)",
                "expected_output": "Quality delta between baseline and optimized",
            },
            {
                "step_number": 7,
                "description": "Record comparison results and tear down resources",
                "expected_output": "Comparison report with recommendation",
            },
        ],
        "gpu-cost-comparison": [
            {
                "step_number": 3,
                "description": f"Deploy reference model on {context.get('gpu_a', 'GPU A')}",
                "expected_output": "Benchmark results for GPU A",
            },
            {
                "step_number": 4,
                "description": f"Deploy same model on {context.get('gpu_b', 'GPU B')}",
                "expected_output": "Benchmark results for GPU B",
            },
            {
                "step_number": 5,
                "description": "Calculate $/token for each GPU at various batch sizes",
                "expected_output": "Cost-performance matrix",
            },
            {
                "step_number": 6,
                "description": "Record results and tear down all resources",
                "expected_output": "Cost comparison report with recommendation",
            },
        ],
        "competitor-analysis": [
            {
                "step_number": 3,
                "description": f"Document {context.get('competitor', 'competitor')}'s feature capabilities",
                "expected_output": "Feature specification document",
            },
            {
                "step_number": 4,
                "description": "Implement equivalent using Superplane infrastructure",
                "expected_output": "Working prototype",
            },
            {
                "step_number": 5,
                "description": "Benchmark and compare performance metrics",
                "expected_output": "Comparison report",
            },
            {
                "step_number": 6,
                "description": "Document gaps and recommendations",
                "expected_output": "Gap analysis with action items",
            },
        ],
        "training-benchmark": [
            {
                "step_number": 3,
                "description": f"Prepare dataset and configure LoRA training for {context.get('model_name', 'model')}",
                "expected_output": "Training configuration and dataset ready",
            },
            {
                "step_number": 4,
                "description": "Run training job and monitor GPU utilization",
                "expected_output": "Training logs, loss curves, GPU utilization stats",
            },
            {
                "step_number": 5,
                "description": "Evaluate fine-tuned model on benchmark tasks",
                "expected_output": "Evaluation scores on target tasks",
            },
            {
                "step_number": 6,
                "description": "Record cost/time/quality metrics and tear down",
                "expected_output": "Training report with cost breakdown",
            },
        ],
    }

    steps = common_steps + theme_specific_steps.get(
        theme_name,
        [
            {
                "step_number": 3,
                "description": "Execute experiment as described in objective",
                "expected_output": "Experiment results",
            },
            {
                "step_number": 4,
                "description": "Record results and tear down resources",
                "expected_output": "Results document",
            },
        ],
    )

    return steps


def build_proposal_from_theme(
    theme_name: str,
    theme_def: dict[str, Any],
    findings: list[ResearchFinding],
    workspace_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build a structured proposal dict from a theme and its findings.

    Extracts entities (models, GPUs, techniques) from findings and fills
    in the theme templates to create a concrete proposal.
    """
    model_names = extract_model_names(findings)
    gpu_types = extract_gpu_types(findings)
    techniques = extract_techniques(findings)
    competitors = extract_competitors(findings)

    # Build context for template filling
    model_name = model_names[0] if model_names else "latest model"
    gpu_type = gpu_types[0] if gpu_types else "L40S"
    technique = techniques[0] if techniques else "optimization"
    competitor = competitors[0] if competitors else "competitor"

    context = {
        "model_name": model_name,
        "gpu_type": gpu_type,
        "technique": technique,
        "competitor": competitor,
        "quant_type": DEFAULT_QUANT_TYPES[0],
        "vram": str(DEFAULT_VRAM.get(gpu_type, 48)),
        "latency_target": str(DEFAULT_LATENCY_TARGETS.get(gpu_type, 200)),
        "improvement_pct": "20-30",
        "workload_type": "inference",
        "duration": str(theme_def["default_duration_hours"]),
        "cost": f"{theme_def['base_cost_per_hour'] * theme_def['default_duration_hours']:.0f}",
    }

    # Handle multi-GPU comparison
    if len(gpu_types) >= 2:
        context["gpu_a"] = gpu_types[0]
        context["gpu_b"] = gpu_types[1]
    else:
        context["gpu_a"] = gpu_type
        context["gpu_b"] = "A100" if gpu_type != "A100" else "H100"

    # Fill templates
    try:
        title = theme_def["title_template"].format(**context)
    except KeyError:
        title = f"Research: {theme_name} investigation"

    try:
        objective = theme_def["objective_template"].format(**context)
    except KeyError:
        objective = f"Investigate {theme_name} based on recent findings"

    try:
        hypothesis = theme_def["hypothesis_template"].format(**context)
    except KeyError:
        hypothesis = f"Investigation of {theme_name} should yield actionable results"

    try:
        required_resources = theme_def["resource_template"].format(**context)
    except KeyError:
        required_resources = "Standard workspace resources"

    duration_hours = theme_def["default_duration_hours"]
    estimated_cost = theme_def["base_cost_per_hour"] * duration_hours

    # Generate experiment plan
    experiment_plan = generate_experiment_plan(theme_name, context)

    # Collect source finding IDs
    source_finding_ids = [str(f.id) for f in findings]

    return {
        "workspace_id": workspace_id,
        "title": title,
        "objective": objective,
        "hypothesis": hypothesis,
        "source_findings": source_finding_ids,
        "estimated_cost_usd": round(estimated_cost, 2),
        "estimated_duration_hours": duration_hours,
        "required_resources": required_resources,
        "experiment_plan": experiment_plan,
        "status": "proposed",
    }


# ---------------------------------------------------------------------------
# Main analysis orchestrator
# ---------------------------------------------------------------------------


async def generate_proposals(
    session: AsyncSession,
    workspace_id: uuid.UUID | None = None,
    min_relevance: int = 50,
    max_proposals: int = 5,
) -> dict[str, Any]:
    """Analyze scanner findings and generate research proposals.

    Steps:
    1. Fetch findings with relevance_score >= min_relevance
    2. Group findings into themes
    3. Generate proposals for top themes (up to max_proposals)
    4. Store proposals in database

    Args:
        session: Async database session.
        workspace_id: Optional workspace scope.
        min_relevance: Minimum relevance score to consider.
        max_proposals: Maximum proposals to generate.

    Returns:
        Summary dict with generation results.
    """
    # 1. Fetch relevant findings
    query = select(ResearchFinding).where(
        ResearchFinding.relevance_score >= min_relevance
    )
    if workspace_id:
        query = query.where(ResearchFinding.workspace_id == workspace_id)

    query = query.order_by(ResearchFinding.relevance_score.desc())

    result = await session.execute(query)
    findings = list(result.scalars().all())

    if not findings:
        logger.info("No findings with relevance >= %d found", min_relevance)
        return {
            "status": "completed",
            "proposals_generated": 0,
            "themes_identified": 0,
            "findings_analyzed": 0,
        }

    logger.info(
        "Analyzing %d findings with relevance >= %d", len(findings), min_relevance
    )

    # 2. Group findings into themes
    theme_groups = group_findings_by_theme(findings)
    logger.info("Identified %d themes from findings", len(theme_groups))

    # 3. Generate proposals — prioritize themes with most high-relevance findings
    proposals_created = 0
    sorted_themes = sorted(
        theme_groups.items(),
        key=lambda x: sum(f.relevance_score for f in x[1]),
        reverse=True,
    )

    for theme_name, theme_findings in sorted_themes:
        if proposals_created >= max_proposals:
            break

        theme_def = THEME_DEFINITIONS.get(theme_name)
        if not theme_def:
            continue

        # Build proposal
        proposal_data = build_proposal_from_theme(
            theme_name=theme_name,
            theme_def=theme_def,
            findings=theme_findings,
            workspace_id=workspace_id,
        )

        # Store in database
        proposal = ResearchProposal(
            id=uuid.uuid4(),
            **proposal_data,
        )
        session.add(proposal)
        proposals_created += 1

        logger.info(
            "Generated proposal: %s (from %d findings)",
            proposal.title,
            len(theme_findings),
        )

    await session.commit()

    logger.info(
        "Analysis complete: %d proposals from %d themes, %d findings",
        proposals_created,
        len(theme_groups),
        len(findings),
    )

    return {
        "status": "completed",
        "proposals_generated": proposals_created,
        "themes_identified": len(theme_groups),
        "findings_analyzed": len(findings),
    }


# ---------------------------------------------------------------------------
# Proposal status management
# ---------------------------------------------------------------------------


def validate_status_transition(current_status: str, new_status: str) -> bool:
    """Check if a status transition is valid.

    Args:
        current_status: Current proposal status.
        new_status: Desired new status.

    Returns:
        True if the transition is valid, False otherwise.
    """
    allowed = STATUS_TRANSITIONS.get(current_status, [])
    return new_status in allowed
