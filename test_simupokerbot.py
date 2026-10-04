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


def test_tournament_has_single_winner_and_full_ranking():
    bots = make_bots(["maniac", "station", "random", "equity"])
    from SimuPokerBot import run_tournament
    ranking = run_tournament(bots, seed=5)
    assert sorted(ranking) == sorted(b.name for b in bots)


def test_tournaments_stats():
    from SimuPokerBot import run_tournaments
    res = run_tournaments(make_bots(["maniac", "station", "random"]), count=3, seed=1)
    assert sum(r["wins"] for r in res.values()) == 3
    assert abs(sum(r["avg_place"] for r in res.values()) - 6) < 1e-9


def scripted(*answers):
    it = iter(answers)
    return lambda _prompt="": next(it)


def test_human_mode_plays_and_quits():
    from SimuPokerBot import run_human
    out = []
    # toujours "c" (suivre/check), puis "q" pour quitter à la main suivante
    answers = ["c"] * 40 + ["q"]
    net, played = run_human(make_bots(["station", "random"]), hands=2, seed=1,
                            input_fn=scripted(*answers), output_fn=out.append)
    assert played == 2
    text = "\n".join(out)
    assert "Vos cartes" in text and "Fin de la partie" in text


def test_human_quit_ends_session():
    from SimuPokerBot import run_human
    net, played = run_human(make_bots(["station"]), seed=1, input_fn=scripted("q"),
                            output_fn=lambda *_: None)
    assert played == 0 and net == 0


def test_human_invalid_input_reprompts_and_hides_opponent_cards():
    from SimuPokerBot import run_human
    out = []
    run_human(make_bots(["station"]), hands=1, seed=2,
              input_fn=scripted("zzz", "r 9999", "c", "c", "c", "c", "c", "c"),
              output_fn=out.append)
    text = "\n".join(out)
    assert "Commande non reconnue" in text or "Montant invalide" in text
    # avant le showdown, les cartes de l'adversaire ne sont pas affichées
    assert not any(l.strip().startswith("station:") and "[" in l for l in out[:3])


def test_human_tournament_all_in_every_hand_ends():
    from SimuPokerBot import run_human_tournament
    out = []
    place = run_human_tournament(make_bots(["station", "maniac"]), seed=7,
                                 input_fn=lambda prompt="": "a" if "a = tapis" in prompt else "c",
                                 output_fn=out.append)
    text = "\n".join(out)
    assert place in (1, 2, 3)
    assert "Bravo" in text or "éliminé" in text


def test_human_tournament_quit_returns_none():
    from SimuPokerBot import run_human_tournament
    assert run_human_tournament(make_bots(["station"]), seed=1, input_fn=scripted("q"),
                                output_fn=lambda *_: None) is None


ASK_END = "\n    > "


def make_client_io(decide, first_chat=None):
    """Simule un humain : répond à chaque invite du serveur via ``decide(prompt)``."""
    import queue
    inbox, log, state = queue.Queue(), [], {"chatted": False}

    def out(text):
        log.append(text)
        if text.endswith(ASK_END):
            if first_chat and not state["chatted"]:
                state["chatted"] = True
                for line in ([first_chat] if isinstance(first_chat, str) else first_chat):
                    inbox.put(line)
            inbox.put(decide(text))

    return (lambda prompt="": inbox.get()), out, log


def _play_network(clients, **server_kwargs):
    import threading
    from SimuPokerBot import run_client, run_server
    port_ready, result, ios = threading.Event(), {}, {}
    port = []

    def on_listen(p):
        port.append(p)
        port_ready.set()

    def serve():
        result["r"] = run_server(port=0, players=len(clients), on_listen=on_listen,
                                 output_fn=lambda *_: None, timeout=10, **server_kwargs)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert port_ready.wait(5)
    threads = []
    for name, spec in clients.items():
        decide, first_chat = spec if isinstance(spec, tuple) else (spec, None)
        ios[name] = make_client_io(decide, first_chat)
        input_fn, out, _ = ios[name]
        c = threading.Thread(target=run_client, daemon=True,
                             args=("127.0.0.1", port[0], name, input_fn, out))
        c.start()
        threads.append(c)
    t.join(60)
    for c in threads:
        c.join(5)
    assert not t.is_alive()
    return result["r"], {name: io[2] for name, io in ios.items()}


