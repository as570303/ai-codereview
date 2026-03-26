# 预期发现：SEC-001 SQL 注入
import sqlite3


def get_user(username: str):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 危险：直接拼接用户输入
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


def login(username: str, password: str):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    # 危险：拼接多个参数
    sql = "SELECT id FROM users WHERE username='" + username + "' AND password='" + password + "'"
    cursor.execute(sql)
    return cursor.fetchone() is not None
