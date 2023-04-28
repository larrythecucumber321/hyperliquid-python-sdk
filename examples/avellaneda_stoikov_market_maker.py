import math
from typing import Dict, Tuple


class AvellanedaStoikovMarketMaker:
    def __init__(self, gamma: float, k: float, r: float):
        self.gamma = gamma
        self.k = k
        self.r = r

    def calculate_quotes(self, mid_price: float, spread: float, inventory: float, vol: float, dt: float) -> Dict[str, Tuple[float, float]]:
        m = -self.gamma * spread / 2
        delta = self.gamma * (vol ** 2) * dt
        l = self.k * math.exp(-self.r * dt)

        bid_price = mid_price - m - inventory * delta / 2 - l
        ask_price = mid_price + m + inventory * delta / 2 + l

        bid_size = (1 / 2) * (1 + inventory * delta / l)
        ask_size = (1 / 2) * (1 - inventory * delta / l)

        return {
            "bid": (bid_price, bid_size),
            "ask": (ask_price, ask_size)
        }