def test_network_cash_two_humans():
    always_call = lambda prompt="": "c"
    totals, out = _play_network({"alice": always_call, "bob": always_call}, hands=3, seed=1)
    assert sum(totals.values()) == 0 and set(totals) == {"alice", "bob"}
    assert any("Vos cartes" in l for l in out["alice"])
    assert any("Fin de la partie" in l for l in out["bob"])


def test_network_player_quits_ends_game():
    totals, out = _play_network({"alice": lambda p="": "q", "bob": lambda p="": "c"}, hands=5, seed=2)
    assert any("Fin de la partie" in l for l in out["bob"])


def test_network_tournament_with_bot():
    from SimuPokerBot import make_bots
    shove = lambda prompt="": "a" if "a = tapis" in prompt else "c"
    ranking, out = _play_network({"alice": shove, "bob": shove}, tournament=True, seed=3,
                                 opponents=make_bots(["station"]))
    assert sorted(ranking) == ["alice", "bob", "station"]
    assert any("Classement final" in l for l in out["alice"])


def test_chat_between_players_and_sanitizing():
    call = lambda prompt="": "c"
    _, out = _play_network({"alice": (call, "/salut tout le monde"), "bob": call}, hands=2, seed=1)
    assert any(l == "[chat] alice: salut tout le monde" for l in out["bob"])
    assert any(l == "[chat] alice: salut tout le monde" for l in out["alice"])
    # le chat n'a pas été pris pour une réponse de jeu
    assert not any("Commande non reconnue" in l for l in out["alice"])


def test_chat_rate_limited():
    call = lambda prompt="": "c"
    _, out = _play_network({"alice": (call, ["/premier", "/second"]), "bob": call}, hands=2, seed=1)
    assert any("trop rapide" in l for l in out["alice"])
    assert any(l == "[chat] alice: premier" for l in out["bob"])
    assert not any("second" in l for l in out["bob"] if l.startswith("[chat]"))


def test_empty_chat_ignored_and_bad_answer_reprompted():
    call = lambda prompt="": "c"
    # "/" seul est ignoré ; "zzz" répond à la décision en attente et est refusé par le jeu
    _, out = _play_network({"alice": (call, ["/", "zzz"]), "bob": call}, hands=1, seed=1)
    assert any("Commande non reconnue" in l for l in out["alice"])
    assert not any(l.strip() == "[chat] alice:" for l in out["bob"])


def test_conn_rejects_garbage():
    import socket as sk
    from SimuPokerBot import Conn
    a, b = sk.socketpair()
    b.sendall(b"pas du json\n")
    with pytest.raises(ConnectionError):
        Conn(a).recv(1)


def _start_server(**kw):
    import threading
    ready, port = threading.Event(), []
    from SimuPokerBot import run_server
    srv = threading.Thread(target=lambda: run_server(
        port=0, on_listen=lambda p: (port.append(p), ready.set()),
        output_fn=lambda *_: None, timeout=10, **kw), daemon=True)
    srv.start()
    assert ready.wait(5)
    return srv, port[0]


def _client(port, name, decide=None, spectate=False):
    import threading
    from SimuPokerBot import run_client
    input_fn, out, log = make_client_io(decide or (lambda p="": "c"))
    t = threading.Thread(target=run_client, daemon=True, kwargs=dict(
        host="127.0.0.1", port=port, name=name, input_fn=input_fn, output_fn=out, spectate=spectate))
    t.start()
    return t, log


def test_spectators_see_public_info_only_lobby_and_late():
    import time
    from SimuPokerBot import make_bots
    srv, port = _start_server(players=1, opponents=make_bots(["station"]), seed=4)
    s1, early = _client(port, "early", spectate=True)
    time.sleep(0.3)  # accepté pendant le lobby
    count = [0]

    def slow_player(prompt=""):
        count[0] += 1
        time.sleep(0.05)
        return "q" if count[0] > 25 else "c"

    p, player_out = _client(port, "alice", slow_player)
    time.sleep(0.4)
    s2, late = _client(port, "late", spectate=True)  # rejoint en cours de partie
    srv.join(60)
    for th in (p, s1, s2):
        th.join(5)
    assert not srv.is_alive()
    for out in (early, late):
        text = "\n".join(out)
        assert "Main" in text and "Le serveur ferme" in text
        assert "Vos cartes" not in text
    assert any("regarde la partie" in l for l in player_out)
    assert any("Vous regardez la partie" in l and "déjà en cours" in l for l in late)


