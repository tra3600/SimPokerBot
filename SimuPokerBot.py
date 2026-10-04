"""Simulateur de Texas Hold'em No-Limit avec bots (hors ligne uniquement).

Fonctionnalités :
  * mains complètes : blinds, 4 tours d'enchères, relances, tapis, side pots ;
  * calcul d'équité Monte-Carlo ;
  * plusieurs stratégies de bots (voir ``STRATEGIES``) ;
  * statistiques de session (gain en bb, bb/100, taux de victoire).

Exemples :
    python SimuPokerBot.py --hands 2000 --seed 1
    python SimuPokerBot.py --bots tight,maniac,station,random --hands 500 --verbose
    python SimuPokerBot.py --mode tournament --tournaments 20 --seed 1
    python SimuPokerBot.py --mode human --bots tight,loose,maniac
    python SimuPokerBot.py --mode human --tournament --bots tight,loose,maniac
"""
import argparse
import json
import os
import queue
import random
import socket
import sys
import threading
import time
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


class QuitGame(Exception):
    """Le joueur humain quitte la partie."""


class HumanBot(Bot):
    """Joueur humain : lit ses décisions au clavier (``input_fn`` / ``output_fn`` injectables)."""
    name = "Vous"

    def __init__(self, name="Vous", input_fn=input, output_fn=print):
        self.name = name
        self.ask, self.say = input_fn, output_fn

    def act(self, view):
        say = self.say
        say(f"\n>>> Vos cartes : {Card.ints_to_pretty_str(view.hole)}")
        if view.board:
            say(f"    Board ({view.street}) : {Card.ints_to_pretty_str(view.board)}")
        say(f"    Pot : {view.pot} | à suivre : {view.to_call} | votre tapis : {view.stack} "
            f"| adversaires : {view.opponents}")
        options = ["f = se coucher"] if view.to_call else []
        options.append(f"c = suivre ({view.to_call})" if view.to_call else "c = check")
        if view.can_raise:
            options.append(f"r [montant] = relancer à {view.min_raise_to}-{view.max_raise_to}")
            options.append("a = tapis")
        options.append("q = quitter")
        while True:
            try:
                raw = self.ask("    " + " | ".join(options) + "\n    > ").strip().lower()
            except EOFError:
                raise QuitGame
            cmd, _, arg = raw.partition(" ")
            if cmd == "q":
                raise QuitGame
            if cmd == "f" and view.to_call:
                return FOLD, 0
            if cmd == "c":
                return CALL, 0
            if cmd == "a" and view.can_raise:
                return RAISE, view.max_raise_to
            if cmd == "r" and view.can_raise:
                if not arg.strip():
                    return RAISE, view.min_raise_to
                try:
                    amount = int(arg)
                except ValueError:
                    amount = None
                if amount is not None and view.min_raise_to <= amount <= view.max_raise_to:
                    return RAISE, amount
                say(f"    Montant invalide (entre {view.min_raise_to} et {view.max_raise_to}).")
                continue
            say("    Commande non reconnue.")


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

    def __init__(self, bots, button=0, stack=100, big_blind=2, rng=random, log=None,
                 show_hole=True, history=None, label=""):
        self.show_hole = show_hole
        self.history, self.label, self.events = history, label, []
        self.bots, self.n = bots, len(bots)
        self.button, self.bb, self.rng = button, big_blind, rng
        self.log = log or (lambda *_: None)
        self.stack = list(stack) if isinstance(stack, (list, tuple)) else [stack] * self.n
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

    def _ev(self, kind, **data):
        self.events.append({"e": kind, **data})

    def _name(self, i):
        return self.bots[i].name

    @staticmethod
    def _strs(cards):
        return [Card.int_to_str(c) for c in cards]

    # -- déroulement -------------------------------------------------------
    def play(self):
        deck = Deck()
        self.hands, _ = deal_hands(self.n, deck)
        if self.show_hole:
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
        self._ev("blind", p=self._name(sb), amt=self.total[sb], role="SB")
        self._ev("blind", p=self._name(bb), amt=self.total[bb], role="BB")

        for street_idx, street in enumerate(STREETS):
            if street_idx > 0:
                n_cards = 3 if street == "flop" else 1
                self.board += deck.draw(n_cards)
                self._ev("street", street=street, board=self._strs(self.board))
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
                self._ev("fold", p=self._name(i))
                self.log(f"  {self.bots[i].name} se couche")
                continue
            if action == CALL:
                self._put(i, to_call)
                self._ev("call" if to_call else "check", p=self._name(i), amt=to_call,
                         allin=self.stack[i] == 0)
                self.log(f"  {self.bots[i].name} {'suit ' + str(to_call) if to_call else 'check'}")
                continue
            # RAISE
            target = max(min_to, min(int(amount), view.max_raise_to))
            increase = target - current
            if increase >= last_raise:
                last_raise = increase
            added = target - self.bet[i]
            self._put(i, added)
            self._ev("raise", p=self._name(i), to=target, amt=added, allin=self.stack[i] == 0)
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
                self._ev("show", p=self._name(i), cards=self._strs(self.hands[i]),
                         hand=EVALUATOR.class_to_string(EVALUATOR.get_rank_class(scores[i])))
                if not self.show_hole:  # cartes révélées seulement au showdown
                    self.log(f"  {self.bots[i].name}: {Card.ints_to_pretty_str(self.hands[i])}")
                self.log(f"  {self.bots[i].name}: {EVALUATOR.class_to_string(EVALUATOR.get_rank_class(scores[i]))}")
        self.profit = [payout[i] - self.total[i] for i in range(self.n)]
        for i, p in enumerate(self.profit):
            if payout[i] > 0:
                self._ev("win", p=self._name(i), amt=payout[i])
            if p > 0:
                self.log(f"  => {self.bots[i].name} gagne {p}")
        if self.history is not None:
            self.history.append({
                "version": 1, "time": time.time(), "label": self.label,
                "button": self._name(self.button), "bb": self.bb, "board": self._strs(self.board),
                "players": [{"name": self._name(i), "stack": self.start[i],
                             "hole": self._strs(self.hands[i])} for i in range(self.n)],
                "events": self.events,
                "profit": {self._name(i): self.profit[i] for i in range(self.n)}})
        return self.profit


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
def run_session(bots, hands=1000, stack=100, big_blind=2, seed=None, verbose=False, history=None):
    rng = random.Random(seed)
    if seed is not None:
        random.seed(seed)  # treys.Deck utilise le module random global
    net = defaultdict(float)
    wins = defaultdict(int)
    log = print if verbose else None
    for h in range(hands):
        if verbose:
            print(f"\n=== Main {h + 1} ===")
        hand = Hand(bots, button=h % len(bots), stack=stack, big_blind=big_blind, rng=rng, log=log,
                    history=history, label="cash")
        profit = hand.play()
        for i, p in enumerate(profit):
            net[bots[i].name] += p
            wins[bots[i].name] += p > 0
    return {b.name: {"net": net[b.name], "bb": net[b.name] / big_blind,
                     "bb_per_100": net[b.name] / big_blind / hands * 100,
                     "win_rate": wins[b.name] / hands} for b in bots}


