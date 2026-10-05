import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import authenticate
from app.service import CatalogService


@pytest.fixture
def service() -> CatalogService:
    return CatalogService()


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service))


@pytest.fixture
def alice():
    return authenticate("alice-key")  # namespace: analytics


@pytest.fixture
def bob():
    return authenticate("bob-key")  # namespace: billing


@pytest.fixture
def admin():
    return authenticate("admin-key")


ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}
ADMIN = {"X-API-Key": "admin-key"}


def make_entity(svc, actor, entity_id, namespace="analytics", fields=None):
    return svc.register_entity(
        actor,
        entity_id,
        name=entity_id,
        namespace=namespace,
        kind="table",
        owner=actor.name,
        fields=fields if fields is not None else [{"name": "id", "type": "long", "required": True}],
    )
