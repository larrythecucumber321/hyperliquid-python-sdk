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

DEPTH = 0.001
ALLOWABLE_DEVIATION = 0.5
MAX_POSITION = 20
COIN = "ARB"

InFlightOrder = TypedDict(
    "InFlightOrder", {"type": Literal["in_flight_order"], "time": int})
Resting = TypedDict(
    "Resting", {"type": Literal["resting"], "px": float, "oid": int})
Cancelled = TypedDict("Cancelled", {"type": Literal["cancelled"]})
Gap = TypedDict("Gap", {"type": Literal["gap"], "oid": int})
ProvideState = Union[InFlightOrder, Resting, Cancelled, Gap]


def side_to_int(side: Side) -> int:
    return 1 if side == "A" else -1


def side_to_uint(side: Side) -> int:
    return 1 if side == "A" else 0


class BasicAdder:
    def __init__(self, wallet: LocalAccount, api_url: str):
        self.info = Info(api_url)
        self.exchange = Exchange(wallet, api_url)

        subscription: L2BookSubscription = {"type": "l2Book", "coin": COIN}
        self.info.subscribe(subscription, self.on_book_update)
        self.info.subscribe(
            {"type": "userEvents", "user": wallet.address}, self.on_user_events)
        self.position: Optional[float] = None
        self.provide_state: Dict[Side, ProvideState] = {
            "A": {"type": "cancelled"},
            "B": {"type": "cancelled"},
        }
        self.recently_cancelled_oid_to_time: Dict[int, int] = {}
        self.poller = threading.Thread(target=self.poll)
        self.poller.start()

    def on_book_update(self, book_msg: L2BookMsg) -> None:
        logging.debug(f"book_msg {book_msg}")
        book_data = book_msg["data"]
        if book_data["coin"] != COIN:
            print("Unexpected book message, skipping")
            return
        for side in SIDES:
            book_price = float(book_data["levels"]
                               [side_to_uint(side)][0]["px"])
            ideal_distance = book_price * DEPTH
            ideal_price = book_price + (ideal_distance * (side_to_int(side)))
            logging.debug(
                f"on_book_update book_price:{book_price} ideal_distance:{ideal_distance} ideal_price:{ideal_price}"
            )

            provide_state = self.provide_state[side]
            best_bid = float(book_data["levels"][0][0]["px"])
            best_ask = float(book_data["levels"][1][0]["px"])
            if best_ask - best_bid > 2 * ideal_distance:
                if side == "A":
                    gap_price = best_bid + ideal_distance * 1.5
                elif side == "B":
                    gap_price = best_ask - ideal_distance * 1.5

                if provide_state["type"] != "cancelled":
                    oid = provide_state["oid"]
                    print(
                        f"cancelling order due to gap condition oid: {oid} side: {side} ideal_gap_price: {gap_price} best_ask: {best_ask} best_bid: {best_bid}, book_price: {book_price}")

                    response = self.exchange.cancel(COIN, oid)
                    if response["status"] == "ok":
                        self.recently_cancelled_oid_to_time[oid] = get_timestamp_ms(
                        )
                        self.provide_state[side] = {"type": "cancelled"}
                    else:
                        print(
                            f"Failed to cancel order {provide_state} {side}", response)

                if self.provide_state[side]["type"] == "cancelled":
                    if self.position is None:
                        logging.debug(
                            "Not placing an order because waiting for next position refresh")
                        continue
                    sz = MAX_POSITION + self.position * (side_to_int(side))
                    if sz * gap_price < 10:
                        logging.debug(
                            "Not placing an order because at position limit")
                        continue
                    px = float(f"{gap_price:.5g}")
                    print(f"placing gap order sz:{sz} px:{px} side:{side}")
                    self.provide_state[side] = {
                        "type": "in_flight_order", "time": get_timestamp_ms()}
                    response = self.exchange.order(COIN, side == "B", sz, px, {
                        "limit": {"tif": "Alo"}})
                    print("placed gap order", response)
                    if response["status"] == "ok":
                        status = response["response"]["data"]["statuses"][0]
                        if "resting" in status:
                            self.provide_state[side] = {
                                "type": "gap", "px": px, "oid": status["resting"]["oid"]}
                        else:
                            print(
                                "Unexpected response from placing gap order. Setting position to None.", response)
                            self.provide_state[side] = {"type": "cancelled"}
                            self.position = None

            if provide_state["type"] == "resting":
                distance = abs((ideal_price - provide_state["px"]))
                if distance > ALLOWABLE_DEVIATION * ideal_distance:
                    oid = provide_state["oid"]
                    print(
                        f"cancelling order due to deviation oid:{oid} side:{side} ideal_price:{ideal_price} px:{provide_state['px']}"
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
                if self.position is None:
                    logging.debug(
                        "Not placing an order because waiting for next position refresh")
                    continue
                sz = MAX_POSITION + self.position * (side_to_int(side))
                if sz * ideal_price < 10:
                    logging.debug(
                        "Not placing an order because at position limit")
                    continue
                # prices should have at most 5 significant digits
                px = float(f"{ideal_price:.5g}")
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
                if provide_state["type"] == "resting" or provide_state["type"] == "gap":
                    ok_oids.add(provide_state["oid"])

            for open_order in open_orders:
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
    BasicAdder(account, constants.MAINNET_API_URL)


if __name__ == "__main__":
    main()
