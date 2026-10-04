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
