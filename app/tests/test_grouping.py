"""Tests for the exact-decimal minimum-group partition algorithm."""

from decimal import Decimal

import pytest

from app.grouping import Peak, solve_deisotoping


def D(text: str) -> Decimal:
    return Decimal(text)


def solve(mz_map, charges=(1, 2, 3), error="0.002"):
    peaks = [Peak(peak_id=pid, mz=D(mz)) for pid, mz in mz_map.items()]
    return solve_deisotoping(peaks, list(charges), D(error))


def test_single_peak_is_singleton_with_no_charge():
    out = solve({"p1": "100.000000"})
    assert out["group_count"] == 1
    assert out["partition"] == [{"peak_ids": ["p1"], "charge": None}]
    possibility = out["peak_possibilities"][0]
    assert possibility["possible_member_sets"] == [["p1"]]
    assert possibility["possible_charges"] == [None]
    assert possibility["certain"] is True


def test_isotopic_pair_charge_one_merges_into_one_group():
    out = solve({"p1": "100.000000", "p2": "101.003355"})
    assert out["group_count"] == 1
    assert out["partition"] == [{"peak_ids": ["p1", "p2"], "charge": 1}]
    for possibility in out["peak_possibilities"]:
        assert possibility["certain"] is True
        assert possibility["possible_member_sets"] == [["p1", "p2"]]
        assert possibility["possible_charges"] == [1]


def test_spacing_exactly_at_error_boundary_is_accepted():
    # deviation == error, rule uses "<=" so the pair must still merge
    out = solve(
        {"p1": "100.000000", "p2": "101.005355"},
        charges=(1,),
        error="0.002",
    )
    assert out["group_count"] == 1
    assert out["partition"][0]["charge"] == 1


def test_spacing_just_over_error_boundary_stays_split():
    out = solve(
        {"p1": "100.000000", "p2": "101.005356"},
        charges=(1,),
        error="0.002",
    )
    assert out["group_count"] == 2
    assert all(group["charge"] is None for group in out["partition"])


def test_charge_two_spacing_half_neutron_mass():
    out = solve(
        {"p1": "200.000000", "p2": "200.5016775"},
        charges=(2,),
        error="0",
    )
    assert out["group_count"] == 1
    assert out["partition"][0]["charge"] == 2


def test_charge_three_spacing():
    # 1.003355 / 3 is a non-terminating decimal, so the spacing computed by
    # the engine is the 60-digit rounded value; a matching input rounded to
    # the same digits is accepted within a tiny tolerance.
    spacing = D("1.003355") / D("3")
    out = solve(
        {"p1": "3", "p2": format(D("3") + spacing, "f")},
        charges=(3,),
        error="0.000000000000000000000000000000000000000000000000000000001",
    )
    assert out["group_count"] == 1
    assert out["partition"][0]["charge"] == 3

    # with a terminating decimal spacing, error zero means truly exact math
    exact = D("0.25")  # would correspond to 1.003355 / charge for charge 4
    out_zero = solve(
        {"p1": "3", "p2": format(D("3") + exact, "f")},
        charges=(2,),  # charge 2 spacing 0.5016775 also terminating
        error="0",
    )
    assert out_zero["group_count"] == 2  # 0.25 != 0.5016775 exactly


def test_adjacent_check_applies_to_every_pair_in_group():
    # p1-p2 fits, p2-p3 fits, but p3-p4 does not -> one triple + one singleton
    spacing = "1.003355"
    out = solve(
        {
            "p1": "100.000000",
            "p2": D("100.000000") + D(spacing),
            "p3": D("100.000000") + D(spacing) * 2,
            "p4": "200.000000",
        },
        charges=(1,),
        error="0",
    )
    assert out["group_count"] == 2
    groups = sorted(out["partition"], key=lambda g: g["peak_ids"])
    assert groups[0] == {"peak_ids": ["p1", "p2", "p3"], "charge": 1}
    assert groups[1] == {"peak_ids": ["p4"], "charge": None}