# --------------------------------------------------------------------------
# Mode entraînement : conseils et débriefing
# --------------------------------------------------------------------------
RANK_NAMES = {"A": "As", "K": "Roi", "Q": "Dame", "J": "Valet", "T": "10"}
ADVICE_MODES = ("always", "ask", "off")


HAND_NAMES_FR = {
    "Royal Flush": "Quinte flush royale", "Straight Flush": "Quinte flush", "Four of a Kind": "Carré",
    "Full House": "Full", "Flush": "Couleur", "Straight": "Quinte", "Three of a Kind": "Brelan",
    "Two Pair": "Double paire", "Pair": "Paire", "High Card": "Carte haute"}


def hand_name_fr(score):
    return HAND_NAMES_FR.get(EVALUATOR.class_to_string(EVALUATOR.get_rank_class(score)), "?")


def _rank_name(ch):
    return RANK_NAMES.get(ch, ch)


def describe_hole(hole):
    """Nom lisible d'une main de départ : « Paire de 7 », « As-Roi assortis »..."""
    a, b = sorted((Card.int_to_str(c) for c in hole), key=lambda c: "23456789TJQKA".index(c[0]),
                  reverse=True)
    if a[0] == b[0]:
        if a[0] == "A":
            return "Paire d'As"
        return f"Paire de {_rank_name(a[0])}" + ("s" if a[0] in "KQJ" else "")
    return f"{_rank_name(a[0])}-{_rank_name(b[0])} " + ("assortis" if a[1] == b[1] else "dépareillés")


def count_outs(hole, board):
    """Cartes (parmi celles restantes) qui améliorent la catégorie de votre main au prochain tirage.

    Estimation pédagogique, valable au flop et au turn. On ignore l'amélioration « pair » qui
    vient seulement d'une paire du board (elle profite aussi à l'adversaire)."""
    if len(board) not in (3, 4):
        return None
    before = EVALUATOR.get_rank_class(EVALUATOR.evaluate(board, hole))
    known = set(hole) | set(board)
    hole_ranks = {Card.get_rank_int(c) for c in hole}
    outs = 0
    for card in Deck.GetFullDeck():
        if card in known:
            continue
        after = EVALUATOR.get_rank_class(EVALUATOR.evaluate(list(board) + [card], hole))
        if after < before and not (before == 9 and after == 8
                                   and Card.get_rank_int(card) not in hole_ranks):
            outs += 1
    return outs


@dataclass
class Advice:
    action: str            # "fold" | "check" | "call" | "raise"
    amount: int
    equity: float
    pot_odds: float
    needed: float          # équité minimale conseillée pour suivre
    ev_call: float         # espérance (jetons) de suivre, approximation sur la main en cours
    outs: int
    hand: str
    reasons: list
    unseen: int = 47       # cartes encore inconnues (pour convertir les outs en %)

    def text(self):
        lines = [f"    Main : {self.hand}",
                 f"    Équité estimée : {self.equity:.0%} (contre des mains aléatoires)"]
        if self.pot_odds:
            lines.append(f"    Cotes du pot : {self.pot_odds:.0%} | EV de suivre : {self.ev_call:+.1f} jetons")
        if self.outs is not None:
            lines.append(f"    Outs approximatifs : {self.outs} "
                         f"(≈ {self.outs / self.unseen:.0%} d'améliorer au prochain tirage)")
        what = {"fold": "SE COUCHER", "check": "CHECK", "call": "SUIVRE",
                "raise": f"RELANCER à {self.amount}"}[self.action]
        lines.append(f"    >>> Conseil : {what}")
        lines += [f"        - {r}" for r in self.reasons]
        return "\n".join(lines)


class Advisor:
    """Conseille une décision à partir de l'équité Monte-Carlo et des cotes du pot."""

    def __init__(self, raise_threshold=0.62, call_margin=0.05, bet_fraction=0.7, iterations=1500):
        self.raise_threshold, self.call_margin = raise_threshold, call_margin
        self.bet_fraction, self.iterations = bet_fraction, iterations

    def advise(self, view):
        eq = estimate_equity(view.hole, view.board, view.opponents, self.iterations, view.rng)
        odds = view.pot_odds
        needed = odds + self.call_margin
        ev_call = eq * (view.pot + view.to_call) - view.to_call
        strong = eq >= self.raise_threshold - (0.08 if view.opponents == 1 else 0.0)
        reasons = []
        if view.board:
            hand = hand_name_fr(EVALUATOR.evaluate(view.board, view.hole)) if len(view.board) >= 3 else ""
        else:
            hand = describe_hole(view.hole)
        if view.opponents > 1:
            reasons.append(f"Table à {view.opponents + 1} joueurs : plus il y a d'adversaires, "
                           "plus il faut de force.")
        if strong and view.can_raise:
            action = "raise"
            amount = min(view.max_raise_to,
                         max(view.min_raise_to, view.to_call + int(view.pot * self.bet_fraction)))
            reasons.insert(0, "Votre équité est élevée : relancez pour faire payer les mains "
                              "inférieures et protéger votre main.")
        elif view.to_call == 0:
            action, amount = "check", 0
            reasons.insert(0, "Rien à payer : voyez la carte suivante gratuitement "
                              "(main trop faible pour miser pour la valeur).")
        elif eq >= needed:
            action, amount = "call", 0
            reasons.insert(0, f"Votre équité ({eq:.0%}) dépasse les cotes du pot ({odds:.0%}) : "
                              "suivre est rentable à long terme.")
        else:
            action, amount = "fold", 0
            reasons.insert(0, f"Il faut ≈ {needed:.0%} d'équité pour suivre (cotes du pot {odds:.0%} "
                              f"+ marge), vous n'avez que {eq:.0%} : se coucher.")
        if view.to_call and view.to_call >= view.stack:
            reasons.append("Suivre vous met à tapis : décision pour tout votre tapis.")
        if view.board and view.to_call:
            reasons.append("Un adversaire qui mise a souvent une main plus forte qu'une main "
                           "aléatoire : soyez plus prudent que le chiffre brut.")
        return Advice(action, amount, eq, odds, needed, ev_call, count_outs(view.hole, view.board),
                      hand, reasons, 52 - 2 - len(view.board))


