"""tests/test_bus.py — EventBus async contract & sync barrier."""

import asyncio

import pytest

from kernel import domain, bus


async def test_subscribe_returns_id_and_unsubscribe() -> None:
    b = bus.EventBus()
    sid = b.subscribe("e.t", lambda ev: None)
    assert isinstance(sid, str)
    assert b.unsubscribe(sid) is True
    assert b.unsubscribe(sid) is False  # already gone


async def test_publish_delivers_to_subscriber() -> None:
    b = bus.EventBus()
    seen: list[str] = []

    async def h(e: domain.Event) -> None:
        seen.append(e.type)

    b.subscribe("e.t", h)
    b.publish(domain.Event(type="e.t", source="test"))
    await asyncio.sleep(0.05)  # let create_task dispatch
    assert seen == ["e.t"]


async def test_multiple_subscribers_all_receive() -> None:
    b = bus.EventBus()
    seen: list[str] = []
    for _ in range(3):
        b.subscribe("e.t", lambda e: seen.append(e.type))
    b.publish(domain.Event(type="e.t"))
    await asyncio.sleep(0.05)
    assert seen == ["e.t", "e.t", "e.t"]


async def test_fault_containment_isolates_bad_handler() -> None:
    b = bus.EventBus()
    good: list[str] = []

    async def bad(e: domain.Event) -> None:
        raise RuntimeError("boom")

    async def ok(e: domain.Event) -> None:
        good.append(e.type)

    b.subscribe("e.t", bad)
    b.subscribe("e.t", ok)
    b.publish(domain.Event(type="e.t"))
    await asyncio.sleep(0.05)
    assert good == ["e.t"]  # other subscriber still got it


async def test_wait_for_resolves_on_match() -> None:
    b = bus.EventBus()
    fut = b.wait_for(["x.y"], timeout=1.0)
    b.publish(domain.Event(type="x.y"))
    ev = await asyncio.wait_for(fut, timeout=2.0)
    assert ev.type == "x.y"


async def test_wait_for_type_isolated() -> None:
    b = bus.EventBus()
    f_other = b.wait_for(["other"], timeout=0.3)
    b.publish(domain.Event(type="evt"))  # unrelated
    await asyncio.sleep(0.05)
    assert not f_other.done()  # must stay pending


async def test_wait_for_timeout() -> None:
    b = bus.EventBus()
    fut = b.wait_for(["never"], timeout=0.2)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(fut, timeout=1.0)


async def test_close_cancels_waiters_and_clears() -> None:
    b = bus.EventBus()
    b.subscribe("e.t", lambda e: None)
    fut = b.wait_for(["e.t"])
    await b.close()
    assert b.subscriber_count("e.t") == 0
    assert fut.done() and fut.cancelled()
