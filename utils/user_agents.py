"""Use this project's real headless Chromium identity, without a fabricated OS/version.

The version comes from the Playwright package used by the running interpreter,
not another Node project or the system Python. Browser tests compare this value
with a default context's navigator.userAgent. Client hints are browser-managed.
"""
import importlib.util
import json
import sys
from pathlib import Path


def bundled_chromium_version():
    try:
        package = importlib.util.find_spec('playwright')
        if package is None or package.origin is None:
            return None
        manifest = Path(package.origin).parent / 'driver' / 'package' / 'browsers.json'
        browsers = json.loads(manifest.read_text())['browsers']
        value = next(b['browserVersion'] for b in browsers if b['name'] == 'chromium')
        return value if value.split('.')[0].isdigit() else None
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return None


CHROMIUM_VERSION = bundled_chromium_version()
CHROME_MAJOR = CHROMIUM_VERSION.split('.')[0] if CHROMIUM_VERSION else ''
if sys.platform == 'win32':
    _platform = 'Windows NT 10.0; Win64; x64'
elif sys.platform == 'darwin':
    _platform = 'Macintosh; Intel Mac OS X 10_15_7'
else:
    _platform = 'X11; Linux x86_64'

CHROME_UA = (
    f'Mozilla/5.0 ({_platform}) AppleWebKit/537.36 (KHTML, like Gecko) '
    f'HeadlessChrome/{CHROME_MAJOR}.0.0.0 Safari/537.36'
) if CHROME_MAJOR else 'ByGoofish (Playwright browser unavailable)'

# Compatibility export only: application HTTP clients do not synthesize UA-CH.
SEC_CH_UA = f'"Chromium";v="{CHROME_MAJOR}", "HeadlessChrome";v="{CHROME_MAJOR}"' if CHROME_MAJOR else ''