class TrainingBot(HumanBot):
    """Humain assisté : conseils avant la décision (``always``) ou sur demande avec ``?`` (``ask``),
    puis retour sur chaque décision et bilan de session."""

    def __init__(self, name="Vous", input_fn=input, output_fn=print, advice="always", advisor=None):
        super().__init__(name, input_fn=self._ask_with_help, output_fn=output_fn)
        self.raw_input, self.mode = input_fn, advice
        self.advisor = advisor or Advisor()
        self.decisions = []
        self._advice = None
        self._shown = self._announced = False
        self.hand_index, self._last_hole = 0, None

    def _ask_with_help(self, prompt):
        if not self._announced:  # après le résumé de la situation, juste avant la saisie
            self._announced = True
            if self.mode == "always":
                self.say(self._advice.text())
                self._shown = True
            elif self.mode == "ask":
                self.say("    (tapez ? pour demander un conseil)")
        while True:
            line = self.raw_input(prompt)
            if self.mode != "off" and line.strip().lower() in ("?", "h", "conseil"):
                self.say(self._advice.text())
                self._shown = True
                continue
            return line

    def act(self, view):
        if view.hole != self._last_hole:
            self._last_hole, self.hand_index = view.hole, self.hand_index + 1
        self._advice, self._shown, self._announced = self.advisor.advise(view), False, False
        action, amount = super().act(view)
        self.say(self._debrief(view, action, self._advice))
        return action, amount

    def _debrief(self, view, action, adv):
        kind = "check" if action == CALL and not view.to_call else action
        agrees = kind == adv.action or (kind in ("call", "check") and adv.action in ("call", "check"))
        loss = 0.0
        if action == FOLD and adv.equity >= adv.needed:
            loss = max(adv.ev_call, 0.0)
        elif action == CALL and view.to_call and adv.equity < adv.pot_odds - 0.03:
            loss = max(-adv.ev_call, 0.0)
        self.decisions.append({"hand": self.hand_index, "street": view.street, "action": kind,
                               "advised": adv.action, "agrees": agrees, "loss": loss,
                               "hole": Card.ints_to_pretty_str(view.hole).strip(),
                               "equity": adv.equity})
        if loss > 0:
            tip = (f"se coucher abandonnait ≈ {loss:.1f} jetons d'espérance (équité {adv.equity:.0%} "
                   f"contre {adv.needed:.0%} nécessaires)" if action == FOLD else
                   f"suivre perd ≈ {loss:.1f} jetons en moyenne (équité {adv.equity:.0%} < cotes "
                   f"du pot {adv.pot_odds:.0%})")
            return f"    ✘ Écart : {tip}."
        if agrees:
            return "    ✔ Conforme au conseil."
        if kind == "raise":
            return "    ~ Relance plus agressive que le conseil : un bluff peut se défendre si l'adversaire peut se coucher."
        return f"    ~ Autre choix que le conseil ({adv.action}) : écart sans perte claire."

    def summary(self, big_blind=2):
        d = self.decisions
        if not d:
            return "Entraînement : aucune décision à analyser."
        ok = sum(x["agrees"] for x in d)
        loss = sum(x["loss"] for x in d)
        errors = sorted((x for x in d if x["loss"] > 0), key=lambda x: -x["loss"])
        lines = [f"=== Bilan d'entraînement ===",
                 f"Décisions : {len(d)} | conformes aux conseils : {ok / len(d):.0%} | "
                 f"erreurs claires : {len(errors)}",
                 f"Espérance perdue estimée : {loss:.1f} jetons ({loss / big_blind:.1f} bb)"]
        for x in errors[:3]:
            lines.append(f"  - Main {x['hand']} ({x['street']}) {x['hole']} : {x['action']} au lieu de "
                         f"{x['advised']} (≈ -{x['loss']:.1f}, équité {x['equity']:.0%})")
        return "\n".join(lines)


def run_training(opponents, hands=None, stack=100, big_blind=2, seed=None, advice="always",
                 input_fn=input, output_fn=print, name="Vous", history=None):
    """Entraînement : comme le mode humain, avec conseils et bilan final. Renvoie le TrainingBot."""
    bot = TrainingBot(name, input_fn=input_fn, output_fn=output_fn, advice=advice)
    run_human(opponents, hands, stack, big_blind, seed, input_fn, output_fn, name=name,
              history=history, human=bot)
    output_fn("\n" + bot.summary(big_blind))
    return bot


# --------------------------------------------------------------------------
# Historique et replay des mains
# --------------------------------------------------------------------------
DEFAULT_HISTORY = "mains.jsonl"


class HandHistory:
    """Ajoute chaque main jouée (une ligne JSON) à un fichier, pour pouvoir la rejouer."""

    def __init__(self, path=DEFAULT_HISTORY):
        self.path = path
        self._lock = threading.Lock()

    def append(self, record):
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_history(path):
    """Renvoie les mains du fichier (les lignes illisibles sont ignorées)."""
    hands = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "players" in rec and "events" in rec:
                hands.append(rec)
    return hands


def _cards(strs):
    return Card.ints_to_pretty_str([Card.new(c) for c in strs]).strip()


def hand_summary(rec):
    """Résumé d'une ligne : joueurs, board final, gagnants."""
    winners = [n for n, p in rec["profit"].items() if p > 0]
    pot = sum(e["amt"] for e in rec["events"] if e["e"] in ("blind", "call", "raise"))
    board = _cards(rec["board"]) if rec["board"] else "(pas de board)"
    return (f"{', '.join(p['name'] for p in rec['players'])} | board {board} | pot {pot} | "
            f"gagne : {', '.join(winners) or 'égalité'}")


