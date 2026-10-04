"""Simulateur de Texas Hold'em No-Limit avec bots (hors ligne uniquement).

Fonctionnalités :
  * mains complètes : blinds, 4 tours d'enchères, relances, tapis, side pots ;
  * calcul d'équité Monte-Carlo ;
  * plusieurs stratégies de bots (voir ``STRATEGIES``) ;
  * statistiques de session (gain en bb, bb/100, taux de victoire).

Exemples :
    python SimuPokerBot.py --hands 2000 --seed 1
    python SimuPokerBot.py --bots tight,maniac,station,random --hands 500 --verbose
"""
import argparse
import random
from collections import defaultdict
from dataclasses import dataclass, field

from treys import Card, Deck, Evaluator

EVALUATOR = Evaluator()
FOLD, CALL, RAISE = "fold", "call", "raise"
STREETS = ("preflop", "flop", "turn", "river")
MAX_RAISES_PER_STREET = 4


# --------------------------------------------------------------------------
# Équité
# --------------------------------------------------------------------------
def estimate_equity(hole, board, num_opponents, iterations=300, rng=random):
    """Probabilité (0..1) de gagner (égalités = partage) contre des mains aléatoires."""
    known = set(hole) | set(board)
    remaining = [c for c in Deck.GetFullDeck() if c not in known]
    missing = 5 - len(board)
    needed = missing + 2 * num_opponents
    total = 0.0
    for _ in range(iterations):
        draw = rng.sample(remaining, needed)
        full_board = list(board) + draw[:missing]
        mine = EVALUATOR.evaluate(full_board, hole)
        best_other, ties = None, 0
        for k in range(num_opponents):
            opp = draw[missing + 2 * k: missing + 2 * k + 2]
            score = EVALUATOR.evaluate(full_board, opp)
            if best_other is None or score < best_other:
                best_other, ties = score, 1
            elif score == best_other:
                ties += 1
        if best_other is None or mine < best_other:
            total += 1
        elif mine == best_other:
            total += 1 / (ties + 1)
    return total / iterations


# --------------------------------------------------------------------------
# Bots
# --------------------------------------------------------------------------
@dataclass
class View:
    """Ce qu'un bot a le droit de voir quand il doit agir."""
    hole: list
    board: list
    street: str
    pot: int
    to_call: int
    stack: int           # jetons restants derrière
    min_raise_to: int    # mise totale minimale pour relancer
    max_raise_to: int    # tapis
    opponents: int       # adversaires encore dans la main
    can_raise: bool
    big_blind: int
    rng: random.Random = field(default=random, repr=False)

    @property
    def pot_odds(self):
        return self.to_call / (self.pot + self.to_call) if self.to_call else 0.0


class Bot:
    name = "bot"

    def act(self, view):
        raise NotImplementedError


class CallingStation(Bot):
    name = "station"

    def act(self, view):
        return CALL, 0


class RandomBot(Bot):
    name = "random"

    def act(self, view):
        r = view.rng.random()
        if r < 0.15 and view.to_call:
            return FOLD, 0
        if r > 0.85 and view.can_raise:
            return RAISE, view.min_raise_to
        return CALL, 0


class Maniac(Bot):
    name = "maniac"

    def act(self, view):
        if view.can_raise and view.rng.random() < 0.65:
            return RAISE, min(view.max_raise_to, view.min_raise_to + view.pot)
        return CALL, 0


class EquityBot(Bot):
    """Décide à partir de l'équité, des cotes du pot et d'un peu de bluff."""

    def __init__(self, name="equity", raise_threshold=0.62, call_margin=0.05,
                 bluff_freq=0.05, bet_fraction=0.7, iterations=250):
        self.name = name
        self.raise_threshold = raise_threshold
        self.call_margin = call_margin
        self.bluff_freq = bluff_freq
        self.bet_fraction = bet_fraction
        self.iterations = iterations
        self._cache = {}

    def equity(self, view):
        key = None
        if not view.board:  # preflop : on met en cache (mains équivalentes)
            ranks = sorted(Card.get_rank_int(c) for c in view.hole)
            suited = Card.get_suit_int(view.hole[0]) == Card.get_suit_int(view.hole[1])
            key = (tuple(ranks), suited, view.opponents)
            if key in self._cache:
                return self._cache[key]
        eq = estimate_equity(view.hole, view.board, view.opponents,
                             self.iterations, view.rng)
        if key is not None:
            self._cache[key] = eq
        return eq

    def act(self, view):
        eq = self.equity(view)
        # Seuil ajusté au nombre d'adversaires : plus il y en a, plus il faut de force.
        threshold = self.raise_threshold - 0.08 * (view.opponents == 1)
        if view.can_raise and (eq >= threshold or
                               (view.rng.random() < self.bluff_freq and not view.to_call)):
            target = view.to_call + int(view.pot * self.bet_fraction)
            return RAISE, max(view.min_raise_to, target)
        if view.to_call == 0:
            return CALL, 0
        if eq >= view.pot_odds + self.call_margin:
            return CALL, 0
        return FOLD, 0


