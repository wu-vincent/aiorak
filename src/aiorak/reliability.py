import enum


class Reliability(enum.IntEnum):
    UNRELIABLE = 0
    UNRELIABLE_SEQUENCED = 1
    RELIABLE = 2
    RELIABLE_ORDERED = 3
    RELIABLE_SEQUENCED = 4

    @property
    def is_reliable(self) -> bool:
        return self in {
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
        }

    @property
    def is_sequenced(self) -> bool:
        return self in {UNRELIABLE_SEQUENCED, Reliability.RELIABLE_SEQUENCED}

    @property
    def is_ordered(self) -> bool:
        return self is Reliability.RELIABLE_ORDERED or self.is_sequenced

    @classmethod
    def from_flags(cls, reliable: bool, ordered: bool, sequenced: bool) -> "Reliability":
        match reliable, ordered, sequenced:
            case False, False, True:
                return cls.UNRELIABLE_SEQUENCED
            case False, False, False:
                return cls.UNRELIABLE
            case True, True, False:
                return cls.RELIABLE_ORDERED
            case True, False, True:
                return cls.RELIABLE_SEQUENCED
            case True, False, False:
                return cls.RELIABLE
            case _:
                raise ValueError("Invalid combination of flags")


@dataclass(slots=True)
class InternalPacket:
    data: bytes
    reliability: Reliability
    reliable_index: int | None = None
    ordering_index: int | None = None
    sequencing_index: int | None = None
    channel: int = 0
    split_id: int | None = None
    split_index: int | None = None
    split_count: int | None = None


class ReliabilityLayer:
    def __init__(self, mtu: int = 576):
        self._mtu = max(mtu, 576)

    def send(
        self,
        data: bytes | memoryview,
        *,
        reliable: bool,
        ordered: bool = False,
        sequenced: bool = False,
        channel: int = 0,
    ) -> None:
        pass

    def next_reliable_index(self) -> int:
        idx = self.reliable_index
        self.reliable_index += 1
        return idx

    def next_sequence_number(self) -> int:
        seq = self.sequence_number
        self.sequence_number += 1
        return seq
