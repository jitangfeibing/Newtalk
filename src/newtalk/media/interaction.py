import re


def playback_stop_from_text(text: str) -> str | None:
    """纯播放只接受完整退出口令，不把引用、否定或疑问中的词当指令。"""
    command = text.strip().rstrip('。.!！').strip()
    if command in {'停', '停一下', '先停一下'}:
        return 'pause'
    match = re.fullmatch(
        r'(?:请)?(?:帮我)?(?:先)?(暂停|停止)(?:播放)?(?:一下)?'
        r'(?:音乐|歌曲|节目|听书|音频|这首歌|这期节目)?(?:一下)?', command)
    if match:
        return 'pause' if match[1] == '暂停' else 'stop'
    if re.fullmatch(r'(?:请)?(?:帮我)?(?:先)?(?:关闭|关掉)(?:音乐|歌曲|节目|听书|音频)', command):
        return 'stop'
    return None
