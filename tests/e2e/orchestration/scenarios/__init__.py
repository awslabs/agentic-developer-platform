"""Q2 registry. Import is network-free; ordinary CI never executes live adapters."""


class DeliveryAdapter:
    def planned_fixtures(self, config):
        from tests.e2e.orchestration.fixtures import FixtureRequest

        base = [
            FixtureRequest(
                "story-1", "qualification-issue", "<qualification-id>/story-1"
            ),
            FixtureRequest(
                "story-2", "qualification-issue", "<qualification-id>/story-2"
            ),
            FixtureRequest("flow", "qualification-flow", "<qualification-id>/flow"),
        ]
        base.extend(
            FixtureRequest(
                f"story-{index}-{kind}",
                "qualification-" + kind,
                f"<qualification-id>/story-{index}-{kind}",
            )
            for index in (1, 2)
            for kind in ("pr", "branch")
        )
        base.append(
            FixtureRequest(
                "refusal", "qualification-flow", "<qualification-id>/refusal"
            )
        )
        base.append(
            FixtureRequest(
                "worker-loss", "qualification-worker", "<qualification-id>/worker-loss"
            )
        )
        for suffix, kind in (
            ("namespace", "qualification-namespace"),
            ("network", "qualification-network-policy"),
            ("deployment", "qualification-runtime"),
        ):
            base.append(
                FixtureRequest(
                    "runtime-" + suffix, kind, "<qualification-id>/runtime-" + suffix
                )
            )
        base.extend(
            FixtureRequest(name, "qualification-flow", "<qualification-id>/" + name)
            for name in ("allowance", "revocation")
        )
        base.extend(
            FixtureRequest(name, kind, "<qualification-id>/" + name)
            for name, kind in (
                ("stop", "qualification-flow"),
                ("stop-issue", "qualification-stop-issue"),
                ("worker-stop", "qualification-worker"),
            )
        )
        return base

    def fixture_providers(self, config):
        from .delivery import FlowProvider
        from .http import Client
        from .manifest import load_manifest
        from .providers import IssueProvider
        from .workers import WorkerProvider
        from .delivery_resources import DeliveryResourceProvider
        from .runtime_faults import RuntimeFixtureProvider
        from .stop import StopIssueProvider

        manifest, _ = load_manifest(config)
        client = Client(config, manifest)
        return (
            *(
                RuntimeFixtureProvider(client, manifest, kind)
                for kind in (
                    "qualification-namespace",
                    "qualification-network-policy",
                    "qualification-runtime",
                )
            ),
            IssueProvider(client),
            StopIssueProvider(client),
            FlowProvider(client),
            WorkerProvider.for_config(config, manifest),
            DeliveryResourceProvider(client, "qualification-pr"),
            DeliveryResourceProvider(client, "qualification-branch"),
        )

    def execute(self, *, config, inventory, providers):
        from .delivery import execute

        return execute(config, inventory, providers)

    def execute_evaluation(self, *, config, inventory, providers, context):
        from .release import evaluate_release

        return evaluate_release(config=config, inventory=inventory, context=context)


def connection_resolver(config):
    if not config.scenario_manifest:
        return None
    from .http import Client
    from .manifest import load_manifest, verify_checkout
    from tests.e2e.orchestration.config import ConfigError

    try:
        manifest, _ = load_manifest(config)
        verify_checkout(config)
    except (ValueError, OSError) as exc:
        raise ConfigError([str(exc)]) from None
    return Client(config, manifest)


REGISTRY = {"autonomous-delivery": DeliveryAdapter()}
