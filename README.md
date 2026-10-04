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

## Mode tournoi
    python SimuPokerBot.py --mode tournament --tournaments 20 --seed 1
    python SimuPokerBot.py --mode tournament --bots tight,maniac,station --verbose

Tapis initial 1000 et blinds 10/20 par défaut (`--stack`, `--big-blind`). Les blinds augmentent de 50 % toutes les `--level-hands` mains (10 par défaut). Les joueurs à 0 jeton sont éliminés ; le rapport donne les victoires et la place moyenne de chaque bot sur `--tournaments` tournois.

## Mode humain
    python SimuPokerBot.py --mode human --bots tight,loose,maniac
    python SimuPokerBot.py --mode human --bots equity --hands 20 --stack 200

Vous êtes le siège 0 contre les bots listés dans `--bots` (1 à 8). Le tapis est remis à `--stack` à chaque main ; la partie continue jusqu'à `--hands` mains ou jusqu'à ce que vous tapiez `q`. Vos cartes et l'état du pot s'affichent à chaque décision ; les cartes des adversaires ne sont révélées qu'au showdown.

Commandes : `f` se coucher · `c` suivre / check · `r [montant]` relancer *à* ce montant total sur le tour (minimum si omis) · `a` tapis · `q` quitter.
