"""UI-only executor must not repeat a paid turn after an uncertain response."""

import io
import json
from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError

from l5_services.adapters.finops.console_executor import (
    ConsoleFinOpsExecutor, FinOpsExecutorEvidence, PlaywrightFinOpsBrowser, PlaywrightFinOpsPage,
)


SURFACE = {"agent_label": "Example FinOps Agent", "agent_identity_selector": "[data-agent-id]",
           "conversation_selector": "[data-conversation-id]", "turn_selector": "[data-turn-id]",
           "question_selector": "[data-question]", "turn_receipt_attribute": "data-turn-id",
           "turn_status_selector": "[data-state]", "answer_selector": "[data-answer]",
           "answer_classification_selector": "[data-outcome]",
           "history_complete_selector": "[data-complete]", "surface_revision": "example-reviewed-ui"}


class Store:
    def __init__(self):
        self.saved = None
        self.answer = None

    async def claim_intent(self, intent, provider_identity):
        value = {"intent": intent, "provider_identity": provider_identity}
        if self.saved is not None:
            assert self.saved == value
            return False
        self.saved = value
        return True

    async def read_intent(self, run_id):
        return self.saved

    async def put_answer(self, run_id, content, observed_at):
        if self.answer is None:
            self.answer = {"artifact": {"artifact_id": f"l5/finops/work/{run_id}/native/answer",
                                        "version": "version-one", "sha256": sha256(content).hexdigest()},
                           "observed_at": observed_at}
        return self.answer


class Browser:
    def __init__(self):
        self.turn = None
        self.sent = []
        self.answer = ""
        self.state = "running"

    @asynccontextmanager
    async def session(self, authority, surface):
        assert surface is SURFACE
        yield self

    async def find_turn(self, intent):
        return self.turn

    async def send_once(self, intent):
        self.sent.append(intent)
        self.turn = {"attempt_token": intent["attempt_token"], "agent_id": intent["agent_id"],
                     "question_hash": intent["question_hash"], "conversation_id": "conversation-example",
                     "turn_id": "turn-example"}

    async def read_turn(self, native_job_ref, intent):
        if not self.turn:
            return None
        assert self.turn["attempt_token"] == intent["attempt_token"]
        return {"state": self.state, "answer": self.answer, "answer_status": "answered"}

    async def stop_turn(self, native_job_ref):
        self.state = "stopped"
        return True


def case():
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    authority = {"operation_id": "00000000-0000-0000-0000-000000000001", "attempt_id": "work-example",
                 "lease_fence": 2, "provider_identity": identity, "tenant_id": "tenant-example",
                 "project_id": "project-example", "product_id": "product-example"}
    intent = {"run_id": authority["operation_id"], "tenant_id": "tenant-example",
              "project_id": "project-example", "product_id": "product-example", "agent_id": "agent-example",
              "agent_label": "Example FinOps Agent",
              "question": "What were costs?", "question_hash": sha256(b"What were costs?").hexdigest(),
              "attempt_token": "opaque-example", "billing_scope": {"scope_digest": "a" * 64},
              "query": {"metric": "UnblendedCost"}}
    browser, store = Browser(), Store()
    verifier = AsyncMock(return_value=authority)
    executor = ConsoleFinOpsExecutor(browser, verifier, store, SURFACE)
    return executor, browser, store, authority, intent, verifier


