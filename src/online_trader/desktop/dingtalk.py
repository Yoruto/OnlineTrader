"""DSV-compatible DingTalk robot messages and desktop delivery settings."""
import base64
from dataclasses import dataclass
from datetime import datetime
import hmac
import json
import re
from threading import BoundedSemaphore, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox

from online_trader.accounts import WindowsVault
from online_trader.errors import TraderError


_VAULT_TARGET = 'OnlineTrader/notifications/dingtalk'
_CONFIGURED = 'dingtalk/configured'


@dataclass(frozen=True, repr=False)
class DingTalkSettings:
    enabled: bool = False
    webhook: str = ''
    secret: str = ''
    mobiles: tuple = ()
    throttle_seconds: int = 10

    @classmethod
    def parse(cls, enabled, webhook, secret, mobiles, throttle_seconds):
        webhook, secret = webhook.strip(), secret.strip()
        try:
            url = urlsplit(webhook)
            query = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise TraderError('E_DINGTALK_SETTINGS', '请填写完整的钉钉机器人 Webhook。') from None
        if webhook and (len(webhook) > 1024 or any(ch.isspace() for ch in webhook)
                or url.scheme != 'https' or url.netloc not in {'oapi.dingtalk.com','oapi.dingtalk.com:443'}
                or url.path != '/robot/send' or url.fragment or len(query) != 1
                or query[0][0] != 'access_token' or not query[0][1]):
            raise TraderError('E_DINGTALK_SETTINGS', '请填写完整的钉钉机器人 Webhook。')
        if type(enabled) is not bool or (enabled and not webhook):
            raise TraderError('E_DINGTALK_SETTINGS', '启用前请填写钉钉机器人 Webhook。')
        recipients = tuple(part.strip() for part in mobiles.split(',') if part.strip())
        if (len(secret.encode('utf-8')) > 256 or len(mobiles) > 512
                or any(re.fullmatch(r'\+?[0-9]{1,18}', number) is None for number in recipients)
                or type(throttle_seconds) is not int or not 0 <= throttle_seconds <= 86400):
            raise TraderError('E_DINGTALK_SETTINGS', '请检查密钥、逗号分隔的手机号和节流秒数。')
        return cls(enabled, webhook, secret, recipients, throttle_seconds)


