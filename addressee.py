"""判断一句话是不是在对机器人说：让它不用唤醒词也能自然接话，同时不和电视对白、家人之间的聊天搭话。"""
from __future__ import annotations
import json
import logging
import re
import time
from urllib import error, request

LOG = logging.getLogger('ebo-addressee')

ENDPOINTS = {
    'cn': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
    'intl': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions',
}

PROMPT = """你在给家里的陪伴机器人做判断。机器人叫“{names}”，平时和家里的妈妈聊天、回答问题、帮忙查东西。
家里经常开着电视，麦克风会听到电视剧、新闻、综艺的对白，也会听到家人之间说话。

请判断下面这句话是不是家里的人在对机器人说话，比如：叫它、跟它打招呼、问它问题、请它帮忙、想和它聊天、回应它刚说的话，
也包括随口说给身边人听的家常话：说自己的感受和身体状况（“我腰有点疼”“好无聊啊”）、想不起来的事（“那个叫什么来着”）。
以下情况回答 no：电视/视频里的对白和台词、新闻播报、广告、歌词、明显在叫别的家人（“老公”“妈”）、打电话。
电视剧对白的特征：剧情冲突、人名称呼、戏剧化的语气，以及婚姻、离婚、财产、遗产、房产证、报警、开庭、复仇这类剧情话题——
这类话除非明确是在问机器人，否则一律 no。
如果给了“最近还听到的话”，它们和这句话连起来像一段电视剧/节目，就回答 no。
拿不准时：像老人日常随口说的家常话回答 yes；像剧情对白回答 no。

只回答 yes 或 no。"""


# “充个电”“充会儿电”中间隔字，用正则
DOCK_HINT = re.compile(r'充.{0,2}电|回窝|回座|回你的窝|回去歇|回去休息')

DOCK_PROMPT = """你在给家里的陪伴机器人做判断。机器人叫“{names}”（这是它的名字，不是在叫小孩），它有自己的充电座。
判断这句话是不是在让机器人现在回充电座、去充电或回去休息。
例如“你去充电吧”“没电了快回去充电”“{name}回窝去”“回去歇着吧”“去充个电” -> yes；
“你充电了吗”“还有多少电”“手机充电器在哪”“我去给手机充电”“等会儿再去充电”“别去充电”“先别回去，陪我聊会儿” -> no。
只回答 yes 或 no。"""


QUIET_HINT = re.compile(r'闭嘴|别说了|不要说了|别说话|别讲了|不需要你|不用你|安静|别吵|别烦|走开|少说|不想听')

QUIET_PROMPT = """你在给家里的陪伴机器人做判断。机器人叫“{names}”（这是它的名字）。
判断这句话是不是在让机器人别说话、安静下来、别打扰。
例如“你闭嘴”“别说了”“你别说了，烦死了”“不需要你”“安静点”“别吵了”“走开，别烦我”“{name}你别说话了” -> yes；
“别说了，太好笑了”“我闭嘴就是了”“他让我别说了”“这个不需要你帮忙查了，我自己看”“你安静地陪着我就行”“不用你提醒，我记得” -> no。
只回答 yes 或 no。"""


class AddresseeClassifier:
    def __init__(self, api_key: str, region: str, model: str, names: tuple[str, ...], timeout: float = 5):
        self.api_key = api_key
        self.url = ENDPOINTS.get(region, ENDPOINTS['cn'])
        self.model = model
        self.timeout = timeout
        self.system = PROMPT.format(names='”或“'.join(names) if names else '猫猫')
        self.quiet_system = QUIET_PROMPT.format(names='”或“'.join(names) if names else '猫猫',
                                                name=names[-1] if names else '猫猫')
        self.dock_system = DOCK_PROMPT.format(names='”或“'.join(names) if names else '猫猫',
                                              name=names[-1] if names else '猫猫')

    def _ask_yes_no(self, system: str, content: str, what: str) -> bool | None:
        body = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}],
            'max_tokens': 3,
            'temperature': 0,
            'enable_thinking': False,
        }
        req = request.Request(self.url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers={
            'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                answer = json.load(response)['choices'][0]['message']['content'].strip().lower()
        except (error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
            LOG.warning('%s check failed: %s', what, exc)
            return None
        result = answer.startswith('y')
        LOG.info('%s %s (%.1fs): %r', what, 'YES' if result else 'no', time.monotonic() - started, content[-60:])
        return result

    def is_addressed(self, text: str, last_reply: str = '', recent: tuple[str, ...] = ()) -> bool | None:
        """True/False；调用失败返回 None，由调用方决定怎么处理。recent：之前一分钟内听到的话（电视对白是连续的）。"""
        lines = []
        if recent:
            lines.append('最近还听到的话（按时间先后）：' + ' / '.join(recent))
        if last_reply:
            lines.append(f'机器人刚才说：{last_reply}')
        lines.append(f'这句话：{text}')
        return self._ask_yes_no(self.system, '\n'.join(lines), 'addressee')

    def wants_quiet(self, text: str) -> bool | None:
        """是不是在让机器人闭嘴/安静。只对含提示词的话调用（见 may_be_quiet_request）。"""
        return self._ask_yes_no(self.quiet_system, f'这句话：{text}', 'quiet-request')

    def wants_to_dock(self, text: str) -> bool | None:
        """是不是在让机器人回充电座。只对含提示词的话调用（见 may_be_dock_request）。"""
        return self._ask_yes_no(self.dock_system, f'这句话：{text}', 'dock-request')


def may_be_quiet_request(text: str) -> bool:
    return bool(QUIET_HINT.search(text))


def may_be_dock_request(text: str) -> bool:
    """本地快速筛选：不含这些词的话不可能是让它去充电，不必调用模型。"""
    return bool(DOCK_HINT.search(text))
