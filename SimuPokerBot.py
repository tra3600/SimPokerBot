from treys import Card, Deck, Evaluator

def deal_hands(num_players):
    deck = Deck()
    hands = [deck.draw(2) for _ in range(num_players)]
    return hands, deck

def simulate_game(num_players):
    hands, deck = deal_hands(num_players)
    board = [deck.draw(1) for _ in range(5)]
    evaluator = Evaluator()

    print("Board: ", Card.print_pretty_cards(board))
    for i, hand in enumerate(hands):
        print(f"Player {i+1}: ", Card.print_pretty_cards(hand))
    
    scores = [evaluator.evaluate(board, hand) for hand in hands]
    best_score = min(scores)
    winners = [i for i, score in enumerate(scores) if score == best_score]

    for i in winners:
        print(f"Player {i+1} wins with a score of {scores[i]}")

if __name__ == "__main__":
    num_players = 4  # Nombre de joueurs
    simulate_game(num_players)