def test_spectator_cannot_chat_or_play():
    from SimuPokerBot import make_bots
    srv, port = _start_server(players=1, opponents=make_bots(["station"]), hands=2, seed=4)
    s, spec_log = _client(port, "fan", spectate=True)
    p, _ = _client(port, "alice")
    srv.join(60)
    assert not srv.is_alive()
    assert not any(l.startswith("[chat] fan") for l in spec_log)


def test_late_player_is_rejected():
    import socket as sk
    import time
    from SimuPokerBot import Conn, make_bots
    srv, port = _start_server(players=1, opponents=make_bots(["station"]), hands=1000)
    count = [0]

    def player(prompt=""):
        count[0] += 1
        return "q" if count[0] > 6 else "c"

    p, _ = _client(port, "a", player)
    time.sleep(0.3)
    c = Conn(sk.create_connection(("127.0.0.1", port), timeout=5))
    c.send(name="intrus", role="player")
    assert "déjà commencée" in c.recv(5)["text"]
    c.close()
    srv.join(30)


# --------------------------------------------------------------------------
# Classement persistant
# --------------------------------------------------------------------------
def test_leaderboard_persists_between_instances(tmp_path):
    from SimuPokerBot import Leaderboard
    path = tmp_path / "lb.json"
    lb = Leaderboard(str(path))
    lb.record_cash({"alice": 5.0, "bob": -5.0})
    lb.record_cash({"alice": -1.0})
    lb.record_tournament({"alice": (1, 4), "bob": (4, 4)})
    again = Leaderboard(str(path))
    a, b = again.players["alice"], again.players["bob"]
    assert a["cash_hands"] == 2 and a["cash_bb"] == 4.0
    assert a["tournaments"] == 1 and a["wins"] == 1 and a["place_pct"] == 1.0
    assert b["place_pct"] == 0.0 and b["wins"] == 0
    text = again.format(min_hands=1)
    assert text.index("alice") < text.index("bob")


def test_leaderboard_min_hands_and_empty(tmp_path):
    from SimuPokerBot import Leaderboard
    lb = Leaderboard(str(tmp_path / "x.json"))
    assert "aucun joueur" in lb.format() and "aucun tournoi" in lb.format()
    lb.record_cash({"alice": 1.0})
    assert "alice" not in lb.format(min_hands=20)
    assert "alice" in lb.format(min_hands=1)


def test_leaderboard_corrupt_file_is_set_aside(tmp_path):
    from SimuPokerBot import Leaderboard
    path = tmp_path / "lb.json"
    path.write_text("{ pas du json")
    lb = Leaderboard(str(path))
    assert lb.players == {}
    assert (tmp_path / "lb.json.bak").exists()
    lb.record_cash({"alice": 1.0})
    assert Leaderboard(str(path)).players["alice"]["cash_hands"] == 1


def test_leaderboard_ignores_malformed_entries_and_merges_external_writes(tmp_path):
    import json
    from SimuPokerBot import Leaderboard
    path = tmp_path / "lb.json"
    path.write_text(json.dumps({"players": {"ok": {"cash_hands": 3, "cash_bb": 1.5}, "bad": 7}}))
    lb = Leaderboard(str(path))
    assert set(lb.players) == {"ok"} and lb.players["ok"]["wins"] == 0
    # un autre processus écrit entre-temps : la mise à jour suivante ne l'écrase pas
    other = Leaderboard(str(path))
    other.record_cash({"zoe": 2.0})
    lb.record_cash({"ok": 1.0})
    final = Leaderboard(str(path)).players
    assert final["zoe"]["cash_hands"] == 1 and final["ok"]["cash_hands"] == 4


def test_human_modes_record_results(tmp_path):
    from SimuPokerBot import Leaderboard, run_human, run_human_tournament
    lb = Leaderboard(str(tmp_path / "lb.json"))
    run_human(make_bots(["station"]), hands=3, seed=1, name="Zoe", leaderboard=lb,
              input_fn=lambda prompt="": "c", output_fn=lambda *_: None)
    assert Leaderboard(lb.path).players["Zoe"]["cash_hands"] == 3
    place = run_human_tournament(make_bots(["station"]), seed=2, name="Zoe", leaderboard=lb,
                                 input_fn=lambda prompt="": "a" if "a = tapis" in prompt else "c",
                                 output_fn=lambda *_: None)
    e = Leaderboard(lb.path).players["Zoe"]
    assert e["tournaments"] == 1 and e["wins"] == (place == 1)
    # abandon : rien n'est enregistré pour le tournoi
    assert run_human_tournament(make_bots(["station"]), seed=2, name="Zoe", leaderboard=lb,
                                input_fn=lambda prompt="": "q", output_fn=lambda *_: None) is None
    assert Leaderboard(lb.path).players["Zoe"]["tournaments"] == 1


