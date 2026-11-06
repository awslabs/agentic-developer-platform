"""Isolated UI-only FinOps action runner; no private provider HTTP API is assumed."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from botocore.exceptions import ClientError


class FinOpsExecutorEvidence:
    def __init__(self, bucket, client):
        self.bucket = bucket
        self.client = client

    @staticmethod
    def _prefix(run_id):
        if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
            raise ValueError("native executor run identity is invalid")
        return f"l5/finops/work/{run_id}/native"

    async def _read(self, key):
        result = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        if not result.get("VersionId") or result.get("ContentLength", 1 << 30) > 1024 * 1024:
            raise ValueError("native executor evidence is not versioned or bounded")
        content = await asyncio.to_thread(result["Body"].read)
        if (len(content) > 1024 * 1024 or sha256(content).hexdigest()
                != result.get("Metadata", {}).get("sha256")):
            raise ValueError("native executor evidence digest changed")
        return content, result

    async def _put_once(self, key, content, observed_at=None):
        metadata = {"sha256": sha256(content).hexdigest()}
        if observed_at is not None:
            metadata["observed-at"] = observed_at
        try:
            result = await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key,
                                             Body=content, Metadata=metadata, IfNoneMatch="*")
            if not result.get("VersionId"):
                raise ValueError("native executor evidence requires versioned storage")
            return result["VersionId"], metadata
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise
        existing, result = await self._read(key)
        if existing != content:
            raise ValueError("native executor attempt already belongs to other input")
        return result["VersionId"], result["Metadata"]

    async def claim_intent(self, intent, provider_identity):
        content = json.dumps({"intent": intent, "provider_identity": provider_identity},
                             sort_keys=True, separators=(",", ":")).encode()
        key = self._prefix(intent["run_id"]) + "/intent.json"
        try:
            result = await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key,
                                             Body=content, Metadata={"sha256": sha256(content).hexdigest()},
                                             IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise
            existing, _ = await self._read(key)
            if existing != content:
                raise ValueError("native executor attempt already belongs to other input") from error
            return False
        if not result.get("VersionId"):
            raise ValueError("native executor attempt requires versioned storage")
        return True

    async def read_intent(self, run_id):
        content, _ = await self._read(self._prefix(run_id) + "/intent.json")
        return json.loads(content)

    async def put_answer(self, run_id, content, observed_at):
        digest = sha256(content).hexdigest()
        key = self._prefix(run_id) + "/answers/original.txt"
        version, metadata = await self._put_once(key, content, observed_at)
        return {"artifact": {"artifact_id": key, "version": version, "sha256": digest},
                "observed_at": metadata["observed-at"]}


class ConsoleFinOpsExecutor:
    def __init__(self, browser, verify_work, evidence_store, surface):
        if (not callable(verify_work) or browser is None or evidence_store is None
                or not isinstance(surface, dict)
                or set(surface) - {"agent_label", "billing_view_selector", "data_role_selector"} != {"agent_identity_selector", "conversation_selector",
                                    "turn_selector", "question_selector", "turn_receipt_attribute",
                                    "answer_selector", "turn_status_selector",
                                    "answer_classification_selector", "history_complete_selector",
                                    "surface_revision"}
                or any(not isinstance(value, str) or not value for value in surface.values())):
            raise ValueError("qualified private FinOps console surface is required")
        self.browser = browser
        self.verify_work = verify_work
        self.evidence_store = evidence_store
        self.surface = surface

    async def _session(self, authority):
        verified = await self._authorize_current(authority)
        return self.browser.session(verified, self.surface)

    async def _authorize_current(self, authority):
        verified = await self.verify_work(authority)
        if (not isinstance(verified, dict) or verified.get("operation_id") != authority.get("operation_id")
                or verified.get("attempt_id") != authority.get("attempt_id")
                or verified.get("lease_fence") != authority.get("lease_fence")
                or verified.get("provider_identity") != authority.get("provider_identity")):
            raise PermissionError("current FinOps executor work unavailable")
        return verified

    async def read_agent(self, *, native_job_ref, authority):
        verified = await self._authorize_current(authority)
        label = ((verified.get("setup_verified") or {}).get("finops") or {}).get("native_agent_display_name")
        if (authority.get("work_kind") != "setup" or native_job_ref != verified.get("native_resource_ref")
                or not isinstance(label, str) or not label.strip()
                or not self.surface.get("data_role_selector")):
            raise PermissionError("FinOps adoption requires a scoped native settings readback")
        async with self.browser.session(verified, self.surface) as page:
            native = await page.read_agent(native_job_ref["record_id"], label)
        return {**native, "native_resource_ref": native_job_ref,
                "provider_identity": {field: verified["provider_identity"][field] for field in ("account_id", "role_arn", "region")}}

    async def submit(self, *, intent, authority):
        if (intent.get("run_id") != authority.get("operation_id")
                or any(intent.get(field) != authority.get(field) for field in ("tenant_id", "product_id", "project_id"))):
            raise PermissionError("FinOps inquiry differs from current work")
        if (not isinstance(intent.get("question"), str) or not intent["question"].strip()
                or len(PlaywrightFinOpsPage._prompt(intent)) > 1000):
            raise ValueError("FinOps inquiry exceeds the visible console prompt limit")
        async with await self._session(authority) as page:
            first = await self.evidence_store.claim_intent(intent, authority["provider_identity"])
            previous = await page.find_turn(intent)
            if not first:
                return self._accepted(intent, previous) if previous is not None else None
            if previous is not None:
                return self._accepted(intent, previous)
            await self._authorize_current(authority)
            await page.send_once(intent)
            observed = await page.find_turn(intent)
            return self._accepted(intent, observed) if observed is not None else None

    async def read_submit(self, *, intent, authority):
        if (intent.get("run_id") != authority.get("operation_id")
                or any(intent.get(field) != authority.get(field) for field in ("tenant_id", "product_id", "project_id"))):
            raise PermissionError("FinOps recovery differs from current work")
        await self._authorize_current(authority)
        saved = await self.evidence_store.read_intent(intent["run_id"])
        if saved != {"intent": intent, "provider_identity": authority["provider_identity"]}:
            raise PermissionError("FinOps recovery attempt differs from retained scope")
        async with await self._session(authority) as page:
            observed = await page.find_turn(intent)
            return self._accepted(intent, observed) if observed is not None else None

    @staticmethod
    def _accepted(intent, observed):
        if (observed.get("attempt_token") != intent["attempt_token"]
                or observed.get("agent_id") != intent["agent_id"]
                or not observed.get("conversation_id")
                or intent.get("conversation_id", observed.get("conversation_id")) != observed.get("conversation_id")
                or not (observed.get("turn_id") or observed.get("turn_locator"))
                or observed.get("question_hash") != intent["question_hash"]):
            raise ValueError("native turn lacks unique UI identity")
        return {"attempt_token": intent["attempt_token"], "native_job_ref": {
            "tenant_id": intent["tenant_id"], "product_id": intent["product_id"],
            "project_id": intent["project_id"], "agent_id": intent["agent_id"],
            "conversation_id": observed["conversation_id"],
            **({"turn_id": observed["turn_id"]} if observed.get("turn_id") else
               {"turn_locator": observed["turn_locator"]}), "question_hash": intent["question_hash"]}}

    async def _turn(self, native_job_ref, authority):
        if not isinstance(native_job_ref, dict) or not (set(native_job_ref) in ({
                "tenant_id", "product_id", "project_id", "agent_id", "conversation_id", "turn_id", "question_hash"}, {
                "tenant_id", "product_id", "project_id", "agent_id", "conversation_id", "turn_locator", "question_hash"})):
            raise PermissionError("FinOps readback requires a pinned native turn")
        if any(not isinstance(value, str) or not value for value in native_job_ref.values()):
            raise PermissionError("FinOps readback requires a pinned native turn")
        await self._authorize_current(authority)
        saved = await self.evidence_store.read_intent(authority["operation_id"])
        if (not isinstance(saved, dict) or saved.get("provider_identity") != authority["provider_identity"]):
            raise PermissionError("FinOps readback lacks retained provider scope")
        intent = saved.get("intent") or {}
        if ((intent.get("tenant_id"), intent.get("product_id"), intent.get("project_id"),
             intent.get("agent_id"), intent.get("question_hash"), intent.get("run_id")) != (
                native_job_ref["tenant_id"], native_job_ref["product_id"], native_job_ref["project_id"],
                native_job_ref["agent_id"], native_job_ref["question_hash"], authority["operation_id"])
                or intent.get("conversation_id", native_job_ref["conversation_id"])
                != native_job_ref["conversation_id"]):
            raise PermissionError("FinOps readback differs from retained attempt")
        if intent.get("product_id") != authority.get("product_id") or intent.get("tenant_id") != authority.get("tenant_id"):
            raise PermissionError("FinOps readback differs from current Product")
        return await self._session(authority), intent

    async def observe(self, *, native_job_ref, authority):
        session, intent = await self._turn(native_job_ref, authority)
        async with session as page:
            status = await page.read_turn(native_job_ref, intent)
            if status is None:
                return None
            if status.get("state") not in {"running", "completed", "failed", "stopped"}:
                raise ValueError("FinOps console status is unqualified")
            return {"native_job_ref": native_job_ref, "outcome": status["state"],
                    "observed_at": datetime.now(UTC).isoformat()}

    async def read_answer(self, *, native_job_ref, authority):
        session, intent = await self._turn(native_job_ref, authority)
        async with session as page:
            status = await page.read_turn(native_job_ref, intent)
            if status is None or status.get("state") != "completed":
                return None
            if status.get("answer_status") not in {"answered", "refused"}:
                raise ValueError("FinOps answer classification is unavailable")
            original = status.get("answer")
            if (not isinstance(original, str) or not original.strip()
                    or re.search(r"(?i)(signin\.aws\.amazon\.com/federation|sessiontoken|secretaccesskey)", original)):
                raise ValueError("native answer cannot be retained safely")
            content = original.encode("utf-8")
            if len(content) > 1024 * 1024:
                raise ValueError("native answer exceeds protected evidence limit")
            evidence = await self.evidence_store.put_answer(
                authority["operation_id"], content, datetime.now(UTC).isoformat())
            artifact = evidence.get("artifact") or {}
            if (set(artifact) != {"artifact_id", "version", "sha256"}
                    or artifact["sha256"] != sha256(content).hexdigest()):
                raise ValueError("native answer evidence is not versioned")
            return {"kind": "finops_native_answer", "native_job_ref": native_job_ref,
                    "status": status["answer_status"], "answer_artifact": artifact,
                    "observed_at": evidence["observed_at"]}

    async def read_native_cost(self, *, native_job_ref, authority):
        session, intent = await self._turn(native_job_ref, authority)
        async with session as page:
            status = await page.read_turn(native_job_ref, intent)
            if status is None or status.get("state") != "completed" or status.get("answer_status") != "answered":
                return None
            answer = status.get("answer")
            if not isinstance(answer, str):
                return None
            try:
                value = json.loads(answer)
            except ValueError:
                return None
            if (not isinstance(value, dict) or value.get("kind") != "finops_native_cost"
                    or value.get("question_hash") != native_job_ref["question_hash"]
                    or value.get("scope_digest") != intent["billing_scope"].get("scope_digest")
                    or value.get("query") != intent.get("query")
                    or value.get("status") != "answered"):
                return None
            evidence = await self.evidence_store.put_answer(
                authority["operation_id"], answer.encode(), datetime.now(UTC).isoformat())
            return {**value, "answer_artifact": evidence["artifact"],
                    "observed_at": evidence["observed_at"]}

    async def stop(self, *, native_job_ref, authority):
        session, intent = await self._turn(native_job_ref, authority)
        async with session as page:
            status = await page.read_turn(native_job_ref, intent)
            if status is None or status.get("state") != "running":
                return None
            await self._authorize_current(authority)
            if not await page.stop_turn(native_job_ref):
                return None
            observed = await page.read_turn(native_job_ref, intent)
            if observed is None or observed.get("state") != "stopped":
                return None
            return {"native_job_ref": native_job_ref, "outcome": "stopped",
                    "observed_at": datetime.now(UTC).isoformat()}


class PlaywrightFinOpsBrowser:
    def __init__(self, region="us-east-1"):
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region):
            raise ValueError("FinOps console region is invalid")
        self.region = region

    @asynccontextmanager
    async def session(self, authority, surface):
        from playwright.async_api import async_playwright

        credentials = {"sessionId": authority["access_key_id"],
                       "sessionKey": authority["secret_access_key"],
                       "sessionToken": authority["session_token"]}
        federation = "https://signin.aws.amazon.com/federation?" + urlencode({
            "Action": "getSigninToken", "Session": json.dumps(credentials)})
        try:
            token = await asyncio.to_thread(self._signin_token, federation)
        except Exception:
            raise ConnectionError("private FinOps console sign-in unavailable") from None
        destination = f"https://{self.region}.console.aws.amazon.com/finops-agent/home?region={self.region}"
        login = "https://signin.aws.amazon.com/federation?" + urlencode({
            "Action": "login", "Issuer": "https://example.invalid", "Destination": destination,
            "SigninToken": token})
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                    selected_agent_id = authority["native_agent_id"]
                    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", selected_agent_id):
                        raise PermissionError("private FinOps agent identity is not a selected app host")
                    app_hostname = f"{selected_agent_id}.webapp.finops-agent.global.app.aws"
                    await context.route("**/*", lambda route: self._restrict_egress(route, app_hostname))
                    page = await context.new_page()
                    await page.goto(login, wait_until="domcontentloaded", timeout=60000)
                    yield PlaywrightFinOpsPage(page, context, surface, destination, app_hostname)
                finally:
                    await browser.close()
        except PermissionError:
            raise PermissionError("private FinOps console authority unavailable") from None
        except Exception:
            raise ConnectionError("private FinOps console operation unavailable") from None

    async def _restrict_egress(self, route, app_hostname):
        parsed = urlsplit(route.request.url)
        hostname = parsed.hostname or ""
        if (parsed.scheme == "https" and (hostname == "signin.aws.amazon.com"
                or hostname == f"{self.region}.console.aws.amazon.com"
                or hostname == app_hostname
                or hostname in {f"finops-agent.{self.region}.amazonaws.com",
                                f"finops-agent.{self.region}.api.aws"}
                or hostname.endswith(".awsstatic.com"))):
            await route.continue_()
        else:
            await route.abort()

    @staticmethod
    def _signin_token(url):
        with urlopen(url, timeout=20) as response:
            value = json.load(response)
        if not isinstance(value, dict) or not isinstance(value.get("SigninToken"), str):
            raise ValueError("selected console federation token unavailable")
        return value["SigninToken"]


class PlaywrightFinOpsPage:
    def __init__(self, page, context, surface, destination, app_hostname):
        self.page = page
        self.context = context
        self.surface = surface
        self.destination = destination
        self.app_hostname = app_hostname
        self.current_turn = None
        self.app = None
        self.conversations = []

    async def _app(self, agent_id, agent_label):
        if self.app_hostname != f"{agent_id}.webapp.finops-agent.global.app.aws":
            raise PermissionError("native FinOps agent differs from durable Product selection")
        if self.app is not None:
            return self.app
        await self.page.goto(self.destination + "#/agents", wait_until="domcontentloaded", timeout=30000)
        if not isinstance(agent_label, str) or not agent_label.strip():
            raise PermissionError("Product native agent display name unavailable")
        rows = self.page.locator("tr").filter(has=self.page.get_by_text(agent_label, exact=True))
        if await rows.count() != 1:
            raise ValueError("selected FinOps agent is not uniquely visible")
        if self.surface["agent_identity_selector"] != ":scope":
            agent_identity = rows.locator(self.surface["agent_identity_selector"])
            if await agent_identity.count() != 1:
                raise ValueError("FinOps console agent identity unavailable")
            observed_agent = (await agent_identity.get_attribute("data-agent-id")
                              or await agent_identity.inner_text())
            if observed_agent != agent_id:
                raise ValueError("FinOps console agent identity changed")
        async with self.page.expect_popup(timeout=20000) as popup:
            await rows.get_by_role("button", name="Open", exact=True).click()
        app = await popup.value
        parsed = urlsplit(app.url)
        if (parsed.scheme != "https" or parsed.hostname != self.app_hostname):
            raise ValueError("FinOps app is not an allowed provider origin")
        self.app = app
        return self.app

    async def read_agent(self, agent_id, agent_label):
        app = await self._app(agent_id, agent_label)
        label = self.page.locator("tr").filter(has=self.page.get_by_text(agent_label, exact=True)).get_by_text(agent_label, exact=True)
        if await label.count() != 1:
            raise ValueError("native FinOps agent label is not uniquely observed")
        observed = {}
        for field, selector in (("data_role_ref", "data_role_selector"),):
            selected = app.locator(self.surface[selector])
            if await selected.count() != 1:
                raise ValueError("native FinOps billing settings are not uniquely observed")
            value = await selected.text_content()
            if not isinstance(value, str) or not value.strip():
                raise ValueError("native FinOps billing settings unavailable")
            observed[field] = value.strip()
        return {**observed, "state": "ready", "native_agent_display_name": (await label.text_content()).strip(),
                "observed_at": datetime.now(UTC).isoformat()}

    async def find_turn(self, intent):
        app = await self._app(intent["agent_id"], intent["agent_label"])
        complete = app.locator(self.surface["history_complete_selector"])
        mode = self.surface["turn_receipt_attribute"] == "ordinal"
        if (await complete.count() != 1 or (not mode and await complete.get_attribute("data-complete") != "true")
                or mode and await complete.get_attribute("aria-busy") == "true"):
            raise ValueError("FinOps history is incomplete; submission cannot proceed")
        conversations = app.locator(self.surface["conversation_selector"])
        count = await conversations.count()
        if count > 25:
            raise ValueError("FinOps conversation history exceeds bounded recovery")
        found = []
        self.conversations = []
        for index in range(count):
            conversation = conversations.nth(index)
            conversation_id = await self._conversation_id(conversation)
            if not conversation_id:
                raise ValueError("native conversation identity unavailable")
            if conversation_id in self.conversations:
                raise ValueError("native conversation identity is not unique")
            self.conversations.append(conversation_id)
            if intent.get("conversation_id") and conversation_id != intent["conversation_id"]:
                continue
            await conversation.click()
            if mode:
                await app.wait_for_url(
                    lambda address: urlsplit(str(address)).fragment == f"/?c={conversation_id}", timeout=15000)
            turns = app.locator(self.surface["turn_selector"])
            if not mode:
                turns = turns.filter(has_text=intent["attempt_token"])
            if await turns.count() > 25:
                raise ValueError("FinOps turn history exceeds bounded recovery")
            for turn_index in range(await turns.count()):
                turn = turns.nth(turn_index)
                question = (turn.locator("p") if self.surface["question_selector"] == ":scope" else
                            turn.locator(self.surface["question_selector"]))
                if await question.count() != 1:
                    raise ValueError("native attempt question is unavailable")
                prompt = await question.text_content()
                if mode and intent["attempt_token"] not in prompt:
                    continue
                if prompt != self._prompt(intent):
                    raise ValueError("native attempt question is not exact")
                turn_locator = (f"ordinal:{turn_index}:{sha256(prompt.encode()).hexdigest()}" if mode else
                                await turn.get_attribute(self.surface["turn_receipt_attribute"]))
                if not turn_locator:
                    raise ValueError("native turn locator unavailable")
                turn_id = None if mode else await turn.get_attribute("data-turn-id")
                found.append({"attempt_token": intent["attempt_token"], "agent_id": intent["agent_id"],
                              "conversation_id": conversation_id, "question_hash": intent["question_hash"],
                              **({"turn_id": turn_id} if turn_id else {"turn_locator": turn_locator})})
                self.current_turn = (app, turn)
        if len(found) > 1:
            raise ValueError("multiple native turns match durable attempt")
        return found[0] if found else None

    @staticmethod
    async def _conversation_id(conversation):
        legacy = await conversation.get_attribute("data-conversation-id")
        if legacy:
            return legacy
        href = await conversation.get_attribute("href")
        if not href or not href.startswith("#/?c="):
            raise ValueError("native conversation URL is unavailable")
        parameters = parse_qs(href[3:], strict_parsing=True)
        if set(parameters) != {"c"} or len(parameters["c"]) != 1:
            raise ValueError("native conversation URL is ambiguous")
        conversation_id = parameters["c"][0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
            raise ValueError("native conversation URL identity is invalid")
        return conversation_id

    @staticmethod
    def _prompt(intent):
        return intent["question"] + "\nReference: " + intent["attempt_token"]

    async def send_once(self, intent):
        app = await self._app(intent["agent_id"], intent["agent_label"])
        selected = intent.get("conversation_id")
        if selected:
            if selected not in self.conversations:
                raise ValueError("original native conversation disappeared")
            conversations = app.locator(self.surface["conversation_selector"])
            matches = [conversations.nth(index) for index in range(await conversations.count())
                       if await self._conversation_id(conversations.nth(index)) == selected]
            if len(matches) != 1:
                raise ValueError("original native conversation disappeared")
            await matches[0].click()
        else:
            if len(self.conversations) >= 25:
                raise ValueError("FinOps conversation history exceeds bounded recovery")
            start = app.get_by_role("button", name="New chat", exact=True)
            if await start.count() != 1:
                raise ValueError("FinOps new conversation action is unavailable")
            await start.click()
        composers = app.locator("textarea")
        if await composers.count() != 1 or not isinstance(intent["question"], str) or not intent["question"].strip():
            raise ValueError("FinOps inquiry composer is unavailable")
        await composers.fill(self._prompt(intent))
        send = app.get_by_role("button", name=("Send message" if
                                self.surface["turn_receipt_attribute"] == "ordinal" else "Send"), exact=True)
        if await send.count() != 1:
            raise ValueError("FinOps inquiry send action is unavailable")
        await send.click()

    async def read_turn(self, native_job_ref, intent):
        result = await self.find_turn({**intent, "conversation_id": native_job_ref["conversation_id"]})
        if result is None:
            return None
        if result != {"attempt_token": intent["attempt_token"], "agent_id": native_job_ref["agent_id"],
                      "conversation_id": native_job_ref["conversation_id"],
                      "question_hash": native_job_ref["question_hash"],
                      **({"turn_id": native_job_ref["turn_id"]} if "turn_id" in native_job_ref else
                         {"turn_locator": native_job_ref["turn_locator"]})}:
            raise ValueError("FinOps UI readback differs from pinned turn")
        _, turn = self.current_turn
        if self.surface["turn_receipt_attribute"] == "ordinal":
            if (self.surface["answer_classification_selector"] != "feedback_controls"
                    or self.surface["answer_selector"] != ".chat-markdown"):
                raise ValueError("native answer text is not isolated from console controls")
            wrappers = turn.locator("xpath=ancestor::*[contains(@class, 'awsui_child_')][1]")
            if await wrappers.count() != 1:
                raise ValueError("native question message wrapper is unavailable")
            sibling = wrappers.locator("xpath=following-sibling::*[1]")
            if await sibling.count() == 0:
                return {"state": "running", "answer": None, "answer_status": None}
            if (await sibling.count() != 1 or not await sibling.evaluate(
                    "(node) => node.className.includes('awsui_child_')")):
                raise ValueError("native answer is not adjacent to pinned question")
            incoming = sibling.locator(self.surface["turn_status_selector"])
            if await incoming.count() != 1:
                raise ValueError("native answer is not adjacent to pinned question")
            for label in ("Helpful", "Not helpful", "Copy response"):
                if await sibling.get_by_role("button", name=label, exact=True).count() != 1:
                    raise ValueError("native terminal answer controls are unavailable")
            answers = incoming.locator(self.surface["answer_selector"])
            if await answers.count() != 1:
                raise ValueError("native answer text is not uniquely isolated")
            answer = await answers.inner_text()
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("native answer text is unavailable")
            return {"state": "completed", "answer": answer, "answer_status": "answered"}
        status = turn.locator(self.surface["turn_status_selector"])
        if await status.count() != 1:
            raise ValueError("native turn status is unavailable")
        state = await status.get_attribute("data-state")
        if state not in {"running", "completed", "failed", "stopped"}:
            raise ValueError("native turn status is unqualified")
        answer = None
        answer_status = None
        if state == "completed":
            answers = turn.locator(self.surface["answer_selector"])
            classifications = turn.locator(self.surface["answer_classification_selector"])
            if await answers.count() != 1 or await classifications.count() != 1:
                raise ValueError("native answer is not uniquely rendered")
            answer = await answers.inner_text()
            answer_status = await classifications.get_attribute("data-outcome")
        return {"state": state, "answer": answer, "answer_status": answer_status}

    async def stop_turn(self, native_job_ref):
        if self.current_turn is None:
            return False
        app, _ = self.current_turn
        buttons = app.get_by_role("button", name="Cancel", exact=True)
        if await buttons.count() != 1:
            return False
        await buttons.click()
        return True
