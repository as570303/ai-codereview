# 预期发现：PERF-001 N+1 查询
from typing import List


def get_all_orders_with_users(db) -> List[dict]:
    orders = db.query("SELECT * FROM orders")
    result = []
    for order in orders:
        # 危险：N+1 查询，每次循环都执行一次查询
        user = db.query(f"SELECT * FROM users WHERE id = {order['user_id']}")
        result.append({"order": order, "user": user})
    return result


def calculate_total_prices(db) -> float:
    products = db.query("SELECT id FROM products")
    total = 0
    for product in products:
        # 危险：在循环中查询
        price = db.query(f"SELECT price FROM prices WHERE product_id = {product['id']}")
        total += price[0]["price"]
    return total


def send_notifications(db):
    users = db.query("SELECT id FROM users WHERE notify = 1")
    for user in users:
        # 危险：循环中多次 IO 调用
        prefs = db.query(f"SELECT * FROM preferences WHERE user_id = {user['id']}")
        messages = db.query(f"SELECT * FROM messages WHERE user_id = {user['id']}")
        print(f"Sending to {user['id']}: {len(messages)} messages")