async def test_ui_submit_recovery_and_answer_require_current_scope():
    executor, browser, store, authority, intent, verifier = case()
    receipt = await executor.submit(intent=intent, authority=authority)
    assert receipt["native_job_ref"]["conversation_id"] == "conversation-example"
    assert receipt["native_job_ref"]["product_id"] == intent["product_id"]
    assert receipt["native_job_ref"]["turn_id"] == "turn-example"
    assert len(browser.sent) == 1
    assert await executor.read_submit(intent=intent, authority=authority) == receipt
    assert await executor.submit(intent=intent, authority=authority) == receipt
    assert len(browser.sent) == 1
    native = receipt["native_job_ref"]
    with pytest.raises(PermissionError, match="retained attempt"):
        await executor.observe(native_job_ref={**native, "product_id": "another-product"}, authority=authority)
    with pytest.raises(PermissionError, match="retained attempt"):
        await executor.read_answer(native_job_ref={**native, "product_id": "another-product"}, authority=authority)
    assert len(browser.sent) == 1
    browser.state = "completed"
    browser.answer = json.dumps({"kind": "finops_native_cost", "status": "answered",
                                 "question_hash": intent["question_hash"],
                                 "scope_digest": intent["billing_scope"]["scope_digest"],
                                 "query": intent["query"], "coverage": "complete",
                                 "amount": "1.00", "currency": "USD", "series": []})
    assert (await executor.observe(native_job_ref=native, authority=authority))["outcome"] == "completed"
    answer = await executor.read_answer(native_job_ref=native, authority=authority)
    assert answer["kind"] == "finops_native_answer"
    assert answer["status"] == "answered"
    cost = await executor.read_native_cost(native_job_ref=native, authority=authority)
    assert cost["answer_artifact"] == answer["answer_artifact"]
    assert cost["observed_at"] == answer["observed_at"]
    foreign = {**authority, "provider_identity": {
        **authority["provider_identity"], "account_id": "000000000001"}}
    verifier.return_value = foreign
    with pytest.raises(PermissionError, match="retained scope"):
        await executor.read_submit(intent=intent, authority=foreign)
    verifier.return_value = {**authority, "lease_fence": 4}
    with pytest.raises(PermissionError, match="current FinOps executor work"):
        await executor.observe(native_job_ref=native, authority=authority)
    assert len(browser.sent) == 1


async def test_lost_ui_identity_never_repeats_a_submitted_turn():
    executor, browser, store, authority, intent, _ = case()
    browser.send_once = AsyncMock(side_effect=lambda *args: browser.sent.append(args))
    assert await executor.submit(intent=intent, authority=authority) is None
    assert len(browser.sent) == 1
    assert await executor.submit(intent=intent, authority=authority) is None
    assert await executor.read_submit(intent=intent, authority=authority) is None
    assert len(browser.sent) == 1
    browser.turn = {"attempt_token": intent["attempt_token"], "agent_id": intent["agent_id"],
                    "question_hash": intent["question_hash"], "conversation_id": "conversation-example"}
    with pytest.raises(ValueError, match="unique UI identity"):
        await executor.read_submit(intent=intent, authority=authority)
    assert len(browser.sent) == 1


async def test_revocation_after_intent_claim_cannot_start_a_late_turn():
    executor, browser, store, authority, intent, verifier = case()
    verifier.side_effect = [authority, {**authority, "lease_fence": 3}]
    with pytest.raises(PermissionError, match="current FinOps executor work"):
        await executor.submit(intent=intent, authority=authority)
    assert store.saved["intent"] == intent
    assert browser.sent == []
    verifier.side_effect = None
    verifier.return_value = authority
    assert await executor.submit(intent=intent, authority=authority) is None
    assert browser.sent == []


async def test_qualified_unique_locator_without_provider_turn_id_is_recoverable():
    executor, browser, _, authority, intent, _ = case()
    intent["conversation_id"] = "conversation-example"
    browser.send_once = AsyncMock(side_effect=lambda *args: browser.sent.append(args))
    assert await executor.submit(intent=intent, authority=authority) is None
    browser.turn = {"attempt_token": intent["attempt_token"], "agent_id": intent["agent_id"],
                    "question_hash": intent["question_hash"], "conversation_id": "conversation-example",
                    "turn_locator": "ui-receipt-example"}
    receipt = await executor.read_submit(intent=intent, authority=authority)
    assert receipt["native_job_ref"]["turn_locator"] == "ui-receipt-example"
    assert "turn_id" not in receipt["native_job_ref"]
    assert len(browser.sent) == 1
    browser.turn["question_hash"] = "wrong-question"
    with pytest.raises(ValueError, match="unique UI identity"):
        await executor.read_submit(intent=intent, authority=authority)
    browser.turn["question_hash"] = intent["question_hash"]
    browser.turn["conversation_id"] = "foreign-conversation"
    with pytest.raises(ValueError, match="unique UI identity"):
        await executor.read_submit(intent=intent, authority=authority)


