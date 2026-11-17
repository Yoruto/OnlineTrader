"""Explicit read-only NTP calibration for the optional simulation boundary."""
from dataclasses import dataclass
from datetime import datetime,timezone
import secrets
import socket,statistics,struct,time
from queue import Empty, SimpleQueue
from threading import Thread
from online_trader.errors import TraderError

@dataclass(frozen=True,slots=True)
class ReferenceClock:
    wall_anchor:float
    monotonic_anchor:float
    offset_seconds:float | None
    samples:tuple
    def now(self):
        elapsed=time.monotonic()-self.monotonic_anchor
        if not 0<=elapsed<300:raise TraderError('E_CLOCK','Simulation clock reference expired or regressed')
        return datetime.fromtimestamp(self.wall_anchor+elapsed,timezone.utc)
    def record(self):
        record = {'mode': 'READ_ONLY_NTP_MONOTONIC', 'samples': list(self.samples), 'os_clock_changed': False}
        if self.offset_seconds is not None:
            record['offset_seconds'] = str(self.offset_seconds)
        return record


class LocalWallClock:
    """DSV startup and execution read the Windows wall clock."""
    def __init__(self):
        self._sample = None

    def sample(self):
        wall, monotonic = self._sample or (datetime.now(timezone.utc), time.monotonic_ns())
        return {'now_utc': wall.isoformat().replace('+00:00', 'Z'), 'monotonic_ns': str(monotonic)}

    def begin_turn(self):
        self._sample = datetime.now(timezone.utc), time.monotonic_ns()

    def end_turn(self):
        self._sample = None

    def now(self):
        return self._sample[0] if self._sample is not None else datetime.now(timezone.utc)

    def record(self):
        return {'mode':'SYSTEM_WALL_CLOCK','os_clock_changed':False}


