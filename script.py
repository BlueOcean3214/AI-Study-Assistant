"有一个 nums 列表 [3, 8, 5, 10, 2]，写一个函数 find_big(nums)，"
from unittest import result

"找出其中大于 5 的数字，最后返回这些数字。"
nums = [3, 8, 5, 10, 2]
def find_big(nums):
    if nums >= 5:
        return nums

"写一个函数 find_adults(users)，从我们之前的用户列表中找出年龄大于等于 18 岁的人，返回一个新列表。"
users = [
    {"name": "小明", "age": 20},
    {"name": "小红", "age": 17},
    {"name": "小王", "age": 22},
    {"name": "小李", "age": 16},
    {"name": "小张", "age": 19}
]

def find_adults(users):
    result = []
    for user in users:
        if user["age"] >= 18:
            result.append(f'{user["name"]}，{user["age"]}岁')
    return result
print(find_adults(users))