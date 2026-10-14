"""通用 HTTP JSON 请求与字段映射，不包含服务商预设。

供 plugin.py 调用：递归展开配置值中的占位符，在总截止时间内请求一个配置地址，
按点号路径提取结果并清洗文本。网络与响应形态错误交给插件执行体统一收口。
"""

from dataclasses import dataclass
from html import unescape
from typing import Any, Callable, Dict, List, Literal, Optional, Union

import asyncio
import re

import httpx

# 单次最多返回的结果条数；正整数，同时作为请求中的 count 占位符值。
MAX_RESULTS = 5
# HTTP 总截止时间，单位秒；须小于宿主工具的 10 秒时限。
HTTP_TIMEOUT_SECONDS = 8
# 错误响应预览的最大字符数，先抹除密钥并折叠空白再截取。
RESPONSE_PREVIEW_CHARS = 200
PLACEHOLDER_PATTERN = re.compile(r'\{(query|count|api_key)\}')
# 标签先移除、实体后还原，避免把实体编码的普通文本再次当成 HTML 标签。
HTML_TAG_PATTERN = re.compile(r'<[^>]+>')


@dataclass(frozen=True)
class SearchResponse:
    """保存清洗后的结果字段与 HTTP 状态，不保留原始响应和鉴权信息。"""

    items: List[Dict[str, str]]
    status_code: Optional[int]


