import json
import logging
import threading
import time

import eth_account
from eth_account.signers.local import LocalAccount

import utils
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import get_timestamp_ms
from hyperliquid.utils.types import (
    L2BookMsg,
    L2BookSubscription,
    UserEventsMsg,
    Side,
    SIDES,
    Dict,
    TypedDict,
    Optional,
    Literal,
    Union,
)

from avellaneda_stoikov_market_maker import AvellanedaStoikovMarketMaker  # Import the class

# Add or update the following constants as required
GAMMA = 0.005
K = 0.001
R = 0.0005
VOL = 0.02
DT = 0.05
# How far from the target price a resting order can deviate before the strategy will cancel and replace it.
# i.e. using the same example as above of a best bid of $1000 and targeted depth of .3%. The ideal distance is $3, so
# bids within $3 * 0.5 = $1.5 will not be cancelled. So any bids > $998.5 or < $995.5 will be cancelled and replaced.
ALLOWABLE_DEVIATION = 0.5

# The maximum absolute position value the strategy can accumulate in units of the coin.
# i.e. the strategy will place orders such that it can long up to 1 ETH or short up to 1 ETH
MAX_POSITION = 100

# The coin to add liquidity on
COIN = "INJ"

InFlightOrder = TypedDict(
    "InFlightOrder", {"type": Literal["in_flight_order"], "time": int})
Resting = TypedDict(
    "Resting", {"type": Literal["resting"], "px": float, "oid": int})
Cancelled = TypedDict("Cancelled", {"type": Literal["cancelled"]})
ProvideState = Union[InFlightOrder, Resting, Cancelled]


def side_to_int(side: Side) -> int:
    return 1 if side == "A" else -1


def side_to_uint(side: Side) -> int:
    return 1 if side == "A" else 0