def test_network_games_update_leaderboard_for_humans_only(tmp_path):
    from SimuPokerBot import Leaderboard
    lb = Leaderboard(str(tmp_path / "lb.json"))
    call = lambda prompt="": "c"
    _, out = _play_network({"alice": call, "bob": call}, hands=3, seed=1,
                           opponents=make_bots(["station"]), leaderboard=lb)
    players = Leaderboard(lb.path).players
    assert set(players) == {"alice", "bob"}  # les bots ne figurent pas au classement
    assert players["alice"]["cash_hands"] == 3
    assert any("Classement cash" in l for l in out["alice"])

    shove = lambda prompt="": "a" if "a = tapis" in prompt else "c"
    _, out = _play_network({"alice": shove, "bob": shove}, tournament=True, seed=3, leaderboard=lb)
    players = Leaderboard(lb.path).players
    assert players["alice"]["tournaments"] == 1 and players["bob"]["tournaments"] == 1
    assert players["alice"]["wins"] + players["bob"]["wins"] == 1


def test_cli_leaderboard_mode(tmp_path, capsys):
    from SimuPokerBot import Leaderboard, main
    path = str(tmp_path / "lb.json")
    Leaderboard(path).record_tournament({"alice": (1, 3)})
    main(["--mode", "leaderboard", "--leaderboard", path])
    assert "alice" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Historique / replay
# --------------------------------------------------------------------------
def _record_hands(path, n=6, bots=("maniac", "station", "random"), seed=1):
    from SimuPokerBot import HandHistory
    run_session(make_bots(list(bots)), hands=n, seed=seed, history=HandHistory(str(path)))


def test_history_records_consistent_hands(tmp_path):
    from SimuPokerBot import load_history
    path = tmp_path / "h.jsonl"
    _record_hands(path, n=12)
    hands = load_history(str(path))
    assert len(hands) == 12
    for rec in hands:
        assert sum(rec["profit"].values()) == 0
        assert [p["name"] for p in rec["players"]] == ["maniac", "station", "random"]
        # le pot reconstruit à partir des événements = somme des mises
        put = sum(e["amt"] for e in rec["events"] if e["e"] in ("blind", "call", "raise"))
        won = sum(e["amt"] for e in rec["events"] if e["e"] == "win")
        assert put == won
        assert rec["events"][0]["e"] == "blind"


def test_replay_shows_all_hole_cards_and_result(tmp_path):
    from SimuPokerBot import load_history, run_replay
    path = tmp_path / "h.jsonl"
    _record_hands(path)
    out = []
    assert run_replay(str(path), number=2, output_fn=out.append)
    text = "\n".join(out)
    rec = load_history(str(path))[1]
    assert "Main 2" in text and "Résultat" in text
    from SimuPokerBot import _cards
    for p in rec["players"]:
        assert _cards(p["hole"]) in text  # les cartes de tous sont visibles dans un replay


def test_replay_default_last_hand_list_and_filter(tmp_path):
    from SimuPokerBot import run_replay
    path = tmp_path / "h.jsonl"
    _record_hands(path, n=4)
    out = []
    assert run_replay(str(path), output_fn=out.append)
    assert "Main 4" in "\n".join(out)
    out.clear()
    assert run_replay(str(path), list_only=True, output_fn=out.append)
    assert len(out) == 4 and out[0].lstrip().startswith("1.")
    out.clear()
    assert not run_replay(str(path), list_only=True, player="inconnu", output_fn=out.append)
    out.clear()
    assert not run_replay(str(path), number=99, output_fn=out.append)
    assert "introuvable" in out[0]


def test_replay_step_mode_quit(tmp_path):
    from SimuPokerBot import run_replay
    path = tmp_path / "h.jsonl"
    _record_hands(path, n=2)
    out, answers = [], iter(["", "", "q"])
    run_replay(str(path), number=1, step=True, output_fn=out.append, input_fn=lambda p="": next(answers))
    assert len(out) == 3  # trois étapes affichées puis arrêt


