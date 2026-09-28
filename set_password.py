"""대시보드 비밀번호 설정/변경:  python set_password.py"""
import getpass
import json
import os

from werkzeug.security import generate_password_hash

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "auth.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
while True:
    pw = getpass.getpass("New dashboard password (8+ chars, hidden while typing): ")
    if len(pw) < 8:
        print("Must be at least 8 characters.")
        continue
    if getpass.getpass("Type it again: ") != pw:
        print("Passwords do not match. Try again.")
        continue
    break
with open(path, "w", encoding="utf-8") as f:
    json.dump({"hash": generate_password_hash(pw)}, f)
os.chmod(path, 0o600)
print("Password saved.")
