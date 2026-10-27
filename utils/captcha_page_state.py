"""验证码页面状态检查；容器存在不代表控件初始化成功。"""
import re

CONTROLS = ('#nc_1_n1z', '.nc_iconfont.btn_slide', '.btn_slide', '#nc_slide',
            '.nc-slider', '#scratch-captcha-btn', '.scratch-captcha-slider', '[role="slider"]')
CONTAINERS = ('#nocaptcha','.nc-container','.scratch-captcha-container')


class ChallengePageError(Exception):
    def __init__(self, code=''):
        self.code = code
        super().__init__('验证页面报错' + (f'（error:{code}）' if code else ''))


async def inspect_page(page, was_ready=False):
    visible_container = False
    ready = False
    uncertain = page.is_closed()
    for frame in page.frames:
        try:
            # 只提取已知报错形态和错误码，不将页面正文返回日志。
            text = await frame.locator('body').inner_text(timeout=700)
            if re.search(r"oops[\s.\u2026]*something['’]?s\s+wrong|please\s+refresh\s*and\s+try\s+again",text,re.I):
                code = re.search(r'error\s*[:：]\s*([A-Za-z0-9_-]{1,32})',text,re.I)
                return {'state':'page_error','error_code':code[1] if code else ''}
            for selector in CONTROLS:
                for element in await frame.query_selector_all(selector):
                    if await element.is_visible() and await element.is_enabled():
                        box = await element.bounding_box()
                        disabled = await element.get_attribute('aria-disabled')
                        if box and box['width']>0 and box['height']>0 and disabled != 'true':
                            ready = True
            for selector in CONTAINERS:
                for element in await frame.query_selector_all(selector):
                    visible_container = visible_container or await element.is_visible()
        except Exception:
            # frame 导航、销毁或无法读取均不能当作完成。
            uncertain = True
    if ready:
        return {'state':'interactive','error_code':''}
    if was_ready and not visible_container and not uncertain:
        return {'state':'completed','error_code':''}
    return {'state':'loading','error_code':''}