def test_replay_missing_or_corrupt_file(tmp_path):
    from SimuPokerBot import run_replay
    out = []
    assert not run_replay(str(tmp_path / "absent.jsonl"), output_fn=out.append)
    assert "Impossible de lire" in out[0]
    bad = tmp_path / "bad.jsonl"
    bad.write_text("pas du json\n{\"x\": 1}\n")
    out.clear()
    assert not run_replay(str(bad), output_fn=out.append)
    assert "Aucune main" in out[0]


def test_history_tournament_and_human_modes(tmp_path):
    from SimuPokerBot import HandHistory, load_history, run_human, run_tournament
    path = tmp_path / "t.jsonl"
    h = HandHistory(str(path))
    run_tournament(make_bots(["maniac", "station"]), seed=3, history=h)
    run_human(make_bots(["station"]), hands=2, seed=1, input_fn=lambda p="": "c",
              output_fn=lambda *_: None, history=h)
    hands = load_history(str(path))
    assert {r["label"] for r in hands} == {"tournoi", "humain"}


def test_cli_record_and_replay(tmp_path, capsys):
    from SimuPokerBot import main
    path = str(tmp_path / "c.jsonl")
    main(["--bots", "tight,station", "--hands", "3", "--seed", "2", "--history", path])
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["--mode", "replay", "--history", path, "--hand", "1"])
    assert e.value.code == 0
    assert "Main 1" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Entraînement avec conseils
# --------------------------------------------------------------------------
def make_view(hole, board=(), pot=10, to_call=0, stack=100, opponents=1, can_raise=True):
    from SimuPokerBot import View
    return View(hole=cards(*hole), board=cards(*board), street="x", pot=pot, to_call=to_call,
                stack=stack, min_raise_to=to_call * 2 + 2, max_raise_to=stack, opponents=opponents,
                can_raise=can_raise, big_blind=2, rng=random.Random(0))


def test_describe_hole():
    from SimuPokerBot import describe_hole
    assert describe_hole(cards("7s", "7h")) == "Paire de 7"
    assert describe_hole(cards("Ah", "Kh")) == "As-Roi assortis"
    assert describe_hole(cards("2c", "Qd")) == "Dame-2 dépareillés"
    assert describe_hole(cards("Ks", "Kd")) == "Paire de Rois"
    assert describe_hole(cards("As", "Ad")) == "Paire d'As"


def test_count_outs_flush_draw_and_no_board_pair_credit():
    from SimuPokerBot import count_outs
    # tirage couleur + deux overcards : 9 cœurs + 3 As + 3 Rois
    assert count_outs(cards("Ah", "Kh"), cards("2h", "7h", "9c")) == 15
    # pas d'outs hors flop/turn
    assert count_outs(cards("Ah", "Kh"), []) is None
    assert count_outs(cards("Ah", "Kh"), cards("2h", "7h", "9c", "3d", "4s")) is None


def test_advisor_recommendations():
    from SimuPokerBot import Advisor
    adv = Advisor(iterations=1200)
    assert adv.advise(make_view(("As", "Ah"))).action == "raise"
    a = adv.advise(make_view(("7c", "2d"), pot=40, to_call=30))
    assert a.action == "fold" and a.equity < a.needed
    a = adv.advise(make_view(("As", "Ks"), ("Qs", "Js", "Ts", "2d", "3c"), pot=50, to_call=10))
    assert a.action == "raise" and a.equity == 1.0 and a.amount >= 22
    a = adv.advise(make_view(("7c", "2d"), ("Ks", "9h", "4d"), pot=6, to_call=0))
    assert a.action == "check" and a.amount == 0
    # ne conseille pas de relancer quand c'est impossible
    assert adv.advise(make_view(("As", "Ah"), can_raise=False, to_call=2)).action == "call"
    text = a.text()
    assert "Conseil : CHECK" in text and "Équité estimée" in text


def test_training_bot_flags_clear_mistakes_and_summarizes():
    from SimuPokerBot import FOLD, TrainingBot
    out = []
    bot = TrainingBot(input_fn=lambda p="": "f", output_fn=out.append, advice="always")
    # AA face à une petite mise : se coucher est une erreur claire
    view = make_view(("As", "Ah"), pot=10, to_call=2)
    assert bot.act(view)[0] == FOLD
    text = "\n".join(out)
    assert "Conseil" in text and "✘ Écart" in text
    summary = bot.summary()
    assert "Décisions : 1" in summary and "erreurs claires : 1" in summary
    assert "Main 1" in summary


