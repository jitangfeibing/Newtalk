from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.memory import MemoryItem, MemoryPage, MemoryRecord, MemorySearchResult
from newtalk.profile import ProfileField, ProfileScope, ProfileSnapshot


class MemoryCenterProvider:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.profile: dict[str, ProfileSnapshot] = {}
        self.memories: dict[str, list[MemoryRecord]] = {}

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot:
        return self.profile.setdefault(
            scope.identity_id,
            ProfileSnapshot(
                scope.identity_id,
                "template-1",
                (ProfileField("偏好.饮料", "茶", False),),
            ),
        )

    async def update_profile(
        self,
        scope: ProfileScope,
        *,
        path: str,
        value: str | None,
        locked: bool,
        remove: bool,
    ) -> ProfileSnapshot:
        self.calls.append(("update_profile", scope.identity_id))
        current = await self.prepare_profile(scope)
        fields = [field for field in current.fields if field.path != path]
        if not remove:
            fields.append(ProfileField(path, value or "", not locked))
        snapshot = ProfileSnapshot(scope.identity_id, "template-1", tuple(fields))
        self.profile[scope.identity_id] = snapshot
        return snapshot

    async def list_memories(
        self,
        scope: ProfileScope,
        *,
        page: int,
        size: int,
        kinds: tuple[str, ...],
    ) -> MemoryPage:
        self.calls.append(("list", scope.identity_id))
        items = [
            item for item in self.memories.get(scope.identity_id, []) if item.kind in kinds
        ]
        return MemoryPage(tuple(items), page, size, len(items), 1 if items else 0)

    async def search(
        self,
        scope: ProfileScope,
        *,
        query: str,
        conversation_id: str,
        limit: int,
        relativity: float,
    ) -> MemorySearchResult:
        self.calls.append(("search", scope.identity_id))
        items = [
            MemoryItem(item.memory_id, item.kind, item.title, item.content, 0.9)
            for item in self.memories.get(scope.identity_id, [])
            if query in item.content
        ]
        return MemorySearchResult(tuple(items[:limit]))

    async def update_memory(
        self,
        scope: ProfileScope,
        *,
        memory_id: str,
        title: str,
        content: str,
    ) -> None:
        self.calls.append(("update", scope.identity_id))
        items = self.memories.get(scope.identity_id, [])
        self.memories[scope.identity_id] = [
            MemoryRecord(item.memory_id, item.kind, title, content)
            if item.memory_id == memory_id
            else item
            for item in items
        ]

    async def delete_memory(self, scope: ProfileScope, *, memory_id: str) -> None:
        self.calls.append(("delete", scope.identity_id))
        self.memories[scope.identity_id] = [
            item
            for item in self.memories.get(scope.identity_id, [])
            if item.memory_id != memory_id
        ]

    async def add_completed_turn(self, *args, **kwargs):
        raise AssertionError("not used")

    async def delete_all_memories(self, scope: ProfileScope) -> None:
        self.memories.pop(scope.identity_id, None)

    async def delete_profile(self, scope: ProfileScope) -> None:
        self.profile.pop(scope.identity_id, None)

    async def aclose(self) -> None:
        return None


def _member(client: TestClient, name: str = "小明") -> dict:
    assert client.post("/api/device").status_code == 201
    response = client.post("/api/members", json={"display_name": name})
    assert response.status_code == 201
    return response.json()


def test_memory_center_profile_and_memory_crud_is_scoped_to_device() -> None:
    provider = MemoryCenterProvider()
    app = create_app(
        AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
        profile_provider=provider,
    )
    with TestClient(app) as owner, TestClient(app) as stranger:
        member = _member(owner)
        identity_id = member["identity_id"]
        provider.memories[identity_id] = [
            MemoryRecord("memory-1", "event", "面试", "上个月参加了面试")
        ]

        profile = owner.get(f"/api/members/{identity_id}/profile")
        assert profile.status_code == 200
        assert profile.json()["fields"] == [
            {"path": "偏好.饮料", "value": "茶", "locked": True}
        ]

        updated_profile = owner.patch(
            f"/api/members/{identity_id}/profile",
            json={"path": "偏好.饮料", "value": "咖啡", "locked": False},
        )
        assert updated_profile.status_code == 200
        assert updated_profile.json()["fields"][0]["value"] == "咖啡"

        listed = owner.get(f"/api/members/{identity_id}/memories?kind=event")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["memory_id"] == "memory-1"

        searched = owner.get(
            f"/api/members/{identity_id}/memories",
            params={"query": "面试"},
        )
        assert searched.status_code == 200
        assert searched.json()["total"] == 1

        changed = owner.patch(
            f"/api/members/{identity_id}/memories/memory-1",
            json={"title": "求职", "content": "完成了一次技术面试"},
        )
        assert changed.status_code == 204
        assert provider.memories[identity_id][0].content == "完成了一次技术面试"

        assert stranger.post("/api/device").status_code == 201
        before = list(provider.calls)
        forbidden = stranger.get(f"/api/members/{identity_id}/memories")
        assert forbidden.status_code == 404
        assert provider.calls == before

        deleted = owner.delete(f"/api/members/{identity_id}/memories/memory-1")
        assert deleted.status_code == 204
        assert provider.memories[identity_id] == []


def test_memory_center_returns_503_when_memory_is_disabled() -> None:
    app = create_app(
        AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
    )
    with TestClient(app) as client:
        member = _member(client)
        response = client.get(f"/api/members/{member['identity_id']}/profile")

    assert response.status_code == 503
    assert response.json()["detail"] == "Memory is disabled"
