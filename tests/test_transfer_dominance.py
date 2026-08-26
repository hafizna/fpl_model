from __future__ import annotations

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import TransferTarget, recommend_single_transfers
from fpl_model.decision.transfer_dominance import audit_goalkeeper_reinvestment
from tests.test_lineup import _projections
from tests.test_squad import _players, _validate


def _target(
    fpl_id: int,
    position: str,
    *,
    xpts: float,
    price: int,
    team_id: int = 90,
) -> TransferTarget:
    player = SquadPlayer(
        fpl_id=fpl_id,
        player_code=1000 + fpl_id,
        player_name=f"Target {fpl_id}",
        team_id=team_id,
        position=position,
        current_price_tenths=price,
        purchase_price_tenths=price,
        selling_price_tenths=price,
        squad_position=1,
        is_captain=False,
        is_vice_captain=False,
    )
    return TransferTarget(
        player=player,
        projection=PlayerGameweekProjection(fpl_id=fpl_id, expected_points=xpts, uncertainty=1.0),
    )


def test_finds_a_dominating_goalkeeper_plus_reinvestment_combo():
    # Squad's fpl_id=12 GK is priced 62 tenths and scores only 2.0 xpts -- the
    # never-starting backup. A cheap GK target frees 22 tenths, which a strong
    # forward target can then absorb for a large net gain.
    squad = _validate(_players(), free_transfers=2, unlimited_transfers=False)
    cheap_gk = _target(50, "GK", xpts=1.0, price=40)
    strong_forward = _target(51, "FWD", xpts=20.0, price=80)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (cheap_gk,))

    audit = audit_goalkeeper_reinvestment(
        squad,
        tuple(_projections()),
        (cheap_gk, strong_forward),
        recommendation=recommendation,
    )

    assert audit.report["applicable"] is True
    assert audit.report["outgoing_backup_goalkeeper_fpl_id"] == 12
    assert audit.report["incoming_cheap_goalkeeper_fpl_id"] == 50
    assert audit.report["bank_freed_tenths"] == 22
    assert audit.report["reinvestment"]["incoming_fpl_id"] == 51
    assert audit.report["transfers_used"] == 2
    assert audit.dominates_recommendation is True
    assert audit.report["combo_net_xpts_gain"] > audit.report["recommended_net_xpts_gain"]


def test_charges_two_hits_when_squad_has_no_free_transfers():
    squad = _validate(_players(), free_transfers=0, unlimited_transfers=False)
    cheap_gk = _target(50, "GK", xpts=1.0, price=40)
    strong_forward = _target(51, "FWD", xpts=20.0, price=80)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (cheap_gk,))

    audit = audit_goalkeeper_reinvestment(
        squad,
        tuple(_projections()),
        (cheap_gk, strong_forward),
        recommendation=recommendation,
    )

    assert audit.report["transfers_used"] == 2
    assert audit.report["combo_transfer_cost"] == 8.0


def test_charges_one_hit_when_squad_has_exactly_one_free_transfer():
    squad = _validate(_players(), free_transfers=1, unlimited_transfers=False)
    cheap_gk = _target(50, "GK", xpts=1.0, price=40)
    strong_forward = _target(51, "FWD", xpts=20.0, price=80)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (cheap_gk,))

    audit = audit_goalkeeper_reinvestment(
        squad,
        tuple(_projections()),
        (cheap_gk, strong_forward),
        recommendation=recommendation,
    )

    assert audit.report["combo_transfer_cost"] == 4.0


def test_not_applicable_when_no_cheaper_goalkeeper_target_exists():
    squad = _validate(_players())
    expensive_gk = _target(50, "GK", xpts=1.0, price=200)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (expensive_gk,))

    audit = audit_goalkeeper_reinvestment(
        squad, tuple(_projections()), (expensive_gk,), recommendation=recommendation
    )

    assert audit.report["applicable"] is False
    assert audit.dominates_recommendation is None


def test_not_applicable_with_no_goalkeeper_targets_at_all():
    squad = _validate(_players())
    forward_only = _target(51, "FWD", xpts=20.0, price=90)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (forward_only,))

    audit = audit_goalkeeper_reinvestment(
        squad, tuple(_projections()), (forward_only,), recommendation=recommendation
    )

    assert audit.report["applicable"] is False
    assert "no legal goalkeeper replacement" in audit.report["reason"]


def test_does_not_dominate_when_reinvestment_target_is_weak():
    squad = _validate(_players(), free_transfers=2)
    cheap_gk = _target(50, "GK", xpts=1.0, price=40)
    weak_forward = _target(51, "FWD", xpts=0.5, price=10)
    recommendation = recommend_single_transfers(squad, tuple(_projections()), (cheap_gk,))

    audit = audit_goalkeeper_reinvestment(
        squad,
        tuple(_projections()),
        (cheap_gk, weak_forward),
        recommendation=recommendation,
    )

    assert audit.report["applicable"] is True
    assert audit.dominates_recommendation is False
