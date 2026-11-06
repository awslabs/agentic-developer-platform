"""Product-scoped worker token repositories — issue #9550."""

from datetime import timedelta

import pytest

from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.products.token_scope import product_repository_scope
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap

TENANT = "tenant-9550"


class FakeGitHub:
    """Reads repositories back by id; records what was asked."""

    def __init__(self, repositories: dict[int, tuple[str, str]], *, fail_ids: set[int] = frozenset()):
        self.repositories = repositories  # id -> (owner, name)
        self.fail_ids = set(fail_ids)
        self.calls: list[tuple] = []

    async def installation_request(self, installation_id, method, path, *, body=None):
        self.calls.append(("request", installation_id, method, path))
        full = path.removeprefix("/repos/")
        for identity, (owner, name) in self.repositories.items():
            if f"{owner}/{name}" == full:
                return {"id": identity, "full_name": full}
        raise RuntimeError("not found")

    async def get_installation_repository(self, installation_id, repository_id):
        self.calls.append(("read", installation_id, repository_id))
        if repository_id in self.fail_ids:
            raise RuntimeError("github unavailable")
        owner, name = self.repositories[repository_id]
        return {"id": repository_id, "name": name, "full_name": f"{owner}/{name}", "owner": {"login": owner, "id": 7}}


async def _connection(db, connection_id: str, installation: str, *, disputed: bool = False, scope: str | None = None) -> ChannelTenantMap:
    row = ChannelTenantMap(
        id=connection_id,
        provider="github",
        provider_scope_id=scope or f"scope-{installation}",
        org_id=TENANT,
        installation_id=installation,
        ownership_disputed=disputed,
    )
    db.add(row)
    return row


async def _product(db, product_id: str, project_id: str) -> Product:
    product = Product(
        tenant_id=TENANT,
        id=product_id,
        name=product_id,
        purpose="Test",
        intended_users=[],
        owner_user_id="owner-1",
        starting_point="idea",
        created_by="owner-1",
    )
    db.add(product)
    db.add(Project(tenant_id=TENANT, id=project_id, name=f"{project_id} base", owner_user_id="owner-1"))
    await db.flush()
    db.add(ProductProjectBinding(tenant_id=TENANT, product_id=product_id, project_id=project_id, is_base=True, provenance={}))
    return product


def _binding(project_id: str, connection_id: str, repository_id: int, *, status: str = "healthy", unlinked: bool = False) -> ProductSourceBinding:
    return ProductSourceBinding(
        tenant_id=TENANT,
        project_id=project_id,
        kind="repository",
        provider="github",
        installation_id=connection_id,
        namespace="7",
        native_id=str(repository_id),
        desired={},
        status="unlinked" if unlinked else status,
        unlinked_at=utcnow() - timedelta(days=1) if unlinked else None,
    )


@pytest.mark.asyncio
async def test_products_other_repositories_in_the_same_installation_are_listed(db_session):
    await _connection(db_session, "conn-a", "17")
    await _connection(db_session, "conn-b", "99")  # another installation: never widened into
    await _product(db_session, "product-1", "project-1")
    db_session.add_all(
        [
            _binding("project-1", "conn-a", 34),  # the run's repository
            _binding("project-1", "conn-a", 35),  # planning repository, same installation
            _binding("project-1", "conn-b", 36),  # bound, but a different installation
            _binding("project-1", "conn-a", 37, status="revoked"),
            _binding("project-1", "conn-a", 38, unlinked=True),
        ]
    )
    await db_session.commit()
    github = FakeGitHub({34: ("org", "api"), 35: ("org", "planning"), 36: ("other", "thing"), 37: ("org", "old"), 38: ("org", "gone")})

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=34, github=github
    )

    assert scope is not None
    assert scope.product_id == "product-1"
    assert scope.repositories == ("org/api", "org/planning")
    assert scope.repository_ids == (34, 35)
    assert scope.repository_names == ("api", "planning")
    # The run's own repository is trusted from the binding; only the other one is read back.
    assert github.calls == [("read", 17, 35)]


@pytest.mark.asyncio
async def test_repository_id_is_read_back_by_name_when_the_row_did_not_record_it(db_session):
    await _connection(db_session, "conn-a", "17")
    await _product(db_session, "product-1", "project-1")
    db_session.add_all([_binding("project-1", "conn-a", 34), _binding("project-1", "conn-a", 35)])
    await db_session.commit()
    github = FakeGitHub({34: ("org", "api"), 35: ("org", "planning")})

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=None, github=github
    )

    assert scope is not None and scope.repositories == ("org/api", "org/planning")
    assert github.calls[0] == ("request", 17, "GET", "/repos/org/api")


@pytest.mark.asyncio
async def test_a_repository_bound_to_two_products_is_not_widened(db_session):
    await _connection(db_session, "conn-a", "17")
    await _product(db_session, "product-1", "project-1")
    await _product(db_session, "product-2", "project-2")
    db_session.add_all(
        [
            _binding("project-1", "conn-a", 34),
            _binding("project-1", "conn-a", 35),
            _binding("project-2", "conn-a", 34),
            _binding("project-2", "conn-a", 36),
        ]
    )
    await db_session.commit()
    github = FakeGitHub({34: ("org", "api"), 35: ("org", "planning"), 36: ("org", "other")})

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=34, github=github
    )

    assert scope is None
    assert github.calls == []


@pytest.mark.asyncio
async def test_unbound_repository_keeps_the_single_scope(db_session):
    await _connection(db_session, "conn-a", "17")
    await _product(db_session, "product-1", "project-1")
    db_session.add(_binding("project-1", "conn-a", 35))
    await db_session.commit()

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=34, github=FakeGitHub({})
    )

    assert scope is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["github_down", "owner_mismatch", "disputed_connection"])
async def test_an_unproven_set_degrades_to_the_single_repository(db_session, failure):
    await _connection(db_session, "conn-a", "17", disputed=failure == "disputed_connection")
    await _product(db_session, "product-1", "project-1")
    db_session.add_all([_binding("project-1", "conn-a", 34), _binding("project-1", "conn-a", 35)])
    await db_session.commit()
    repositories = {34: ("org", "api"), 35: ("someone-else" if failure == "owner_mismatch" else "org", "planning")}
    github = FakeGitHub(repositories, fail_ids={35} if failure == "github_down" else set())

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=34, github=github
    )

    if failure == "disputed_connection":
        # A disputed connection contributes nothing: the set is exactly the run's repository.
        assert scope is not None and scope.repositories == ("org/api",)
    else:
        assert scope is None


@pytest.mark.asyncio
async def test_the_runs_repository_is_listed_first_even_with_a_larger_id(db_session):
    """Review F3 (#9604): the response contract is "the run's repository first"."""
    await _connection(db_session, "conn-a", "17")
    await _product(db_session, "product-1", "project-1")
    db_session.add_all([_binding("project-1", "conn-a", 35), _binding("project-1", "conn-a", 34)])
    await db_session.commit()
    github = FakeGitHub({34: ("org", "planning"), 35: ("org", "api")})

    scope = await product_repository_scope(
        db_session, tenant_id=TENANT, installation_id=17, repo_full_name="org/api", repository_id=35, github=github
    )

    assert scope is not None
    assert scope.repositories == ("org/api", "org/planning")
    assert scope.repository_ids == (35, 34)
