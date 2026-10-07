import json
import os
import random
import time
import uuid
from pathlib import Path

import gevent
from locust import HttpUser, LoadTestShape, between, events, task

ROOT = Path(os.environ.get("ADP_PERF_OUTPUT_DIR", "/tmp/adp-gateway-perf"))
ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
TOKEN = Path(os.environ["ADP_PERF_TOKEN_FILE"]).read_text().strip()
LABEL = os.environ.get("PERF_LABEL", "trial")
MAX_REQUESTS = int(os.environ.get("PERF_MAX_REQUESTS", "1200"))
REQUESTS = 0
ACTIVE = 0
RESULTS = []
START = time.monotonic()
OUT = (ROOT / (LABEL + "-requests.jsonl")).open("w", buffering=1)


class CampaignShape(LoadTestShape):
    def tick(self):
        raw = os.environ.get("PERF_STAGES", "30:3")
        for item in raw.split(","):
            end, users = map(int, item.split(":"))
            if self.get_run_time() < end:
                if users == 0:
                    # Stop all users in one batch. With stop_timeout, stopping
                    # one at a time lets the remaining users start new calls.
                    return 0, max(1, self.get_current_user_count())
                return users, max(1, min(10, users))
        return None


class GatewayUser(HttpUser):
    wait_time = between(0.5, 2)

    @task
    def generate(self):
        global REQUESTS, ACTIVE
        if REQUESTS >= MAX_REQUESTS:
            gevent.sleep(1)
            return
        REQUESTS += 1
        long = random.random() < float(os.environ.get("PERF_LONG_RATIO", ".7"))
        stream = True
        if long:
            prompt = "Write a detailed technical explanation of how an asynchronous HTTP proxy handles backpressure, cancellation, retries and graceful shutdown. Use numbered paragraphs and continue until you have covered every topic in depth."
            # A synthetic source-document context, not private user data.
            prompt += "\nContext: " + (
                "The gateway authenticates callers, enforces tenant budgets, routes model requests, and relays streamed tokens. "
                * int(os.environ.get("PERF_CONTEXT_REPEATS", "60"))
            )
            limit = int(os.environ.get("PERF_LONG_TOKENS", "768"))
            words = os.environ.get("PERF_LONG_WORDS")
            if words:
                prompt += (
                    f"\nKeep the complete answer to approximately {int(words)} words."
                )
            elif limit > 1000:
                prompt += "\nWrite at least 5000 words, with extensive examples and pseudocode. Do not conclude early."
        else:
            prompt = "Count from 1 to 40, separated by spaces. Output only the numbers."
            limit = 128
        if os.environ.get("PERF_UNIQUE_PROMPTS", "false").lower() == "true":
            # Vary the prefix, not just the suffix, to probe prompt-cache reuse.
            prompt = f"Synthetic request ID: {uuid.uuid4().hex}\n" + prompt
        payload = {
            "model": os.environ.get("PERF_MODEL", "haiku45"),
            "max_tokens": limit,
            "stream": stream,
            "messages": [{"role": "user", "content": prompt}],
        }
        api = os.environ.get("PERF_API", "chat")
        if api not in {"chat", "responses"}:
            raise ValueError("PERF_API must be chat or responses")
        path = "/v1/chat/completions"
        if api == "responses":
            path = "/openai/v1/responses"
            limit = int(os.environ.get("PERF_RESPONSES_MAX_TOKENS", "2048"))
            payload = {
                "model": payload["model"],
                "input": prompt,
                "stream": True,
                "max_output_tokens": limit,
                "reasoning": {
                    "effort": os.environ.get("PERF_REASONING_EFFORT", "medium")
                },
            }
        started = time.monotonic()
        ACTIVE += 1
        row = {
            "started_epoch": time.time(),
            "scenario": "long" if long else "short",
            "max_tokens": limit,
            "model": payload["model"],
            "api": api,
            "reasoning_effort": payload.get("reasoning", {}).get("effort"),
        }
        try:
            with self.client.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer " + TOKEN},
                stream=True,
                catch_response=True,
                name="LLM/" + row["scenario"],
                timeout=(10, int(os.environ.get("PERF_READ_TIMEOUT", "180"))),
            ) as response:
                row["status"] = response.status_code
                row["request_id"] = response.headers.get("X-Request-ID")
                if response.status_code != 200:
                    try:
                        err = response.json()
                        row["error"] = str(
                            err.get("error", err.get("detail", "unknown"))
                        )[:240]
                        row["error_message"] = str(err.get("message", ""))[:240]
                        row["error_details"] = err.get("details")
                    except Exception:
                        row["error"] = "non_json_error"
                    response.failure(
                        "HTTP " + str(response.status_code) + " " + row["error"]
                    )
                else:
                    done = False
                    chunks = 0
                    content_chars = 0
                    last = None
                    maxgap = 0
                    stream_error = False
                    for line in response.iter_lines(chunk_size=64):
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            if api == "chat":
                                done = True
                            continue
                        item = json.loads(data)
                        kind = item.get("type")
                        if api == "responses":
                            if kind == "response.completed":
                                done = (
                                    item.get("response", {}).get("status")
                                    == "completed"
                                )
                            if kind in {
                                "error",
                                "response.failed",
                                "response.incomplete",
                            }:
                                row["error"] = kind
                                stream_error = True
                            terminal = item.get("response", {})
                            if terminal.get("error"):
                                row["terminal_error"] = terminal["error"]
                            if terminal.get("usage"):
                                row["usage"] = terminal["usage"]
                            if terminal.get("incomplete_details"):
                                row["incomplete_details"] = terminal[
                                    "incomplete_details"
                                ]
                        if item.get("error"):
                            row["error"] = str(item["error"])[:240]
                            stream_error = True
                        if item.get("usage"):
                            row["usage"] = item["usage"]
                        choices = item.get("choices", [])
                        if api == "responses" and kind == "response.output_text.delta":
                            choices = [{"delta": {"content": item.get("delta")}}]
                        for c in choices:
                            content = c.get("delta", {}).get("content")
                            if content:
                                now = time.monotonic()
                                if chunks == 0:
                                    row["ttft_ms"] = (now - started) * 1000
                                if last is not None:
                                    maxgap = max(maxgap, now - last)
                                last = now
                                chunks += 1
                                content_chars += len(content)
                            if c.get("finish_reason"):
                                row["finish_reason"] = c["finish_reason"]
                    row.update(
                        chunks=chunks,
                        content_chars=content_chars,
                        complete=done,
                        max_chunk_gap_ms=maxgap * 1000,
                    )
                    if not (done and chunks and not stream_error):
                        row.setdefault("error", "incomplete_stream")
                        response.failure(row["error"])
                    else:
                        row["success"] = True
                        response.success()
                        events.request.fire(
                            request_type="STREAM",
                            name="TTFT/" + row["scenario"],
                            response_time=row["ttft_ms"],
                            response_length=0,
                            exception=None,
                            context={},
                        )
                row["duration_ms"] = (time.monotonic() - started) * 1000
                response.request_meta["response_time"] = row["duration_ms"]
        except gevent.GreenletExit:
            row["cancelled_by_runner"] = True
            row["duration_ms"] = (time.monotonic() - started) * 1000
            raise
        except Exception as e:
            row["error"] = type(e).__name__
            row["duration_ms"] = (time.monotonic() - started) * 1000
        finally:
            ACTIVE -= 1
            RESULTS.append(row)
            OUT.write(json.dumps(row) + "\n")


@events.test_start.add_listener
def start(environment, **kwargs):
    def sample():
        with (ROOT / (LABEL + "-load.jsonl")).open("w", buffering=1) as f:
            while environment.runner.state not in ["stopped", "cleanup"]:
                f.write(
                    json.dumps(
                        {
                            "epoch": time.time(),
                            "elapsed": time.monotonic() - START,
                            "users": environment.runner.user_count,
                            "inflight": ACTIVE,
                            "started_requests": REQUESTS,
                            "completed_requests": len(RESULTS),
                            "errors": sum(not x.get("success", False) for x in RESULTS),
                        }
                    )
                    + "\n"
                )
                gevent.sleep(2)

    gevent.spawn(sample)
