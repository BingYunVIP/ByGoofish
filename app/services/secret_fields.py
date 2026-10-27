"""展示层不返回凭证；修改时空白秘密字段表示保留原值。"""
import json

SECRET_FIELDS={'password','login_password','smtp_password','email_password','api_key',
               'bot_token','secret','token','device_key','webhook_url','signing_secret',
               'headers','config'}


def public_account(details):
    result=dict(details)
    cookie_value=result.pop('value','')
    value=cookie_value or result.get('cookie','')
    result.pop('cookie',None)
    password=result.pop('password','') or result.pop('login_password','')
    result.pop('login_password',None)
    result.update(cookie_configured=bool(value),cookie_length=len(value),login_password_configured=bool(password))
    return result


def public_channel(channel):
    result=dict(channel)
    config=json.loads(result['config']) if isinstance(result.get('config'),str) else dict(result.get('config') or {})
    result['configured_fields']=[key for key in SECRET_FIELDS if config.get(key)]
    result['config']=json.dumps({key:('' if key in SECRET_FIELDS else value) for key,value in config.items()},ensure_ascii=False)
    return result


def merge_channel_config(existing,incoming):
    old=json.loads(existing) if isinstance(existing,str) else dict(existing or {})
    new=json.loads(incoming) if isinstance(incoming,str) else dict(incoming or {})
    for key in SECRET_FIELDS:
        if not new.get(key) and old.get(key):new[key]=old[key]
    return json.dumps(new,ensure_ascii=False)


def secret_setting(key):
    return key in {'smtp_password','qq_reply_secret_key','admin_password_hash'} or key.endswith(('_secret','_api_key','_token'))


def public_settings(settings):
    result={}
    for key,value in settings.items():
        if key in ('qq_reply_secret_key','admin_password_hash'):continue
        if secret_setting(key):
            result[key]=''
            result[key+'_configured']=bool(value)
        else:result[key]=value
    return result