class SearchRequestError(ValueError):
    """携带可诊断的响应错误与可选 HTTP 状态码。"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        """保存响应错误，交给工具执行体返回失败结果。

        :param message: 中文诊断文本；响应预览已抹除密钥，最终出口再次抹除。
        :param status_code: 已收到的 HTTP 状态码；未收到响应时为 None。
        """
        super().__init__(message)
        self.status_code = status_code


def redact_text(text: str, api_key: str) -> str:
    """抹除对外文本中的密钥。

    :param text: 错误、观察或日志字段文本，不修改原字符串。
    :param api_key: 已配置的密钥；空字符串表示无需抹除。
    :return: 密钥所有完整出现均替换为 *** 的文本。
    """
    return text.replace(api_key, '***') if api_key else text


def uses_api_key(value: Any) -> bool:
    """检查请求模板是否需要非空密钥。

    :param value: 解析后的任意模板值，递归检查字典值与列表元素。
    :return: 任意字符串值引用密钥占位符时为真；字典键不参与检查。
    """
    if isinstance(value, str):
        return '{api_key}' in value
    if isinstance(value, dict):
        return any(uses_api_key(item) for item in value.values())
    if isinstance(value, list):
        return any(uses_api_key(item) for item in value)
    return False


def expand_values(value: Any, *, query: str, api_key: str) -> Any:
    """递归展开解析后的模板值，原样保留键与非字符串标量。

    :param value: 字符串、字典、列表或 JSON 标量，不修改传入对象。
    :param query: 已校验查询词；引号、反斜杠和花括号均按原文保留。
    :param api_key: 部署者填写的密钥，可为空。
    :return: 新结构；独立 count 占位符为整数，嵌入字符串时为文本。

    连续替换会把查询词中的密钥占位符再次展开，泄露鉴权值。一次正则替换仅扫描
    原模板，返回的替换文本不再扫描，避免输入字符串改变模板语义。
    """
    if isinstance(value, str):
        if value == '{count}':
            return MAX_RESULTS
        replacements = {'query': query, 'count': str(MAX_RESULTS), 'api_key': api_key}
        return PLACEHOLDER_PATTERN.sub(lambda match: replacements[match[1]], value)
    if isinstance(value, dict):
        return {key: expand_values(item, query=query, api_key=api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_values(item, query=query, api_key=api_key) for item in value]
    return value


def value_at_path(value: Any, path: str) -> Any:
    """逐段读取配置指定的字典路径。

    :param value: 响应根对象或单条结果，不修改原结构。
    :param path: 点号分隔的字典键，不支持数组下标。
    :return: 路径对应的原始值；键缺失或中间值不是字典时为 None。
    """
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def clean_text(value: Any) -> str:
    """按固定顺序清洗结果字段。

    :param value: 任意原始字段值，None 表示空字段。
    :return: 去标签、还原实体并折叠空白后的字符串，不含首尾空白。
    """
    if value is None:
        return ''
    return ' '.join(unescape(HTML_TAG_PATTERN.sub('', str(value))).split())


def _field_text(item: Dict[str, Any], paths: Union[str, List[str]]) -> str:
    """按候选路径顺序选择字段，不解释服务商字段名。

    :param item: 单条结果对象。
    :param paths: 点号路径或路径数组；None、空串与纯空白字符串均跳过。
    :return: 第一个非空原始值清洗后的文本；没有候选值时为空串。
    """
    for path in [paths] if isinstance(paths, str) else paths:
        raw = value_at_path(item, path)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        return clean_text(raw)
    return ''


def _response_preview(response: httpx.Response, api_key: str) -> str:
    """保留响应开头用于排错；先抹除密钥再折叠空白和截取字符。"""
    return ' '.join(redact_text(response.text, api_key).split())[:RESPONSE_PREVIEW_CHARS]


def map_results(document: Any, *, results_path: str, fields: Dict[str, Union[str, List[str]]],
                preview: str, status_code: Optional[int] = None) -> SearchResponse:
    """HTTP 与 MCP 共用同一路径提取和清洗，不解释服务商字段、不伪造空结果。"""
    rows = value_at_path(document, results_path)
    if not isinstance(rows, list):
        raise SearchRequestError(f'返回内容里找不到结果列表 {results_path}；响应开头：{preview}', status_code)
    return SearchResponse(
        [{name: _field_text(item, paths) for name, paths in fields.items()} for item in rows if isinstance(item, dict)],
        status_code,
    )


async def search_request(
    *, url: str, method: Literal['GET', 'POST'], api_key: str,
    headers: Dict[str, str], params: Dict[str, Any], body: Dict[str, Any],
    results_path: str, fields: Dict[str, Union[str, List[str]]], query: str,
    proxy: str, on_request: Callable[[], None], transport: Optional[httpx.AsyncBaseTransport] = None,
) -> SearchResponse:
    """按部署配置请求 JSON 接口，返回清洗后的结果。

    :param url: 配置中的接口地址，不访问结果里的链接。
    :param method: GET 仅带查询参数；POST 的 body 通过 json 参数编码。
    :param api_key: 仅用于占位符展开与错误响应抹除。
    :param headers: 请求头模板；键不替换。
    :param params: 查询参数模板，可含嵌套结构。
    :param body: POST JSON 体模板，GET 时配置必须为空。
    :param results_path: 响应结果数组的非空点号路径。
    :param fields: 标题、摘要、链接、站点和日期的点号路径或候选路径数组。
    :param query: 去首尾空白后的查询词，长度由调用方校验。
    :param proxy: 宿主注入的代理；空字符串表示直连。
    :param on_request: 实际发起请求前同步计数并检查日上限，不能发起异步工作。
    :param transport: 用例注入的 MockTransport；运行时省略。
    :return: 清洗后的字段字典列表与 HTTP 状态码，空数组同样成功。
    :raises SearchRequestError: 超时、非 2xx、非 JSON 或找不到结果数组。
    :raises Exception: 其他请求或映射错误，由工具执行体转为中文失败。
    :raises asyncio.CancelledError: 宿主取消时原样上抛。
    """
    expanded_headers = expand_values(headers, query=query, api_key=api_key)
    # count 独立占位符在结构上仍为整数；HTTP 头值必须在协议边界转为文本。
    options = {'headers': {name: str(value) for name, value in expanded_headers.items()},
               'params': expand_values(params, query=query, api_key=api_key)}
    if method == 'POST':
        options['json'] = expand_values(body, query=query, api_key=api_key)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HTTP_TIMEOUT_SECONDS
    try:
        # 单独设置 HTTP 分阶段时限会使慢速分块累加到宿主时限，中文原因来不及回灌。
        # 总截止时间同时覆盖连接、响应读取和客户端收尾，保持内层比宿主更早结束。
        async with asyncio.timeout_at(deadline):
            async with httpx.AsyncClient(
                proxy=proxy if proxy else None, trust_env=False, follow_redirects=False,
                transport=transport, timeout=deadline - loop.time(),
            ) as client:
                on_request()
                response = await client.request(method, url, **options)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise SearchRequestError(f'超过 {HTTP_TIMEOUT_SECONDS:g} 秒没有响应') from exc
    status = response.status_code
    preview = _response_preview(response, api_key)
    if 300 <= status < 400:
        target = redact_text(response.headers.get('Location', ''), api_key)
        raise SearchRequestError(f'HTTP {status}，跳转目标：{target}；请把 url 改成跳转后的地址', status)
    if not 200 <= status < 300:
        hint = ('；密钥无效、过期或余额不足' if status in {401, 403} else
                '；请求太频繁' if status == 429 else '')
        raise SearchRequestError(f'HTTP {status}{hint}；响应开头：{preview}', status)
    try:
        document = response.json()
    except ValueError as exc:
        raise SearchRequestError(f'返回内容不是 JSON；响应开头：{preview}', status) from exc
    return map_results(document, results_path=results_path, fields=fields, preview=preview, status_code=status)