class BasicAdder:
    def __init__(self, wallet: LocalAccount, api_url: str, reconnect_attempts: int = 5):
        self.wallet = wallet
        self.api_url = api_url
        self.reconnect_attempts = reconnect_attempts
        self.info = None
        self.exchange = None
        self.position = None
        self.provide_state = {
            "A": {"type": "cancelled"},
            "B": {"type": "cancelled"},
        }
        self.recently_cancelled_oid_to_time = {}

        self.market_maker = AvellanedaStoikovMarketMaker(gamma=GAMMA, k=K, r=R)
        self.poller = None

    def connect(self):
        self.info = Info(self.api_url)
        self.exchange = Exchange(self.wallet, self.api_url)
        subscription: L2BookSubscription = {"type": "l2Book", "coin": COIN}
        self.info.subscribe(subscription, self.on_book_update)
        self.info.subscribe(
            {"type": "userEvents", "user": self.wallet.address}, self.on_user_events)

        if self.poller is None or not self.poller.is_alive():
            self.poller = threading.Thread(target=self.poll)
            self.poller.start()

    def reconnect(self):
        attempt = 0
        while attempt < self.reconnect_attempts:
            try:
                self.connect()
                print("Connected successfully.")
                break
            except Exception as e:
                print(f"Connection failed. Attempt {attempt + 1}: {e}")
                attempt += 1
                if attempt < self.reconnect_attempts:
                    time.sleep(5)
                else:
                    print("Failed to reconnect after maximum attempts.")
                    raise

    def on_book_update(self, book_msg: L2BookMsg) -> None:
        logging.debug(f"book_msg {book_msg}")
        book_data = book_msg["data"]
        if book_data["coin"] != COIN:
            print("Unexpected book message, skipping")
            return
        mid_price = (float(book_data["levels"][0][0]["px"]) +
                     float(book_data["levels"][1][0]["px"])) / 2
        spread = float(book_data["levels"][1][0]["px"]) - \
            float(book_data["levels"][0][0]["px"])

        for side in SIDES:
            # Calculate the bid and ask quotes using the Avellaneda-Stoikov model
            position = self.position if self.position is not None else 0
            quotes = self.market_maker.calculate_quotes(
                mid_price, spread, position, VOL, DT)
            quote_price, quote_size = quotes["bid"] if side == "B" else quotes["ask"]

            logging.debug(
                f"on_book_update quote_price:{quote_price} quote_size:{quote_size}")

            # If a resting order exists, maybe cancel it
            provide_state = self.provide_state[side]
            if provide_state["type"] == "resting":
                distance = abs((quote_price - provide_state["px"]))
                if distance > ALLOWABLE_DEVIATION * spread:
                    oid = provide_state["oid"]
                    print(
                        f"cancelling order due to deviation oid:{oid} side:{side} ideal_price:{quote_price} px:{provide_state['px']}"
                    )
                    response = self.exchange.cancel(COIN, oid)
                    if response["status"] == "ok":
                        self.recently_cancelled_oid_to_time[oid] = get_timestamp_ms(
                        )
                        self.provide_state[side] = {"type": "cancelled"}
                    else:
                        print(
                            f"Failed to cancel order {provide_state} {side}", response)
            elif provide_state["type"] == "in_flight_order":
                if get_timestamp_ms() - provide_state["time"] > 10000:
                    print(
                        "Order is still in flight after 10s treating as cancelled", provide_state)
                    self.provide_state[side] = {"type": "cancelled"}

            # If we aren't providing, maybe place a new order
            provide_state = self.provide_state[side]
            if provide_state["type"] == "cancelled":
                sz = MAX_POSITION + position * (side_to_int(side))
                # if sz * quote_price < 10:
                #     print(
                #         "Not placing an order because at position limit")
                #     continue
                # prices should have at most 5 significant digits
                px = float(f"{quote_price:.5g}")
                print(f"placing order sz:{sz} px:{px} side:{side}")
                self.provide_state[side] = {
                    "type": "in_flight_order", "time": get_timestamp_ms()}
                response = self.exchange.order(COIN, side == "B", sz, px, {
                                               "limit": {"tif": "Alo"}})
                print("placed order", response)
                if response["status"] == "ok":
                    status = response["response"]["data"]["statuses"][0]
                    if "resting" in status:
                        self.provide_state[side] = {
                            "type": "resting", "px": px, "oid": status["resting"]["oid"]}
                    else:
                        print(
                            "Unexpected response from placing order. Setting position to None.", response)
                        self.provide_state[side] = {"type": "cancelled"}
                        self.position = None

    def on_user_events(self, user_events: UserEventsMsg) -> None:
        print(user_events)
        user_events_data = user_events["data"]
        if "fills" in user_events_data:
            with open("fills", "a+") as f:
                f.write(json.dumps(user_events_data["fills"]))
                f.write("\n")
        # Set the position to None so that we don't place more orders without knowing our position
        # You might want to also update provide_state to account for the fill. This could help avoid sending an
        # unneeded cancel or failing to send a new order to replace the filled order, but we skipped this logic
        # to make the example simpler
        self.position = None

    def poll(self):
        while True:
            open_orders = self.info.open_orders(self.exchange.wallet.address)
            print("open_orders", open_orders)

            ok_oids = set(self.recently_cancelled_oid_to_time.keys())
            for provide_state in self.provide_state.values():
                if provide_state["type"] == "resting":
                    ok_oids.add(provide_state["oid"])

            for open_order in open_orders:
                print("Checking open_order:", open_order)
                if open_order["coin"] == COIN and open_order["oid"] not in ok_oids:
                    print("Cancelling unknown oid", open_order["oid"])
                    self.exchange.cancel(open_order["coin"], open_order["oid"])

            current_time = get_timestamp_ms()
            self.recently_cancelled_oid_to_time = {
                oid: timestamp
                for (oid, timestamp) in self.recently_cancelled_oid_to_time.items()
                if current_time - timestamp > 30000
            }

            user_state = self.info.user_state(self.exchange.wallet.address)
            for position in user_state["assetPositions"]:
                if position["position"]["coin"] == COIN:
                    self.position = float(position["position"]["szi"])
                    print(f"set position to {self.position}")
                    break
            time.sleep(10)


def main():
    # Setting this to logging.DEBUG can be helpful for debugging websocket callback issues
    logging.basicConfig(level=logging.ERROR)
    config = utils.get_config()
    account = eth_account.Account.from_key(config["secret_key"])
    print("Running with account address:", account.address)
    adder = BasicAdder(account, constants.MAINNET_API_URL)
    adder.reconnect()


if __name__ == "__main__":
    main()
