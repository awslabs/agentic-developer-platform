"""Source definitions and configuration for the external data scanner (US-G2).

Each source has its own configuration including API endpoints, categories to
monitor, scanning frequency, and relevance keywords for scoring.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceConfig:
    """Configuration for a single external data source."""

    name: str
    display_name: str
    frequency: str  # e.g. "daily", "6h", "weekly"
    api_url: str
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    score_threshold: int = 0  # minimum score/upvotes to consider


# ---------------------------------------------------------------------------
# Source configurations
# ---------------------------------------------------------------------------

ARXIV_CONFIG = SourceConfig(
    name="arxiv",
    display_name="arXiv",
    frequency="daily",
    api_url="https://export.arxiv.org/api/query",
    categories=["cs.LG", "cs.CL", "cs.CV"],
    keywords=[
        "large language model",
        "inference",
        "GPU",
        "distributed training",
        "model serving",
        "quantization",
        "mixture of experts",
        "vLLM",
        "speculative decoding",
        "KV cache",
        "transformer",
    ],
)

HUGGINGFACE_CONFIG = SourceConfig(
    name="huggingface",
    display_name="HuggingFace",
    frequency="daily",
    api_url="https://huggingface.co/api",
    categories=["models", "trending", "datasets"],
    keywords=[
        "llama",
        "mistral",
        "qwen",
        "deepseek",
        "gemma",
        "phi",
        "vllm",
        "gguf",
        "gptq",
        "awq",
    ],
)

GITHUB_CONFIG = SourceConfig(
    name="github",
    display_name="GitHub",
    frequency="daily",
    api_url="https://api.github.com",
    categories=["trending", "releases"],
    keywords=[
        "vLLM",
        "SGLang",
        "SkyPilot",
        "llama.cpp",
        "text-generation-inference",
        "ollama",
        "LangChain",
        "transformers",
        "triton",
        "deepspeed",
        "megatron",
    ],
)

TWITTER_CONFIG = SourceConfig(
    name="twitter",
    display_name="Twitter/X",
    frequency="6h",
    api_url="https://api.twitter.com/2",
    categories=["ml_accounts", "trending_ml"],
    keywords=[
        "GPU",
        "LLM",
        "inference",
        "training",
        "NVIDIA",
        "H100",
        "H200",
        "B200",
        "vLLM",
        "SkyPilot",
    ],
)

REDDIT_CONFIG = SourceConfig(
    name="reddit",
    display_name="Reddit",
    frequency="daily",
    api_url="https://www.reddit.com",
    categories=["r/MachineLearning", "r/LocalLLaMA"],
    keywords=[
        "model release",
        "benchmark",
        "GPU",
        "inference",
        "fine-tuning",
        "quantization",
        "deployment",
    ],
    score_threshold=50,
)

HACKERNEWS_CONFIG = SourceConfig(
    name="hackernews",
    display_name="Hacker News",
    frequency="daily",
    api_url="https://hacker-news.firebaseio.com/v0",
    categories=["ai", "ml", "gpu", "llm"],
    keywords=[
        "AI",
        "ML",
        "GPU",
        "LLM",
        "inference",
        "training",
        "NVIDIA",
        "model",
    ],
    score_threshold=100,
)

AWS_WHATSNEW_CONFIG = SourceConfig(
    name="aws_whatsnew",
    display_name="AWS What's New",
    frequency="daily",
    api_url="https://aws.amazon.com/about-aws/whats-new/recent/feed/",
    categories=["machine-learning", "compute", "gpu"],
    keywords=[
        "SageMaker",
        "Bedrock",
        "EC2",
        "GPU",
        "p5",
        "p4",
        "g6",
        "Inferentia",
        "Trainium",
        "EKS",
        "ParallelCluster",
    ],
)

NVIDIA_BLOG_CONFIG = SourceConfig(
    name="nvidia_blog",
    display_name="NVIDIA Blog",
    frequency="weekly",
    api_url="https://blogs.nvidia.com/feed/",
    categories=["gpu", "ai", "data-center"],
    keywords=[
        "H100",
        "H200",
        "B200",
        "GB200",
        "CUDA",
        "TensorRT",
        "Triton",
        "NIM",
        "DGX",
        "NVLink",
        "Blackwell",
        "Hopper",
    ],
)

COMPETITOR_BLOG_CONFIG = SourceConfig(
    name="competitor_blog",
    display_name="Competitor Blogs",
    frequency="weekly",
    api_url="",  # Multiple URLs handled in scanner
    categories=[
        "anyscale",
        "modal",
        "runpod",
        "together_ai",
        "lambda_labs",
    ],
    keywords=[
        "pricing",
        "GPU",
        "inference",
        "serverless",
        "cluster",
        "deployment",
        "fine-tuning",
        "new feature",
    ],
)

# Competitor blog URLs
COMPETITOR_URLS = {
    "anyscale": "https://www.anyscale.com/blog",
    "modal": "https://modal.com/blog",
    "runpod": "https://blog.runpod.io",
    "together_ai": "https://www.together.ai/blog",
    "lambda_labs": "https://lambdalabs.com/blog",
}

# All source configs indexed by name
ALL_SOURCES: dict[str, SourceConfig] = {
    cfg.name: cfg
    for cfg in [
        ARXIV_CONFIG,
        HUGGINGFACE_CONFIG,
        GITHUB_CONFIG,
        TWITTER_CONFIG,
        REDDIT_CONFIG,
        HACKERNEWS_CONFIG,
        AWS_WHATSNEW_CONFIG,
        NVIDIA_BLOG_CONFIG,
        COMPETITOR_BLOG_CONFIG,
    ]
}

# High-relevance keywords that boost score significantly
HIGH_RELEVANCE_KEYWORDS = [
    "vllm",
    "skypilot",
    "sglang",
    "h100",
    "h200",
    "b200",
    "gb200",
    "inference optimization",
    "model serving",
    "gpu cluster",
    "distributed inference",
    "tensor parallelism",
    "speculative decoding",
    "kv cache optimization",
    "mixture of experts",
]

# Tags automatically assigned based on keyword detection
AUTO_TAGS = {
    "new-model": [
        "release",
        "new model",
        "model release",
        "open source model",
        "open-weight",
    ],
    "inference": [
        "inference",
        "serving",
        "vllm",
        "sglang",
        "tgi",
        "triton",
        "tensorrt",
    ],
    "training": [
        "training",
        "fine-tuning",
        "finetuning",
        "pretraining",
        "deepspeed",
        "megatron",
    ],
    "gpu-hardware": [
        "h100",
        "h200",
        "b200",
        "gb200",
        "a100",
        "gpu",
        "nvidia",
        "blackwell",
        "hopper",
    ],
    "quantization": ["quantization", "gguf", "gptq", "awq", "int8", "int4", "fp8"],
    "benchmark": ["benchmark", "eval", "leaderboard", "mmlu", "humaneval"],
    "pricing": ["pricing", "cost", "cheaper", "discount", "spot"],
    "competitor": ["anyscale", "modal", "runpod", "together", "lambda labs"],
    "cloud-update": ["aws", "sagemaker", "bedrock", "ec2", "eks"],
}
