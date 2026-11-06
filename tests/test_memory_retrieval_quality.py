import json
from dataclasses import replace

import pytest

from newtalk.memory import MemoryItem, MemorySearchResult


def test_memory_item_preserves_five_positional_arguments() -> None:
    item = MemoryItem("m1", "fact", "title", "content", 0.8)

    assert (item.memory_id, item.kind, item.title, item.content, item.relativity) == (
        "m1", "fact", "title", "content", 0.8,
    )
    assert item.created_at is None
    assert item.updated_at is None
    assert item.conversation_id is None
    assert item.confidence is None
    assert item.event_time == ()


def test_deduplicated_keeps_first_id_and_stable_order_without_mutating_input() -> None:
    first = MemoryItem("m1", "fact", "first", "first content", 0.8)
    second = MemoryItem("m2", "preference", "second", "second content", 0.9)
    duplicate = MemoryItem("m1", "event", "changed", "changed content", 1.0)
    result = MemorySearchResult((first, second, duplicate))

    deduplicated = result.deduplicated()

    assert deduplicated.items == (first, second)
    assert deduplicated.items[0] is first
    assert result.items == (first, second, duplicate)
    assert deduplicated.deduplicated() == deduplicated


def test_deduplicated_normalizes_only_whitespace_and_preserves_original_text() -> None:
    first = MemoryItem("m1", "fact", " original title ", "  likes\t tea\n daily  ", 0.8)
    duplicate = MemoryItem("m2", "fact", "other title", "likes tea daily", 0.9)
    paraphrase = MemoryItem("m3", "fact", "title", "enjoys tea daily", 0.7)
    case_change = MemoryItem("m4", "fact", "title", "Likes tea daily", 0.7)

    result = MemorySearchResult((first, duplicate, paraphrase, case_change))

    assert result.deduplicated().items == (first, paraphrase, case_change)
    payload = json.loads(result.to_tool_content(max_chars=4000))
    assert [item["memory_id"] for item in payload["memories"]] == ["m1", "m3", "m4"]
    assert payload["memories"][0]["title"] == first.title
    assert payload["memories"][0]["content"] == first.content


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "preference"},
        {"created_at": "2026-10-26"},
        {"conversation_id": "conversation-2"},
        {"event_time": ("2026-10-26",)},
    ],
)
def test_same_content_with_different_provenance_or_kind_is_not_merged(change) -> None:
    first = MemoryItem(
        "m1", "fact", "title", "same content", 0.8,
        created_at="2026-10-25", conversation_id="conversation-1",
        event_time=("2026-10-25",),
    )
    second = replace(first, memory_id="m2", **change)

    assert MemorySearchResult((first, second)).deduplicated().items == (first, second)


@pytest.mark.parametrize(
    "metadata",
    [{}, {"conversation_id": "conversation-1"}, {"updated_at": "2026-10-25"}],
)
def test_events_without_event_or_creation_time_keep_distinct_ids(metadata) -> None:
    first = MemoryItem("m1", "event", "title", "went running", 0.8, **metadata)
    second = replace(first, memory_id="m2")

    assert MemorySearchResult((first, second, first)).deduplicated().items == (first, second)


@pytest.mark.parametrize(
    "metadata", [{"created_at": "2026-10-25"}, {"event_time": ("2026-10-25",)}],
)
def test_events_with_matching_time_can_be_deduplicated(metadata) -> None:
    first = MemoryItem("m1", "event", "title", "went running", 0.8, **metadata)
    second = replace(first, memory_id="m2", updated_at="2026-10-26", confidence=0.9)

    assert MemorySearchResult((first, second)).deduplicated().items == (first,)


def test_text_duplicate_id_is_still_seen_if_later_content_changes() -> None:
    first = MemoryItem("m1", "fact", "title", "same", 0.8)
    duplicate = replace(first, memory_id="m2")
    changed = replace(duplicate, content="changed")

    assert MemorySearchResult((first, duplicate, changed)).deduplicated().items == (first,)