def test_training_bot_conforming_decision_and_ask_mode_hint():
    from SimuPokerBot import CALL, TrainingBot
    out, answers = [], iter(["?", "c"])
    bot = TrainingBot(input_fn=lambda p="": next(answers), output_fn=out.append, advice="ask")
    view = make_view(("As", "Ah"), pot=10, to_call=2, can_raise=False)
    assert bot.act(view)[0] == CALL
    text = "\n".join(out)
    assert "tapez ? pour demander un conseil" in text
    assert "Conseil : SUIVRE" in text and "✔ Conforme" in text


def test_training_off_mode_never_shows_advice_before_decision():
    from SimuPokerBot import TrainingBot
    out, answers = [], iter(["?", "c"])  # "?" n'est pas une commande en mode off
    bot = TrainingBot(input_fn=lambda p="": next(answers), output_fn=out.append, advice="off")
    bot.act(make_view(("As", "Ah"), pot=10, to_call=2, can_raise=False))
    text = "\n".join(out)
    assert ">>> Conseil" not in text and "Commande non reconnue" in text


def test_run_training_end_to_end():
    from SimuPokerBot import run_training
    out = []
    bot = run_training(make_bots(["station"]), hands=3, seed=1, input_fn=lambda p="": "c",
                       output_fn=out.append, advice="always")
    text = "\n".join(out)
    assert "Bilan d'entraînement" in text
    assert len(bot.decisions) >= 3 and bot.hand_index == 3


def test_cli_training_mode(monkeypatch, capsys):
    import io
    from SimuPokerBot import main
    monkeypatch.setattr("sys.stdin", io.StringIO("c\n" * 50))
    main(["--mode", "training", "--bots", "station", "--hands", "2", "--seed", "4", "--advice", "off"])
    assert "Bilan d'entraînement" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Statistiques avancées
# --------------------------------------------------------------------------
def fake_hand(events, profit, hole=(("As", "Kd"), ("7c", "2h")), button="A", bb=2, label="cash"):
    names = ["A", "B"]
    return {"label": label, "bb": bb, "button": button, "board": [],
            "players": [{"name": n, "stack": 100, "hole": list(h)} for n, h in zip(names, hole)],
            "events": events, "profit": profit}


def test_position_and_starting_hand_helpers():
    from SimuPokerBot import position_name, starting_hand_key
    assert [position_name(i, 2) for i in (0, 1)] == ["BTN/SB", "BB"]
    assert [position_name(i, 6) for i in range(6)] == ["BTN", "SB", "BB", "EP/MP", "EP/MP", "CO"]
    assert starting_hand_key(["Kd", "As"]) == "AKo"
    assert starting_hand_key(["Ks", "As"]) == "AKs"
    assert starting_hand_key(["7c", "7h"]) == "77"


def test_stats_counters_on_scripted_hand():
    from SimuPokerBot import analyze_hand, derive
    ev = [
        {"e": "blind", "p": "A", "amt": 1, "role": "SB"}, {"e": "blind", "p": "B", "amt": 2, "role": "BB"},
        {"e": "raise", "p": "A", "to": 6, "amt": 5, "allin": False},   # PFR + VPIP de A
        {"e": "raise", "p": "B", "to": 18, "amt": 16, "allin": False},  # 3bet de B
        {"e": "call", "p": "A", "amt": 12, "allin": False},
        {"e": "street", "street": "flop", "board": ["2c", "7d", "9h"]},
        {"e": "check", "p": "B", "amt": 0, "allin": False},
        {"e": "raise", "p": "A", "to": 10, "amt": 10, "allin": False},  # pas de c-bet : A n'est plus dernier relanceur
        {"e": "call", "p": "B", "amt": 10, "allin": False},
        {"e": "show", "p": "A", "cards": ["As", "Kd"], "hand": "High Card"},
        {"e": "show", "p": "B", "cards": ["7c", "2h"], "hand": "Two Pair"},
        {"e": "win", "p": "B", "amt": 56},
    ]
    stats = {}
    analyze_hand(fake_hand(ev, {"A": -28, "B": 28}), stats)
    a, b = derive(stats["A"]), derive(stats["B"])
    assert a["vpip"] == 1 and a["pfr"] == 1 and a["threebet"] is None
    assert b["vpip"] == 1 and b["pfr"] == 1 and b["threebet"] == 1.0
    assert a["wtsd"] == 1 and a["wsd"] == 0 and b["wsd"] == 1
    assert a["af"] == float("inf") and b["af"] == 0.0  # A : 1 relance / 0 call ; B : 0 / 1
    assert a["bb_per_100"] == -1400 and b["bb_per_100"] == 1400
    # B est le dernier relanceur préflop et agit en premier au flop : check => opportunité de c-bet manquée
    assert stats["B"]["cbet_opp"] == 1 and stats["B"]["cbet"] == 0
    assert stats["A"]["cbet_opp"] == 0
    assert stats["A"]["positions"]["BTN/SB"] == [1, -14.0] and stats["B"]["positions"]["BB"] == [1, 14.0]


