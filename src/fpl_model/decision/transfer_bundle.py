"""Atomic same-deadline transfers, including jointly funded upgrades."""

from dataclasses import replace
from itertools import combinations

from fpl_model.decision.squad import SquadPlayer, ValidatedSquad, validate_squad


def apply_transfer_bundle(
    squad: ValidatedSquad,
    moves: tuple[tuple[int, SquadPlayer], ...],
) -> ValidatedSquad:
    """Validate the final squad/budget, never an artificial intermediate squad."""
    owned = {p.fpl_id: p for p in squad.players}
    outgoing = [out for out, _ in moves]
    incoming = [p.fpl_id for _, p in moves]
    if not moves or len(set(outgoing)) != len(outgoing) or len(set(incoming)) != len(incoming):
        raise ValueError("transfer bundle must contain unique outgoing and incoming players")
    if not set(outgoing) <= owned.keys() or set(incoming) & owned.keys():
        raise ValueError("bundle must sell owned players and buy unowned players")
    replacements = {}
    bank = squad.bank_tenths
    for out, new in moves:
        old = owned[out]
        if old.position != new.position:
            raise ValueError("transfer positions must match")
        bank += old.selling_price_tenths - new.current_price_tenths
        replacements[out] = replace(
            new, purchase_price_tenths=new.current_price_tenths,
            selling_price_tenths=new.current_price_tenths,
            squad_position=old.squad_position,
            is_captain=old.is_captain, is_vice_captain=old.is_vice_captain,
        )
    return validate_squad(
        tuple(replacements.get(p.fpl_id, p) for p in squad.players),
        bank_tenths=bank, free_transfers=squad.free_transfers,
        unlimited_transfers=False, chip_period=squad.chip_period,
        chip_states=dict(squad.chip_states),
    )


def shortlisted_pairs(squad, targets, scores, *, protected=frozenset(), limit=20):
    """Prune legal atomic pairs by horizon player gain before exact XI scoring."""
    moves = [(old.fpl_id, target.player) for old in squad.players
             if old.fpl_id not in protected for target in targets
             if old.position == target.player.position]
    candidates = {}
    for first, second in combinations(moves, 2):
        if first[0] == second[0] or first[1].fpl_id == second[1].fpl_id:
            continue
        key = (tuple(sorted((first[0], second[0]))),
               tuple(sorted((first[1].fpl_id, second[1].fpl_id))))
        if key in candidates:
            continue
        try:
            result = apply_transfer_bundle(squad, (first, second))
        except ValueError:
            continue
        gain = sum(scores[p.fpl_id] - scores[out] for out, p in (first, second))
        candidates[key] = (gain, key, (first, second), result)
    return tuple((moves, result) for _, _, moves, result in
                 sorted(candidates.values(), key=lambda row: (-row[0], row[1]))[:limit])
