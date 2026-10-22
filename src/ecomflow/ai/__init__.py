"""AI 辅助模块：自然语言问数、结果解读、产出交叉校验。"""

from .nl2sql import AIAnswer, SQLPlan, ask, execute_plan, guard_sql, plan_question
from .narrator import narrate
from .verify import verify_offline

__all__ = ["AIAnswer", "SQLPlan", "ask", "execute_plan", "guard_sql", "plan_question",
           "narrate", "verify_offline"]
