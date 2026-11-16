"""De-isotoping partition algorithm.

A peak group is either:

* a singleton peak (no charge), or
* a multi-peak group sharing one allowed charge ``q``; peaks are ordered by
  increasing m/z and every adjacent spacing must satisfy

      |(mz[k+1] - mz[k]) - 1.003355 / q| <= method_error

Every peak is used exactly once.  Among all partitions covering every peak we
keep the ones with the minimum number of groups (an optimal de-isotoping
partition), enumerate every optimal partition and derive, per peak, the set of
member-ID sets and charges it can have across *all* optimal partitions.

All m/z arithmetic uses :class:`decimal.Decimal` so the comparisons are exact
on the submitted decimal values rather than on binary floating point numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Optional

# Precision 60 comfortably exceeds any realistic LC-MS decimal length (m/z
# values with an integer part plus dozens of fractional digits) so the rounded
# division 1.003355 / q round-trips exactly through its textual form and the
# exact decimal-string comparisons stay self-consistent.
getcontext().prec = 60

NEUTRON_MASS = Decimal("1.003355")


@dataclass(frozen=True)
class Peak:
    peak_id: str
    mz: Decimal


@dataclass(frozen=True)
class Group:
    """One group inside a concrete partition.

    ``charge`` is ``None`` for a singleton group, otherwise the shared charge.
    """

    peak_ids: tuple[str, ...]
    charge: Optional[int]


def _valid_multi_group(
    mz_values: list[Decimal], start: int, end: int, charge: int, error: Decimal
) -> bool:
    """Check the adjacent-spacing rule for peaks [start, end] with charge."""
    spacing = NEUTRON_MASS / Decimal(charge)
    for k in range(start, end):
        if abs(mz_values[k + 1] - mz_values[k] - spacing) > error:
            return False
    return True


def _build_dp(
    peaks: list[Peak], charges: list[int], error: Decimal
) -> tuple[list[int], list[list[tuple[int, Optional[int]]]]]:
    """Suffix dynamic programming over the sorted peaks.

    Returns the minimum group count for every suffix and, for every suffix
    start, the list of predecessors that attain that minimum.  A predecessor
    is ``(end_index, charge)`` with ``charge is None`` for the singleton
    ``[start]`` and an integer charge for the multi group ``[start..end]``.
    """
    n = len(peaks)
    mz_values = [p.mz for p in peaks]
    dp = [0] * (n + 1)
    choices: list[list[tuple[int, Optional[int]]]] = [[] for _ in range(n)]

    for start in range(n - 1, -1, -1):
        best = 1 + dp[start + 1]
        best_choices: list[tuple[int, Optional[int]]] = [(start, None)]

        for end in range(start + 1, n):
            for charge in charges:  # charges sorted ascending -> deterministic
                if not _valid_multi_group(mz_values, start, end, charge, error):
                    continue
                candidate = 1 + dp[end + 1]
                if candidate < best:
                    best = candidate
                    best_choices = [(end, charge)]
                elif candidate == best and (end, charge) not in best_choices:
                    best_choices.append((end, charge))

        dp[start] = best
        choices[start] = best_choices

    return dp, choices


def _enumerate_optimal_partitions(
    peaks: list[Peak],
    choices: list[list[tuple[int, Optional[int]]]],
) -> list[tuple[Group, ...]]:
    """Enumerate every partition attaining the DP optimum."""
    n = len(peaks)
    results: list[tuple[Group, ...]] = []
    current: list[Group] = []

    def walk(start: int) -> None:
        if start == n:
            results.append(tuple(current))
            return
        for end, charge in choices[start]:
            member_ids = tuple(peaks[k].peak_id for k in range(start, end + 1))
            current.append(Group(member_ids, charge))
            walk(end + 1)
            current.pop()

    walk(0)
    return results


def solve_deisotoping(
    raw_peaks: list[Peak], charges: list[int], error: Decimal
) -> dict:
    """Compute an optimal partition plus per-peak possibility information.

    Returns a JSON-serialisable dict with:

    * ``group_count``       – minimum achievable number of groups;
    * ``partition``         – one deterministic optimal partition;
    * ``optimal_partitions``– every optimal partition (sorted);
    * ``peak_possibilities``– for every peak the member-ID sets and charges it
      may have across all optimal partitions, with certainty flags.
    """
    if not raw_peaks:
        raise ValueError("at least one peak is required")
    if not charges:
        raise ValueError("at least one allowed charge is required")
    if error < 0:
        raise ValueError("error must be non-negative")

    # Deterministic m/z order; peak id breaks ties for equal decimal m/z.
    peaks = sorted(raw_peaks, key=lambda p: (p.mz, p.peak_id))
    ordered_charges = sorted(set(charges))

    _, choices = _build_dp(peaks, ordered_charges, error)
    partitions = _enumerate_optimal_partitions(peaks, choices)

    # Aggregate, per peak, every member set / charge seen in an optimum.
    member_sets_by_peak: dict[str, set[frozenset[str]]] = {p.peak_id: set() for p in peaks}
    charges_by_peak: dict[str, set[Optional[int]]] = {p.peak_id: set() for p in peaks}
    serialised_partitions: list[list[dict]] = []

    for groups in partitions:
        serial_groups: list[dict] = []
        for group in groups:
            serial_groups.append(
                {
                    "peak_ids": list(group.peak_ids),
                    "charge": group.charge,
                }
            )
            members = frozenset(group.peak_ids)
            for peak_id in group.peak_ids:
                member_sets_by_peak[peak_id].add(members)
                charges_by_peak[peak_id].add(group.charge)
        serialised_partitions.append(serial_groups)

    # Deterministic representative partition: choices already place the
    # singleton first and charges ascending, so the first DFS output is stable.
    representative = serialised_partitions[0]

    peak_possibilities: list[dict] = []
    for peak in peaks:
        possible_member_sets = sorted(
            (sorted(member_set) for member_set in member_sets_by_peak[peak.peak_id]),
            key=lambda ids: ids,
        )
        possible_charges = sorted(
            charges_by_peak[peak.peak_id],
            key=lambda q: (q is not None, q if q is not None else 0),
        )
        member_set_certain = len(possible_member_sets) == 1
        charge_certain = len(possible_charges) == 1
        peak_possibilities.append(
            {
                "peak_id": peak.peak_id,
                "possible_member_sets": possible_member_sets,
                "possible_charges": possible_charges,
                "member_set_certain": member_set_certain,
                "charge_certain": charge_certain,
                "certain": member_set_certain and charge_certain,
            }
        )

    return {
        "group_count": len(representative),
        "partition": representative,
        "optimal_partitions": serialised_partitions,
        "peak_possibilities": peak_possibilities,
    }
