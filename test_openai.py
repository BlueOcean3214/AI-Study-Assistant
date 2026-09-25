import requests

response = requests.post(
    "http://localhost:11434/api/chat",
    json={
        "model": "qwen3.5:4b",
        "messages": [
            {
                "role": "user",
                "content": """
请告诉我一个学生的信息。
必须返回 JSON。
字段：
name：姓名
age：年龄
major：专业
"""
            }
        ],
        "format": "json",
        "stream": False,
        "think": False
    }
)

data = response.json()

print(data["message"]["content"])