# SimPokerBot
Simulateur hors ligne de Texas Hold'em No-Limit où s'affrontent des bots. Il ne se connecte à aucun site de poker : c'est un outil d'étude de stratégies.

## Installation
    pip install -r requirements.txt

## Utilisation
    python SimuPokerBot.py --hands 2000 --seed 1
    python SimuPokerBot.py --bots tight,maniac,station,random --hands 500
    python SimuPokerBot.py --bots tight,maniac --hands 3 --verbose   # détail de chaque main

Options : `--bots`, `--hands`, `--stack` (tapis à chaque main), `--big-blind`, `--seed`, `--verbose`.

## Fonctionnalités
- Mains complètes : blinds, préflop/flop/turn/river, relances (max 4 par tour), tapis et side pots.
- Équité Monte-Carlo (`estimate_equity`) contre des mains aléatoires.
- Stratégies : `tight`, `equity`, `loose` (basées sur l'équité et les cotes du pot), `maniac`, `station`, `random`.
- Rapport final : gain en jetons et en grosses blinds, bb/100, mains gagnées.

## Ajouter un bot
Créer une sous-classe de `Bot` avec `act(view)` renvoyant `("fold"|"call"|"raise", montant_total)`, puis l'ajouter à `STRATEGIES`.

## Tests
    pytest