def test_stats_cbet_and_bb_check_not_vpip():
    from SimuPokerBot import analyze_hand, derive
    ev = [
        {"e": "blind", "p": "A", "amt": 1, "role": "SB"}, {"e": "blind", "p": "B", "amt": 2, "role": "BB"},
        {"e": "raise", "p": "A", "to": 6, "amt": 5, "allin": False},
        {"e": "call", "p": "B", "amt": 4, "allin": False},
        {"e": "street", "street": "flop", "board": ["2c", "7d", "9h"]},
        {"e": "check", "p": "B", "amt": 0, "allin": False},
        {"e": "raise", "p": "A", "to": 6, "amt": 6, "allin": False},   # c-bet de A
        {"e": "fold", "p": "B"},
        {"e": "win", "p": "A", "amt": 16},
    ]
    stats = {}
    analyze_hand(fake_hand(ev, {"A": 6, "B": -6}), stats)
    assert derive(stats["A"])["cbet"] == 1.0 and derive(stats["B"])["cbet"] is None
    # BB qui ne fait que checker préflop n'est pas compté au VPIP
    stats = {}
    ev2 = [{"e": "blind", "p": "A", "amt": 1, "role": "SB"}, {"e": "blind", "p": "B", "amt": 2, "role": "BB"},
           {"e": "call", "p": "A", "amt": 1, "allin": False}, {"e": "check", "p": "B", "amt": 0, "allin": False},
           {"e": "win", "p": "A", "amt": 4}]
    analyze_hand(fake_hand(ev2, {"A": 2, "B": -2}), stats)
    assert stats["A"]["vpip"] == 1 and stats["B"]["vpip"] == 0


def test_player_style_labels():
    from SimuPokerBot import player_style
    base = {"hands": 100}
    assert player_style({**base, "vpip": 0.18, "pfr": 0.14}) == "TAG (serré-agressif)"
    assert player_style({**base, "vpip": 0.18, "pfr": 0.04}) == "Rock (serré-passif)"
    assert player_style({**base, "vpip": 0.35, "pfr": 0.05}) == "Calling station"
    assert player_style({**base, "vpip": 0.35, "pfr": 0.25}) == "LAG (large-agressif)"
    assert player_style({**base, "vpip": 0.7, "pfr": 0.5}) == "Maniaque"
    assert player_style({"hands": 5, "vpip": 0.5, "pfr": 0.5}) == "échantillon trop petit"


def test_run_stats_end_to_end(tmp_path):
    import json
    from SimuPokerBot import HandHistory, run_session, run_stats
    path = str(tmp_path / "s.jsonl")
    run_session(make_bots(["maniac", "station", "random"]), hands=60, seed=2, history=HandHistory(path))
    out = []
    assert run_stats(path, output_fn=out.append)
    text = "\n".join(out)
    assert "Statistiques sur 60 main(s)" in text and "maniac" in text and "VPIP" in text
    out.clear()
    assert run_stats(path, player="maniac", starting_hands=True, output_fn=out.append)
    assert "Profil de maniac" in out[0] and "Par position" in out[0] and "Mains de départ" in out[0]
    out.clear()
    assert run_stats(path, as_json=True, output_fn=out.append)
    data = json.loads(out[0])
    assert data["maniac"]["hands"] == 60 and "vpip" in data["maniac"]
    out.clear()
    assert not run_stats(path, player="inconnu", output_fn=out.append) and "introuvable" in out[0]
    out.clear()
    assert not run_stats(path, label="tournoi", output_fn=out.append)
    out.clear()
    assert not run_stats(str(tmp_path / "absent.jsonl"), output_fn=out.append)


