"""Task API ingress (T2, issue #5795).

Loaded lazily from the existing ingress Lambda router so that a task-only
import or initialization failure cannot affect the GitHub, EventBridge or
agent-trigger handlers (T2-AC04).

Design revision b5761a4a2502aceaa9133afef552b567a19cb46e.
"""
