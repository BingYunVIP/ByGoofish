"""闲鱼浏览器始终直连；不改变主进程中其他服务的代理配置。"""

import os


def direct_environment(environment=None):
    """返回不继承代理的子进程环境，保留 DISPLAY、PATH 和浏览器路径。"""
    source = os.environ if environment is None else environment
    proxy_keys = {'http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy',
                  'no_proxy', 'node_use_env_proxy'}
    result = {key: value for key, value in source.items() if key.lower() not in proxy_keys}
    result.update(NO_PROXY='*', no_proxy='*')
    return result


def direct_browser_options(options=None):
    """对普通及持久化 Chromium 启动统一禁用环境、系统和 PAC 代理。"""
    result = dict(options or {})
    result.pop('proxy', None)
    proxy_flags = ('--proxy-server', '--proxy-pac-url', '--proxy-auto-detect',
                   '--proxy-bypass-list', '--no-proxy-server')
    result['args'] = [arg for arg in result.get('args', [])
                      if arg.split('=', 1)[0] not in proxy_flags] + ['--no-proxy-server']
    result['env'] = direct_environment({**os.environ, **result.get('env', {})})
    return result
