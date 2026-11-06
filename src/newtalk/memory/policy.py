"""长期记忆素材资格和 MemOS 提取要求，不改写聊天原文。"""

import re
import unicodedata


EXTRACTION_POLICY_VERSION = 1
MEMORY_EXTRACTION_PROMPTS = {
    "detail_factual": (
        "仅提取有长期价值且有明确依据的用户事实，忽略寒暄、连接测试和纯设备控制确认。"
        "结合相同会话的前文理解指代；依据不足时不猜测。保留否定、更正和不确定性。"
        "区分用户陈述、助手建议及已验证结果；助手说已完成不代表工具实际执行成功。"
        "不要把助手建议、假设、试探性问题或尚未发生的计划提取为已发生事实。"
    ),
    "preference": (
        "提取用户明确表达或确认的偏好，结合前文理解简短确认；不能将助手建议视为用户偏好。"
        "保留否定与明确更正，新偏好变化应体现时间，不将一次活动推断为长期喜好。"
        "忽略寒暄、连接测试和纯设备控制；信息不足时不要推断。"
    ),
    "profile": (
        "仅提取有充分依据的稳定个人信息，遵守字段锁定及用户明确更正。"
        "不将一次经历、短期心情、助手建议、猜测或设备操作推断为稳定画像。"
        "结合会话理解指代，缺少依据时不要补全或猜测。"
    ),
    "event": (
        "结合会话前文整理同一事件的补充信息，保留参与者、时间、地点及有依据的结果。"
        "用消息 chat_time 理解相对时间，不用后台上传时间替代原始发言时间。"
        "不同日期的独立事件不要合并；同一事件的明确更正应保留更正后的事实。"
        "区分计划、建议和实际发生，助手口头确认不能证明动作成功。"
        "忽略寒暄、连接测试和纯设备控制；时间或指代不明时保留不确定性，不编造细节。"
    ),
}

_TEST_PHRASES = re.compile(
    r"(?:你好|您好|哈喽|hello|hi|hey|喂|在吗|在不在|"
    r"听得到吗|能听到吗|可以听到吗|听到我说话吗|听得到我说话吗|"
    r"能听到我说话吗|可以听到我说话吗|你能听到我说话吗)+"
)
_CONTROL_PHRASES = {
    "关闭麦克风", "关麦", "停止播放", "停止回复", "断开连接", "关闭摄像头",
}


def memory_skip_reason(text: str) -> str | None:
    # 只匹配完整的明确口令/问候，不能靠关键词或长度丢掉事实、否定与多轮确认。
    normalized = "".join(
        char for char in text.casefold()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    if normalized in _CONTROL_PHRASES:
        return "pure_device_control"
    if _TEST_PHRASES.fullmatch(normalized):
        return "pure_greeting_or_connection_test"
    return None