def check_wall_clock():
    offsets = []
    for host in ('ntp.aliyun.com', 'ntp.tencent.com', 'time.cloudflare.com'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(2.0)
                sent_at = time.time()
                client.sendto(b'\x1b' + bytes(47), (host, 123))
                response, _ = client.recvfrom(64)
                received_at = time.time()
            if len(response) >= 48:
                seconds, fraction = struct.unpack_from('!II', response, 32)
                server_time = seconds - 2_208_988_800 + fraction / 2**32
                offsets.append(server_time - (sent_at + received_at) / 2)
        except OSError:
            continue
    if not offsets:
        return {'code': 'NTP_UNAVAILABLE', 'message': 'NTP 样本不可用，策略使用 Windows 时间。'}
    offset = statistics.median(offsets)
    return {'code': 'NTP_SKEW' if abs(offset) > 1 else 'NTP_ALIGNED',
            'offset_seconds': str(round(offset, 6)),
            'message': (f'本机时钟偏差 {offset:+.3f} 秒，请核对 Windows 时间。'
                        if abs(offset) > 1 else f'本机时钟偏差 {offset:+.3f} 秒。')}


def clock_observation(clock):
    """Reference minus current Windows wall clock, including later OS jumps."""
    reference=clock.now();wall=time.time()
    return {'system_utc':datetime.fromtimestamp(wall,timezone.utc).isoformat().replace('+00:00','Z'),
            'reference_utc':reference.isoformat().replace('+00:00','Z'),
            'reference_minus_system_ms':str(round((reference.timestamp()-wall)*1000,3)),
            'monotonic_ns':str(time.monotonic_ns()),'maximum_system_skew_ms':1000,
            'system_aligned':abs(reference.timestamp()-wall)<=1,
            'reference':clock.record() | {'offset_seconds': str(reference.timestamp() - wall)},'os_clock_changed':False}

def measure_reference():
    samples, estimates = [], []
    for host in ('ntp.aliyun.com', 'ntp.tencent.com', 'time.cloudflare.com'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(1.5)
                client.connect((host, 123))
                packet = bytearray(48)
                packet[0] = 0x23
                packet[40:48] = nonce = secrets.token_bytes(8)
                sent = time.monotonic()
                client.send(packet)
                raw = client.recv(1024)
                received = time.monotonic()
            if (len(raw) < 48 or raw[0] >> 6 == 3 or raw[0] & 7 != 4
                    or not 1 <= raw[1] <= 15 or raw[24:32] != nonce):
                raise ValueError('Invalid NTP reply')
            t2, f2, t3, f3 = struct.unpack_from('!4I', raw, 32)
            if not (t2 or f2) or not (t3 or f3):
                raise ValueError('Missing NTP timestamp')
            # Unfold around 2026, independent of the system date, including
            # server timestamps after the 2036 NTP era rollover.
            pivot = 3_976_214_400
            receive_utc = pivot + (t2 - pivot + 2**31) % 2**32 - 2**31 + f2 / 2**32
            transmit_utc = pivot + (t3 - pivot + 2**31) % 2**32 - 2**31 + f3 / 2**32
            delay = (received - sent) - (transmit_utc - receive_utc)
            if transmit_utc < receive_utc or not 0 <= delay <= 0.5:
                raise ValueError('Invalid NTP round trip')
            utc_at_receive = transmit_utc - 2_208_988_800 + delay / 2
            estimates.append(utc_at_receive - received)
            samples.append({'host': host, 'valid': True, 'rtt_seconds': str(delay)})
        except (OSError, ValueError):
            samples.append({'host': host, 'valid': False})
    if len(estimates) < 2 or max(estimates) - min(estimates) > 0.25:
        raise TraderError('E_CLOCK', 'Insufficient consistent NTP samples; no opening permitted')
    monotonic = time.monotonic()
    anchor = monotonic + statistics.median(estimates)
    return ReferenceClock(anchor, monotonic, None, tuple(samples))


class CalibratedClock:
    """Share one UTC estimate across an online owner and its gateway."""

    def __init__(self, *, lazy=False, recovery_anchor=None, recovering=False):
        self._recovery_anchor = recovery_anchor
        self._using_holdover = False
        try:
            self._reference = None if lazy else measure_reference()
        except TraderError as error:
            pending = recovering() if callable(recovering) else recovering
            saved = recovery_anchor.load() if error.code == 'E_CLOCK' and pending and recovery_anchor else None
            if saved is None:
                raise
            utc, monotonic = saved
            self._reference = ReferenceClock(utc, monotonic, None, ())
            self._using_holdover = True
        self._anchor = self._reference.monotonic_anchor if self._reference else time.monotonic()
        self._utc = self._reference.wall_anchor if self._reference else None
        self._correction = 0.0
        self._valid = self._reference is not None and not self._using_holdover
        self._checkpoint_saved = None
        self._next_refresh = self._anchor + 120 if self._valid else time.monotonic()
        self._results = SimpleQueue()
        self._worker = None
        if self._valid:
            self._checkpoint_reference()

    def bind_recovery_anchor(self, anchor):
        self._recovery_anchor = anchor
        if self._valid:
            self._checkpoint_reference()

    def _checkpoint_reference(self):
        if self._recovery_anchor is not None:
            self._checkpoint_saved = self._recovery_anchor.save(
                self._reference.wall_anchor, self._reference.monotonic_anchor)

    def _timestamp(self, monotonic):
        if self._utc is None:
            raise TraderError('E_CLOCK', 'Initial UTC calibration is unavailable')
        elapsed = monotonic - self._anchor
        correction = min(abs(self._correction), max(0, elapsed) * 0.01)
        return self._utc + elapsed + (correction if self._correction >= 0 else -correction)

    def now(self):
        # Preserve UTC continuity for already-owned positions during an outage.
        return datetime.fromtimestamp(self._timestamp(time.monotonic()), timezone.utc)

    def sample(self):
        monotonic_ns = time.monotonic_ns()
        wall = datetime.fromtimestamp(self._timestamp(monotonic_ns / 1e9), timezone.utc)
        return {'now_utc': wall.isoformat().replace('+00:00', 'Z'), 'monotonic_ns': str(monotonic_ns)}

    def entry_ready(self):
        monotonic = time.monotonic()
        return (self._valid and 0 <= monotonic - self._reference.monotonic_anchor < 300
                and abs(self._timestamp(monotonic) - (self._reference.wall_anchor + monotonic
                        - self._reference.monotonic_anchor)) <= 0.25)

    def record(self):
        record = self._reference.record() | {'entry_ready': self.entry_ready()}
        if self._using_holdover:
            record['mode'] = 'PERSISTED_NTP_HOLDOVER'
        if self._checkpoint_saved is not None:
            record['recovery_anchor_saved'] = self._checkpoint_saved
        return record

    def poll(self, *, before_install=None):
        """Install a fresh sample on the owner thread and sample off-thread."""
        now = time.monotonic()
        try:
            reference = self._results.get_nowait()
        except Empty:
            pass
        else:
            self._worker = None
            if reference is None or not 0 <= now - reference.monotonic_anchor < 30:
                self._valid = False
            else:
                candidate = reference.wall_anchor + now - reference.monotonic_anchor
                current = self._timestamp(now) if self._reference else candidate
                if before_install is not None:
                    try:
                        before_install(reference)
                    except Exception:
                        self._valid = False
                        self._next_refresh = now + 10
                        raise
                phase_error = candidate - current
                self._anchor, self._utc = now, current
                self._correction = max(-0.5, min(0.5, phase_error))
                self._reference = reference
                self._valid = True
                self._using_holdover = False
                self._checkpoint_reference()
            self._next_refresh = now + (120 if self.entry_ready() else 10)
        if self._worker is None and now >= self._next_refresh:
            def refresh():
                try:
                    reference = measure_reference()
                except Exception:
                    reference = None
                self._results.put(reference)

            worker = Thread(target=refresh, name='OnlineTrader-UTC', daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._valid = False
                self._next_refresh = now + 10
            else:
                self._worker = worker
