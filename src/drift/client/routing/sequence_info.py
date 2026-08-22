import dataclasses
import time
from typing import Iterable, List, Optional, Tuple

from hivemind.p2p import PeerID
from hivemind.utils.logging import get_logger

from drift.data_structures import ModuleUID, RemoteModuleInfo, RemoteSpanInfo, ServerState
from drift.utils.dht import compute_spans

logger = get_logger(__name__)


@dataclasses.dataclass
class RemoteSequenceInfo:
    """
    A dataclass that stores general information about which servers hold any given layer;
    - updated by RemoteSequenceManager in a background thread
    - accessed by routing strategies in .on_update
    :note: this class should *not* be modified by RoutingStrategy.on_update to avoid interference between strategies;
     Any metadata specific to one routing strategy, it should be stored inside that strategy. Any information that
     is used by most routing strategies should be moved from said strategies to this class.
    """

    block_uids: Tuple[ModuleUID, ...]
    block_infos: Tuple[RemoteModuleInfo, ...]  # note: the contents of RemoteModuleInfo can and will be updated
    spans_by_priority: List[RemoteSpanInfo]
    spans_containing_block: Tuple[List[RemoteSpanInfo], ...]
    last_updated_time: Optional[float]

    @classmethod
    def make_empty(cls, block_uids: Iterable[ModuleUID]) -> "RemoteSequenceInfo":
        block_uids = tuple(block_uids)
        empty_block_infos = tuple(RemoteModuleInfo(uid, {}) for uid in block_uids)
        empty_spans = tuple([] for _ in range(len(block_uids)))
        return cls(block_uids, empty_block_infos, [], empty_spans, last_updated_time=None)

    def __getitem__(self, ix: slice):
        assert isinstance(ix, slice)
        block_uids, block_infos = self.block_uids[ix], self.block_infos[ix]
        spans_by_priority, spans_containing_block = self._sort_spans(block_infos)
        return RemoteSequenceInfo(
            block_uids, block_infos, spans_by_priority, spans_containing_block, self.last_updated_time
        )

    def __len__(self):
        return len(self.block_uids)

    def update_(self, new_block_infos: List[RemoteModuleInfo]):
        assert len(new_block_infos) == len(self.block_uids)
        for block_index, (uid, info) in enumerate(zip(self.block_uids, new_block_infos)):
            assert uid == info.uid, f"The DHT entry for {uid} actually points to {info.uid}"
            self.block_infos[block_index].servers = info.servers

        self.spans_by_priority, self.spans_containing_block = self._sort_spans(self.block_infos)
        self.last_updated_time = time.perf_counter()

    def remove_peer_(self, peer_id: PeerID) -> bool:
        """Remove a failed peer and rebuild the derived routing spans immediately."""
        for block_info in self.block_infos:
            block_info.servers.pop(peer_id, None)

        self.spans_by_priority, self.spans_containing_block = self._sort_spans(self.block_infos)
        return any(not block_info.servers for block_info in self.block_infos)

    @staticmethod
    def _sort_spans(block_infos: List[RemoteModuleInfo]):
        spans_by_priority = list(compute_spans(block_infos, min_state=ServerState.ONLINE).values())
        spans_by_priority.sort(key=lambda span: span.length, reverse=True)

        spans_containing_block = tuple([] for _ in range(len(block_infos)))
        for span in spans_by_priority:
            for block_index in range(span.start, span.end):
                spans_containing_block[block_index].append(span)

        return spans_by_priority, spans_containing_block
