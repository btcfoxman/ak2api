"""Accepted generations can outlive the first hundred recent account results."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import app.generation_lookup as lookup
from app.generation_lookup import GenerationLookupCache, begin_lookup


def poll(cache, owner=78, resource="accepted-resource", *, target_page=None,
         pages=1000, first_next="page-1"):
    round_ = cache.begin(owner, resource)
    requests = []
    while (cursor := round_.next_cursor()) is not None:
        page = int(cursor.split("-")[1]) if cursor else 0
        requests.append(page)
        if target_page == page:
            return requests, round_.found()
        following = (first_next if page == 0 else "page-" + str(page + 1))
        round_.observe(following if page + 1 < pages else None)
    return requests, round_.finish()


def test_resource_older_than_first_hundred_results_is_found_without_more_than_five_reads():
    cache = GenerationLookupCache()
    first, state = poll(cache, target_page=6)
    assert first == [0, 1, 2, 3, 4]
    assert state["status"] == "scanning" and state["has_more"]
    second, state = poll(cache, target_page=6)
    assert second == [0, 5, 6]
    assert state["status"] == "found" and state["resumed"]
    assert state["deep_pages_scanned"] == 6
    assert cache.entry_count == 0


def test_recent_target_wins_before_any_older_cursor_read_and_clears_scan():
    cache = GenerationLookupCache()
    poll(cache)
    requests, state = poll(cache, target_page=0)
    assert requests == [0] and state["status"] == "found"
    assert not state["has_more"] and cache.entry_count == 0
    assert poll(cache)[0] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("owner,resource", [(79, "accepted-resource"), (78, "another-resource")])
def test_interleaved_owner_or_resource_has_its_own_cursor(owner, resource):
    cache = GenerationLookupCache()
    poll(cache)
    assert poll(cache, owner, resource)[0] == [0, 1, 2, 3, 4]
    assert poll(cache)[0] == [0, 5, 6, 7, 8]


def test_known_owner_shared_state_survives_a_client_replacement(monkeypatch):
    monkeypatch.setattr(lookup, "_SHARED_CACHE", GenerationLookupCache())
    old_client_private = GenerationLookupCache()
    round_ = begin_lookup(78, "resource", private_cache=old_client_private)
    assert round_.next_cursor() == ""
    round_.observe("next-page")
    round_.finish()
    new_client_private = GenerationLookupCache()
    recovered = begin_lookup("078", "resource", private_cache=new_client_private)
    assert recovered.next_cursor() == ""
    recovered.observe("new-head-page")
    assert recovered.next_cursor() == "next-page"


@pytest.mark.parametrize("owner", [None, 0, -1, False, True, "", "not-a-local-account", "1.0"])
def test_unknown_owner_never_uses_shared_cross_client_state(owner, monkeypatch):
    shared = GenerationLookupCache()
    monkeypatch.setattr(lookup, "_SHARED_CACHE", shared)
    first = begin_lookup(owner, "same-resource")
    assert first.next_cursor() == ""
    first.observe("old-cursor")
    first.finish()
    second = begin_lookup(owner, "same-resource")
    assert second.next_cursor() == ""
    second.observe("new-head")
    assert second.next_cursor() == "new-head"
    assert shared.entry_count == 0


def test_unknown_owner_can_resume_only_in_its_own_client_private_cache(monkeypatch):
    shared = GenerationLookupCache()
    monkeypatch.setattr(lookup, "_SHARED_CACHE", shared)
    a, b = GenerationLookupCache(), GenerationLookupCache()
    first = begin_lookup(None, "same", private_cache=a)
    first.next_cursor(); first.observe("older"); first.finish()
    same = begin_lookup(None, "same", private_cache=a)
    same.next_cursor(); same.observe("head")
    assert same.next_cursor() == "older"
    other = begin_lookup(None, "same", private_cache=b)
    other.next_cursor(); other.observe("other-head")
    assert other.next_cursor() == "other-head"
    accidental_shared = begin_lookup(None, "same", private_cache=shared)
    accidental_shared.next_cursor(); accidental_shared.observe("public-head")
    assert accidental_shared.next_cursor() == "public-head"
    assert shared.entry_count == 0


def test_self_repeating_cursor_is_bounded_and_next_round_restarts():
    cache = GenerationLookupCache()
    round_ = cache.begin(78, "resource")
    assert round_.next_cursor() == ""
    round_.observe("loop")
    assert round_.next_cursor() == "loop"
    round_.observe("loop")
    assert round_.next_cursor() is None
    state = round_.finish()
    assert state["status"] == "cursor_cycle" and state["pages_requested"] == 2
    assert cache.entry_count == 0
    assert poll(cache)[0] == [0, 1, 2, 3, 4]


def test_cursor_cycle_across_rounds_is_detected_without_reading_duplicate_page():
    cache = GenerationLookupCache()
    poll(cache)
    round_ = cache.begin(78, "accepted-resource")
    round_.next_cursor(); round_.observe("page-1")
    assert round_.next_cursor() == "page-5"
    round_.observe("page-2")
    assert round_.next_cursor() is None
    assert round_.finish()["status"] == "cursor_cycle"


def test_end_of_list_is_explicit_nonterminal_lookup_information():
    cache = GenerationLookupCache()
    requests, state = poll(cache, pages=3)
    assert requests == [0, 1, 2]
    assert state["status"] == "not_found" and not state["has_more"]
    assert cache.entry_count == 0
    assert not any(key in state for key in ("generation_status", "FAILED", "resubmit", "submit"))
    assert poll(cache)[0] == [0, 1, 2, 3, 4]


def test_recent_page_ending_history_discards_a_stale_older_cursor():
    cache = GenerationLookupCache()
    poll(cache)
    requests, state = poll(cache, pages=1)
    assert requests == [0] and state["status"] == "not_found"
    assert cache.entry_count == 0


def test_exact_256_deep_page_cap_is_cross_round_and_resets_next_round():
    cache = GenerationLookupCache()
    for _ in range(63):
        requests, state = poll(cache)
        assert len(requests) == 5 and state["status"] == "scanning"
    requests, state = poll(cache)
    assert requests == [0, 253, 254, 255, 256]
    assert state["status"] == "scan_capped" and state["deep_pages_scanned"] == 256
    assert not state["has_more"] and cache.entry_count == 0
    assert poll(cache)[0] == [0, 1, 2, 3, 4]


def test_found_at_cap_remains_found_not_capped():
    cache = GenerationLookupCache(max_deep_pages=4)
    requests, state = poll(cache, target_page=4)
    assert requests == [0, 1, 2, 3, 4]
    assert state["status"] == "found" and cache.entry_count == 0


def test_partial_read_error_keeps_last_completed_cursor_for_safe_query_retry():
    cache = GenerationLookupCache()
    round_ = cache.begin(78, "resource")
    round_.next_cursor(); round_.observe("page-1")
    assert round_.next_cursor() == "page-1"
    # Simulate an exception from the read, without any observation of that page.
    state = round_.finish()
    assert state["status"] == "query_interrupted" and state["state_saved"]
    retry = cache.begin(78, "resource")
    retry.next_cursor(); retry.observe("newer-head")
    assert retry.next_cursor() == "page-1"


@pytest.mark.parametrize("bad", [{"cursor": "x"}, [], True, " " , "x" * 4097])
def test_malformed_cursor_never_becomes_a_request(bad):
    cache = GenerationLookupCache()
    round_ = cache.begin(78, "resource")
    round_.next_cursor(); round_.observe(bad)
    assert round_.next_cursor() is None
    assert round_.finish()["status"] == "invalid_cursor"


@pytest.mark.parametrize("end", [None, "", 0, False])
def test_empty_provider_cursor_ends_the_scan(end):
    cache = GenerationLookupCache()
    round_ = cache.begin(78, "resource")
    round_.next_cursor(); round_.observe(end)
    assert round_.finish()["status"] == "not_found"


def test_compare_and_swap_keeps_first_concurrent_commit_and_reports_stale_writer():
    cache = GenerationLookupCache()
    barrier = threading.Barrier(2)
    committed = threading.Event()

    def worker(cursor, first):
        round_ = cache.begin(78, "resource")
        round_.next_cursor(); round_.observe(cursor)
        barrier.wait(timeout=2)
        if not first:
            assert committed.wait(timeout=2)
        result = round_.finish()
        if first:
            committed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(worker, "winner", True)
        b = pool.submit(worker, "stale", False)
        winner, stale = a.result(timeout=4), b.result(timeout=4)
    assert winner["state_saved"] and not winner["concurrent_update"]
    assert not stale["state_saved"] and stale["concurrent_update"]
    next_ = cache.begin(78, "resource")
    next_.next_cursor(); next_.observe("recent")
    assert next_.next_cursor() == "winner"


def test_found_in_parallel_prevents_stale_scan_resurrection():
    cache = GenerationLookupCache()
    a, b = cache.begin(78, "resource"), cache.begin(78, "resource")
    a.next_cursor(); a.observe("stale")
    b.next_cursor(); b.found()
    assert cache.entry_count == 0
    # A new lookup gets a fresh version; the old writer cannot overwrite it.
    c = cache.begin(78, "resource")
    c.next_cursor(); c.observe("fresh"); c.finish()
    assert a.finish()["concurrent_update"]
    fresh = cache.begin(78, "resource")
    fresh.next_cursor(); fresh.observe("head")
    assert fresh.next_cursor() == "fresh"


def test_snapshots_do_not_mutate_one_another_before_commit():
    cache = GenerationLookupCache()
    a, b = cache.begin(78, "resource"), cache.begin(78, "resource")
    a.next_cursor(); a.observe("seen-only-by-a")
    assert a.next_cursor() == "seen-only-by-a"
    a.observe("later")
    b.next_cursor(); b.observe("independent")
    assert b.next_cursor() == "independent"
    b.found()


def test_lru_is_bounded_and_expired_state_restarts_from_recent():
    now = [0.0]
    cache = GenerationLookupCache(max_entries=2, ttl_seconds=10, clock=lambda: now[0])
    poll(cache, resource="a"); poll(cache, resource="b")
    poll(cache, resource="a")
    poll(cache, resource="c")
    assert cache.entry_count == 2
    assert poll(cache, resource="b")[0] == [0, 1, 2, 3, 4]
    now[0] = 10.0
    assert cache.entry_count == 0
    assert poll(cache, resource="a")[0] == [0, 1, 2, 3, 4]


def test_expired_round_cannot_commit_after_ttl():
    now = [0.0]
    cache = GenerationLookupCache(ttl_seconds=10, clock=lambda: now[0])
    round_ = cache.begin(78, "resource")
    round_.next_cursor(); round_.observe("expired")
    now[0] = 10.0
    state = round_.finish()
    assert state["concurrent_update"] and not state["state_saved"]
    assert cache.entry_count == 0


def test_default_capacity_is_a_hard_upper_bound_across_many_resources():
    cache = GenerationLookupCache()
    for i in range(300):
        poll(cache, resource="resource-" + str(i))
    assert cache.entry_count == 256
    assert poll(cache, resource="resource-0")[0] == [0, 1, 2, 3, 4]


def test_diagnostics_are_copied_and_do_not_expose_cursor_or_identity():
    cache = GenerationLookupCache()
    round_ = cache.begin(78, "private-resource")
    round_.next_cursor(); round_.observe("private-cursor")
    first = round_.finish()
    first["status"] = "changed"
    assert round_.finish()["status"] == "scanning"
    assert "private" not in repr(round_.finish())
    assert all(not isinstance(v, (dict, list, set)) for v in round_.finish().values())


def test_round_rejects_multiple_requests_without_observing_previous_page():
    round_ = GenerationLookupCache().begin(78, "resource")
    round_.next_cursor()
    with pytest.raises(RuntimeError, match="previous lookup page"):
        round_.next_cursor()


@pytest.mark.parametrize("kwargs", [{"max_entries": 257}, {"max_entries": 0},
    {"ttl_seconds": 3601}, {"ttl_seconds": 0}, {"ttl_seconds": float("nan")},
    {"max_deep_pages": 257}, {"max_deep_pages": False}])
def test_cache_limits_cannot_be_disabled_or_exceeded(kwargs):
    with pytest.raises(ValueError):
        GenerationLookupCache(**kwargs)