def test_stats_net_matches_session_results(tmp_path):
    from SimuPokerBot import HandHistory, compute_stats, load_history
    path = str(tmp_path / "n.jsonl")
    res = run_session(make_bots(["maniac", "station"]), hands=40, seed=6, history=HandHistory(path))
    stats = compute_stats(load_history(path))
    for name, r in res.items():
        assert abs(stats[name]["net_bb"] - r["bb"]) < 1e-9


def test_cli_stats_mode(tmp_path, capsys):
    from SimuPokerBot import main
    path = str(tmp_path / "c.jsonl")
    main(["--bots", "tight,station", "--hands", "5", "--seed", "2", "--history", path])
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["--mode", "stats", "--history", path])
    assert e.value.code == 0 and "VPIP" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["--mode", "stats", "--history", path, "--player", "zzz"])
    assert e.value.code == 1


# --------------------------------------------------------------------------
# Tournoi par équipes
# --------------------------------------------------------------------------
def test_parse_teams():
    from SimuPokerBot import parse_teams
    assert parse_teams("Rouge:tight,maniac;Bleu:station,loose") == [
        ("Rouge", ["tight", "maniac"]), ("Bleu", ["station", "loose"])]
    assert parse_teams("tight,loose; maniac ,station;")[0][0] == "Équipe 1"
    for bad in ("tight,loose", "A:tight;B:station,loose", "A:tight;A:loose", "A:;B:tight",
                "A:tight,tight,tight,tight,tight;B:tight,tight,tight,tight,tight"):
        with pytest.raises(ValueError):
            parse_teams(bad)


def test_team_scores_points_and_tiebreak():
    from SimuPokerBot import team_scores
    ranking = ["a1", "b1", "b2", "a2"]
    sc = team_scores(ranking, {"a1": "A", "a2": "A", "b1": "B", "b2": "B"})
    assert sc["A"]["points"] == 3 + 0 and sc["B"]["points"] == 2 + 1
    assert sc["A"]["best_place"] == 1 and sc["B"]["best_place"] == 2
    assert sc["B"]["places"] == {"b1": 2, "b2": 3}


def test_team_tournament_consistent_and_deterministic():
    from SimuPokerBot import parse_teams, run_team_tournament
    teams = parse_teams("Rouge:maniac,station;Bleu:random,maniac")
    r = run_team_tournament(teams, seed=4)
    assert sorted(r["ranking"]) == ["maniac1", "maniac2", "random", "station"]  # doublons renommés
    assert sum(t["points"] for t in r["scores"].values()) == 6  # 0+1+2+3
    assert r["winner"] in ("Rouge", "Bleu")
    assert r["teams_of"]["maniac1"] == "Rouge" and r["teams_of"]["maniac2"] == "Bleu"
    assert run_team_tournament(teams, seed=4)["ranking"] == r["ranking"]


def test_team_tournaments_aggregate_and_history(tmp_path):
    from SimuPokerBot import HandHistory, load_history, parse_teams, run_team_tournaments
    path = str(tmp_path / "t.jsonl")
    res = run_team_tournaments(parse_teams("A:maniac;B:station"), count=3, seed=1,
                               history=HandHistory(path))
    assert sum(a["wins"] for a in res["teams"].values()) == 3
    assert abs(sum(a["avg_points"] for a in res["teams"].values()) - 1) < 1e-9  # 1 point en jeu
    assert {r["label"] for r in load_history(path)} == {"tournoi-equipe"}


def test_cli_team_mode(capsys):
    from SimuPokerBot import main
    main(["--mode", "team", "--teams", "A:maniac;B:station", "--tournaments", "2", "--seed", "3"])
    out = capsys.readouterr().out
    assert "Résultats par équipe sur 2 tournoi(s)" in out and "Classement individuel" in out
    with pytest.raises(SystemExit) as e:
        main(["--mode", "team", "--teams", "A:maniac"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        main(["--mode", "team", "--teams", "A:nope;B:station"])


def test_same_seed_gives_same_games():
    from SimuPokerBot import run_tournament
    names = ["maniac", "station", "random", "equity"]
    a = run_session(make_bots(names), hands=15, seed=11)
    b = run_session(make_bots(names), hands=15, seed=11)
    assert a == b
    assert run_session(make_bots(names), hands=15, seed=12) != a
    assert run_tournament(make_bots(names), seed=5) == run_tournament(make_bots(names), seed=5)
