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

### Tournoi en mode humain
    python SimuPokerBot.py --mode human --tournament --bots tight,loose,maniac --seed 1

Même format que le mode tournoi (1000 jetons, blinds 10/20 croissantes toutes les `--level-hands` mains), mais les tapis restent d'une main à l'autre : la ligne « Tapis » rappelle les stacks avant chaque main. Le tournoi s'arrête quand vous êtes éliminé (votre place finale s'affiche), quand vous gagnez, ou quand vous tapez `q`.

## Mode multijoueur en réseau
Jeu à plusieurs humains (jetons fictifs) via TCP, un joueur héberge, les autres se connectent.

    # Hôte (attend 3 joueurs, avec 2 bots en complément ; tournoi)
    python SimuPokerBot.py --mode server --host 0.0.0.0 --port 5555 --players 3 --bots tight,maniac --tournament

    # Chaque joueur (sur sa machine)
    python SimuPokerBot.py --mode client --host <ip_de_l_hote> --port 5555 --name Alice

- Sans `--tournament` : mode cash (tapis remis à `--stack` à chaque main, totaux affichés), jusqu'à `--hands` mains ou jusqu'à ce qu'il ne reste plus assez de joueurs.
- Avec `--tournament` : élimination, blinds croissantes ; le classement final est diffusé à tous (les éliminés continuent à suivre la partie).
- Les clients voient leurs propres cartes ; celles des autres ne sont révélées qu'au showdown. Mêmes commandes qu'en mode humain (`f`, `c`, `r [montant]`, `a`, `q`).
- `--timeout` (120 s par défaut) : sans réponse, le joueur se couche / check. Un joueur déconnecté se couche automatiquement.
- Total humains + bots : 2 à 9. `--host` vaut `127.0.0.1` par défaut (local uniquement) ; `0.0.0.0` ouvre la partie au réseau local.
- Sécurité : le protocole est en clair et sans authentification (pseudo libre) — à réserver à un réseau de confiance, ne pas exposer sur Internet.

### Spectateurs
    python SimuPokerBot.py --mode client --host <ip_de_l_hote> --port 5555 --name Public --spectate

Un spectateur peut se connecter avant ou pendant la partie (20 au maximum). Il reçoit tout ce qui est public : actions, board, pots, résultats, totaux ou classement. Les cartes cachées (y compris celles des bots) ne sont montrées qu'au showdown, comme pour les joueurs. Les joueurs sont prévenus quand quelqu'un rejoint comme spectateur. Un spectateur qui se déconnecte ou ne lit plus ses messages est retiré sans ralentir la partie. Une fois la partie commencée, les nouveaux joueurs sont refusés (ils peuvent rejoindre en spectateurs).

### Chat
Les joueurs (et eux seuls) peuvent discuter à tout moment, même hors de leur tour : une ligne qui commence par `/` est envoyée au chat, par exemple `/bien joué !`. Toute autre ligne répond à la décision en cours. Le chat est diffusé à tous les joueurs, aux spectateurs et dans la console du serveur sous la forme `[chat] Alice: bien joué !`. Les messages sont limités à 200 caractères et à un message toutes les 0,5 s par joueur ; les caractères de contrôle sont supprimés. Les spectateurs lisent le chat mais ne peuvent pas écrire.

## Classement persistant
Les résultats des humains sont cumulés dans un fichier JSON (`classement.json` par défaut, option `--leaderboard FICHIER`, désactivable avec `--no-leaderboard`). Les bots n'y figurent pas.

- Enregistrés automatiquement : le mode `server` (cash : une entrée par main ; tournoi : place finale) et les modes `human` / `human --tournament` (sous le pseudo donné par `--name`, « Vous » par défaut). Un tournoi abandonné avec `q` n'est pas compté.
- Affichage : `python SimuPokerBot.py --mode leaderboard [--min-hands 20]`, et automatiquement à la fin d'une partie en réseau (diffusé aux joueurs et spectateurs).
- Cash : classé par gain moyen en grosses blinds pour 100 mains (bb/100), pour les joueurs ayant au moins `--min-hands` mains. Tournois : classé par score moyen (100 % pour un vainqueur, 0 % pour le dernier, quelle que soit la taille de la table), puis par victoires.
- Le fichier est réécrit de façon atomique à chaque mise à jour et relu avant chaque écriture (plusieurs serveurs peuvent partager le même fichier). S'il est illisible, il est conservé en `.bak` et un nouveau classement démarre.
- Attention : le pseudo n'est pas authentifié, n'importe qui peut jouer sous le nom d'un autre joueur.

## Replay des mains
Enregistrer les mains jouées, puis les rejouer avec **toutes** les cartes visibles :

    python SimuPokerBot.py --bots tight,maniac,station --hands 200 --history mains.jsonl
    python SimuPokerBot.py --mode replay --history mains.jsonl --list
    python SimuPokerBot.py --mode replay --history mains.jsonl --hand 17 --step

- `--history FICHIER` enregistre chaque main (une ligne JSON par main, en ajout) dans les modes `cash`, `tournament`, `human` et `server`. Rien n'est enregistré sans cette option. Le fichier contient les cartes de tous les joueurs : à garder en local.
- `--mode replay` relit le fichier (`mains.jsonl` par défaut) : `--list` liste les mains numérotées (joueurs, board, pot, gagnants ; `--player NOM` filtre), `--hand N` rejoue la main N (la dernière par défaut), `--step` avance à chaque Entrée (`q` pour arrêter), `--delay 1.5` marque une pause entre les étapes.
- Le replay montre pour chaque action le pot courant, les tapis, les cartes montrées au showdown et le résultat net de chacun.
- Les lignes illisibles du fichier sont ignorées ; le code de sortie est 1 si le replay est impossible (fichier absent, main introuvable).

## Mode entraînement (conseils)
    python SimuPokerBot.py --mode training --bots tight,loose
    python SimuPokerBot.py --mode training --bots equity --advice ask --hands 30 --history mains.jsonl

Comme le mode humain (mêmes commandes), mais avec un coach :
- `--advice always` (défaut) : avant chaque décision, votre main, l'équité estimée (Monte-Carlo contre des mains aléatoires), les cotes du pot, l'espérance de gain d'un call, les outs approximatifs au flop/turn, puis le conseil (se coucher / check / suivre / relancer à X) avec ses raisons.
- `--advice ask` : le conseil n'apparaît que si vous tapez `?` ; `--advice off` : aucun conseil, uniquement le retour après coup.
- Après chaque décision : ✔ conforme au conseil, ~ autre choix sans perte claire, ou ✘ erreur claire (ex. se coucher alors que l'équité dépasse nettement les cotes, ou suivre avec une équité inférieure aux cotes) avec l'espérance perdue estimée.
- À la fin (`q` ou `--hands N`) : bilan avec le % de décisions conformes, le nombre d'erreurs claires, l'espérance perdue en jetons/bb et vos 3 pires erreurs (main, tour, cartes).

Limites : l'équité est calculée contre des mains aléatoires et l'EV ne regarde que la main en cours (pas de cotes implicites, de bluff ni de lecture de l'adversaire). Le conseil est un repère pédagogique, pas un solveur. Les résultats de l'entraînement ne sont pas enregistrés au classement.
