"""Compare cross-client onboarding observations with a fresh Gateway read."""


def assert_resume(expected, client_observation, api_readback, *, product_id, project_id):
    for observation in (expected, client_observation, api_readback):
        assert observation["product_id"] == product_id
        assert observation["project_id"] == project_id
        assert observation["id"] == expected["id"]
        assert observation["revision"] == expected["revision"]
        assert observation["steps"] == expected["steps"]
        assert observation["tenant_id"] == expected["tenant_id"]
        assert observation["resume_path"] == expected["resume_path"]
    assert client_observation == api_readback