async def test_page_reuses_original_conversation_and_exact_receipt():
    class Node:
        def __init__(self, attributes=None, text=""):
            self.attributes = attributes or {}
            self.text = text

        async def count(self):
            return 1

        async def get_attribute(self, name):
            return self.attributes.get(name)

        async def inner_text(self):
            return self.text

        async def text_content(self):
            return self.text

    class Conversation(Node):
        async def click(self):
            app.active = self.attributes["data-conversation-id"]

    class Conversations:
        async def count(self):
            return len(conversations)

        def nth(self, index):
            return conversations[index]

    class Turn(Node):
        def locator(self, selector):
            if selector == "[data-question]":
                return Node(text=self.text)
            return Node({"data-state": "completed", "data-outcome": "answered"}, "Native answer")

    class Turns:
        token = None

        def filter(self, *, has_text):
            self.token = has_text
            return self

        def matched(self):
            return [turn for turn in app.turns[app.active] if self.token in turn.text]

        async def count(self):
            return len(self.matched())

        def nth(self, index):
            return self.matched()[index]

    class Composer(Node):
        async def fill(self, text):
            self.text = text

        async def press(self, key):
            assert key == "Enter"
            app.sent.append((app.active, self.text))
            app.turns[app.active].append(Turn({"data-turn-receipt": "receipt-example"}, self.text))

    class App:
        active = "other"

        def __init__(self):
            self.turns = {"original": [], "other": [], "third": []}
            self.sent = []
            self.composer = Composer()

        def locator(self, selector):
            return {"[data-complete]": Node({"data-complete": "true"}),
                    "[data-conversation-id]": Conversations(),
                    "[data-turn-receipt]": Turns(), "textarea": self.composer}[selector]

        def get_by_role(self, role, *, name, exact):
            assert role == "button" and exact is True
            if name == "New chat":
                class Start(Node):
                    async def click(self):
                        conversations.append(Conversation({"data-conversation-id": "fresh"}))
                        app.turns["fresh"] = []
                        app.active = "fresh"
                return Start()
            assert name == "Send"
            class Send(Node):
                async def click(self):
                    await app.composer.press("Enter")
            return Send()

    app = App()
    conversations = [Conversation({"data-conversation-id": value}) for value in ("original", "other", "third")]
    surface = {**SURFACE, "turn_selector": "[data-turn-receipt]",
               "turn_receipt_attribute": "data-turn-receipt"}
    page = PlaywrightFinOpsPage(None, None, surface, "https://us-east-1.console.aws.amazon.com/",
                            "agent-example.webapp.finops-agent.global.app.aws")
    page.app = app
    _, _, _, _, intent, _ = case()
    assert await page.find_turn(intent) is None
    assert app.active == "third"
    intent["conversation_id"] = "original"
    assert await page.find_turn(intent) is None
    await page.send_once(intent)
    assert app.sent == [("original", intent["question"] + "\nReference: " + intent["attempt_token"])]
    observed = await page.find_turn(intent)
    assert observed["conversation_id"] == "original" and observed["turn_locator"] == "receipt-example"
    native = {"tenant_id": intent["tenant_id"], "product_id": intent["product_id"],
              "project_id": intent["project_id"],
              "agent_id": intent["agent_id"], "conversation_id": "original",
              "turn_locator": "receipt-example", "question_hash": intent["question_hash"]}
    assert (await page.read_turn(native, intent))["answer"] == "Native answer"
    app.turns["original"][0].text = "similar but different question\nReference: " + intent["attempt_token"]
    with pytest.raises(ValueError, match="question is not exact"):
        await page.read_turn(native, intent)
    fresh_intent = {**intent, "attempt_token": "fresh-token-example"}
    fresh_intent.pop("conversation_id")
    assert await page.find_turn(fresh_intent) is None
    await page.send_once(fresh_intent)
    assert app.sent[-1] == ("fresh", fresh_intent["question"] + "\nReference: fresh-token-example")
    assert (await page.find_turn(fresh_intent))["conversation_id"] == "fresh"
    conversations.clear()
    app.turns.clear()
    empty_intent = {**intent, "attempt_token": "empty-token-example"}
    empty_intent.pop("conversation_id")
    assert await page.find_turn(empty_intent) is None
    await page.send_once(empty_intent)
    assert (await page.find_turn(empty_intent))["conversation_id"] == "fresh"
    assert len(app.sent) == 3


