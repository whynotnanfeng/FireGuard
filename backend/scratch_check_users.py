import sqlite3
import os

db_path = r'e:\DetPlatform\backend\data\fire_detection.db'
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    cursor.execute("SELECT id, username FROM users")
    users = cursor.fetchall()
    print("Users in database:")
    for user in users:
        print(f"ID: {user[0]}, Username: {user[1]}")
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
