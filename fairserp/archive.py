"""Search archive and async jobs, on the cache store.

Retention: an archived outcome expires at the earlier of the archive TTL and its own
retention deadline (the shortest storage limit among the providers that contributed).
When that deadline has already passed, for example for Brave results whose terms allow no
storage, only a tombstone without any provider data is kept, so the archive can answer
410 with the reason instead of a misleading 404.

Async jobs run in-process. They are not durable: a job carries a lease, and a job whose
lease ran out without finishing (the worker restarted or crashed) reads as failed.
"""

import time
from datetime import UTC, datetime
from typing import Any

from fairserp.model import SearchOutcome
from fairserp.store import Store, StoreUnavailable

TOMBSTONE_TTL = 24 * 3600


class Archive:
    def __init__(self, cache: Store, archive_ttl: int) -> None:
        self._cache = cache
        self._ttl = archive_ttl

    @staticmethod
    def _key(search_id: str) -> str:
        return f"search:{search_id}"

    async def save(self, owner: str, outcome: SearchOutcome, context: dict[str, Any]) -> bool:
        """Store an outcome for its owner. Returns False when retention forbids keeping it or
        the store is unreachable, so no response advertises a link that would 404."""
        ttl = float(self._ttl)
        if outcome.retention.expires_at:
            deadline = datetime.fromisoformat(outcome.retention.expires_at).timestamp()
            ttl = min(ttl, deadline - time.time())
        if ttl <= 0:
            await self._cache.set_json(self._key(outcome.id), {
                "owner": owner, "kind": "expired",
                "reason": "a contributing provider's terms do not allow storing results",
            }, TOMBSTONE_TTL)
            return False
        try:
            await self._cache.set_json(self._key(outcome.id), {
                "owner": owner, "kind": "outcome", "context": context,
                "outcome": outcome.model_dump(mode="json"),
            }, ttl, required=True)
        except StoreUnavailable:
            return False
        return True

    async def start_job(self, owner: str, search_id: str, context: dict[str, Any],
                        lease_seconds: float) -> dict[str, Any]:
        record = {"owner": owner, "kind": "job", "context": context,
                  "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                  "lease_until": time.time() + lease_seconds}
        # Required: without the record the caller would poll a 404 for a search it paid for.
        await self._cache.set_json(self._key(search_id), record, self._ttl, required=True)
        return record

    async def fail_job(self, owner: str, search_id: str, context: dict[str, Any],
                       message: str) -> None:
        await self._cache.set_json(self._key(search_id), {
            "owner": owner, "kind": "failed", "context": context, "error": message}, self._ttl)

    async def load(self, search_id: str) -> dict[str, Any] | None:
        record = await self._cache.get_json(self._key(search_id))
        if record and record["kind"] == "job" and record["lease_until"] < time.time():
            record = {**record, "kind": "failed",
                      "error": "interrupted: the worker running this search stopped before it "
                               "finished; async jobs are not durable across restarts"}
        return record
