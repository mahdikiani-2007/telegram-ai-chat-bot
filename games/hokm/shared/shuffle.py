import hashlib
import secrets
from typing import List, Tuple
from games.hokm.shared.cards import Card


def secure_shuffle(deck: List[Card]) -> Tuple[List[Card], str, str]:
    """Cryptographically secure Fisher-Yates shuffle.

    Returns:
        (shuffled_deck, seed_hex, sha256_commitment)
    """
    shuffled = list(deck)
    n = len(shuffled)
    # Generate 32 bytes of secure entropy for verification commitment
    seed_bytes = secrets.token_bytes(32)
    seed_hex = seed_bytes.hex()
    commitment = hashlib.sha256(seed_bytes).hexdigest()

    # Unbiased Fisher-Yates with secrets.randbelow
    for i in range(n - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        shuffled[i], shuffled[j] = shuffled[j], shuffled[i]

    return shuffled, seed_hex, commitment
