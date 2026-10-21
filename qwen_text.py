"""千问文字模型（DashScope OpenAI 兼容接口）：要一个 JSON 回答。记忆的两层提炼都用它。"""
from __future__ import annotations
import json
import logging
import time
from urllib import error, request

from addressee import ENDPOINTS

LOG = logging.getLogger('qwen-text')


class ModelMissing(RuntimeError):
    """这个账号/地域没有这个模型（404 model_not_found），换下一个候选模型。"""


class QwenText:
    def __init__(self, api_key: str, region: str = 'cn', timeout: float = 90):
        self.api_key = api_key
        self.url = ENDPOINTS.get(region, ENDPOINTS['cn'])
        self.timeout = timeout

    def chat_json(self, model: str, system: str, user: str, max_tokens: int = 2000) -> dict:
        body = {
            'model': model,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'response_format': {'type': 'json_object'},
            'temperature': 0,
            'max_tokens': max_tokens,
            'enable_thinking': False,
        }
        req = request.Request(self.url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers={
            'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            data = self._send(req)
        except error.HTTPError as exc:
            detail = exc.read()[:300].decode('utf-8', 'replace')
            if exc.code == 404 and 'model_not_found' in detail:
                raise ModelMissing(model) from None
            raise RuntimeError(f'HTTP {exc.code}: {detail}') from None
        content = data['choices'][0]['message']['content']
        usage = data.get('usage') or {}
        LOG.info('%s answered in %.1fs (tokens in %s, out %s)', model, time.monotonic() - started,
                 usage.get('prompt_tokens'), usage.get('completion_tokens'))
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError('model did not return a JSON object')
        return result

    def _send(self, req: request.Request) -> dict:
        """网络偶尔断（实测 RemoteDisconnected）：隔 3 秒再试一次。HTTP 错误不重试。"""
        for attempt in (1, 2):
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    return json.load(response)
            except error.HTTPError:
                raise
            except (error.URLError, OSError) as exc:
                if attempt == 2:
                    raise
                LOG.info('Qwen text request failed (%s); retrying once', exc)
                time.sleep(3)

    def chat_json_first(self, models: tuple[str, ...], system: str, user: str, max_tokens: int = 2000) -> tuple[str, dict]:
        """按顺序试候选模型，跳过账号里没有的；返回 (实际用的模型, 回答)。"""
        last: Exception | None = None
        for model in models:
            try:
                return model, self.chat_json(model, system, user, max_tokens)
            except ModelMissing as exc:
                LOG.info('Model %s is not available; trying the next one', exc)
                last = exc
        raise RuntimeError(f'none of the models are available: {models}') from last
