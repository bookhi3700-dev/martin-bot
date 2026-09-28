"""대시보드 비밀번호 설정/변경:  python set_password.py"""
import getpass
import json
import os

from werkzeug.security import generate_password_hash

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "auth.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
while True:
    pw = getpass.getpass("새 대시보드 비밀번호 (8자 이상, 입력해도 화면에 안 보입니다): ")
    if len(pw) < 8:
        print("8자 이상으로 입력하세요.")
        continue
    if getpass.getpass("한 번 더 입력: ") != pw:
        print("두 번 입력한 비밀번호가 다릅니다.")
        continue
    break
with open(path, "w", encoding="utf-8") as f:
    json.dump({"hash": generate_password_hash(pw)}, f)
os.chmod(path, 0o600)
print("비밀번호를 저장했습니다.")
