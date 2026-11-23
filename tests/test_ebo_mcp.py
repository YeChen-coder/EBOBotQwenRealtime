"""engine/ebo_mcp.py 的工具（FastMCP in-memory client，不连真机器人）。
要 fastmcp：在 engine 容器里跑（Git Bash）：
  MSYS_NO_PATHCONV=1 docker exec ebo-qwen-engine mkdir -p /tmp/mcptest
  MSYS_NO_PATHCONV=1 docker cp engine/ebo_mcp.py ebo-qwen-engine:/tmp/mcptest/
  MSYS_NO_PATHCONV=1 docker cp tests/test_ebo_mcp.py ebo-qwen-engine:/tmp/mcptest/
  MSYS_NO_PATHCONV=1 docker exec -w /tmp/mcptest ebo-qwen-engine python test_ebo_mcp.py"""
import asyncio, json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
for candidate in (here.parent / 'engine', here):
    if (candidate / 'ebo_mcp.py').exists():
        sys.path.insert(0, str(candidate))
        break
os.environ['EBO_API_TOKEN'] = 'test-token'
import ebo_mcp
from fastmcp import Client

ebo_mcp.mcp.auth = None   # in-memory transport has no HTTP headers
robot = {'node': 'ebo', 'online': True, 'camera': 'on', 'state': {'charging': 'true', 'docked': 'true'}}
sent, assistant_calls = [], []


async def fake_robots():
    return [robot]


async def fake_cmd(node, suffix, payload=''):
    sent.append((node, suffix, payload))
    return 200


async def fake_assistant(method, path, **kw):
    assistant_calls.append((method, path, kw))
    if path == '/mcp/ask':
        return {'accepted': True, 'question': kw['json']['question'], 'expires_on': '2026-11-26'}
    if path == '/mcp/asks':
        return {'asks': [{'question': '晚饭吃了什么', 'status': 'asked'}]}
    return {'name': kw['params']['name'], 'times_seen': 3}


ebo_mcp._robots, ebo_mcp._cmd, ebo_mcp._assistant = fake_robots, fake_cmd, fake_assistant


def text(result):
    return result.content[0].text


async def main():
    async with Client(ebo_mcp.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert 'ebo_listen' not in tools
        for name in ('ebo_ask_mom', 'ebo_check_asks', 'ebo_seen', 'ebo_move', 'ebo_look'):
            assert name in tools, name
        assert tools['ebo_check_asks'].annotations.read_only_hint is True
        assert tools['ebo_seen'].annotations.read_only_hint is True
        assert not (tools['ebo_ask_mom'].annotations and tools['ebo_ask_mom'].annotations.read_only_hint)

        # 充电座检查真的生效（以前在 FastMCP 3+/4 上被悄悄跳过）
        ebo_mcp._last_look['ebo'] = __import__('time').time()
        r = await client.call_tool('ebo_move', {'direction': 'forward'})
        assert 'charging base' in text(r) and not sent, (text(r), sent)
        robot['state'] = {'charging': 'false', 'docked': 'false'}
        r = await client.call_tool('ebo_move', {'direction': 'forward', 'speed': 99, 'seconds': 9})
        assert sent and sent[-1][1] == 'move/vector', sent
        payload = json.loads(sent[-1][2])
        assert payload['ly'] == -ebo_mcp.MAX_SPEED and payload['hold'] == ebo_mcp.MAX_SECONDS, payload

        r = await client.call_tool('ebo_ask_mom', {'question': '晚饭吃了什么'})
        assert '晚饭吃了什么' in text(r) and assistant_calls[-1][:2] == ('POST', '/mcp/ask')
        r = await client.call_tool('ebo_check_asks', {})
        assert r.data['asks'][0]['status'] == 'asked'
        r = await client.call_tool('ebo_seen', {})
        assert assistant_calls[-1][2]['params'] == {'name': '妈妈', 'day': 'today'} and r.data['times_seen'] == 3

asyncio.run(main())
print('all ebo_mcp tests passed')