STRATEGIES = {
    "tight": lambda: EquityBot("tight", raise_threshold=0.70, call_margin=0.08, bluff_freq=0.02),
    "equity": lambda: EquityBot("equity"),
    "loose": lambda: EquityBot("loose", raise_threshold=0.55, call_margin=0.0, bluff_freq=0.10),
    "maniac": Maniac,
    "station": CallingStation,
    "random": RandomBot,
}


def make_bots(names):
    bots, seen = [], defaultdict(int)
    for n in names:
        if n not in STRATEGIES:
            raise ValueError(f"Stratégie inconnue : {n!r} (choix : {', '.join(STRATEGIES)})")
        bot = STRATEGIES[n]()
        seen[n] += 1
        if names.count(n) > 1:
            bot.name = f"{n}{seen[n]}"
        bots.append(bot)
    return bots


# --------------------------------------------------------------------------
# Moteur de jeu
# --------------------------------------------------------------------------
def deal_hands(num_players, deck=None):
    deck = deck or Deck()
    return [deck.draw(2) for _ in range(num_players)], deck


def split_pots(contributions, live):
    """Construit les pots (montant, joueurs éligibles) à partir des mises totales."""
    pots, prev = [], 0
    for level in sorted({c for c in contributions if c > 0}):
        amount = sum(min(c, level) - min(c, prev) for c in contributions)
        eligible = [i for i in live if contributions[i] >= level]
        if not eligible:  # mise d'un joueur couché au-delà de tous les joueurs actifs
            top = max(contributions[i] for i in live)
            eligible = [i for i in live if contributions[i] == top]
        if pots and pots[-1][1] == eligible:
            pots[-1] = (pots[-1][0] + amount, eligible)
        else:
            pots.append((amount, eligible))
        prev = level
    return pots