def format_replay(rec, number=None):
    """Découpe une main en étapes lisibles (liste de textes), cartes de tous les joueurs révélées."""
    steps = []
    title = f"Main {number}" if number else "Main"
    if rec.get("label"):
        title += f" ({rec['label']})"
    steps.append(f"=== {title} | blinds {rec['bb'] // 2}/{rec['bb']} | bouton : {rec['button']} ===\n"
                 + "\n".join(f"  {p['name']:<10} tapis {p['stack']:>5}   {_cards(p['hole'])}"
                             for p in rec["players"]))
    pot = 0
    for e in rec["events"]:
        kind = e["e"]
        if kind in ("blind", "call", "raise"):
            pot += e["amt"]
        tag = " (tapis)" if e.get("allin") else ""
        if kind == "blind":
            steps.append(f"{e['p']} poste la {e['role']} ({e['amt']})")
        elif kind == "street":
            steps.append(f"--- {e['street'].capitalize()} : {_cards(e['board'])} | pot {pot}")
        elif kind == "fold":
            steps.append(f"{e['p']} se couche")
        elif kind == "check":
            steps.append(f"{e['p']} check")
        elif kind == "call":
            steps.append(f"{e['p']} suit {e['amt']}{tag} | pot {pot}")
        elif kind == "raise":
            steps.append(f"{e['p']} relance à {e['to']}{tag} | pot {pot}")
        elif kind == "show":
            steps.append(f"{e['p']} montre {_cards(e['cards'])} : {e['hand']}")
        elif kind == "win":
            steps.append(f"=> {e['p']} remporte {e['amt']}")
    steps.append("Résultat : " + ", ".join(f"{n} {p:+d}" for n, p in rec["profit"].items()))
    return steps


def replay_hand(rec, number=None, output_fn=print, input_fn=None, delay=0.0):
    """Affiche une main étape par étape. Avec ``input_fn`` (mode pas à pas) : Entrée = suite,
    ``q`` = arrêter. Renvoie False si l'utilisateur a quitté."""
    for step in format_replay(rec, number):
        output_fn(step)
        if input_fn:
            try:
                if input_fn("   [Entrée = suite, q = quitter] ").strip().lower() == "q":
                    return False
            except EOFError:
                return False
        elif delay:
            time.sleep(delay)
    return True


def run_replay(path, number=None, list_only=False, player=None, step=False, delay=0.0,
               output_fn=print, input_fn=input):
    """Mode replay : liste les mains enregistrées ou rejoue la main ``number`` (la dernière par défaut)."""
    try:
        hands = load_history(path)
    except OSError as err:
        output_fn(f"Impossible de lire l'historique {path} : {err.strerror or err}")
        return False
    if not hands:
        output_fn("Aucune main enregistrée.")
        return False
    if list_only:
        shown = 0
        for i, rec in enumerate(hands, 1):
            if player and player not in (p["name"] for p in rec["players"]):
                continue
            output_fn(f"{i:>5}. {hand_summary(rec)}")
            shown += 1
        if not shown:
            output_fn(f"Aucune main pour {player!r}.")
        return bool(shown)
    number = len(hands) if number is None else number
    if not 1 <= number <= len(hands):
        output_fn(f"Main {number} introuvable : l'historique en contient {len(hands)}.")
        return False
    replay_hand(hands[number - 1], number, output_fn, input_fn if step else None, delay)
    return True


# --------------------------------------------------------------------------
# Classement persistant (fichier JSON)
# --------------------------------------------------------------------------
DEFAULT_LEADERBOARD = "classement.json"
_EMPTY_STATS = {"cash_hands": 0, "cash_bb": 0.0, "tournaments": 0, "wins": 0, "place_pct": 0.0}


class Leaderboard:
    """Statistiques cumulées par pseudo, sauvegardées dans un fichier JSON.

    Chaque mise à jour relit le fichier puis le réécrit de façon atomique (fichier temporaire
    + ``os.replace``) : un plantage ne corrompt pas le classement et plusieurs serveurs
    peuvent partager le même fichier. Un fichier illisible est mis de côté en ``.bak``."""

    def __init__(self, path=DEFAULT_LEADERBOARD):
        self.path = path
        self._lock = threading.Lock()
        self.players = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f).get("players", {})
            if not isinstance(raw, dict):
                raise ValueError("format inattendu")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError):
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
            return {}
        players = {}
        for name, entry in raw.items():
            try:
                players[str(name)] = {k: type(v)(entry.get(k, v)) for k, v in _EMPTY_STATS.items()}
            except (AttributeError, TypeError, ValueError):
                continue  # entrée abîmée : ignorée
        return players

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "players": self.players}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _update(self, apply):
        with self._lock:
            self.players = self._load()
            apply()
            self._save()

    def _entry(self, name):
        return self.players.setdefault(name, dict(_EMPTY_STATS))

    def record_cash(self, results):
        """``results`` : {pseudo: gain de la main en grosses blinds}. Une main par pseudo."""
        def apply():
            for name, bb in results.items():
                e = self._entry(name)
                e["cash_hands"] += 1
                e["cash_bb"] += bb
        self._update(apply)

    def record_tournament(self, places):
        """``places`` : {pseudo: (place, nombre de joueurs)}, place 1 = vainqueur."""
        def apply():
            for name, (place, size) in places.items():
                e = self._entry(name)
                e["tournaments"] += 1
                e["wins"] += place == 1
                e["place_pct"] += (size - place) / (size - 1) if size > 1 else 1.0
        self._update(apply)

    def format(self, top=10, min_hands=20):
        with self._lock:
            self.players = self._load()
            players = dict(self.players)
        cash = [(n, e) for n, e in players.items() if e["cash_hands"] >= min_hands]
        cash.sort(key=lambda ne: -ne[1]["cash_bb"] / ne[1]["cash_hands"])
        tours = [(n, e) for n, e in players.items() if e["tournaments"]]
        tours.sort(key=lambda ne: (-ne[1]["place_pct"] / ne[1]["tournaments"], -ne[1]["wins"]))
        lines = [f"== Classement cash (au moins {min_hands} mains) =="]
        if cash:
            lines.append(f"{'#':>2} {'Joueur':<15}{'Mains':>7}{'Gain (bb)':>11}{'bb/100':>9}")
            for i, (n, e) in enumerate(cash[:top], 1):
                lines.append(f"{i:>2} {n:<15}{e['cash_hands']:>7}{e['cash_bb']:>11.1f}"
                             f"{e['cash_bb'] / e['cash_hands'] * 100:>9.1f}")
        else:
            lines.append("(aucun joueur n'a encore assez de mains)")
        lines.append("== Classement tournois (score = 100 % pour un vainqueur, 0 % pour le dernier) ==")
        if tours:
            lines.append(f"{'#':>2} {'Joueur':<15}{'Tournois':>9}{'Victoires':>10}{'Score moy.':>11}")
            for i, (n, e) in enumerate(tours[:top], 1):
                lines.append(f"{i:>2} {n:<15}{e['tournaments']:>9}{e['wins']:>10}"
                             f"{e['place_pct'] / e['tournaments']:>11.0%}")
        else:
            lines.append("(aucun tournoi joué)")
        return "\n".join(lines)