async def test_visible_ui_conversation_url_and_exact_ordinal_receipt():
    executor, browser, store, authority, intent, _ = case()
    intent["conversation_id"] = "shortID"
    visible = {**SURFACE, "conversation_selector": 'a[href^="#/?c="]',
               "history_complete_selector": ".chat-active-messages",
               "turn_selector": '[class*="awsui_chat-bubble-type-outgoing_"]',
               "question_selector": ":scope", "turn_receipt_attribute": "ordinal",
               "turn_status_selector": '[class*="awsui_chat-bubble-type-incoming_"]',
               "answer_selector": ".chat-markdown", "answer_classification_selector": "feedback_controls"}
    executor.surface = visible
    page = PlaywrightFinOpsPage(None, None, visible, "https://us-east-1.console.aws.amazon.com/",
                            "agent-example.webapp.finops-agent.global.app.aws")

    class Element:
        def __init__(self, text="", attributes=None):
            self.text, self.attributes = text, attributes or {}
            self.controls = {"Helpful", "Not helpful", "Copy response"}
            self.wrapper = None
            self.next_wrapper = None
            self.incoming = None
            self.answer_text = None

        async def count(self):
            return 1

        async def inner_text(self):
            if "outgoing" in self.attributes.get("class", ""):
                return self.text.replace("\n", " ") + " 12:34 PM"
            return self.text

        async def text_content(self):
            return self.text

        async def get_attribute(self, name):
            return self.attributes.get(name)

        async def click(self):
            app.current = self.attributes.get("href", "").split("c=")[-1]

        def locator(self, selector):
            if selector == "p":
                return Element(text=self.text)
            if selector == "xpath=ancestor::*[contains(@class, 'awsui_child_')][1]":
                return self.wrapper or List([])
            if selector == "xpath=following-sibling::*[1]":
                return self.next_wrapper or List([])
            if selector == visible["turn_status_selector"]:
                return self.incoming or List([])
            if selector == ".chat-markdown":
                return Element(text=self.answer_text) if self.answer_text is not None else List([])
            raise AssertionError("unexpected console selector")

        def get_by_role(self, role, *, name, exact):
            assert (role, exact) == ("button", True)
            controls = self.incoming.controls if self.incoming is not None else self.controls
            return List([Element()]) if name in controls else List([])

        async def evaluate(self, script):
            assert "className.includes" in script
            return "awsui_child_" in self.attributes.get("class", "")

    class List:
        def __init__(self, items):
            self.items = items

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class App:
        current = "other"

        def __init__(self):
            self.incoming = Element(attributes={"class": "awsui_chat-bubble-type-incoming_example"})
            self.incoming.answer_text = "Original native answer"
            self.answer_wrapper = Element(attributes={"class": "awsui_child_18582_example"})
            self.answer_wrapper.incoming = self.incoming
            self.question_wrapper = Element(attributes={"class": "awsui_child_18582_example"})
            self.question_wrapper.next_wrapper = self.answer_wrapper
            self.question_turn = Element(text=intent["question"] + "\nReference: " + intent["attempt_token"],
                                         attributes={"class": "awsui_chat-bubble-type-outgoing_example"})
            self.question_turn.wrapper = self.question_wrapper

        async def wait_for_url(self, predicate, **kwargs):
            assert predicate("https://agent-example.webapp.finops-agent.global.app.aws/#/?c=" + self.current)

        def locator(self, selector):
            return {visible["history_complete_selector"]: Element(),
                    visible["conversation_selector"]: List([
                        Element(attributes={"href": "#/?c=shortID"}),
                        Element(attributes={"href": "#/?c=other"}),
                        Element(attributes={"href": "#/?c=third"})]),
                    visible["turn_selector"]: (List([
                        Element(text="Earlier question"), self.question_turn
                    ]) if self.current == "shortID" else List([]))}[selector]

    app = App()
    page.app = app
    observed = await page.find_turn(intent)
    assert observed["conversation_id"] == "shortID"
    assert observed["question_hash"] == intent["question_hash"]
    assert observed["turn_locator"].startswith("ordinal:1:")
    assert "turn_id" not in observed
    native = {"tenant_id": intent["tenant_id"], "product_id": intent["product_id"],
              "project_id": intent["project_id"], "agent_id": intent["agent_id"],
              "conversation_id": "shortID", "turn_locator": observed["turn_locator"],
              "question_hash": intent["question_hash"]}
    assert await page.read_turn(native, intent) == {
        "state": "completed", "answer": "Original native answer", "answer_status": "answered"}
    app.question_wrapper.next_wrapper = None
    assert (await page.read_turn(native, intent))["state"] == "running"
    app.question_wrapper.next_wrapper = Element(attributes={"class": "other-bubble"})
    with pytest.raises(ValueError, match="adjacent"):
        await page.read_turn(native, intent)
    app.question_wrapper.next_wrapper = app.answer_wrapper
    app.answer_wrapper.incoming = None
    with pytest.raises(ValueError, match="adjacent"):
        await page.read_turn(native, intent)
    app.answer_wrapper.incoming = app.incoming
    app.incoming.controls.remove("Copy response")
    with pytest.raises(ValueError, match="controls"):
        await page.read_turn(native, intent)
    app.incoming.controls.add("Copy response")
    app.incoming.answer_text = None
    with pytest.raises(ValueError, match="isolated"):
        await page.read_turn(native, intent)
    page.surface = {**visible, "answer_selector": ".answer-text"}
    with pytest.raises(ValueError, match="isolated"):
        await page.read_turn(native, intent)
    assert app.current == "shortID"
    assert await page._conversation_id(Element(attributes={"href": "#/?c=shortID"})) == "shortID"
    with pytest.raises(ValueError, match="ambiguous"):
        await page._conversation_id(Element(attributes={"href": "#/?c=shortID&c=other"}))
    assert store.saved is None and browser.sent == []


