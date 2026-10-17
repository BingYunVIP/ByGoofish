"""严格验证TLS证书，按整体截止时间执行SMTP；DATA后故障视为结果不确定。"""
import smtplib
import ssl
import time


class TLSVerificationFailed(RuntimeError):
    pass


class DeliveryUncertain(RuntimeError):
    pass


class DeadlineMixin:
    def remaining(self):
        left=self.deadline-time.monotonic()
        if left<=0:raise TimeoutError('SMTP deadline exceeded')
        if self.sock is not None:self.sock.settimeout(left)
        return left

    def getreply(self):
        self.remaining()
        return super().getreply()

    def send(self, data):
        self.remaining()
        return super().send(data)


class DeadlineSMTP(DeadlineMixin,smtplib.SMTP):
    def __init__(self,*args,deadline,**kwargs):
        self.deadline=deadline
        super().__init__(*args,**kwargs)


class DeadlineSMTPSSL(DeadlineMixin,smtplib.SMTP_SSL):
    def __init__(self,*args,deadline,**kwargs):
        self.deadline=deadline
        super().__init__(*args,**kwargs)


def send_message(message, host, port, username, password, *, use_ssl=False, use_tls=True, timeout=30):
    port=int(port);timeout=max(1,min(float(timeout),60));deadline=time.monotonic()+timeout
    context=ssl.create_default_context();server=None;submission=False
    if not use_ssl and port!=465 and not use_tls:
        raise ValueError('邮件认证必须使用SSL或STARTTLS')
    try:
        if use_ssl or port==465:
            server=DeadlineSMTPSSL(host,port,deadline=deadline,timeout=timeout,context=context)
        else:
            server=DeadlineSMTP(host,port,deadline=deadline,timeout=timeout)
            server.starttls(context=context)
        server.login(username,password)
        # 此后任何网络异常都不能断言邮件未送达，禁止自动补发。
        submission=True
        server.send_message(message)
    except ssl.SSLCertVerificationError:
        raise TLSVerificationFailed('SMTP证书验证失败，请检查服务器域名和系统CA证书') from None
    except smtplib.SMTPResponseException:
        raise
    except (OSError,smtplib.SMTPServerDisconnected):
        if submission:raise DeliveryUncertain('邮件提交结果不确定，请先查收邮箱，不要立即重复发送') from None
        raise
    finally:
        if server is not None:server.close()