def run_human(opponents, hands=None, stack=100, big_blind=2, seed=None,
              input_fn=input, output_fn=print, name="Vous", leaderboard=None, history=None, human=None):
    """Partie interactive : l'humain (siège 0) contre des bots, tapis remis à ``stack`` à chaque main.

    S'arrête après ``hands`` mains (illimité si None) ou quand le joueur tape ``q``.
    Renvoie le gain net de l'humain en jetons et le nombre de mains terminées.
    """
    rng = random.Random(seed)
    if seed is not None:
        random.seed(seed)
    human = human or HumanBot(name, input_fn=input_fn, output_fn=output_fn)
    bots = [human] + list(opponents)
    net, played = 0, 0
    while hands is None or played < hands:
        output_fn(f"\n=== Main {played + 1} (blinds {big_blind // 2}/{big_blind}) ===")
        hand = Hand(bots, button=played % len(bots), stack=stack, big_blind=big_blind,
                    rng=rng, log=output_fn, show_hole=False, history=history, label="humain")
        try:
            profit = hand.play()
        except QuitGame:
            break
        net += profit[0]
        played += 1
        if leaderboard:
            leaderboard.record_cash({name: profit[0] / big_blind})
        output_fn(f"--- Résultat : {profit[0]:+d} | total : {net:+d} jetons "
                  f"({net / big_blind:+.1f} bb) sur {played} main(s)")
    output_fn(f"\nFin de la partie : {net:+d} jetons ({net / big_blind:+.1f} bb) sur {played} main(s).")
    return net, played


def run_tournament(bots, starting_stack=1000, big_blind=20, level_hands=10,
                   blind_growth=1.5, max_hands=1000, seed=None, verbose=False,
                   log=None, show_hole=True, stop_when_out=None, should_stop=None, history=None,
                   label="tournoi"):
    """Joue un tournoi jusqu'à ce qu'il reste un joueur.

    Les blinds augmentent de ``blind_growth`` toutes les ``level_hands`` mains.
    Renvoie la liste des noms classés du vainqueur au premier éliminé.
    ``stop_when_out`` : un bot dont l'élimination arrête le tournoi (le reste est classé
    selon les tapis). ``should_stop()`` est testé avant chaque main. ``log`` reçoit le texte affiché, ``show_hole=False`` cache les cartes.
    """
    rng = random.Random(seed)
    if seed is not None:
        random.seed(seed)
    stacks = {i: starting_stack for i in range(len(bots))}
    out = []  # éliminés, du premier au dernier
    button, bb, hand_no = 0, big_blind, 0
    log = log or (print if verbose else None)
    say = log or (lambda *_: None)
    while len(stacks) > 1 and hand_no < max_hands and not (should_stop and should_stop()):
        alive = list(stacks)
        if hand_no and hand_no % level_hands == 0:
            bb = max(bb + 2, int(bb * blind_growth)) // 2 * 2
        say(f"\n=== Main {hand_no + 1} (blinds {bb // 2}/{bb}) ===")
        if stop_when_out is not None:
            say("Tapis : " + ", ".join(f"{bots[i].name} {stacks[i]}" for i in alive))
        button %= len(alive)
        hand = Hand([bots[i] for i in alive], button=button,
                    stack=[stacks[i] for i in alive], big_blind=bb, rng=rng, log=log, show_hole=show_hole,
                    history=history, label=label)
        profit = hand.play()
        busted = []
        for k, i in enumerate(alive):
            stacks[i] += profit[k]
            if stacks[i] <= 0:
                busted.append(i)
        # Plusieurs éliminations sur la même main : le plus petit tapis de départ sort en premier.
        for i in sorted(busted, key=lambda j: hand.start[alive.index(j)]):
            del stacks[i]
            out.append(i)
            say(f"  *** {bots[i].name} est éliminé ({len(stacks) + 1}e)")
        if any(bots[i] is stop_when_out for i in busted):
            break
        button = (button + 1) % max(len(stacks), 1)
        hand_no += 1
    # Si max_hands atteint : classement par tapis restant.
    out += sorted(stacks, key=lambda j: stacks[j])
    return [bots[i].name for i in reversed(out)]


def run_human_tournament(opponents, stack=1000, big_blind=20, level_hands=10, seed=None,
                         input_fn=input, output_fn=print, name="Vous", leaderboard=None,
                         history=None):
    """Tournoi interactif : l'humain affronte ``opponents`` jusqu'à son élimination ou sa victoire.

    Renvoie la place finale de l'humain (1 = vainqueur) ou None s'il abandonne (``q``).
    """
    human = HumanBot(name, input_fn=input_fn, output_fn=output_fn)
    bots = [human] + list(opponents)
    try:
        ranking = run_tournament(bots, stack, big_blind, level_hands, seed=seed, log=output_fn,
                                 show_hole=False, stop_when_out=human, history=history,
                                 label="humain-tournoi")
    except QuitGame:
        output_fn("\nVous avez abandonné le tournoi.")
        return None
    place = ranking.index(human.name) + 1
    if leaderboard:
        leaderboard.record_tournament({name: (place, len(bots))})
    if place == 1:
        output_fn("\n*** Bravo, vous remportez le tournoi ! ***")
    else:
        output_fn(f"\nVous êtes éliminé : {place}e sur {len(bots)}. Meneur au moment de votre élimination : {ranking[0]}.")
    return place


def run_tournaments(bots, count=1, **kwargs):
    """Répète des tournois ; renvoie victoires et place moyenne par bot."""
    seed = kwargs.pop("seed", None)
    wins, places = defaultdict(int), defaultdict(int)
    for t in range(count):
        ranking = run_tournament(bots, seed=None if seed is None else seed + t, **kwargs)
        wins[ranking[0]] += 1
        for place, name in enumerate(ranking, 1):
            places[name] += place
    return {b.name: {"wins": wins[b.name], "avg_place": places[b.name] / count} for b in bots}