async def test_ordinal_new_chat_recovers_unique_token_across_bounded_history():
    _, _, _, _, intent, _ = case()
    visible = {**SURFACE, "conversation_selector": 'a[href^="#/?c="]',
               "history_complete_selector": ".chat-active-messages",
               "turn_selector": '[class*="awsui_chat-bubble-type-outgoing_"]',
               "question_selector": ":scope", "turn_receipt_attribute": "ordinal"}

    class Node:
        def __init__(self, text="", href=None):
            self.text, self.href = text, href

        async def count(self):
            return 1

        async def inner_text(self):
            return self.text

        async def text_content(self):
            return self.text

        def locator(self, selector):
            assert selector == "p"
            return self

        async def get_attribute(self, name):
            return self.href if name == "href" else None

        async def click(self):
            app.active = self.href.split("c=")[-1]

        async def fill(self, value):
            self.text = value

    class List:
        def __init__(self, values):
            self.values = values

        async def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class App:
        def __init__(self):
            self.history = ["old", "other", "third"]
            self.turns = {name: [] for name in self.history}
            self.active = "third"
            self.composer = Node()
            self.sends = 0

        async def wait_for_url(self, predicate, **kwargs):
            assert predicate("https://agent-example.webapp.finops-agent.global.app.aws/#/?c=" + self.active)

        def locator(self, selector):
            if selector == visible["history_complete_selector"]:
                return Node()
            if selector == visible["conversation_selector"]:
                return List([Node(href="#/?c=" + name) for name in self.history])
            if selector == visible["turn_selector"]:
                return List(self.turns[self.active])
            if selector == "textarea":
                return self.composer
            raise AssertionError("unexpected console selector")

        def get_by_role(self, role, *, name, exact):
            assert role == "button" and exact is True
            if name == "New chat":
                async def start():
                    self.active = "newchat"
                    self.history.append(self.active)
                    self.turns[self.active] = []
                return SimpleNamespace(count=Node().count, click=start)
            if name == "Send message":
                async def send():
                    self.sends += 1
                    self.turns[self.active].append(Node(text=self.composer.text))
                return SimpleNamespace(count=Node().count, click=send)
            raise AssertionError("unexpected native action")

    app = App()
    page = PlaywrightFinOpsPage(None, None, visible, "https://us-east-1.console.aws.amazon.com/",
                                "agent-example.webapp.finops-agent.global.app.aws")
    page.app = app
    assert await page.find_turn(intent) is None
    await page.send_once(intent)
    recovered = await page.find_turn(intent)
    assert recovered["conversation_id"] == "newchat"
    assert recovered["turn_locator"].startswith("ordinal:0:")
    assert app.sends == 1
    app.history.clear()
    app.turns.clear()
    app.active = ""
    blank = {**intent, "attempt_token": "new-empty-token"}
    assert await page.find_turn(blank) is None
    await page.send_once(blank)
    assert (await page.find_turn(blank))["conversation_id"] == "newchat"
    assert app.sends == 2
    app.history = ["c" + str(index) for index in range(25)]
    app.turns = {name: [] for name in app.history}
    overflow = {**intent, "attempt_token": "over-limit-token"}
    assert await page.find_turn(overflow) is None
    with pytest.raises(ValueError, match="bounded recovery"):
        await page.send_once(overflow)
    assert app.sends == 2