def test_ambiguity_between_two_two_peak_groups():
    # A-B fits charge 1 (gap 1.003355), B-C fits charge 2 (gap ~0.5016775).
    # ABC is not a valid group (one shared charge cannot satisfy both gaps),
    # so the two optimal partitions are [[A,B],[C]] and [[A],[B,C]].
    out = solve(
        {"A": "100.000000", "B": "101.003355", "C": "101.5050325"},
        charges=(1, 2),
        error="0",
    )
    assert out["group_count"] == 2
    assert len(out["optimal_partitions"]) == 2

    by_peak = {p["peak_id"]: p for p in out["peak_possibilities"]}
    # In one partition A is a singleton; in the other A is the first member
    # of the charge-1 pair, so both its member set and charge are uncertain.
    assert by_peak["A"]["certain"] is False
    assert by_peak["A"]["possible_member_sets"] == [["A"], ["A", "B"]]
    assert by_peak["A"]["possible_charges"] == [None, 1]
    assert by_peak["C"]["possible_member_sets"] == [["B", "C"], ["C"]]
    assert by_peak["C"]["possible_charges"] == [None, 2]
    # B belongs either to {A,B}@1 or {B,C}@2
    assert by_peak["B"]["certain"] is False
    assert by_peak["B"]["possible_member_sets"] == [["A", "B"], ["B", "C"]]
    assert by_peak["B"]["possible_charges"] == [1, 2]
    assert by_peak["B"]["member_set_certain"] is False
    assert by_peak["B"]["charge_certain"] is False


def test_uncertain_member_set_but_certain_charge():
    # A-B and B-C fit charge 2 (gap s2); C-D fits charge 1 (gap s1).  ABC is
    # therefore a valid charge-2 group but ABCD is not.  Two optimal
    # partitions, both with 2 groups:
    #   [ABC@2, D]  and  [AB@2, CD@1]
    # Peak B keeps charge 2 in both, yet its member set differs.
    s2 = D("1.003355") / D("2")
    s1 = D("1.003355")
    a = D("100.000000")
    out = solve(
        {
            "A": str(a),
            "B": str(a + s2),
            "C": str(a + s2 * 2),
            "D": str(a + s2 * 2 + s1),
        },
        charges=(1, 2),
        error="0",
    )
    assert out["group_count"] == 2
    assert len(out["optimal_partitions"]) == 2
    by_peak = {p["peak_id"]: p for p in out["peak_possibilities"]}
    b = by_peak["B"]
    assert b["charge_certain"] is True
    assert b["possible_charges"] == [2]
    assert b["member_set_certain"] is False
    assert sorted(b["possible_member_sets"]) == [["A", "B"], ["A", "B", "C"]]


def test_every_peak_used_exactly_once_in_every_optimal_partition():
    out = solve(
        {
            "p1": "100.000000",
            "p2": "101.003355",
            "p3": "200.000000",
            "p4": "200.5016775",
        },
        charges=(1, 2),
        error="0.001",
    )
    for partition in out["optimal_partitions"]:
        used = [pid for group in partition for pid in group["peak_ids"]]
        assert sorted(used) == ["p1", "p2", "p3", "p4"]
        assert len(used) == len(set(used))


def test_decimal_string_with_long_fraction_is_compared_exactly():
    # Charge 2 spacing 1.003355 / 2 = 0.5016775 is a terminating decimal, so
    # with error zero equality is exact; perturbing the final digit splits
    # the group, proving comparison is on decimal text rather than float.
    p2 = D("3") + D("0.5016775")
    out = solve(
        {"p1": "3", "p2": format(p2, "f")},
        charges=(2,),
        error="0",
    )
    assert out["group_count"] == 1

    text = format(p2, "f")
    perturbed = text[:-1] + ("6" if text[-1] != "6" else "4")
    out2 = solve(
        {"p1": "3", "p2": perturbed},
        charges=(2,),
        error="0",
    )
    assert out2["group_count"] == 2


def test_twelve_peak_limit_is_supported_and_sorted_by_mz():
    spacing = D("1.003355")
    base = D("400.000000")
    # deliberately unordered input
    pairs = {f"p{i:02d}": str(base + spacing * (11 - i)) for i in range(12)}
    out = solve(pairs, charges=(1,), error="0")
    assert out["group_count"] == 1
    group = out["partition"][0]
    assert group["charge"] == 1
    mz_lookup = {pid: D(mz) for pid, mz in pairs.items()}
    ordered = [mz_lookup[pid] for pid in group["peak_ids"]]
    assert ordered == sorted(ordered)
    assert len(group["peak_ids"]) == 12


def test_singleton_allowed_even_when_charge_would_not_fit():
    out = solve(
        {"p1": "100.000000", "p2": "100.700000"},
        charges=(1, 2, 3),
        error="0.001",
    )
    assert out["group_count"] == 2
    assert {tuple(g["peak_ids"]) for g in out["partition"]} == {("p1",), ("p2",)}


def test_empty_inputs_are_rejected():
    with pytest.raises(ValueError):
        solve_deisotoping([], [1], D("0"))
    with pytest.raises(ValueError):
        solve_deisotoping([Peak("p", D("1"))], [], D("0"))
    with pytest.raises(ValueError):
        solve_deisotoping([Peak("p", D("1"))], [1], D("-0.1"))
