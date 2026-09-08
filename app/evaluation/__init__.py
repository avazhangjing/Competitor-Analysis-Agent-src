"""Agent 性能评估模块。

基于评估驱动开发(EDD)理念，对每次竞品分析自动计算多维度质量指标，
支持历史对比和迭代优化。
"""

from .evaluator import EvaluationResult, evaluate_analysis

__all__ = ["EvaluationResult", "evaluate_analysis"]