async def test_visible_prompt_limit_prevents_a_paid_action():
    executor, browser, store, authority, intent, _ = case()
    intent["question"] = "C" * 925
    intent["attempt_token"] = "a" * 64
    with pytest.raises(ValueError, match="visible console prompt limit"):
        await executor.submit(intent=intent, authority=authority)
    assert not browser.sent and store.saved is None


async def test_browser_only_allows_selected_aws_hosts():
    browser = PlaywrightFinOpsBrowser("us-east-1")
    for url, allowed in (
        ("https://signin.aws.amazon.com/federation", True),
        ("https://us-east-1.console.aws.amazon.com/finops-agent/home", True),
        ("https://finops-agent.us-east-1.amazonaws.com", True),
        ("https://finops-agent.eu-west-1.amazonaws.com", False),
        ("https://agent-example.webapp.finops-agent.global.app.aws/#/?c=example", True),
        ("https://other-agent.webapp.finops-agent.global.app.aws/", False),
        ("https://agent-example.webapp.finops-agent.global.app.aws.evil.invalid/", False),
        ("https://foreign.example.invalid", False),
        ("http://signin.aws.amazon.com/federation", False),
    ):
        route = SimpleNamespace(request=SimpleNamespace(url=url), continue_=AsyncMock(), abort=AsyncMock())
        await browser._restrict_egress(route, "agent-example.webapp.finops-agent.global.app.aws")
        assert route.continue_.await_count == int(allowed)
        assert route.abort.await_count == int(not allowed)
    with pytest.raises(ValueError, match="region"):
        PlaywrightFinOpsBrowser("https://foreign.example.invalid")


async def test_native_evidence_claim_and_answer_are_immutable_and_versioned():
    class StoreClient:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch):
            assert IfNoneMatch == "*"
            if Key in self.objects:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.objects[Key] = (Body, Metadata)
            return {"VersionId": "version-one"}

        def get_object(self, *, Bucket, Key):
            content, metadata = self.objects[Key]
            return {"VersionId": "version-one", "ContentLength": len(content),
                    "Metadata": metadata, "Body": io.BytesIO(content)}

    client = StoreClient()
    evidence = FinOpsExecutorEvidence("example-private-bucket", client)
    _, _, _, authority, intent, _ = case()
    identity = authority["provider_identity"]
    assert await evidence.claim_intent(intent, identity) is True
    assert await evidence.claim_intent(intent, identity) is False
    assert await evidence.read_intent(intent["run_id"]) == {"intent": intent, "provider_identity": identity}
    with pytest.raises(ValueError, match="other input"):
        await evidence.claim_intent({**intent, "question": "changed"}, identity)
    answer = b"Native answer, no credentials"
    first = await evidence.put_answer(intent["run_id"], answer, "2026-02-01T00:00:00Z")
    second = await evidence.put_answer(intent["run_id"], answer, "2026-02-02T00:00:00Z")
    assert second == first
    with pytest.raises(ValueError, match="other input"):
        await evidence.put_answer(intent["run_id"], b"changed terminal answer", "2026-02-02T00:00:00Z")
    client.objects[first["artifact"]["artifact_id"]] = (b"changed", {"sha256": "0" * 64})
    with pytest.raises(ValueError, match="digest changed"):
        await evidence.put_answer(intent["run_id"], answer, "2026-02-02T00:00:00Z")


async def test_stop_rechecks_the_original_turn_after_ui_action():
    executor, browser, _, authority, intent, _ = case()
    native = (await executor.submit(intent=intent, authority=authority))["native_job_ref"]
    stopped = await executor.stop(native_job_ref=native, authority=authority)
    assert stopped["outcome"] == "stopped"
    assert stopped["native_job_ref"] == native
    assert browser.state == "stopped"
    assert len(browser.sent) == 1


async def test_ui_refuses_agent_other_than_sql_pinned_app_before_navigation():
    page = PlaywrightFinOpsPage(None, None, {**SURFACE, "agent_identity_selector": ":scope"},
                                "https://us-east-1.console.aws.amazon.com/",
                                "agent-example.webapp.finops-agent.global.app.aws")
    with pytest.raises(PermissionError, match="durable Product selection"):
        await page._app("another-agent", "Other Agent")


