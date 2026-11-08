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
家人还会用昵称叫它，比如“小人儿”（语音识别常写成“小人”）、“小家伙”“宝贝”。
妈妈跟它说话时常用“妈妈”自称（“妈妈回来了”“你怎么不跟妈妈问好”“快叫妈妈”），这是在跟机器人说话，回答 yes；
带“你”、像在叫它、问它、逗它、责怪它没反应的话（“你怎么不理我”“你跟妈妈问好呀”）也回答 yes。
以下情况回答 no：电视/视频里的对白和台词、新闻播报、广告、歌词、明显在叫别的家人（“老公”“爸”）、打电话。
电视剧对白的特征：剧情冲突、人名称呼、戏剧化的语气，以及婚姻、离婚、财产、遗产、房产证、报警、开庭、复仇这类剧情话题——
这类话除非明确是在问机器人，否则一律 no。
如果给了“最近还听到的话”，它们和这句话连起来像一段电视剧/节目，就回答 no。
如果给了“机器人刚才说”，说明家人正在和机器人聊天。这时带着“我跟你说”“跟你讲”“告诉你”，给它讲自己今天的事、工作上的事
（“今天我跟你说，我今天做了什么”“我跟你讲，客户提的问题我都解决了”），回答 yes；语音识别在长句上常漏字、出错，后面说得乱也算。
没有这类说法的，还按上面的规则判断。
拿不准时：像老人日常随口说的家常话回答 yes；像剧情对白回答 no。

