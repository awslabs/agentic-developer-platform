"""Storage names must not turn another repository's content into an allowed hit."""

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from door.acl import CallerPrincipal, filter_results
from door.browse_backend import browse


class _FakeACLStore:
    def __init__(self, allowed):
        self.allowed = allowed

    def get_allowed_repos(self, caller):
        return self.allowed


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.requested_keys = []

    def get_object(self, *, Bucket, Key):
        self.requested_keys.append(Key)
        return {"Body": BytesIO(self.objects[Key])}

    def list_objects_v2(self, *, Bucket, Prefix, Delimiter):
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(Prefix)]}


@pytest.mark.parametrize("action", ["read", "list"])
@pytest.mark.parametrize("reverse_catalog", [False, True])
async def test_colliding_names_cannot_inherit_the_callers_scope(action, reverse_catalog):
    # Both real repository names map to a-b-c. The object store cannot identify
    # which one produced these bytes, even though the caller names an allowed one.
    names = [("a-b/c",), ("a/b-c",)]
    if reverse_catalog:
        names.reverse()
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = (
        names
    )
    s3 = _FakeS3({"content/wikis/a-b-c-wiki.md": b"other-tenant-content"})
    hits = await browse(
        action,
        "content/wikis/a-b-c-wiki.md" if action == "read" else "content/wikis",
        db_pool=pool,
        s3_client=s3,
        bucket="offline-test",
        repo_scope="a/b-c",
    )
    caller = CallerPrincipal(github_login="alice", tenant_id="tenant-a", run_bound=True)
    assert filter_results(hits, caller, _FakeACLStore({"a/b-c"})) == []
    if action == "read":
        assert s3.requested_keys == []


@pytest.mark.parametrize("action", ["read", "list"])
async def test_unambiguous_catalogue_preserves_authorized_content(action):
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ("a/b-c",)
    ]
    s3 = _FakeS3({"content/wikis/a-b-c-wiki.md": b"permitted-content"})
    hits = await browse(
        action,
        "content/wikis/a-b-c-wiki.md" if action == "read" else "content/wikis",
        db_pool=pool,
        s3_client=s3,
        bucket="offline-test",
        repo_scope="a/b-c",
    )
    caller = CallerPrincipal(github_login="alice", tenant_id="tenant-a", run_bound=True)
    visible = filter_results(hits, caller, _FakeACLStore({"a/b-c"}))
    assert len(visible) == 1
    assert visible[0].repo_name == "a/b-c"
    if action == "read":
        assert visible[0].data["content"] == "permitted-content"


@pytest.mark.parametrize("foreign", [False, True])
async def test_hierarchical_sbom_keys_keep_their_exact_owner(foreign):
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        ("a/b-c",),
        ("a-b/c",),
    ]
    owner = "a-b/c" if foreign else "a/b-c"
    key = f"sbom/repos/{owner}/sbom.json"
    s3 = _FakeS3({key: b"sbom-content"})
    hits = await browse(
        "read", key, db_pool=pool, s3_client=s3, bucket="offline-test", repo_scope="a/b-c"
    )
    if foreign:
        assert hits == []
        assert s3.requested_keys == []
    else:
        assert len(hits) == 1
        assert hits[0].repo_name == "a/b-c"