class Hand:
    """Une main de Hold'em entre ``len(bots)`` joueurs. ``self.profit`` = gain net par joueur."""

    def __init__(self, bots, button=0, stack=100, big_blind=2, rng=random, log=None):
        self.bots, self.n = bots, len(bots)
        self.button, self.bb, self.rng = button, big_blind, rng
        self.log = log or (lambda *_: None)
        self.stack = [stack] * self.n
        self.start = list(self.stack)
        self.total = [0] * self.n      # mise totale dans la main
        self.bet = [0] * self.n        # mise sur la street courante
        self.folded = [False] * self.n
        self.board = []
        self.hands = []

    # -- utilitaires -------------------------------------------------------
    def _pot(self):
        return sum(self.total)

    def _live(self):
        return [i for i in range(self.n) if not self.folded[i]]

    def _can_act(self):
        return [i for i in self._live() if self.stack[i] > 0]

    def _put(self, i, amount):
        amount = min(amount, self.stack[i])
        self.stack[i] -= amount
        self.bet[i] += amount
        self.total[i] += amount

    def _seat(self, offset):
        return (self.button + offset) % self.n

    # -- déroulement -------------------------------------------------------
    def play(self):
        deck = Deck()
        self.hands, _ = deal_hands(self.n, deck)
        for i, h in enumerate(self.hands):
            self.log(f"  {self.bots[i].name}: {Card.ints_to_pretty_str(h)}")

        if self.n == 2:
            sb, bb = self.button, self._seat(1)
            first_pre = sb
        else:
            sb, bb = self._seat(1), self._seat(2)
            first_pre = self._seat(3)
        self._put(sb, self.bb // 2)
        self._put(bb, self.bb)

        for street_idx, street in enumerate(STREETS):
            if street_idx > 0:
                n_cards = 3 if street == "flop" else 1
                self.board += deck.draw(n_cards)
                self.log(f"[{street}] {Card.ints_to_pretty_str(self.board)}")
                self.bet = [0] * self.n
            if len(self._live()) == 1:
                break
            if len(self._can_act()) > 1 or (street_idx == 0 and self._needs_action()):
                start = first_pre if street_idx == 0 else self._first_after_button()
                self._betting_round(start, street, street_idx == 0)
        # Compléter le board si tout le monde est à tapis.
        while len(self.board) < 5 and len(self._live()) > 1:
            self.board += deck.draw(3 if not self.board else 1)
        return self._showdown()

    def _needs_action(self):
        # Préflop avec un seul joueur pouvant agir : il doit quand même égaliser les blinds.
        top = max(self.bet)
        return any(self.bet[i] < top for i in self._can_act())

    def _first_after_button(self):
        i = self.button
        for _ in range(self.n):
            i = (i + 1) % self.n
            if not self.folded[i] and self.stack[i] > 0:
                return i
        return self.button

    def _betting_round(self, start, street, preflop):
        last_raise = self.bb
        raises = 0
        order = [(start + k) % self.n for k in range(self.n)]
        pending = [i for i in order if not self.folded[i] and self.stack[i] > 0]
        while pending:
            i = pending.pop(0)
            if self.folded[i] or self.stack[i] == 0:
                continue
            if len(self._live()) == 1:
                return
            current = max(self.bet)
            to_call = min(current - self.bet[i], self.stack[i])
            if to_call == 0 and not any(self.stack[j] > 0 for j in self._live() if j != i):
                continue  # personne à qui relancer
            can_raise = (raises < MAX_RAISES_PER_STREET
                         and self.stack[i] > to_call
                         and any(self.stack[j] > 0 for j in self._live() if j != i))
            min_to = min(current + last_raise, self.bet[i] + self.stack[i])
            view = View(
                hole=self.hands[i], board=list(self.board), street=street,
                pot=self._pot(), to_call=to_call, stack=self.stack[i],
                min_raise_to=min_to, max_raise_to=self.bet[i] + self.stack[i],
                opponents=len(self._live()) - 1, can_raise=can_raise,
                big_blind=self.bb, rng=self.rng)
            action, amount = self.bots[i].act(view)

            if action == FOLD and to_call == 0:
                action = CALL  # on ne se couche jamais gratuitement
            if action == RAISE and not can_raise:
                action = CALL
            if action == FOLD:
                self.folded[i] = True
                self.log(f"  {self.bots[i].name} se couche")
                continue
            if action == CALL:
                self._put(i, to_call)
                self.log(f"  {self.bots[i].name} {'suit ' + str(to_call) if to_call else 'check'}")
                continue
            # RAISE
            target = max(min_to, min(int(amount), view.max_raise_to))
            increase = target - current
            if increase >= last_raise:
                last_raise = increase
            self._put(i, target - self.bet[i])
            raises += 1
            self.log(f"  {self.bots[i].name} relance à {self.bet[i]}")
            # tous les autres joueurs doivent répondre, en repartant après le relanceur
            idx = order.index(i)
            rotated = order[idx + 1:] + order[:idx]
            pending = [j for j in rotated if not self.folded[j] and self.stack[j] > 0]

    def _showdown(self):
        live = self._live()
        payout = [0] * self.n
        if len(live) == 1:
            payout[live[0]] = self._pot()
        else:
            scores = {i: EVALUATOR.evaluate(self.board, self.hands[i]) for i in live}
            for amount, eligible in split_pots(self.total, live):
                best = min(scores[i] for i in eligible)
                winners = [i for i in eligible if scores[i] == best]
                share, rest = divmod(amount, len(winners))
                for k, w in enumerate(winners):
                    payout[w] += share + (1 if k < rest else 0)
            for i in live:
                self.log(f"  {self.bots[i].name}: {EVALUATOR.class_to_string(EVALUATOR.get_rank_class(scores[i]))}")
        self.profit = [payout[i] - self.total[i] for i in range(self.n)]
        for i, p in enumerate(self.profit):
            if p > 0:
                self.log(f"  => {self.bots[i].name} gagne {p}")
        return self.profit


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
def run_session(bots, hands=1000, stack=100, big_blind=2, seed=None, verbose=False):
    rng = random.Random(seed)
    if seed is not None:
        random.seed(seed)  # treys.Deck utilise le module random global
    net = defaultdict(float)
    wins = defaultdict(int)
    log = print if verbose else None
    for h in range(hands):
        if verbose:
            print(f"\n=== Main {h + 1} ===")
        hand = Hand(bots, button=h % len(bots), stack=stack, big_blind=big_blind, rng=rng, log=log)
        profit = hand.play()
        for i, p in enumerate(profit):
            net[bots[i].name] += p
            wins[bots[i].name] += p > 0
    return {b.name: {"net": net[b.name], "bb": net[b.name] / big_blind,
                     "bb_per_100": net[b.name] / big_blind / hands * 100,
                     "win_rate": wins[b.name] / hands} for b in bots}


def print_report(results, hands):
    print(f"\nRésultats sur {hands} mains")
    print(f"{'Bot':<10}{'Gain (jetons)':>15}{'Gain (bb)':>12}{'bb/100':>10}{'Mains gagnées':>16}")
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["net"]):
        print(f"{name:<10}{r['net']:>15.0f}{r['bb']:>12.1f}{r['bb_per_100']:>10.1f}{r['win_rate']:>15.1%}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Simulation de Texas Hold'em No-Limit entre bots")
    p.add_argument("--bots", default="tight,equity,loose,station",
                   help=f"stratégies séparées par des virgules ({', '.join(STRATEGIES)})")
    p.add_argument("--hands", type=int, default=1000)
    p.add_argument("--stack", type=int, default=100, help="tapis initial à chaque main")
    p.add_argument("--big-blind", type=int, default=2)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--verbose", action="store_true", help="affiche chaque main")
    args = p.parse_args(argv)

    names = [n.strip() for n in args.bots.split(",") if n.strip()]
    if not 2 <= len(names) <= 9:
        p.error("il faut entre 2 et 9 bots")
    bots = make_bots(names)
    results = run_session(bots, args.hands, args.stack, args.big_blind, args.seed, args.verbose)
    print_report(results, args.hands)


if __name__ == "__main__":
    main()
