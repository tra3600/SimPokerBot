import random

import pytest
from treys import Card

from SimuPokerBot import (CALL, FOLD, RAISE, Bot, CallingStation, Hand, Maniac,
                          STRATEGIES, estimate_equity, make_bots, run_session, split_pots)


def cards(*names):
    return [Card.new(n) for n in names]


def test_equity_aces_beat_random():
    eq = estimate_equity(cards("As", "Ah"), [], 1, iterations=1500, rng=random.Random(0))
    assert 0.80 < eq < 0.90


def test_equity_nuts_on_river():
    eq = estimate_equity(cards("As", "Ks"), cards("Qs", "Js", "Ts", "2d", "3c"), 2,
                         iterations=200, rng=random.Random(0))
    assert eq == 1.0


def test_split_pots_side_pot():
    # A all-in 50, B et C 100 ; A, B, C vivants
    pots = split_pots([50, 100, 100], [0, 1, 2])
    assert pots == [(150, [0, 1, 2]), (100, [1, 2])]


def test_split_pots_uncalled_bet_returns():
    pots = split_pots([100, 40], [0, 1])
    assert pots == [(80, [0, 1]), (60, [0])]


class Folder(Bot):
    name = "folder"

    def act(self, view):
        return FOLD, 0


class AllIn(Bot):
    name = "allin"

    def act(self, view):
        return (RAISE, view.max_raise_to) if view.can_raise else (CALL, 0)


@pytest.mark.parametrize("n", [2, 3, 6])
def test_chips_conserved_and_terminates(n):
    rng = random.Random(n)
    random.seed(n)
    pool = [CallingStation, Maniac, AllIn, Folder]
    for h in range(60):
        bots = [pool[(h + k) % 4]() for k in range(n)]
        hand = Hand(bots, button=h % n, rng=rng)
        profit = hand.play()
        assert sum(profit) == 0
        assert all(s >= 0 for s in hand.stack)


def test_everyone_folds_blinds_win():
    bots = [Folder(), Folder(), Folder()]
    hand = Hand(bots, button=0)
    profit = hand.play()
    # SB=1 perd 1, BB=2 gagne 1 ; le bouton se couche gratuitement impossible -> check/call
    assert sum(profit) == 0


def test_session_and_strategies():
    res = run_session(make_bots(list(STRATEGIES)[:4]), hands=30, seed=3)
    assert abs(sum(r["net"] for r in res.values())) < 1e-9


def test_unknown_strategy():
    with pytest.raises(ValueError):
        make_bots(["nope"])
