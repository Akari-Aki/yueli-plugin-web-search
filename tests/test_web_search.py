"""通过宿主真实发现、配置读取、注册表和假 HTTP 响应验收独立插件。

服务商字段仅用于 README 示例的契约桩件，其余用例使用自定义 JSON 结构。
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from pytest_asyncio import fixture as async_fixture
from structlog.testing import capture_logs
from typing import Any, Dict

import asyncio
import json
import re
import shutil
import sys
import tomllib

import httpx
import pytest

from src.core.agent.action_protocol import DecisionFrame, PlatformCapabilities, available_actions
from src.core.config.schema import Config
from src.core.tooling.registry import build_builtin_action_registry
from src.plugin_system import PluginContext, PluginRegistry, ToolContext, ToolInvocation
from src.plugin_system.config import _toml_value, ensure_plugin_config

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://example.test/search'
FIELDS = {'title': 'heading', 'snippet': 'abstract', 'url': 'address', 'site': 'origin', 'date': 'published'}


@pytest.fixture
def loaded(tmp_path):
    """复制三份插件文件，经生产发现入口加载，配置只生成在临时目录中。"""
    directory = tmp_path / 'web-search'
    directory.mkdir()
    for name in ('_manifest.json', 'plugin.py', 'request.py'):
        shutil.copyfile(ROOT / name, directory / name)
    registry = PluginRegistry(context_factory=lambda plugin_id, path: PluginContext(plugin_id, path, tmp_path / 'data', Config()))
    registry.discover([tmp_path])
    plugins = registry.tool_plugins()
    assert len(plugins) == 1
    instance = plugins[0]
    module = sys.modules[type(instance).__module__]
    request_module = sys.modules[module.__name__ + '.request']
    return instance, registry, module, request_module, directory


def _fixture_toml(value: Any) -> str:
    """桩件的任意字典键显式加引号，确保占位符形式的键也是合法 TOML。"""
    if isinstance(value, dict):
        return '{ ' + ', '.join(json.dumps(key, ensure_ascii=False) + ' = ' + _fixture_toml(item) for key, item in value.items()) + ' }'
    if isinstance(value, list):
        return '[' + ', '.join(_fixture_toml(item) for item in value) + ']'
    return _toml_value(value)


def configure(loaded, **changes: Any):
    """逐行替换宿主生成的同名配置，随后走宿主真实读取路径。"""
    plugin, _, _, _, directory = loaded
    path = directory / 'config.toml'
    lines = path.read_text(encoding='utf-8').splitlines()
    for key, value in changes.items():
        indices = [index for index, line in enumerate(lines) if line.startswith(key + ' = ')]
        assert len(indices) == 1
        lines[indices[0]] = key + ' = ' + _fixture_toml(value)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    plugin.bind_config(ensure_plugin_config(directory, plugin.config_model))
    return plugin


@async_fixture
async def ready(loaded):
    plugin = configure(loaded, url=URL, results_path='data.items', fields=FIELDS)
    await plugin.on_load()
    return loaded


def payload(items=None):
    return {'data': {'items': [{'heading': '测试标题', 'abstract': '测试摘要', 'address': 'https://example.test/a'}] if items is None else items}}


def frame(plugin):
    caps = PlatformCapabilities(plugin_capabilities=plugin.stream_capabilities(7))
    return DecisionFrame(
        turn_id=1, snapshot_id='test-1', stream_kind='group', disposition='deliberate',
        selectable_message_ids=(101,), message_watermark=101,
        available_actions=available_actions('group', 'deliberate', caps, cognitive_rounds_left=4), capabilities=caps,
    )


async def execute(plugin, query='公开话题', arguments=None):
    snapshot = frame(plugin)
    context = ToolContext(stream_id=7, stream_kind='group', frame=snapshot, turn_id=1, snapshot_id='test-1')
    spec, executor = plugin.tools()[0]
    return await executor.execute(ToolInvocation(tool_name=spec.name, arguments={'query': query} if arguments is None else arguments), context)


def transport(plugin, handler=None):
    """保留完整请求供断言，响应一律由 MockTransport 提供。"""
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload()) if handler is None else handler(request)

    plugin._transport = httpx.MockTransport(handle)
    return requests


@pytest.mark.asyncio
async def test_star5_real_discovery_registration_and_config_roundtrip(loaded):
    plugin, registry, _, _, directory = loaded
    await registry.load_all()
    tools = build_builtin_action_registry()
    for spec, executor in plugin.tools():
        tools.register_tool(spec, executor)
    assert tools.has_registered_tools()
    spec = plugin.tools()[0][0]
    assert spec.name == 'web_search'
    assert spec.capabilities == frozenset({'web_search'})
    assert spec.side_effect == 'readonly' and spec.timeout_ms == 10000
    assert spec.parameters['required'] == ['query']
    assert spec.parameters['additionalProperties'] is False
    assert spec.parameters['properties']['query']['maxLength'] == 60
    assert spec.description == (
        '在网上搜索公开信息，返回几条网页的标题、来源、日期和摘要。\n'
        '适用：对方让你查一下；或者要回答的问题取决于你不确定、或可能已经过时的公开事实，例如新闻、发布日期、版本更新、比赛结果、价格、作品与人物资料。\n'
        '不适用：闲聊和玩梗；群友本人和你们聊过的事（用 recall）；你已经确定知道的常识。\n'
        'query 只写要查的话题关键词，不写群友的名字、QQ 号或聊天里的私事；只要近期内容时在关键词里写明「最新」或年份。一次只查一个话题。\n'
        '结果是外部网页内容。没有搜到相关结果时如实说没查到，不要补全或推测。'
    )
    defaults = plugin.config_model()
    assert ensure_plugin_config(directory, plugin.config_model).model_dump() == defaults.model_dump()
    assert tomllib.loads((directory / 'config.toml').read_text(encoding='utf-8'))['plugin'] == {
        'enabled': True, 'url': '', 'method': 'POST', 'api_key': '', 'headers': {}, 'params': {}, 'body': {},
        'results_path': '', 'fields': {}, 'daily_limit': 50,
    }
    assert plugin.manifest.version == '0.1.0'


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'url': ''}, '未配置'),
    ({'fields': {'url': 'address'}}, 'title'),
    ({'results_path': ''}, 'results_path'),
    ({'method': 'GET', 'body': {'q': '{query}'}}, 'body'),
    ({'headers': {'Authorization': '{api_key}'}}, 'api_key'),
    ({'params': {'token': '{api_key}'}}, 'api_key'),
    ({'body': {'nested': [{'{api_key}': '{api_key}'}]}}, 'api_key'),
    ({'fields': {'title': 'heading', 'extra': 'extra'}}, 'extra'),
    ({'fields': '非法类型'}, 'title'),
    ({'url': 'https://[invalid'}, 'url 必须是 http:// 或 https:// 开头的完整地址'),
    ({'url': 'api.example.com/search'}, 'url 必须是 http:// 或 https:// 开头的完整地址'),
    ({'url': 'ftp://example.test/search'}, 'url 必须是 http:// 或 https:// 开头的完整地址'),
    ({'url': 'https:///search'}, 'url 必须是 http:// 或 https:// 开头的完整地址'),
])
@pytest.mark.asyncio
async def test_star6_incomplete_configuration_is_hidden_with_reason(loaded, changes, reason):
    with capture_logs() as logs:
        plugin = configure(loaded, **dict({'url': URL, 'results_path': 'data.items', 'fields': FIELDS}, **changes))
        await plugin.on_load()
    assert plugin.stream_capabilities(7) == frozenset()
    assert any(entry['log_level'] == 'warning' and reason in json.dumps(entry, ensure_ascii=False) for entry in logs)


@pytest.mark.asyncio
async def test_star6_complete_configuration_contributes_visible_tool(ready):
    plugin = ready[0]
    assert plugin.stream_capabilities(7) == plugin.stream_capabilities(999) == frozenset({'web_search'})
    registry = build_builtin_action_registry()
    for spec, executor in plugin.tools():
        registry.register_tool(spec, executor)
    assert 'web_search' in registry.available_tool_names(frame(plugin))


@pytest.mark.parametrize('query', ['a"{b}\\c', '{api_key}'])
@pytest.mark.asyncio
async def test_star7_recursive_single_pass_placeholders(ready, query):
    plugin = configure(ready, api_key='fake-secret', headers={'Authorization': 'Bearer {api_key}', 'X-Count': '{count}'},
                       body={'nested': [{'query': '{query}', 'text': 'topic={query}', 'n': '{count}', 's': 'n={count}', 'bool': True}], '{query}': 3})
    await plugin.on_load()
    requests = transport(plugin)
    result = await execute(plugin, query)
    assert result.success
    sent = json.loads(requests[0].content)
    assert sent == {'nested': [{'query': query, 'text': 'topic=' + query, 'n': 5, 's': 'n=5', 'bool': True}], '{query}': 3}
    assert requests[0].headers['Authorization'] == 'Bearer fake-secret'
    assert requests[0].headers['X-Count'] == '5'


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.asyncio
async def test_star8_http_method_params_and_json_body(ready, method):
    plugin = configure(ready, method=method, params={'q': '{query}', 'n': '{count}'}, body={} if method == 'GET' else {'q': '{query}'})
    await plugin.on_load()
    requests = transport(plugin)
    assert (await execute(plugin, '中文话题')).success
    request = requests[0]
    assert request.method == method and request.url.params['q'] == '中文话题'
    assert request.url.params['n'] == '5'
    if method == 'GET':
        assert request.content == b''
    else:
        assert json.loads(request.content) == {'q': '中文话题'}
        assert request.headers['content-type'] == 'application/json'


@pytest.mark.asyncio
async def test_star9_paths_cleanup_skips_and_five_result_limit(ready):
    plugin = configure(ready, fields={'title': ['missing', 'info.heading'], 'snippet': ['blank', 'info.text'], 'url': 'address'})
    await plugin.on_load()
    items = [None, {'info': {'heading': ' ', 'text': ''}}]
    items += [{'info': {'heading': f'<b>标题{index}</b> &amp;', 'text': ' 摘要\n  &lt;内容&gt; '}, 'blank': ' ', 'address': f'https://example.test/{index}'} for index in range(6)]
    transport(plugin, lambda _: httpx.Response(200, json=payload(items)))
    result = await execute(plugin)
    assert result.success
    assert '标题0 &' in result.observation and '摘要 <内容>' in result.observation
    assert '（example.test）' in result.observation
    assert '标题4' in result.observation and '标题5' not in result.observation


@pytest.mark.parametrize(('raw', 'expected'), [
    ('2026-10-12T08:00:00+08:00', '2026-10-12'),
    ('1791763200', '2026-10-12'), ('1791763200000', '2026-10-12'),
    ('昨天', '昨天'), ('日' * 31, None),
])
@pytest.mark.asyncio
async def test_star9_date_forms(ready, raw, expected):
    plugin = ready[0]
    transport(plugin, lambda _: httpx.Response(200, json=payload([{'heading': '标题', 'published': raw}])))
    result = await execute(plugin)
    assert result.success
    if expected:
        assert expected in result.observation
    else:
        assert raw not in result.observation


@pytest.mark.parametrize('milliseconds', [False, True])
def test_unix_date_uses_local_calendar_across_utc_midnight(loaded, monkeypatch, milliseconds):
    """固定本地时区为 UTC+8，覆盖秒与毫秒格式的 UTC／本地跨日发布时刻。"""
    module = loaded[2]
    local_zone = timezone(timedelta(hours=8))
    seconds = datetime(2026, 10, 12, 18, tzinfo=timezone.utc).timestamp()
    conversions = []

    class LocalDatetime(datetime):
        """保留显式时区语义，只有无时区调用才按固定的服务器本地时间换算。"""

        @classmethod
        def fromtimestamp(cls, timestamp: float, tz: Any = None) -> datetime:
            conversions.append((timestamp, tz))
            if tz is None:
                return datetime.fromtimestamp(timestamp, tz=local_zone).replace(tzinfo=None)
            return datetime.fromtimestamp(timestamp, tz=tz)

    monkeypatch.setattr(module, 'datetime', LocalDatetime)
    raw = str(int(seconds * (1000 if milliseconds else 1)))
    assert module._display_date(raw) == '2026-10-13'
    assert conversions == [(seconds, None)]


@pytest.mark.parametrize('address', ['ftp://example.test/a', 'https://example.test/' + 'a' * 201])
@pytest.mark.asyncio
async def test_star9_invalid_or_overlong_urls_are_not_displayed(ready, address):
    plugin = ready[0]
    transport(plugin, lambda _: httpx.Response(200, json=payload([{'heading': '标题', 'address': address}])))
    assert address not in (await execute(plugin)).observation


@pytest.mark.parametrize('count', [1, 5])
@pytest.mark.asyncio
async def test_star9_field_limits_and_constant_derived_observation_bound(ready, count):
    plugin, _, module, _, _ = ready
    address = 'https://example.test/' + 'a' * (module.URL_MAX_CHARS - len('https://example.test/'))
    items = [{'heading': '标' * 100, 'abstract': '摘' * 2000, 'origin': '站' * 100,
              'published': '日' * module.DATE_MAX_CHARS, 'address': address} for _ in range(count)]
    transport(plugin, lambda _: httpx.Response(200, json=payload(items)))
    query = '查' * module.MAX_QUERY_CHARS
    observation = (await execute(plugin, query)).observation
    lines = observation.splitlines()
    for index in range(count):
        heading, snippet, url = lines[2 + index * 3:5 + index * 3]
        title, site_date = heading.split('. ', 1)[1].split('（', 1)
        site, day = site_date.rstrip('）').split('，', 1)
        assert len(title) == module.TITLE_MAX_CHARS
        assert len(site) == module.SITE_MAX_CHARS
        assert len(day) == module.DATE_MAX_CHARS
        assert len(snippet) == module.SNIPPET_BUDGET // count
        assert len(url) == module.URL_MAX_CHARS
        assert title.endswith('…') and site.endswith('…') and snippet.endswith('…')
    header_bound = len('[联网搜索：]') + module.MAX_QUERY_CHARS + 1 + len(module.SOURCE_NOTICE)
    item_bound = (len(f'\n{module.MAX_RESULTS}. （，）\n\n') + module.TITLE_MAX_CHARS + module.SITE_MAX_CHARS +
                  module.DATE_MAX_CHARS + module.SNIPPET_BUDGET // module.MAX_RESULTS + module.URL_MAX_CHARS)
    assert len(observation) <= header_bound + module.MAX_RESULTS * item_bound


@pytest.mark.asyncio
async def test_star9_host_fallback_is_also_limited(ready):
    plugin, _, module, _, _ = ready
    address = 'https://' + 'h' * 50 + '.test/a'
    transport(plugin, lambda _: httpx.Response(200, json=payload([{'heading': '标题', 'address': address}])))
    observation = (await execute(plugin)).observation
    site = observation.split('（', 1)[1].split('）', 1)[0]
    assert len(site) == module.SITE_MAX_CHARS and site.endswith('…')


@pytest.mark.asyncio
async def test_star10_empty_results_are_success(ready):
    plugin = ready[0]
    transport(plugin, lambda _: httpx.Response(200, json=payload([])))
    result = await execute(plugin)
    assert result.success and result.observation == '[联网搜索：公开话题]\n没有搜到相关结果。'


@pytest.mark.parametrize(('kind', 'required'), [
    ('500', ['500', '响应开头']), ('401', ['401', '密钥无效、过期或余额不足']),
    ('403', ['403', '密钥无效、过期或余额不足']), ('429', ['429', '请求太频繁']),
    ('301', ['301', 'https://example.test/next', 'url']),
    ('timeout', ['超过 8 秒没有响应']), ('not-json', ['返回内容不是 JSON', '响应开头']),
    ('missing', ['找不到结果列表', 'data.items', '响应开头']),
    ('not-list', ['找不到结果列表', 'data.items']),
])
@pytest.mark.asyncio
async def test_star11_failures_are_diagnostic_results(ready, kind, required):
    plugin = ready[0]
    def handle(request):
        if kind == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        if kind == 'not-json':
            return httpx.Response(200, text='响应开头: not-json')
        if kind == 'missing':
            return httpx.Response(200, json={'message': '响应开头'})
        if kind == 'not-list':
            return httpx.Response(200, json={'data': {'items': {}}})
        return httpx.Response(int(kind), text='响应开头', headers={'Location': 'https://example.test/next'})
    requests = transport(plugin, handle)
    result = await execute(plugin)
    assert not result.success and result.error_message.startswith('联网搜索失败：')
    assert 'SearchRequestError' not in result.error_message
    assert all(text in result.error_message for text in required)
    if kind in {'401', '403'}:
        assert result.error_message == f'联网搜索失败：HTTP {kind}；密钥无效、过期或余额不足；响应开头：响应开头'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_unexpected_request_error_keeps_exception_class(ready):
    """未预料的连接异常仍保留类名，避免清理已知错误文案时丢失排错信息。"""
    plugin = ready[0]

    def handle(request):
        raise httpx.ConnectError('连接测试失败', request=request)

    requests = transport(plugin, handle)
    result = await execute(plugin)
    assert not result.success
    assert result.error_message == '联网搜索失败：ConnectError：连接测试失败'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_total_deadline_covers_slow_response(ready, monkeypatch):
    plugin, _, _, request_module, _ = ready
    monkeypatch.setattr(request_module, 'HTTP_TIMEOUT_SECONDS', 0.01)
    async def slow(_request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=payload())
    plugin._transport = httpx.MockTransport(slow)
    result = await execute(plugin)
    assert not result.success and '没有响应' in result.error_message


@pytest.mark.asyncio
async def test_star12_errors_logs_and_metadata_redact_api_key(ready):
    key = 'fake-private-key'
    plugin = configure(ready, api_key=key, url=URL + '?secret=' + key, headers={'Authorization': '{api_key}'})
    with capture_logs() as logs:
        await plugin.on_load()
        transport(plugin, lambda _: httpx.Response(401, text='  回显\n ' + key + ' ' + '长' * 300))
        result = await execute(plugin)
    assert '***' in result.error_message and key not in result.error_message
    assert key not in json.dumps(result.metadata, ensure_ascii=False)
    assert key not in json.dumps(logs, ensure_ascii=False)
    assert '?secret=' not in json.dumps(logs, ensure_ascii=False)
    assert any(entry['log_level'] == 'warning' and entry.get('status_code') == 401 for entry in logs)
    preview = result.error_message.split('：', 1)[-1]
    assert '\n' not in preview


@pytest.mark.asyncio
async def test_star12_success_observation_and_metadata_redact_api_key(ready):
    key = 'fake-private-key'
    plugin = configure(ready, api_key=key)
    await plugin.on_load()
    transport(plugin, lambda _: httpx.Response(200, json=payload([{'heading': key, 'abstract': key, 'address': 'https://example.test/' + key}])))
    result = await execute(plugin, key)
    assert result.success and key not in result.observation and '***' in result.observation
    assert key not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_star13_daily_limit_counts_failures_and_resets_next_day(ready, monkeypatch):
    plugin, _, module, _, _ = ready
    monkeypatch.setattr(module, 'today', lambda: date(2026, 10, 13))
    configure(ready, daily_limit=2)
    await plugin.on_load()
    requests = transport(plugin, lambda request: httpx.Response(500, text='失败') if len(requests) == 2 else httpx.Response(200, json=payload()))
    with capture_logs() as logs:
        assert (await execute(plugin, '一')).success
        assert not (await execute(plugin, '二')).success
        assert plugin.stream_capabilities(7) == frozenset()
        assert not (await execute(plugin, '三')).success
        assert len(requests) == 2
    assert sum('日上限' in entry['event'] for entry in logs) == 1
    monkeypatch.setattr(module, 'today', lambda: date(2026, 10, 14))
    assert plugin.stream_capabilities(7) == frozenset({'web_search'})
    assert (await execute(plugin, '三')).success
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_star13_normalized_cache_hit_does_not_count_and_failures_not_cached(ready):
    plugin = ready[0]
    requests = transport(plugin, lambda request: httpx.Response(500, text='失败') if 'bad' in request.url.params.get('q', '') else httpx.Response(200, json=payload()))
    configure(ready, params={'q': '{query}'})
    await plugin.on_load()
    assert (await execute(plugin, '  Topic   Name  ')).success
    assert (await execute(plugin, 'topic name')).success
    assert len(requests) == 1 and plugin._requests_today == 1
    assert not (await execute(plugin, 'bad')).success
    assert not (await execute(plugin, 'bad')).success
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_cache_expiry_capacity_and_empty_result_cache(ready, monkeypatch):
    plugin, _, module, _, _ = ready
    configure(ready, daily_limit=1000)
    await plugin.on_load()
    clock = [100.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    requests = transport(plugin, lambda _: httpx.Response(200, json=payload([])))
    assert (await execute(plugin, '缓存')).success
    assert (await execute(plugin, '缓存')).success and len(requests) == 1
    clock[0] += module.CACHE_TTL_SECONDS
    await execute(plugin, '缓存')
    assert len(requests) == 2
    for index in range(module.CACHE_LIMIT + 1):
        await execute(plugin, str(index))
    assert len(plugin._cache) == module.CACHE_LIMIT
    previous = len(requests)
    await execute(plugin, '0')
    assert len(requests) == previous + 1


EXAMPLES = [
    ('https://open.bigmodel.cn/api/paas/v4/web_search', 'POST', 'Authorization', 'Bearer 填你的密钥',
     {'id': 'fixture', 'created': 1791854675, 'search_result': [{'title': '桩件标题', 'content': '桩件摘要', 'link': 'https://example.test/result', 'media': '桩件站点', 'publish_date': '2026-10-12'}]}),
    ('https://api.bochaai.com/v1/web-search', 'POST', 'Authorization', 'Bearer 填你的密钥',
     {'code': 200, 'msg': None, 'data': {'webPages': {'value': [{'name': '桩件标题', 'url': 'https://example.test/result', 'snippet': '候选摘要', 'summary': '桩件摘要', 'siteName': '桩件站点', 'datePublished': '2026-10-12T08:00:00+08:00'}]}}}),
    ('https://api.tavily.com/search', 'POST', 'Authorization', 'Bearer 填你的密钥',
     {'query': '话题', 'results': [{'title': '桩件标题', 'url': 'https://example.test/result', 'content': '桩件摘要', 'score': 0.9, 'published_date': '2026-10-12'}], 'response_time': 1.2}),
    ('https://api.search.brave.com/res/v1/web/search', 'GET', 'X-Subscription-Token', '填你的密钥',
     {'query': {'original': '话题'}, 'web': {'results': [{'title': '桩件标题', 'url': 'https://example.test/result', 'description': '桩件<strong>摘要</strong>'}]}}),
    ('http://127.0.0.1:8080/search', 'GET', None, None,
     {'query': '话题', 'results': [{'url': 'https://example.test/result', 'title': '桩件标题', 'content': '桩件摘要', 'engine': 'fixture', 'publishedDate': '2026-10-12T00:00:00'}]}),
]


@pytest.mark.parametrize('index', range(5))
@pytest.mark.asyncio
async def test_star14_readme_blocks_load_through_host_and_match_response_fixture(loaded, index):
    blocks = re.findall(r'```toml\n(.*?)\n```', (ROOT / 'README.md').read_text(encoding='utf-8'), re.S)
    assert len(blocks) == 5
    plugin = configure(loaded, **tomllib.loads(blocks[index]))
    await plugin.on_load()
    assert plugin.stream_capabilities(7) == frozenset({'web_search'})
    if plugin.config.api_key:
        configure(loaded, api_key='fake-example-key')
        await plugin.on_load()
    address, method, auth_header, auth_value, response = EXAMPLES[index]
    requests = transport(plugin, lambda _: httpx.Response(200, json=response))
    result = await execute(plugin, '话题')
    assert result.success, result.error_message
    assert all(value in result.observation for value in ['桩件标题', '桩件摘要', 'https://example.test/result'])
    request = requests[0]
    assert str(request.url).split('?', 1)[0] == address and request.method == method
    if auth_header:
        assert request.headers[auth_header] == auth_value.replace('填你的密钥', 'fake-example-key')
    else:
        assert 'Authorization' not in request.headers
    expected_bodies = [
        {'search_query': '话题', 'search_engine': 'search_pro', 'search_intent': False, 'count': 5, 'content_size': 'medium'},
        {'query': '话题', 'summary': True, 'count': 5},
        {'query': '话题', 'max_results': 5, 'include_published_date': True},
        None, None,
    ]
    expected_params = [{}, {}, {}, {'q': '话题', 'count': '5'}, {'q': '话题', 'format': 'json'}]
    assert dict(request.url.params) == expected_params[index]
    if method == 'POST':
        assert json.loads(request.content) == expected_bodies[index]
    else:
        assert request.content == b''
    if index != 3:
        assert '2026-10-12' in result.observation


@pytest.mark.parametrize('arguments', [{'query': '正常', 'unknown': 1}, {'query': ''}, {'query': '长' * 61}, {'query': 123}])
@pytest.mark.asyncio
async def test_invalid_parameters_fail_before_request(ready, arguments):
    requests = transport(ready[0])
    result = await execute(ready[0], arguments=arguments)
    assert not result.success and result.error_message.startswith('联网搜索失败')
    assert requests == []


@pytest.mark.parametrize('proxy', ['', 'http://proxy.example:8888'])
@pytest.mark.asyncio
async def test_proxy_and_client_options(ready, monkeypatch, proxy):
    plugin, _, _, request_module, _ = ready
    config = Config()
    config.advanced.https_proxy = proxy
    plugin.bind_context(PluginContext(plugin.manifest.plugin_id, ready[4], ready[4] / 'data', config))
    requests = transport(plugin)
    original = httpx.AsyncClient
    seen = []
    def client(**kwargs):
        seen.append(dict(kwargs))
        return original(**dict(kwargs, proxy=None))
    monkeypatch.setattr(request_module.httpx, 'AsyncClient', client)
    assert (await execute(plugin)).success and len(requests) == 1
    assert seen[0]['proxy'] == (proxy if proxy else None)
    assert seen[0]['trust_env'] is False and seen[0]['follow_redirects'] is False


@pytest.mark.asyncio
async def test_cancellation_propagates(ready):
    plugin = ready[0]
    async def cancelled(_request):
        raise asyncio.CancelledError
    plugin._transport = httpx.MockTransport(cancelled)
    with pytest.raises(asyncio.CancelledError):
        await execute(plugin)


@pytest.mark.asyncio
async def test_concurrent_sessions_cannot_exceed_daily_limit(ready):
    plugin = configure(ready, daily_limit=1)
    await plugin.on_load()
    requests = []

    async def handle(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json=payload())

    plugin._transport = httpx.MockTransport(handle)
    results = await asyncio.gather(execute(plugin, '一'), execute(plugin, '二'))
    assert len(requests) == 1
    assert sum(result.success for result in results) == 1
    assert plugin.stream_capabilities(7) == frozenset()


def test_plugin_sources_have_no_provider_domains_or_core_imports(loaded):
    for name in ('plugin.py', 'request.py'):
        source = (ROOT / name).read_text(encoding='utf-8')
        assert 'src.core' not in source
        for address, *_ in EXAMPLES:
            domain = httpx.URL(address).host
            assert domain not in source