def test_executor_surface_does_not_select_an_environment_wide_agent_label():
    executor, browser, store, _, _, verifier = case()
    surface = {key: value for key, value in SURFACE.items() if key != "agent_label"}
    selected = ConsoleFinOpsExecutor(browser, verifier, store, surface)
    assert selected.surface is surface
    assert executor.surface is SURFACE


async def test_adoption_uses_current_product_label_and_read_only_browser():
    _, _, store, authority, _, _ = case()
    authority = {**authority, "work_kind": "setup"}
    reference = {"record_id": "agent-example", "record_type": "finops_agent"}
    selected = {**authority, "native_resource_ref": reference,
                "setup_verified": {"finops": {"native_agent_display_name": "Product-selected Agent"}}}
    page = SimpleNamespace(read_agent=AsyncMock(return_value={"state": "ready", "native_agent_display_name": "Product-selected Agent"}))

    @asynccontextmanager
    async def session(verified, surface):
        assert verified is selected
        yield page

    surface = {**SURFACE, "data_role_selector": "[data-role]"}
    verifier = AsyncMock(return_value=selected)
    executor = ConsoleFinOpsExecutor(SimpleNamespace(session=session), verifier, store, surface)
    result = await executor.read_agent(native_job_ref=reference, authority=authority)
    assert result["native_resource_ref"] == reference
    page.read_agent.assert_awaited_once_with("agent-example", "Product-selected Agent")
    assert store.saved is None
    selected["setup_verified"]["finops"].clear()
    with pytest.raises(PermissionError, match="scoped native settings"):
        await executor.read_agent(native_job_ref=reference, authority=authority)
    page.read_agent.assert_awaited_once()


async def test_adoption_reads_native_data_role_without_a_billing_view_field():
    role = "arn:aws:iam::123456789012:role/ExampleData"
    label = SimpleNamespace(count=AsyncMock(return_value=1), text_content=AsyncMock(return_value="Example Agent"))
    row = SimpleNamespace(get_by_text=Mock(return_value=label))
    page = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(filter=Mock(return_value=row))), get_by_text=Mock())
    selected = SimpleNamespace(count=AsyncMock(return_value=1), text_content=AsyncMock(return_value=role))
    app = SimpleNamespace(locator=Mock(return_value=selected))
    reader = PlaywrightFinOpsPage(page, None, {**SURFACE, "data_role_selector": "[data-role]"}, "", "")
    reader._app = AsyncMock(return_value=app)
    observed = await reader.read_agent("agent-example", "Example Agent")
    assert observed["data_role_ref"] == role and observed["state"] == "ready"
    assert observed["native_agent_display_name"] == "Example Agent"
    assert "view_ref" not in observed
    app.locator.assert_called_once_with("[data-role]")


async def test_product_label_selects_only_matching_native_host():
    selected = object()
    matching = SimpleNamespace(url="https://agent-example.webapp.finops-agent.global.app.aws/#/?c=example")
    open_button = SimpleNamespace(click=AsyncMock())

    class Rows:
        def filter(self, *, has):
            assert has is selected
            return self

        async def count(self):
            return 1

        def get_by_role(self, role, *, name, exact):
            assert (role, name, exact) == ("button", "Open", True)
            return open_button

    class Page:
        async def goto(self, url, **kwargs):
            assert url.endswith("#/agents")

        def locator(self, selector):
            assert selector == "tr"
            return Rows()

        def get_by_text(self, text, *, exact):
            assert (text, exact) == ("Product-owned Agent", True)
            return selected

        @asynccontextmanager
        async def expect_popup(self, **kwargs):
            async def opened_page():
                return matching

            yield SimpleNamespace(value=opened_page())

    surface = {**SURFACE, "agent_identity_selector": ":scope"}
    page = PlaywrightFinOpsPage(Page(), None, surface,
                                "https://us-east-1.console.aws.amazon.com/finops-agent/home",
                                "agent-example.webapp.finops-agent.global.app.aws")
    assert await page._app("agent-example", "Product-owned Agent") is matching
    open_button.click.assert_awaited_once()
    wrong = PlaywrightFinOpsPage(Page(), None, surface,
                                 "https://us-east-1.console.aws.amazon.com/finops-agent/home",
                                 "other-agent.webapp.finops-agent.global.app.aws")
    with pytest.raises(ValueError, match="allowed provider origin"):
        await wrong._app("other-agent", "Product-owned Agent")