def print_tournament_report(results, count):
    print(f"\nRésultats sur {count} tournoi(s)")
    print(f"{'Bot':<10}{'Victoires':>11}{'Place moy.':>12}")
    for name, r in sorted(results.items(), key=lambda kv: kv[1]["avg_place"]):
        print(f"{name:<10}{r['wins']:>11}{r['avg_place']:>12.2f}")


def print_report(results, hands):
    print(f"\nRésultats sur {hands} mains")
    print(f"{'Bot':<10}{'Gain (jetons)':>15}{'Gain (bb)':>12}{'bb/100':>10}{'Mains gagnées':>16}")
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["net"]):
        print(f"{name:<10}{r['net']:>15.0f}{r['bb']:>12.1f}{r['bb_per_100']:>10.1f}{r['win_rate']:>15.1%}")


# --------------------------------------------------------------------------
# Multijoueur en réseau (TCP, jetons fictifs, protocole JSON ligne par ligne)
# --------------------------------------------------------------------------
DEFAULT_PORT = 5555
MAX_SPECTATORS = 20
MAX_CHAT = 200          # longueur maximale d'un message de chat
CHAT_INTERVAL = 0.5     # secondes minimum entre deux messages d'un même joueur
MAX_LINE = 4096  # taille maximale d'un message reçu d'un client


