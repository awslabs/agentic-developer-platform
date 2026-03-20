"""Chat Logging Module for Bedrock Gateway.

Issue #143: Async Chat Logging with PII Scrubbing to S3

This module provides:
- Async chat logging for all proxy requests
- Sensitive data scrubbing (headers, regex patterns, Comprehend PII)
- S3 storage with lifecycle policies
- Fire-and-forget pattern for zero latency impact

Components:
- schemas: Pydantic models for chat log structure
- scrubber: Header and regex-based sensitive data scrubbing
- comprehend_client: Amazon Comprehend PII detection
- s3_writer: Async S3 write with circuit breaker
- service: Main orchestration service
"""

from src.chat_logging.comprehend_client import ComprehendPiiDetector
from src.chat_logging.s3_writer import ChatLogS3Writer
from src.chat_logging.schemas import ChatLog, ChatLogRequest, ChatLogResponse, ScrubbingMetadata
from src.chat_logging.scrubber import HeaderScrubber, RegexScrubber, ScrubPipeline
from src.chat_logging.service import ChatLoggingService

__all__ = [
    "ChatLog",
    "ChatLogRequest",
    "ChatLogResponse",
    "ChatLoggingService",
    "ChatLogS3Writer",
    "ComprehendPiiDetector",
    "HeaderScrubber",
    "RegexScrubber",
    "ScrubbingMetadata",
    "ScrubPipeline",
]
