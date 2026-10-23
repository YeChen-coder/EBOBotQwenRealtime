"""唤醒词容错匹配：按读音比，不按字比。

本地 Whisper 常把同音字写错（“猫猫”写成“毛毛”“茂茂”，“小朋友”写成“小盆友”“晓朋友”），
家人发音也不一定标准（平翘舌、前后鼻音、n/l 不分）。这里把文本和唤醒词都转成拼音、
抹平这些口音差异后再比；每个字最多差一点，整个词只允许差一处，避免放得太宽。
"""
from __future__ import annotations
from functools import lru_cache
import re

from pypinyin import lazy_pinyin

# 常见的其他叫法（读音不同，但明显是在叫它）
ALIASES = {
    '猫猫': ('猫咪', '小猫', '喵喵'),
    '小朋友': ('小盆友',),
    # Whisper 偶尔把“狗狗”写成英文；“小机器人”说快了常被截成“小机器”
    '狗狗': ('gogo',),
    '小机器人': ('小机器',),
    # “小人儿”的“儿”很轻，语音识别几乎总是只写“小人”
    '小人儿': ('小人',),
}

# 含别名、但说的不是它的词：先去掉再找别名（电视剧里“卑鄙小人”很常见）
ALIAS_EXCLUDE = ('卑鄙小人', '阴险小人', '奸诈小人', '势利小人', '真小人', '伪小人', '小人物', '小人书',
                 '小人国', '小人得志', '小人之心', '防小人', '犯小人', '小人长戚戚', '远小人', '是小人', '个小人', '那小人', '这小人')

# 读音只差一点、但本身是常用词的：不算叫它（妈妈是老师，“高二”“勾股”在家里很常见）
CONFUSABLE = {
    '狗儿': ('高二', '高儿', '够二'),
    '狗狗': ('勾股', '够格'),
    # “我的妈呀”是感叹；“大妈”是别人；“马马虎虎”
    '妈妈': ('妈呀', '大妈', '马马', '码码'),
}

_INITIALS = (('zh', 'z'), ('ch', 'c'), ('sh', 's'))


def _norm(syllable: str) -> str:
    """抹平常见口音差异：平翘舌、前后鼻音、n/l。"""
    for a, b in _INITIALS:
        if syllable.startswith(a):
            syllable = b + syllable[len(a):]
            break
    if syllable.startswith('n'):
        syllable = 'l' + syllable[1:]
    for a, b in (('ing', 'in'), ('eng', 'en'), ('ang', 'an')):
        if syllable.endswith(a):
            syllable = syllable[: -len(a)] + b
            break
    return syllable


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@lru_cache(maxsize=64)
def _syllables(word: str) -> tuple[str, ...]:
    return tuple(_norm(s) for s in lazy_pinyin(word))


def _hanzi(text: str) -> str:
    return ''.join(re.findall(r'[一-鿿]', text or ''))


def matches(text: str, wake_words: tuple[str, ...]) -> bool:
    """text 里有没有（读音近似的）唤醒词或其常见叫法。"""
    compact = ''.join(re.findall(r'[\w一-鿿]', text or '')).lower()
    if any(w in compact for w in wake_words):
        return True
    chars = _hanzi(text)
    if not chars:
        return False
    spoken = tuple(_norm(s) for s in lazy_pinyin(chars))
    for word in wake_words:
        # 别名只认原字：读音放宽到别名上太宽（实测“毛衣”“猫是”都和“猫咪”只差一个字母）
        stripped = compact
        for phrase in ALIAS_EXCLUDE:
            stripped = stripped.replace(phrase, '')
        if any(alias in stripped for alias in ALIASES.get(word, ())):
            return True
        target = _syllables(word)
        k = len(target)
        for start in range(len(spoken) - k + 1):
            window = spoken[start:start + k]
            dists = [_lev(a, b) for a, b in zip(window, target)]
            # 每个字最多差一点，整个词只允许差一处
            if max(dists) <= 1 and sum(dists) <= 1 and chars[start:start + k] not in CONFUSABLE.get(word, ()):
                return True
    return False
