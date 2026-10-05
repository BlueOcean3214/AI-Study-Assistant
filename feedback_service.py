"""学习反馈统计：纯函数，供 API 层与 Agent Tool 层共用。

为什么单独拆出 calculate_summary：
- 它原本在 main.py 里，而 main.py 在导入时就执行 init_db() / migrate_json_to_db()，
  所以任何想复用它的一方（例如未来的 Agent Tool 层）只要 import main 就会产生数据库副作用。
- 本模块只依赖标准库，不依赖 FastAPI、数据库、LLM，纯函数、无副作用、不 import main。

口径与移动前（main.py 中的同名函数）完全一致：字段名、计算方式、四舍五入方式都不变。
"""


def calculate_summary(feedback_list):
    """统计学习反馈，返回计数字段与平均分钟数。"""

    total_count = len(feedback_list)

    if total_count == 0:
        return {
            "total_count": 0,
            "completed_count": 0,
            "partial_count": 0,
            "not_completed_count": 0,
            "too_difficult_count": 0,
            "average_estimated_minutes": 0,
            "average_actual_minutes": 0,
        }

    return {
        "total_count": total_count,
        "completed_count": sum(
            item["status"] == "completed"
            for item in feedback_list
        ),
        "partial_count": sum(
            item["status"] == "partial"
            for item in feedback_list
        ),
        "not_completed_count": sum(
            item["status"] == "not_completed"
            for item in feedback_list
        ),
        "too_difficult_count": sum(
            item.get("reason") == "任务难度太高"
            for item in feedback_list
        ),
        "average_estimated_minutes": round(
            sum(
                item["estimated_minutes"]
                for item in feedback_list
            ) / total_count,
            1
        ),
        "average_actual_minutes": round(
            sum(
                item["actual_minutes"]
                for item in feedback_list
            ) / total_count,
            1
        ),
    }
