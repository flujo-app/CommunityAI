"""Standalone async-generator lifecycle probe; no network, model or GPU."""

import asyncio


class Instance:
    def __init__(self):
        self.quarantined = False
        self.dispatches = 0

    async def stream(self, *, finish):
        if self.quarantined:
            raise RuntimeError("backend quarantined")
        self.dispatches += 1
        complete = False
        try:
            yield "started"
            yield "partial"
            if finish:
                complete = True
                yield "completed"
        finally:
            if not complete:
                self.quarantined = True


async def main():
    instance = Instance()
    pending = instance.stream(finish=True)
    assert await anext(pending) == "started"
    await pending.aclose()
    assert instance.quarantined
    try:
        await anext(instance.stream(finish=True))
    except RuntimeError:
        pass
    else:
        raise AssertionError("uncertain backend reused")
    assert instance.dispatches == 1
    fresh = Instance()
    assert [part async for part in fresh.stream(finish=True)] == ["started", "partial", "completed"]
    assert not fresh.quarantined
    print("standalone vLLM quarantine lifecycle PASS")


if __name__ == "__main__":
    asyncio.run(main())
