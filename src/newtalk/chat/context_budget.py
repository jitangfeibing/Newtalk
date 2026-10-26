"""发送边界上的完整请求预算；裁剪请求副本，不修改原始 Dialogue。"""

import json
from typing import Any
from newtalk.chat.prompts import MEMORY_CONTEXT_PREFIX

TOKEN_ESTIMATOR = "utf8_conservative_estimate"


class ContextBudgetExceeded(ValueError):
    def __init__(self, decision: dict[str, Any]) -> None:
        super().__init__("System context and current input exceed the input budget")
        self.decision = decision


def _cost(value: Any) -> int:
    # 没有与供应商匹配的 Tokenizer，使用 UTF-8 长度和结构余量做保守估算。
    # 这不是官方 Token 数，也不是模型容量的严格证明；看板必须显示估算方式。
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 8


def estimate_request_tokens(request: dict[str, Any]) -> int:
    return _cost({key: value for key, value in request.items() if key != "messages"}) + sum(
        _cost(message) for message in request["messages"]
    ) + 32


def fit_request(
    request: dict[str, Any],
    *,
    limit: int,
    source_turn_ids: list[str | None],
    memory_context_indices: set[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if limit <= 0:
        raise ValueError("Input token budget must be positive")
    messages = [dict(message) for message in request["messages"]]
    current = max(index for index, message in enumerate(messages) if message["role"] == "user")
    costs = [_cost(message) for message in messages]
    before = estimate_request_tokens(request)
    total = before
    tool_results_truncated: list[str] = []
    memory_indices = memory_context_indices or set()
    memory_context_truncated = False
    for index in sorted(memory_indices):
        if total <= limit:
            break
        replacement = {**messages[index], "content": MEMORY_CONTEXT_PREFIX + json.dumps({
            "status": "truncated", "memories": [], "reason": "input_budget",
            "message": "Memory candidates omitted to fit the input budget; not a no-match result.",
        }, separators=(",", ":"))}
        cost = _cost(replacement)
        if cost < costs[index]:
            total += cost - costs[index]
            messages[index] = replacement
            costs[index] = cost
            memory_context_truncated = True

    # 第二轮不能靠一个很大的检索结果挤掉原对话；保留调用/结果配对，用合法
    # JSON 明示结果未提供，而不是截断成无效 JSON 或伪装成检索未命中。
    memory_calls = {
        call["id"] for message in messages[current + 1:]
        for call in message.get("tool_calls", [])
        if call["function"]["name"] == "memory_search"
    }
    for index in range(current + 1, len(messages)):
        message = messages[index]
        if total <= limit:
            break
        if message["role"] != "tool" or message.get("tool_call_id") not in memory_calls:
            continue
        replacement = {**message, "content": json.dumps({
            "status": "truncated", "memories": [], "reason": "input_budget",
            "message": "Memory results omitted to fit the input budget; this is not a no-match result.",
        }, separators=(",", ":"))}
        cost = _cost(replacement)
        if cost < costs[index]:
            total += cost - costs[index]
            messages[index] = replacement
            costs[index] = cost
            tool_results_truncated.append(message["tool_call_id"])

    # 按用户输入分组淘汰最旧历史；一次移除整个组，不能留下孤立的助手回答
    # 或 tool 结果。连续 user 消息（被打断的问句）各自也是一个完整组。
    groups: list[list[int]] = []
    for index, message in enumerate(messages[:current]):
        if message["role"] == "system" or index in memory_indices:
            continue
        if message["role"] == "user" or not groups:
            groups.append([])
        groups[-1].append(index)
    removed: set[int] = set()
    for group in groups:
        if total <= limit:
            break
        removed.update(group)
        total -= sum(costs[index] for index in group)
    kept = [index for index in range(len(messages)) if index not in removed]
    result = {**request, "messages": [messages[index] for index in kept]}

    def ids(indices):
        return list(dict.fromkeys(source_turn_ids[index] for index in indices
                                  if source_turn_ids[index] is not None))

    decision = {
        "estimator": TOKEN_ESTIMATOR, "is_estimate": True, "input_token_limit": limit,
        "before_tokens_estimate": before, "after_tokens_estimate": total,
        "included_history_turn_ids": ids(index for index in kept if index < current),
        "omitted_history_turn_ids": ids(sorted(removed)),
        "omitted_history_groups": sum(bool(set(group) & removed) for group in groups),
        "included_history_groups": sum(not bool(set(group) & removed) for group in groups),
        "tool_results_truncated": tool_results_truncated,
        "memory_context_truncated": memory_context_truncated,
        "components": {
            "system_and_profile": sum(costs[i] for i in kept if messages[i]["role"] == "system"),
            "history": sum(costs[i] for i in kept if i < current and messages[i]["role"] != "system" and i not in memory_indices),
            "preloaded_memory": sum(costs[i] for i in memory_indices),
            "current_input": costs[current],
            "tool_exchange": sum(costs[i] for i in kept if i > current),
            "request_and_tool_definitions": total - sum(costs[i] for i in kept),
        },
    }
    if total > limit:
        raise ContextBudgetExceeded(decision)
    return result, decision
