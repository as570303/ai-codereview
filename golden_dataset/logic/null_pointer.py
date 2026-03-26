# 预期发现：LOGIC-001 空值未检查
from typing import Optional


def get_user_email(user_id: int) -> str:
    user = find_user(user_id)
    # 危险：未检查 user 是否为 None
    return user.email


def find_user(user_id: int) -> Optional[object]:
    # 可能返回 None
    return None


def process_order(order_id: int):
    order = fetch_order(order_id)
    # 危险：链式访问未做 None 检查
    total = order.items[0].price * order.quantity
    return total


def fetch_order(order_id: int):
    return None


def divide(a: int, b: int) -> float:
    # 危险：未检查除零
    return a / b
