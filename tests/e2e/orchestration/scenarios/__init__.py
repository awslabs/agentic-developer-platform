"""Q2 registry. Import is network-free; ordinary CI never executes live adapters."""


class DeliveryAdapter:
    def planned_fixtures(self, config):
        from tests.e2e.orchestration.fixtures import FixtureRequest

        return [
            FixtureRequest(
                "story-1", "qualification-issue", "<qualification-id>/story-1"
            ),
            FixtureRequest(
                "story-2", "qualification-issue", "<qualification-id>/story-2"
            ),
            FixtureRequest("flow", "qualification-flow", "<qualification-id>/flow"),
        ]

    def fixture_providers(self, config):
        from .delivery import FlowProvider
        from .http import Client
        from .manifest import load_manifest
        from .providers import IssueProvider

        manifest, _ = load_manifest(config)
        client = Client(config, manifest)
        return IssueProvider(client), FlowProvider(client)

    def execute(self, *, config, inventory, providers):
        from .delivery import execute

        return execute(config, inventory, providers)


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