class Conn:
    """Connexion JSON ligne par ligne avec tampon (supporte les timeouts sans corruption)."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b""
        self._send_lock = threading.Lock()  # le moteur et un thread d'accueil peuvent écrire

    def send(self, **msg):
        data = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
        with self._send_lock:
            self.sock.settimeout(10)  # un client qui ne lit plus ne doit pas bloquer la partie
            self.sock.sendall(data)

    def recv(self, timeout=None):
        """Renvoie le prochain message (dict). Lève TimeoutError ou ConnectionError."""
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_LINE:
                raise ConnectionError("message trop long")
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("connexion fermée")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            msg = json.loads(line)
        except ValueError:
            raise ConnectionError("message invalide")
        if not isinstance(msg, dict):
            raise ConnectionError("message invalide")
        return msg

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # réveille un thread bloqué en lecture
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class RemoteBot(HumanBot):
    """Joueur distant : mêmes décisions que HumanBot, mais saisies/affichages passent par le réseau.

    Un thread lit en continu la connexion : les messages de chat sont relayés tout de suite
    (``on_chat(bot, texte)``), les réponses aux décisions vont dans une file."""

    def __init__(self, name, conn, timeout=120, on_chat=None):
        super().__init__(name, input_fn=self._ask, output_fn=self.tell)
        self.conn, self.timeout, self.gone = conn, timeout, False
        self.on_chat, self.last_chat = on_chat, 0.0
        self.replies, self.closed = queue.Queue(), False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = self.conn.recv(None)
                if "chat" in msg:
                    if self.on_chat and not self.gone:
                        self.on_chat(self, msg["chat"])
                elif "r" in msg:
                    self.replies.put(msg["r"] if isinstance(msg["r"], str) else "")
        except (OSError, ConnectionError):
            pass
        finally:
            self.closed = True
            self.replies.put(None)

    def tell(self, text):
        if self.gone:
            return
        try:
            self.conn.send(t="msg", text=str(text))
        except OSError:
            self.disconnect()

    def _ask(self, prompt):
        while True:  # on jette les réponses périmées (arrivées après un timeout)
            try:
                self.replies.get_nowait()
            except queue.Empty:
                break
        if self.closed:
            raise ConnectionError("connexion fermée")
        self.conn.send(t="ask", text=prompt)
        try:
            reply = self.replies.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError
        if reply is None:
            raise ConnectionError("connexion fermée")
        return reply

    def disconnect(self):
        self.gone = True
        self.conn.close()

    def act(self, view):
        if self.gone:
            return FOLD, 0
        try:
            return super().act(view)
        except TimeoutError:
            self.tell(f"Temps écoulé ({self.timeout}s) : vous êtes couché / check.")
            return FOLD, 0
        except (QuitGame, OSError):  # QuitGame : le client a tapé q ; OSError : coupure
            self.disconnect()
            return FOLD, 0


def _unique_name(name, taken):
    name = "".join(ch for ch in str(name) if ch.isprintable()).strip()[:15] or "Joueur"
    base, k = name, 2
    while name in taken:
        name, k = f"{base}{k}", k + 1
    return name


def _handshake(sock, timeout=10):
    """Lit le message d'accueil d'un client. Renvoie (conn, hello) ou (None, None) si invalide."""
    conn = Conn(sock)
    try:
        return conn, conn.recv(timeout)
    except (OSError, ConnectionError):
        conn.close()
        return None, None


def run_server(host="127.0.0.1", port=DEFAULT_PORT, players=2, opponents=(), hands=None,
               tournament=False, stack=None, big_blind=None, level_hands=10, seed=None,
               timeout=120, on_listen=None, output_fn=print, leaderboard=None, history=None):
    """Héberge une partie : attend ``players`` joueurs humains, les fait jouer entre eux
    (et contre ``opponents``) puis renvoie les résultats (gains cash ou classement tournoi).

    Des spectateurs (``run_client(..., spectate=True)``) peuvent se connecter avant ou
    pendant la partie : ils reçoivent tout ce qui est public (jamais les cartes cachées)."""
    stack = stack or (1000 if tournament else 100)
    big_blind = big_blind or (20 if tournament else 2)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    remotes, spectators, taken = [], [], {b.name for b in opponents}
    lock = threading.Lock()

    def broadcast(text):
        output_fn(text)
        for r in remotes:
            r.tell(text)
        with lock:
            watchers = list(spectators)
        for w in watchers:
            try:
                w.send(t="msg", text=str(text))
            except OSError:
                drop_spectator(w)

    def on_chat(bot, text):
        text = "".join(ch for ch in str(text) if ch.isprintable()).strip()[:MAX_CHAT]
        if not text:
            return
        now = time.monotonic()
        if now - bot.last_chat < CHAT_INTERVAL:
            bot.tell("(chat) trop rapide, patientez un instant.")
            return
        bot.last_chat = now
        broadcast(f"[chat] {bot.name}: {text}")

    def drop_spectator(conn):
        with lock:
            if conn in spectators:
                spectators.remove(conn)
        conn.close()

    def add_spectator(conn, hello, addr, late):
        with lock:
            name = _unique_name(hello.get("name", ""), taken)
            taken.add(name)
            full = len(spectators) >= MAX_SPECTATORS
        try:
            if full:
                conn.send(t="msg", text="Trop de spectateurs sur ce serveur.")
                conn.close()
                return
            conn.send(t="msg", text=f"Vous regardez la partie en tant que {name}."
                      + (" Elle est déjà en cours." if late else " Elle va commencer."))
        except OSError:
            conn.close()
            return
        with lock:
            spectators.append(conn)
        broadcast(f"[{name} regarde la partie]")

    def late_client(sock, addr):
        conn, hello = _handshake(sock)
        if conn is None:
            return
        if hello.get("role") == "spectator":
            add_spectator(conn, hello, addr, late=True)
        else:
            try:
                conn.send(t="msg", text="La partie est déjà commencée : vous pouvez la suivre "
                                        "avec --spectate.")
            except OSError:
                pass
            conn.close()

    def accept_late():
        while True:
            try:
                sock, addr = server.accept()
            except OSError:  # socket fermée en fin de partie
                return
            threading.Thread(target=late_client, args=(sock, addr), daemon=True).start()

    try:
        server.bind((host, port))
        server.listen(players + MAX_SPECTATORS)
        output_fn(f"Serveur en écoute sur {host}:{server.getsockname()[1]} "
                  f"({players} joueur(s) attendu(s))")
        if on_listen:
            on_listen(server.getsockname()[1])
        while len(remotes) < players:
            sock, addr = server.accept()
            conn, hello = _handshake(sock)
            if conn is None:
                continue
            if hello.get("role") == "spectator":
                add_spectator(conn, hello, addr, late=False)
                continue
            name = _unique_name(hello.get("name", ""), taken)
            taken.add(name)
            bot = RemoteBot(name, conn, timeout, on_chat)
            remotes.append(bot)
            output_fn(f"{name} connecté depuis {addr[0]} ({len(remotes)}/{players})")
            bot.tell(f"Bienvenue {name} ! En attente des autres joueurs ({len(remotes)}/{players})... "
                     "Tapez /message pour discuter avec la table.")
            for other in remotes[:-1]:
                other.tell(f"{name} a rejoint la table ({len(remotes)}/{players}).")
    except BaseException:
        server.close()
        raise
    threading.Thread(target=accept_late, daemon=True).start()

    everyone = remotes + list(opponents)
    broadcast("La partie commence : " + ", ".join(b.name for b in everyone))
    rng = random.Random(seed)
    if seed is not None:
        random.seed(seed)
    try:
        if tournament:
            ranking = run_tournament(everyone, stack, big_blind, level_hands, seed=seed, log=broadcast,
                                     show_hole=False, history=history, label="réseau-tournoi",
                                     should_stop=lambda: all(r.gone for r in remotes))
            broadcast("\nClassement final : " + " > ".join(f"{i}. {n}" for i, n in enumerate(ranking, 1)))
            if leaderboard:
                leaderboard.record_tournament(
                    {r.name: (ranking.index(r.name) + 1, len(ranking)) for r in remotes})
                broadcast("\n" + leaderboard.format())
            return ranking
        totals, played = defaultdict(int), 0
        while hands is None or played < hands:
            active = [b for b in everyone if not getattr(b, "gone", False)]
            if len(active) < 2 or all(r.gone for r in remotes):
                break
            broadcast(f"\n=== Main {played + 1} (blinds {big_blind // 2}/{big_blind}) ===")
            hand = Hand(active, button=played % len(active), stack=stack, big_blind=big_blind,
                        rng=rng, log=broadcast, show_hole=False, history=history,
                        label="réseau")
            profits = hand.play()
            for b, p in zip(active, profits):
                totals[b.name] += p
            if leaderboard:
                leaderboard.record_cash({b.name: p / big_blind for b, p in zip(active, profits)
                                         if isinstance(b, RemoteBot)})
            played += 1
            broadcast("--- Totaux : " + ", ".join(f"{b.name} {totals[b.name]:+d}" for b in everyone))
        broadcast(f"\nFin de la partie après {played} main(s).")
        if leaderboard:
            broadcast("\n" + leaderboard.format())
        return dict(totals)
    finally:
        server.close()
        for r in remotes:
            if not r.gone:
                r.tell("Le serveur ferme la partie. Merci d'avoir joué !")
                r.disconnect()
        with lock:
            watchers, spectators[:] = list(spectators), []
        for w in watchers:
            try:
                w.send(t="msg", text="Le serveur ferme la partie. Merci d'avoir regardé !")
            except OSError:
                pass
            w.close()


def run_client(host, port, name="Joueur", input_fn=input, output_fn=print, spectate=False):
    """Se connecte à un serveur et relaie affichages / saisies jusqu'à la fin de la partie.

    Les messages du serveur sont affichés en continu (chat compris). Une ligne commençant par
    ``/`` est envoyée au chat à tout moment ; toute autre ligne répond à la décision en attente.
    En mode ``spectate`` on regarde la partie sans jouer."""
    conn = Conn(socket.create_connection((host, port), timeout=15))
    conn.send(name=name, role="spectator" if spectate else "player")
    done, lock, state = threading.Event(), threading.Lock(), {"pending": False}

    def network():
        try:
            while True:
                msg = conn.recv(None)
                if msg.get("t") == "msg":
                    output_fn(msg.get("text", ""))
                elif msg.get("t") == "ask" and not spectate:
                    with lock:
                        state["pending"] = True
                    output_fn(msg.get("text", ""))
        except (ConnectionError, OSError):
            output_fn("Connexion terminée.")
        finally:
            done.set()

    def keyboard():
        while not done.is_set():
            try:
                line = input_fn("").strip()
            except (EOFError, KeyboardInterrupt):  # entrée fermée : on quitte proprement
                with lock:
                    leaving, state["pending"] = state["pending"], False
                if leaving:
                    try:
                        conn.send(r="q")
                    except OSError:
                        pass
                return
            try:
                if line.startswith("/"):
                    if line[1:].strip():
                        conn.send(chat=line[1:].strip())
                    continue
                with lock:
                    answering = state["pending"] and bool(line)
                    if answering:
                        state["pending"] = False
                if answering:
                    conn.send(r=line)
                elif line:
                    output_fn("(ce n'est pas votre tour — tapez /message pour discuter)")
                if line == "q" and answering:
                    return
            except OSError:
                return

    threading.Thread(target=network, daemon=True).start()
    if not spectate:
        threading.Thread(target=keyboard, daemon=True).start()
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Simulation de Texas Hold'em No-Limit entre bots")
    p.add_argument("--bots", default=None,
                   help=f"stratégies séparées par des virgules ({', '.join(STRATEGIES)}) ; "
                        "défaut : tight,equity,loose,station (aucun en mode server)")
    p.add_argument("--hands", type=int, default=1000)
    p.add_argument("--stack", type=int, default=100, help="tapis initial à chaque main")
    p.add_argument("--big-blind", type=int, default=2)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--mode", choices=("cash", "tournament", "human", "server", "client", "leaderboard", "replay", "training"), default="cash",
                   help="cash : bots seuls, tapis remis à zéro à chaque main ; tournament : élimination ; "
                        "human : vous jouez contre les bots ; server / client : partie en réseau")
    p.add_argument("--host", default=None,
                   help="client : adresse du serveur (défaut 127.0.0.1) ; server : adresse d'écoute "
                        "(défaut 127.0.0.1, utilisez 0.0.0.0 pour le réseau local)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--name", default=None,
                   help="votre pseudo (client : 'Joueur' par défaut ; human : 'Vous', clé du classement)")
    p.add_argument("--leaderboard", default=DEFAULT_LEADERBOARD, metavar="FICHIER",
                   help=f"fichier du classement persistant (défaut {DEFAULT_LEADERBOARD}) ; "
                        "utilisé par les modes server et human, et affiché par --mode leaderboard")
    p.add_argument("--no-leaderboard", action="store_true", help="ne pas enregistrer les résultats")
    p.add_argument("--min-hands", type=int, default=20,
                   help="mains minimum pour figurer au classement cash affiché")
    p.add_argument("--history", default=None, metavar="FICHIER",
                   help="enregistre chaque main jouée dans ce fichier (cash, tournament, human, server) ; "
                        f"avec --mode replay : fichier à relire (défaut {DEFAULT_HISTORY})")
    p.add_argument("--hand", type=int, default=None, help="mode replay : numéro de la main (défaut : la dernière)")
    p.add_argument("--list", action="store_true", help="mode replay : liste les mains enregistrées")
    p.add_argument("--step", action="store_true", help="mode replay : Entrée pour passer à l'étape suivante")
    p.add_argument("--delay", type=float, default=0.0, help="mode replay : pause (s) entre deux étapes")
    p.add_argument("--player", default=None, help="mode replay --list : n'afficher que les mains de ce joueur")
    p.add_argument("--advice", choices=ADVICE_MODES, default="always",
                   help="mode training : always = conseil avant chaque décision, ask = sur demande (?), "
                        "off = seulement le retour après coup")
    p.add_argument("--spectate", action="store_true",
                   help="mode client : regarder la partie sans jouer (avant ou pendant)")
    p.add_argument("--players", type=int, default=2, help="joueurs humains attendus (mode server)")
    p.add_argument("--timeout", type=int, default=120, help="secondes par décision (mode server)")
    p.add_argument("--tournaments", type=int, default=1, help="nombre de tournois (mode tournament)")
    p.add_argument("--level-hands", type=int, default=10, help="mains par niveau de blinds (tournoi)")
    p.add_argument("--tournament", action="store_true",
                   help="avec --mode human ou server : jouer un tournoi (élimination, blinds croissantes)")
    p.add_argument("--verbose", action="store_true", help="affiche chaque main")
    args = p.parse_args(argv)

    if args.mode == "replay":
        ok = run_replay(args.history or DEFAULT_HISTORY, args.hand, args.list, args.player,
                        args.step, args.delay)
        raise SystemExit(0 if ok else 1)
    history = HandHistory(args.history) if args.history else None
    board = None if args.no_leaderboard else Leaderboard(args.leaderboard)
    if args.mode == "leaderboard":
        print(Leaderboard(args.leaderboard).format(min_hands=args.min_hands))
        return
    hands_given = "--hands" in (argv if argv is not None else sys.argv)
    if args.mode == "client":
        run_client(args.host or "127.0.0.1", args.port, args.name or "Joueur", spectate=args.spectate)
        return
    raw = args.bots if args.bots is not None else ("" if args.mode == "server" else "tight,equity,loose,station")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if args.mode == "server":
        if not 2 <= args.players + len(names) <= 9:
            p.error("il faut entre 2 et 9 joueurs au total (humains + bots)")
        run_server(args.host or "127.0.0.1", args.port, args.players, make_bots(names),
                   args.hands if hands_given else None, args.tournament,
                   args.stack if args.stack != 100 else None,
                   args.big_blind if args.big_blind != 2 else None,
                   args.level_hands, args.seed, args.timeout, leaderboard=board, history=history)
        return
    human = args.mode in ("human", "training")
    if not (1 if human else 2) <= len(names) <= (8 if human else 9):
        p.error(f"il faut entre {1 if human else 2} et {8 if human else 9} bots")
    bots = make_bots(names)
    if args.mode == "training":
        run_training(bots, args.hands if hands_given else None, args.stack, args.big_blind, args.seed,
                     args.advice, name=args.name or "Vous", history=history)
        return
    if args.mode == "human" and args.tournament:
        run_human_tournament(bots, args.stack if args.stack != 100 else 1000,
                             args.big_blind if args.big_blind != 2 else 20,
                             args.level_hands, args.seed, name=args.name or "Vous", leaderboard=board, history=history)
        return
    if args.mode == "human":
        run_human(bots, args.hands if hands_given else None,
                  args.stack, args.big_blind, args.seed, name=args.name or "Vous", leaderboard=board,
                  history=history)
        return
    if args.mode == "tournament":
        stack = args.stack if args.stack != 100 else 1000
        bb = args.big_blind if args.big_blind != 2 else 20
        res = run_tournaments(bots, args.tournaments, starting_stack=stack, big_blind=bb,
                              level_hands=args.level_hands, seed=args.seed, verbose=args.verbose,
                              history=history)
        print_tournament_report(res, args.tournaments)
        return
    results = run_session(bots, args.hands, args.stack, args.big_blind, args.seed, args.verbose,
                          history)
    print_report(results, args.hands)


if __name__ == "__main__":
    main()
