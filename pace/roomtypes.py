"""Room type inventory and who wants which room.

A hotel does not sell one product.  It sells a standard queen, a deluxe king,
an executive room with lounge access and a suite, out of one building, to
guests who mostly do not care very much which one they get.  That last clause
is the whole reason this module exists.

The single resource model in optimize.py treats a refused booking as a lost
booking.  In a hotel with more than one room type that is simply false: the
guest who finds the executive room too expensive takes the deluxe, and the
hotel keeps the revenue at a slightly lower rate.  A system that cannot see
the substitution will believe closing the top of the house costs it the whole
booking, and will therefore price the top of the house far too low.

Physical capacity is declared here.  Preference is declared here too, as a
matrix of taste parameters over (segment, room type), because taste is a
property of the market a hotel sells into and not of the optimizer.  What the
guest does with a set of prices is choice.py's problem, and how the house
values a room across types and nights is network.py's.

Nothing in this module is loaded unless a hotel declares room types.  A
property with a single room type behaves exactly as it did before, which is
what keeps the results in METHOD.md sections 8 to 13 reproducible.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .config import SEGMENT_ORDER


@dataclass(frozen=True)
class RoomType:
    """One sellable room product.

    code            short identifier, used as the resource key in the network
    name            what it is called on the website
    rooms           physical count.  These must sum to the hotel's room count.
    rate_multiplier what this type is quoted relative to the published BAR.
                    The BAR belongs to the entry type by convention, so the
                    entry type carries a multiplier of exactly 1.0.
    rank            quality order, 0 for the entry type and rising.  Used for
                    nothing in the mathematics and everything in the display:
                    a bid price table that is not in room order is unreadable.
    """

    code: str
    name: str
    rooms: int
    rate_multiplier: float
    rank: int


class Inventory:
    """The set of room types a property sells, in quality order."""

    def __init__(self, types: Sequence[RoomType]):
        if not types:
            raise ValueError("an inventory needs at least one room type")
        self.types: Tuple[RoomType, ...] = tuple(sorted(types, key=lambda t: t.rank))
        self.by_code: Dict[str, RoomType] = {t.code: t for t in self.types}
        if len(self.by_code) != len(self.types):
            raise ValueError("room type codes must be unique")

    # ----------------------------------------------------------------- reads

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(t.code for t in self.types)

    @property
    def entry(self) -> RoomType:
        """The type the published rate refers to."""
        return self.types[0]

    def total_rooms(self) -> int:
        return sum(t.rooms for t in self.types)

    def capacity(self) -> Dict[str, int]:
        return {t.code: t.rooms for t in self.types}

    def rate_of(self, code: str, bar: float) -> float:
        """What this type is quoted when the published rate is bar."""
        return bar * self.by_code[code].rate_multiplier

    def validate(self, hotel_rooms: int) -> None:
        """Fail loudly rather than price a building that does not exist."""
        total = self.total_rooms()
        if total != hotel_rooms:
            raise ValueError(
                "room types sum to %d rooms but the hotel has %d"
                % (total, hotel_rooms))
        if abs(self.entry.rate_multiplier - 1.0) > 1e-9:
            raise ValueError(
                "the entry room type carries the published rate, so its "
                "multiplier must be 1.0, not %.3f" % self.entry.rate_multiplier)

    def __len__(self) -> int:
        return len(self.types)

    def __iter__(self):
        return iter(self.types)


# --------------------------------------------------------------- the property

DEFAULT_INVENTORY = Inventory([
    RoomType("STD", "Standard queen", 74, 1.00, 0),
    RoomType("DLX", "Deluxe king", 46, 1.18, 1),
    RoomType("EXE", "Executive king, lounge access", 20, 1.42, 2),
    RoomType("STE", "One bedroom suite", 10, 1.85, 3),
])


# Taste, as a utility offset per (segment, room type), with the entry type
# fixed at zero because only differences matter in a choice model.
#
# These are not free parameters to be tuned until the answer looks good.  Each
# row is a statement about the market that a revenue manager would either
# recognise or argue with, which is the only useful kind of assumption:
#
#   RETAIL  leisure guests trade up on the way in, and a suite is a special
#           occasion rather than a default
#   OTA     the channel sorts by price, so demand collapses onto the entry
#           type harder than in any other segment
#   CORP    an expense account, and a preference for a king bed and the lounge
#   GROUP   blocks are quoted on the entry type, and a tour operator asking
#           for thirty suites is not a real request
#
# Calibrated so that demand over the whole house roughly tracks the rooms that
# were built, because a hotel that wanted a different mix would have built a
# different hotel.  Roughly, not exactly: at ladder prices this market wants
# 1.34 executive rooms for every one that exists and only 0.88 of the standard
# rooms, which is the tension the network optimizer is there to trade off.
SEGMENT_TYPE_PREFERENCE: Dict[str, Dict[str, float]] = {
    "RETAIL": {"STD": 0.00, "DLX": -0.15, "EXE": -1.00, "STE": -1.50},
    "OTA":    {"STD": 0.00, "DLX": -0.50, "EXE": -1.75, "STE": -2.25},
    "CORP":   {"STD": 0.00, "DLX": 0.35, "EXE": 0.45, "STE": -1.35},
    "GROUP":  {"STD": 0.00, "DLX": -1.20, "EXE": -2.60, "STE": -3.25},
}


def preferences_for(inventory: Inventory,
                    table: Optional[Dict[str, Dict[str, float]]] = None
                    ) -> Dict[str, Dict[str, float]]:
    """Preference matrix restricted to the types this property actually has.

    A missing entry is zero, which reads as indifference against the entry
    type.  That is the right default: an unknown taste should not become a
    strong opinion just because nobody filled in the table.
    """
    table = SEGMENT_TYPE_PREFERENCE if table is None else table
    out: Dict[str, Dict[str, float]] = {}
    for code in SEGMENT_ORDER:
        row = table.get(code, {})
        out[code] = {t.code: float(row.get(t.code, 0.0)) for t in inventory}
    return out
