"""Compare discovered identities with independent read-only provider observations."""


def assert_source_reuse(repository, environment, provider_repository, provider_environment):
    assert repository["kind"] == "repository"
    assert repository["namespace"] == str(provider_repository["owner"]["id"])
    assert repository["native_id"] == str(provider_repository["id"])
    assert repository["source_revision"] == provider_repository["updated_at"]

    assert environment["kind"] == "environment"
    verified = environment["verified_environment"]
    assert verified["provider"] == "aws"
    assert environment["namespace"] == verified["account_id"] == provider_environment["account_id"]
    assert environment["native_id"] == provider_environment["region"]
    assert verified["regions"] == [provider_environment["region"]]
    assert verified["role_ref"] == provider_environment["role_ref"]
    assert verified["source_revision"] == environment["source_revision"]
    assert verified["verified_at"] == environment["observed_at"]
