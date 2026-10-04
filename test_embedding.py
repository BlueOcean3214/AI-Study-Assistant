import requests


OLLAMA_URL = "http://localhost:11434/api/embeddings"


def get_embedding(text):

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": "bge-m3",
            "prompt": text
        }
    )

    data = response.json()

    return data["embedding"]


text1 = "定积分怎么学习"

text2 = "积分这一章应该如何复习"

text3 = "今天晚上吃什么"


v1 = get_embedding(text1)
v2 = get_embedding(text2)
v3 = get_embedding(text3)


print("向量长度:")
print(len(v1))


print("前10个数字:")
print(v1[:10])
import math


def cosine_similarity(a, b):

    dot = sum(
        x * y for x, y in zip(a, b)
    )

    norm_a = math.sqrt(
        sum(x * x for x in a)
    )

    norm_b = math.sqrt(
        sum(x * x for x in b)
    )

    return dot / (norm_a * norm_b)



print(
    "定积分 vs 积分复习:",
    cosine_similarity(v1, v2)
)


print(
    "定积分 vs 吃什么:",
    cosine_similarity(v1, v3)
)