这句话来自语音识别，常有同音错别字（比如“充电”被识别成“空店”“通店”“冲店”“充店”），请按读音理解。
只回答 yes 或 no。"""


# “充个电”“充会儿电”中间隔字，用正则
# 实测用户只说“嗯，回去吧”，没有“充电”两个字，以前根本不判断，模型却答应了“我这就回去充电”
# 实测 Whisper 把“回去充电”听成“回去空店”“回去通店”：按读音放宽（chong/kong/tong/cong + dian）
DOCK_HINT = re.compile(r'[充冲重虫空通葱从][^，。,.]{0,2}[电店点垫甸]|回窝|回座|回你的窝|回去|回家')

DOCK_PROMPT = """你在给家里的陪伴机器人做判断。机器人叫“{names}”（这是它的名字，不是在叫小孩），它有自己的充电座。
判断这句话是不是在让机器人现在回充电座、去充电或回去休息。
例如“你去充电吧”“没电了快回去充电”“{name}回窝去”“回去歇着吧”“去充个电”“回去吧”“嗯，回去吧”“你回去吧”“回家去吧” -> yes；
“你充电了吗”“还有多少电”“手机充电器在哪”“我去给手机充电”“等会儿再去充电”“别去充电”“先别回去，陪我聊会儿”
“我先回去了”“我回家了”“回去再说”“他回去了吗”“回家路上堵车” -> no。
（“回去吧”这类话，没有别的对象时就是在让机器人回去。）
这句话来自语音识别，常有同音错别字（比如“充电”被识别成“空店”“通店”“冲店”“充店”），请按读音理解。
只回答 yes 或 no。"""


# 爸妈嫌它吵时常说“你好烦呐”“吵死了”“不要说话”：这些也放进来，是不是在说机器人交给模型判断
QUIET_HINT = re.compile(r'闭嘴|(别|甭|不要)再?(说了|说话|讲了|讲话|出声|吭声|吱声)|'
                        r'不需要你|不用你|安静|别吵|吵死|好吵|太吵|真吵|别烦|好烦|真烦|烦死|烦人|烦不烦|'
                        r'走开|少说|不想听|别啰嗦|少啰嗦|别叨叨|少废话')

QUIET_PROMPT = """你在给家里的陪伴机器人做判断。机器人叫“{names}”（这是它的名字）。
判断这句话是不是在让机器人别说话、安静下来、别打扰。
例如“你闭嘴”“别说了”“你别说了，烦死了”“不需要你”“安静点”“别吵了”“走开，别烦我”“{name}你别说话了”
“不要说话”“你好烦呐”“你好烦啊”“烦死了，别说了”“吵死了”“你烦不烦啊”“甭说了”“哎呀，甭说话了，一会儿你再说” -> yes；
（嫌机器人烦、嫌它吵，就是让它别说话。）
“别说了，太好笑了”“我闭嘴就是了”“他让我别说了”“这个不需要你帮忙查了，我自己看”“你安静地陪着我就行”“不用你提醒，我记得”
“我今天好烦”“上班烦死了”“楼上装修吵死了”“这个人真烦” -> no。
这句话来自语音识别，常有同音错别字（比如“充电”被识别成“空店”“通店”“冲店”“充店”），请按读音理解。
只回答 yes 或 no。"""


# 模型常常自己答应安静（“好，那我就在这儿安静待着”），程序不知道，会话就一直挂到空闲超时
PROMISE_HINT = re.compile(r'安静|不说了|不说话|不出声|不吭声|不吵|闭嘴|不打扰')

PROMISE_PROMPT = """你在给家里的陪伴机器人做判断。下面是机器人刚说的话。
判断它是不是已经答应了从现在起不说话、安静待着、不打扰对方，等对方叫它再说。
例如“好，那我就在这儿安静待着。”“行，那我先安静待着，等你忙完再聊。”“好嘞，那就不说了。你歇会儿，别太累着。”
“好嘞，我不打扰你。快闭上眼睛睡吧，晚安。”“行行行，我闭嘴。”“没关系，不想说就不说，我陪着你安静一会儿就好。”
“好嘞，那我不打扰你看剧了，有事随时喊我。”（“有事叫我”就是在等对方叫它） -> yes；
“要是您嫌吵，我就安静点”（只是假设）“你想聊点别的，还是安静待会儿？”（在问对方）“家里挺安静的，你想让我帮你看看吗”（在说别的）
“不说话也行，你想到什么说什么，我听着呢”（还在等对方往下说）“一个人待着也挺自在的，正好能安安静静歇会儿。今天降温了，你裹暖和点”（在聊天） -> no。
只回答 yes 或 no。"""


FAMILY_TITLES = {'妈妈', '爸爸', '姐姐'}


class AddresseeClassifier:
    def __init__(self, api_key: str, region: str, model: str, names: tuple[str, ...], timeout: float = 5):
        self.api_key = api_key
        self.url = ENDPOINTS.get(region, ENDPOINTS['cn'])
        self.model = model
        self.timeout = timeout
        # “妈妈”也是唤醒词（妈妈常用“妈妈”自称跟它说话），但不是机器人的名字，不能写进“机器人叫……”
        names = tuple(n for n in names if n not in FAMILY_TITLES)
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

    def promised_quiet(self, reply: str) -> bool | None:
        """机器人刚说的话是不是答应了安静。只对含提示词的话调用（见 may_promise_quiet）。"""
        return self._ask_yes_no(PROMISE_PROMPT, f'机器人说：{reply}', 'quiet-promise')

    def wants_to_dock(self, text: str, last_reply: str = '') -> bool | None:
        """是不是在让机器人回充电座。只对含提示词的话调用（见 may_be_dock_request）。"""
        content = f'机器人刚才说：{last_reply}\n这句话：{text}' if last_reply else f'这句话：{text}'
        return self._ask_yes_no(self.dock_system, content, 'dock-request')


def may_be_quiet_request(text: str) -> bool:
    return bool(QUIET_HINT.search(text))


def may_promise_quiet(reply: str) -> bool:
    return bool(PROMISE_HINT.search(reply))


def may_be_dock_request(text: str) -> bool:
    """本地快速筛选：不含这些词的话不可能是让它去充电，不必调用模型。"""
    return bool(DOCK_HINT.search(text))