def test_tool_content_includes_only_supplied_metadata_and_never_infers_confidence() -> None:
    plain = MemoryItem("plain", "fact", "title", "content", 0.0)
    rich = MemoryItem(
        "rich", "event", "event title", "event content", 0.91,
        created_at="2026-10-25", updated_at="2026-10-26",
        conversation_id="conversation-1", confidence=0.0,
        event_time=("2026-10-24", "2026-10-25"),
    )
    empty = MemoryItem(
        "empty", "preference", "title", "other content", 0.5,
        created_at="", updated_at="", conversation_id="",
    )

    payload = json.loads(MemorySearchResult((plain, rich, empty)).to_tool_content(max_chars=4000))

    assert payload["status"] == "ok"
    assert payload["memories"][0] == {
        "memory_id": "plain", "type": "fact", "title": "title",
        "content": "content", "relativity": 0.0,
    }
    assert payload["memories"][1] == {
        "memory_id": "rich", "type": "event", "title": "event title",
        "content": "event content", "relativity": 0.91,
        "created_at": "2026-10-25", "updated_at": "2026-10-26",
        "conversation_id": "conversation-1", "confidence": 0.0,
        "event_time": ["2026-10-24", "2026-10-25"],
    }
    assert set(payload["memories"][2]) == set(payload["memories"][0])


def test_oversized_items_do_not_discard_later_short_items_or_count_duplicates_as_omitted() -> None:
    long = MemoryItem("long", "fact", "title", "x" * 5000, 1.0)
    first = MemoryItem("short-1", "fact", "title", "short one", 0.9)
    second = MemoryItem("short-2", "fact", "title", "short two", 0.8)
    result = MemorySearchResult((long, first, replace(long, memory_id="long-copy"), second, first))

    text = result.to_tool_content(max_chars=320)
    payload = json.loads(text)

    assert len(text) <= 320
    assert payload["status"] == "truncated"
    assert payload["omitted_count"] == 1
    assert [item["memory_id"] for item in payload["memories"]] == ["short-1", "short-2"]


def test_budget_selection_keeps_first_fitting_candidate_in_order() -> None:
    items = tuple(MemoryItem(str(index), "fact", "title", str(index) * 20, 0.8) for index in range(3))
    result = MemorySearchResult(items)

    text = result.to_tool_content(max_chars=190)
    payload = json.loads(text)

    assert len(text) <= 190
    assert payload["status"] == "truncated"
    assert payload["omitted_count"] == 2
    assert [item["memory_id"] for item in payload["memories"]] == ["0"]


def test_minimal_truncated_payload_boundary_is_valid_json_or_explicit_error() -> None:
    result = MemorySearchResult((MemoryItem("m1", "fact", "title", "x" * 5000, 0.8),))
    minimum = '{"status":"truncated","memories":[],"omitted_count":1}'

    text = result.to_tool_content(max_chars=len(minimum))

    assert len(text) <= len(minimum)
    assert json.loads(text) == json.loads(minimum)
    with pytest.raises(ValueError, match="budget"):
        result.to_tool_content(max_chars=len(minimum) - 1)


@pytest.mark.parametrize("budget", [-1, 0, 1, 20])
@pytest.mark.parametrize("has_items", [False, True])
def test_impossibly_small_budget_raises_value_error(budget: int, has_items: bool) -> None:
    items = (MemoryItem("m1", "fact", "title", "content", 0.8),) if has_items else ()

    with pytest.raises(ValueError, match="budget"):
        MemorySearchResult(items).to_tool_content(max_chars=budget)


def test_no_results_keep_existing_no_match_shape_at_exact_budget() -> None:
    expected = '{"status":"no_match","memories":[]}'
    result = MemorySearchResult(())

    assert result.deduplicated().items == ()
    assert json.loads(result.to_tool_content(max_chars=len(expected))) == json.loads(expected)
    with pytest.raises(ValueError, match="budget"):
        result.to_tool_content(max_chars=len(expected) - 1)


def test_default_budget_normal_path_preserves_all_distinct_items() -> None:
    items = tuple(MemoryItem(str(index), "fact", "title", f"content {index}", 0.8) for index in range(5))

    text = MemorySearchResult(items).to_tool_content(max_chars=4000)
    payload = json.loads(text)

    assert payload["status"] == "ok"
    assert "omitted_count" not in payload
    assert len(text) <= 4000
    assert [item["memory_id"] for item in payload["memories"]] == [item.memory_id for item in items]
