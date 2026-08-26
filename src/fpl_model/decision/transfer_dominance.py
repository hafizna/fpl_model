"""Audit a transfer recommendation against a named goalkeeper reinvestment counterfactual.

`decision/transfer.py`'s single-transfer engine already enumerates selling an
expensive backup goalkeeper for a cheap one -- but as an isolated one-swap
comparison, it never spends the freed bank on anyone else. Design principle 6
requires comparing against "a set-and-forget goalkeeper plus a cheap backup ...
after the saved funds are optimally reinvested" -- a genuine TWO-transfer combo
(sell the expensive backup for the cheapest legal replacement, then take the
single best net-gain reinvestment transfer with the money that frees up).
General multi-transfer search is a separate, larger, not-yet-built Sprint 6
item; this module is deliberately narrower -- one NAMED two-transfer
counterfactual, not a general search -- so it can exist before that.

This never changes what `recommend_single_transfers` returns. It is read-only
auditing layered on top, exactly like `decision/initial_squad_dominance.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.squad import ValidatedSquad
from fpl_model.decision.transfer import (
    DEFAULT_HIT_COST,
    TransferRecommendation,
    TransferTarget,
    apply_single_transfer,
    recommend_single_transfers,
)


@dataclass(frozen=True, slots=True)
class GoalkeeperReinvestmentAudit:
    report: dict[str, Any]

    @property
    def dominates_recommendation(self) -> bool | None:
        return self.report["dominates_recommendation"]


def _transfer_cost_for(squad: ValidatedSquad, *, transfers_used: int, hit_cost: float) -> float:
    if squad.unlimited_transfers:
        return 0.0
    free_transfers = squad.free_transfers or 0
    chargeable = max(0, transfers_used - free_transfers)
    return chargeable * hit_cost


def audit_goalkeeper_reinvestment(
    squad: ValidatedSquad,
    owned_projections: tuple[PlayerGameweekProjection, ...],
    targets: tuple[TransferTarget, ...],
    *,
    recommendation: TransferRecommendation,
    hit_cost: float = DEFAULT_HIT_COST,
) -> GoalkeeperReinvestmentAudit:
    """Check whether selling the priciest bench GK and reinvesting beats the recommendation.

    Identifies the squad's most expensive goalkeeper who is NOT the higher-xPts
    starter of the two (i.e. the backup), replaces it with the cheapest legal
    same-position target, then -- among the remaining non-goalkeeper targets --
    takes the single best net-gain reinvestment using the bank freed by the
    goalkeeper swap. Both legs are charged their own hit cost from the squad's
    free-transfer state (0, 1, or 2 free transfers available); this does not
    assume both are free.

    Reports `dominates_recommendation` (comparing the combo's OWN two-transfer
    net xPts gain against `recommendation.recommended`'s net gain) rather than
    silently replacing it -- a dominated recommendation should be reviewed, not
    auto-replaced.
    """
    goalkeepers = sorted(
        (player for player in squad.players if player.position == "GK"),
        key=lambda player: (-player.current_price_tenths, player.fpl_id),
    )
    if len(goalkeepers) != 2:
        return GoalkeeperReinvestmentAudit(
            report={
                "label": "goalkeeper_reinvestment_audit_v1",
                "applicable": False,
                "reason": f"squad does not have exactly two goalkeepers (found {len(goalkeepers)})",
                "dominates_recommendation": None,
            }
        )
    backup = goalkeepers[0]

    gk_targets = sorted(
        (
            target
            for target in targets
            if target.player.position == "GK" and target.player.fpl_id != backup.fpl_id
        ),
        key=lambda target: (target.player.current_price_tenths, target.player.fpl_id),
    )
    if not gk_targets:
        return GoalkeeperReinvestmentAudit(
            report={
                "label": "goalkeeper_reinvestment_audit_v1",
                "applicable": False,
                "reason": "no legal goalkeeper replacement target available",
                "dominates_recommendation": None,
            }
        )
    cheapest_gk = gk_targets[0]
    if cheapest_gk.player.current_price_tenths >= backup.current_price_tenths:
        return GoalkeeperReinvestmentAudit(
            report={
                "label": "goalkeeper_reinvestment_audit_v1",
                "applicable": False,
                "reason": (
                    "the cheapest legal goalkeeper replacement is not cheaper than the "
                    "squad's own backup goalkeeper, so there is no bank to reinvest"
                ),
                "dominates_recommendation": None,
            }
        )

    bank_after_gk_swap = (
        squad.bank_tenths + backup.selling_price_tenths - cheapest_gk.player.current_price_tenths
    )
    try:
        squad_after_gk_swap = apply_single_transfer(
            squad,
            outgoing=backup,
            incoming=cheapest_gk.player,
            bank_after_tenths=bank_after_gk_swap,
        )
    except ValueError as error:
        return GoalkeeperReinvestmentAudit(
            report={
                "label": "goalkeeper_reinvestment_audit_v1",
                "applicable": False,
                "reason": f"goalkeeper swap is not legal on its own: {error}",
                "dominates_recommendation": None,
            }
        )

    projections_after_gk_swap = tuple(
        cheapest_gk.projection if row.fpl_id == backup.fpl_id else row
        for row in owned_projections
    )
    non_gk_targets = tuple(target for target in targets if target.player.position != "GK")
    reinvestment = recommend_single_transfers(
        squad_after_gk_swap,
        projections_after_gk_swap,
        non_gk_targets,
        top_n=1,
        hit_cost=hit_cost,
    )
    best_reinvestment = reinvestment.recommended

    transfers_used = 1 if best_reinvestment.is_no_transfer else 2
    combo_transfer_cost = _transfer_cost_for(
        squad, transfers_used=transfers_used, hit_cost=hit_cost
    )
    combo_gross_gain = best_reinvestment.lineup.total_xpts - recommendation.no_transfer.lineup.total_xpts
    combo_net_gain = combo_gross_gain - combo_transfer_cost

    recommended_net_gain = recommendation.recommended.net_xpts_gain
    dominates = combo_net_gain > recommended_net_gain

    payload: dict[str, Any] = {
        "label": "goalkeeper_reinvestment_audit_v1",
        "applicable": True,
        "outgoing_backup_goalkeeper_fpl_id": backup.fpl_id,
        "incoming_cheap_goalkeeper_fpl_id": cheapest_gk.player.fpl_id,
        "bank_freed_tenths": backup.selling_price_tenths - cheapest_gk.player.current_price_tenths,
        "reinvestment": {
            "outgoing_fpl_id": (
                None if best_reinvestment.outgoing is None else best_reinvestment.outgoing.fpl_id
            ),
            "incoming_fpl_id": (
                None if best_reinvestment.incoming is None else best_reinvestment.incoming.fpl_id
            ),
        },
        "transfers_used": transfers_used,
        "combo_transfer_cost": combo_transfer_cost,
        "combo_gross_xpts_gain": combo_gross_gain,
        "combo_net_xpts_gain": combo_net_gain,
        "recommended_net_xpts_gain": recommended_net_gain,
        "dominates_recommendation": dominates,
        "limitations": [
            "This audits exactly one named two-transfer combo (backup goalkeeper for the "
            "cheapest legal replacement, then the single best reinvestment), not a general "
            "multi-transfer search -- see the not-yet-built multi-transfer/chip-aware "
            "optimization Sprint 6 item.",
            "Both legs' hit cost is derived from the squad's OWN free-transfer state, not "
            "assumed free -- a squad with zero free transfers pays two hits.",
            "'dominates_recommendation' compares only against recommendation.recommended's "
            "net gain, not against every other single-transfer alternative.",
        ],
    }
    return GoalkeeperReinvestmentAudit(report=payload)