def _signed_url(config, timestamp_ms):
    if not config.secret:
        return config.webhook
    secret = config.secret.encode('utf-8')
    message = str(timestamp_ms).encode('ascii') + b'\n' + secret
    sign = quote_plus(base64.b64encode(hmac.digest(secret, message, 'sha256')))
    return f'{config.webhook}&timestamp={timestamp_ms}&sign={sign}'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_robot(url, payload, timeout):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = Request(url, raw, {'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(8193)
        if len(raw) > 8192:
            raise ValueError('DingTalk response exceeds capacity')
        return json.loads(raw)


def send_dingtalk(config, message, *, post=_post_robot, clock=time.time):
    """Send one notification and expose only bounded, credential-free results."""
    url = _signed_url(config, int(clock() * 1000))
    payload = {'msgtype': 'text', 'text': {'content': message},
               'at': {'atMobiles': list(config.mobiles), 'isAtAll': False}}
    try:
        result = post(url, payload, 8)
    except HTTPError as error:
        return False, f'HTTP {error.code}'
    except (URLError, TimeoutError, OSError):
        return False, '连接钉钉失败或超时'
    except (ValueError, TypeError):
        return False, '钉钉响应格式无法识别'
    code = result.get('errcode') if isinstance(result, dict) else None
    if type(code) is int:
        return (True, '发送成功') if code == 0 else (False, f'钉钉返回错误码 {code}')
    return False, '钉钉响应格式无法识别'


class WorkbenchDingTalk(QObject):
    status_changed = Signal(str)
    delivered = Signal(bool, str, bool)

    def __init__(self, settings, *, vault=None, sender=send_dingtalk, monotonic=time.monotonic, log=None, initial_config=None):
        super().__init__()
        self.settings = settings
        self.vault = vault
        self.sender = sender
        self.monotonic = monotonic
        self.log = log
        self._config = initial_config
        self._last_sent = {}
        self._slots = BoundedSemaphore(8)
        self._test_pending = False
        self.delivered.connect(self._finish)

    def _vault(self):
        if self.vault is None:
            self.vault = WindowsVault()
        return self.vault

    def current(self):
        if self._config is None:
            try:
                self._config = DingTalkSettings()
                if (self.vault is not None or QApplication.platformName() != 'offscreen') and self.settings.value(_CONFIGURED, False, bool):
                    stored = self._vault().get(_VAULT_TARGET)
                    private = json.loads(stored['password'])
                    self._config = DingTalkSettings.parse(
                        private['enabled'], stored['username'], private['secret'],
                        private['mobiles'], private['throttle_seconds'])
            except (TraderError, OSError, ValueError, KeyError, TypeError, AttributeError):
                self.status_changed.emit('读取钉钉设置失败，请重新保存。')
        return self._config

    def save(self, config):
        try:
            if config.webhook:
                private = json.dumps({'enabled': config.enabled, 'secret': config.secret,
                                      'mobiles': ','.join(config.mobiles), 'throttle_seconds': config.throttle_seconds},
                                     separators=(',', ':'))
                self._vault().put(_VAULT_TARGET, config.webhook, private)
            elif self.settings.value(_CONFIGURED, False, bool):
                self._vault().delete(_VAULT_TARGET)
            self.settings.setValue(_CONFIGURED, bool(config.webhook))
            self.settings.sync()
        except (TraderError, OSError):
            self.status_changed.emit('保存钉钉设置失败。')
            return False
        self._config = config
        self._last_sent.clear()
        self.status_changed.emit('钉钉通知设置已保存。')
        return True

    def notify(self, event, contract, message):
        config = self.current()
        if not config.enabled or not config.webhook:
            return False
        key = contract, event
        moment = self.monotonic()
        previous = self._last_sent.get(key)
        if previous is not None and moment - previous < config.throttle_seconds:
            return False
        text = f"[交易策略] {datetime.now():%H:%M:%S}\n{message}"
        if self._start(config, text, test=False):
            self._last_sent[key] = moment
            return True
        return False

    def test(self, config):
        if not config.webhook:
            self.status_changed.emit('请先填写钉钉 Webhook。')
            return False
        message = f"[测试] 交易策略通知测试\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n（收到这条说明 Webhook 可用）"
        return self._start(config, message, test=True)

    def _start(self, config, message, *, test):
        if self.sender is send_dingtalk and QApplication.platformName() == 'offscreen':
            self.status_changed.emit('离屏工作台不发送钉钉推送。')
            return False
        if test and self._test_pending:
            return False
        if not self._slots.acquire(blocking=False):
            self.status_changed.emit('钉钉推送仍在进行，请稍后再试。')
            return False

        def deliver():
            try:
                ok, info = self.sender(config, message)
            except Exception:
                ok, info = False, '钉钉发送异常'
            finally:
                self._slots.release()
            self.delivered.emit(ok, info, test)

        if test:
            self._test_pending = True
            self.status_changed.emit('正在发送测试通知…')
        try:
            Thread(target=deliver, daemon=True).start()
        except RuntimeError:
            self._slots.release()
            if test:
                self._test_pending = False
            self.status_changed.emit('无法启动钉钉推送。')
            return False
        return True

    @Slot(bool, str, bool)
    def _finish(self, ok, info, test):
        if test:
            self._test_pending = False
        message = f"{'测试推送' if test else '钉钉推送'}：{info}"
        if test or not ok:
            self.status_changed.emit(message)
            if self.log is not None:
                self.log(message)

    def settings_panel(self):
        panel = QGroupBox('钉钉通知')
        layout = QFormLayout(panel)
        status = QLabel()
        status.setWordWrap(True)
        self.status_changed.connect(status.setText)
        current = self.current()
        enabled = QCheckBox('启用钉钉推送')
        enabled.setChecked(current.enabled)
        layout.addRow(enabled)
        webhook = QLineEdit(current.webhook)
        webhook.setObjectName('dingtalkWebhook')
        webhook.setEchoMode(QLineEdit.Password)
        webhook.setPlaceholderText('填写钉钉群机器人的 Webhook')
        layout.addRow('Webhook', webhook)
        mobiles = QLineEdit(','.join(current.mobiles))
        mobiles.setObjectName('dingtalkMobiles')
        mobiles.setPlaceholderText('逗号分隔，可留空')
        layout.addRow('通知手机号', mobiles)
        throttle = QSpinBox()
        throttle.setRange(0, 86400)
        throttle.setValue(current.throttle_seconds)
        layout.addRow('推送节流（秒）', throttle)
        secret = QLineEdit(current.secret)
        secret.setObjectName('dingtalkSecret')
        secret.setEchoMode(QLineEdit.Password)
        secret.setPlaceholderText('使用加签时填写')
        layout.addRow('加签密钥', secret)

        def use_inputs(action):
            try:
                config = DingTalkSettings.parse(enabled.isChecked(), webhook.text(), secret.text(),
                                               mobiles.text(), throttle.value())
            except TraderError as error:
                self.status_changed.emit(str(error))
                return
            action(config)

        row = QHBoxLayout()
        test = QPushButton('测试推送')
        test.clicked.connect(lambda: use_inputs(self.test))
        row.addWidget(test)
        save = QPushButton('保存通知设置')
        save.clicked.connect(lambda: use_inputs(self.save))
        row.addWidget(save)
        row.addStretch()
        layout.addRow(row)
        layout.addRow(status)
        return panel
