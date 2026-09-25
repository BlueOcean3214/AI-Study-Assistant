import requests

response = requests.get(
    "https://jsonplaceholder.typicode.com/users/1"
)

print("状态码：", response.status_code)

data = response.json()

print("姓名：", data["name"])
print("邮箱：", data["email"])