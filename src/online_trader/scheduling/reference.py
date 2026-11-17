"""Explicit read-only NTP calibration for the optional simulation boundary."""
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
from queue import Empty, SimpleQueue
import secrets
import socket,statistics,struct,time
from threading import Thread
from online_trader.errors import TraderError
from online_trader.scheduling.clock import NS_PER_SECOND, SystemClock
from online_trader.scheduling.holdover import within_holdover_budget

@dataclass(frozen=True,slots=True)
class ReferenceClock:
    wall_anchor:float
    monotonic_anchor:float
    offset_seconds:float
    samples:tuple
    error_seconds:float=0.0
    def now(self):
        elapsed=time.monotonic()-self.monotonic_anchor
        if not 0<=elapsed<300:raise TraderError('E_CLOCK','Simulation clock reference expired or regressed')
        return datetime.fromtimestamp(self.wall_anchor+elapsed,timezone.utc)
    def record(self):
        return {'mode':'READ_ONLY_NTP_MONOTONIC','offset_seconds':str(self.offset_seconds),'samples':list(self.samples),'os_clock_changed':False}


class LocalWallClock(SystemClock):
    """DSV startup and execution read the Windows wall clock."""
    def __init__(self):
        super().__init__()
        self._sample = None

    def capture(self):
        return self._sample if self._sample is not None else super().capture()

    def begin_turn(self):
        self._sample = super().capture()

    def end_turn(self):
        self._sample = None

    def now(self):
        return self._sample.utc if self._sample is not None else self.utc_now()

    def record(self):
        return {'mode':'SYSTEM_WALL_CLOCK','os_clock_changed':False}


def check_wall_clock():
    offsets = []
    for host in ('ntp.aliyun.com', 'time.windows.com', 'pool.ntp.org'):
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
            'reference':clock.record(),'os_clock_changed':False}

def measure_reference():
    samples=[];valid=[]
    for host in ('ntp.aliyun.com','ntp1.aliyun.com'):
        for attempt in range(2):
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
                    client.settimeout(1.5);packet=bytearray(48);packet[0]=0x23;t1=time.time();stamp=t1+2208988800
                    struct.pack_into('!II',packet,40,int(stamp),int(stamp%1*2**32));client.sendto(packet,(host,123));raw,_=client.recvfrom(1024);t4=time.time()
                if len(raw)<48 or raw[0]>>6==3 or raw[0]&7!=4 or not 1<=raw[1]<=15 or raw[24:32]!=packet[40:48]:raise ValueError()
                fields=struct.unpack('!12I',raw[:48]);t2=fields[8]+fields[9]/2**32-2208988800;t3=fields[10]+fields[11]/2**32-2208988800
                offset=((t2-t1)+(t3-t4))/2;delay=(t4-t1)-(t3-t2)
                if not 0<=delay<=0.5:raise ValueError()
                valid.append((host,offset));samples.append({'host':host,'sample':attempt,'valid':True,'offset_seconds':str(offset),'rtt_seconds':str(delay)})
            except Exception:samples.append({'host':host,'sample':attempt,'valid':False})
    if len(valid)<3 or len({row[0] for row in valid})!=2 or max(row[1] for row in valid)-min(row[1] for row in valid)>0.25:
        raise TraderError('E_CLOCK','Insufficient consistent NTP samples; no opening permitted')
    offset=statistics.median(row[1] for row in valid)
    return ReferenceClock(time.time()+offset,time.monotonic(),offset,tuple(samples))


def measure_utc_anchor():
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
    error = max(float(row['rtt_seconds']) for row in samples if row['valid']) / 2 + max(estimates) - min(estimates)
    return ReferenceClock(anchor, monotonic, None, tuple(samples), error)


class CalibratedClock(LocalWallClock):
    """Advance externally calibrated UTC without consulting the system wall clock."""

    def __init__(self, *, recovery_anchor=None, recovering=False, lazy=False):
        super().__init__()
        self._recovery_anchor = recovery_anchor
        self._checkpoint_saved = None
        self._trusted_reference_mono = None
        self._trusted_error = None
        self._holding = False
        self._correction = 0.0
        self._last_poll = time.monotonic()
        self._entry_ready_since = None
        self._results = SimpleQueue()
        self._worker = None
        if lazy:
            self._reference = self._utc = None
            self._valid = False
            self._anchor = self._next_refresh = self._last_poll
            return
        try:
            self._reference = measure_utc_anchor()
            self._anchor = self._reference.monotonic_anchor
            self._trusted_reference_mono = self._anchor
            self._trusted_error = self._reference.error_seconds
            self._utc = self._reference.wall_anchor
        except TraderError as error:
            pending = recovering() if callable(recovering) else recovering
            saved = recovery_anchor.load() if error.code == 'E_CLOCK' and pending and recovery_anchor else None
            if saved is None:
                raise
            (self._utc, self._anchor, self._trusted_reference_mono, self._correction,
             self._trusted_error) = saved
            self._reference = None
            self._holding = True
        self._valid = not self._holding
        self._last_poll = time.monotonic()
        self._next_refresh = self._anchor + 120 if self._valid else self._last_poll
        if self._valid:
            self._checkpoint(self._anchor)

    def _applied_correction(self, now):
        applied = min(abs(self._correction), max(0, now - self._anchor) * .01)
        return applied if self._correction >= 0 else -applied

    def _timestamp(self, now):
        if self._utc is None:
            raise TraderError('E_CLOCK', 'Initial application UTC calibration is unavailable')
        return self._utc + now - self._anchor + self._applied_correction(now)

    def utc_now(self):
        return datetime.fromtimestamp(self._timestamp(time.monotonic()), timezone.utc)

    def _checkpoint(self, now):
        if self._recovery_anchor is not None:
            self._checkpoint_saved = self._recovery_anchor.save(
                self._timestamp(now), now, self._trusted_reference_mono,
                self._correction - self._applied_correction(now), self._trusted_error)

    def bind_recovery_anchor(self, anchor):
        self._recovery_anchor = anchor
        if self._valid:
            self._checkpoint(time.monotonic())

    def entry_ready(self):
        now = time.monotonic()
        if not self._valid or not 0 <= now - self._last_poll <= 30:
            return False
        reference = self._reference
        elapsed = now - reference.monotonic_anchor
        return (0 <= elapsed < 300
                and abs(self._timestamp(now) - (reference.wall_anchor + elapsed)) <= .25)

    def protection_ready(self):
        anchor = self._trusted_reference_mono
        return anchor is not None and within_holdover_budget(time.monotonic() - anchor, self._trusted_error)

    def entry_stable(self):
        since = self._entry_ready_since
        return since is not None and self.entry_ready() and time.monotonic() - since >= 2

    def record(self):
        return {'mode': 'PERSISTED_NTP_HOLDOVER' if self._holding else 'APPLICATION_UTC',
                'entry_ready': self.entry_ready(), 'protection_ready': self.protection_ready(),
                'recovery_anchor_saved': self._checkpoint_saved,
                'os_clock_changed': False}

    def health(self, sample):
        if not self.entry_ready():
            return {'reference_status': 'REFERENCE_UNAVAILABLE', 'utc_error_ns': None}
        reference = self._reference
        age = sample.monotonic / NS_PER_SECOND - reference.monotonic_anchor
        uncertainty = reference.error_seconds + max(0, age) / 1000
        offset = reference.wall_anchor + age - sample.utc.timestamp()
        return {'reference_status': 'OBSERVED_OK',
                'reference': {'source': 'APPLICATION_UTC_NTP', 'offset_ns': str(round(offset * NS_PER_SECOND)),
                              'estimated_error_ns': str(round(uncertainty * NS_PER_SECOND))},
                'utc_error_ns': str(round((uncertainty + abs(offset)) * NS_PER_SECOND))}

    def poll(self, *, before_install=None):
        now = time.monotonic()
        if not self.entry_ready():
            self._entry_ready_since = None
        if not 0 <= now - self._last_poll <= 30:
            self._valid = False
            self._next_refresh = now
        self._last_poll = now
        try:
            reference = self._results.get_nowait()
        except Empty:
            pass
        else:
            self._worker = None
            self._valid = reference is not None and 0 <= now - reference.monotonic_anchor < 30
            if self._valid:
                candidate = reference.wall_anchor + now - reference.monotonic_anchor
                current = candidate if self._utc is None else self._timestamp(now)
                self._valid = abs(candidate - current) <= .25
                if self._valid:
                    if before_install is not None:
                        self._valid = False
                        self._next_refresh = now + 10
                        before_install(reference)
                        self._valid = True
                    self._anchor, self._utc = now, current
                    self._correction = candidate - current
                    self._reference = reference
            self._next_refresh = now + (120 if self.entry_ready() else 10)
        if self.entry_ready() and self._reference.monotonic_anchor != self._trusted_reference_mono:
            self._trusted_reference_mono = self._reference.monotonic_anchor
            self._trusted_error = self._reference.error_seconds + abs(self._correction)
            self._holding = False
            self._checkpoint(now)
        if not self.entry_ready():
            self._entry_ready_since = None
        elif self._entry_ready_since is None:
            self._entry_ready_since = now
        if self._worker is None and now >= self._next_refresh:
            self._worker = Thread(target=self._refresh, name='OnlineTrader-UTC', daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._valid = False
                self._worker = None
                self._next_refresh = now + 10

    def _refresh(self):
        try:
            reference = measure_utc_anchor()
        except Exception:
            reference = None
        self._results.put(reference)


class ReferenceMonitor:
    """A bounded, read-only diagnostic observer. No SDK or journal ownership."""

    def __init__(self, clock, *, interval_seconds=3600, measurement=measure_reference):
        self.clock = clock
        self.interval_ns = interval_seconds * NS_PER_SECOND
        self.measurement = measurement
        self.results = SimpleQueue()
        self.worker = None
        self.generation = 0
        self.next_ns = 0
        self.latest = None

    def _read(self, generation):
        try:
            reading = self.measurement()
            offsets = [Decimal(row['offset_seconds']) for row in reading.samples if row['valid']]
            rtt = max(Decimal(row['rtt_seconds']) for row in reading.samples if row['valid'])
            offset = Decimal(str(reading.offset_seconds))
            self.results.put((generation, self.clock.capture(), {
                'source': 'NTP_TWO_SOURCE_READ_ONLY', 'offset_ns': str(int(offset * NS_PER_SECOND)),
                'round_trip_ns': str(int(rtt * NS_PER_SECOND)),
                'estimated_error_ns': str(int((rtt + max(offsets) - min(offsets)) * NS_PER_SECOND)),
            }))
        except Exception:
            self.results.put((generation, self.clock.capture(), None))

    def poll(self, sample, *, invalidate=False):
        if invalidate:
            self.generation += 1
            self.latest = None
            self.next_ns = 0
        try:
            generation, at, value = self.results.get_nowait()
        except Empty:
            pass
        else:
            if generation == self.generation:
                self.latest = at, value
        if sample.monotonic >= self.next_ns and (self.worker is None or not self.worker.is_alive()):
            self.next_ns = sample.monotonic + self.interval_ns
            self.worker = Thread(target=self._read, args=(self.generation,),
                                 name='OnlineTrader-clock-reference', daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                self.latest = sample, None
        if self.latest is None:
            return {'reference_status': 'UNOBSERVED'}
        at, reading = self.latest
        age = sample.monotonic - at.monotonic
        if not 0 <= age <= self.interval_ns or reading is None or at.clock_epoch != sample.clock_epoch:
            return {'reference_status': 'REFERENCE_UNAVAILABLE'}
        # NTP estimates are diagnostic. Use a short-lived, conservative bound
        # only for elapsed-time recovery, with no correction of decision UTC.
        utc_error = None
        if age <= 30 * NS_PER_SECOND:
            uncertainty = int(reading['estimated_error_ns']) + abs(int(reading['offset_ns']))
            utc_error = str(uncertainty + int(at.sample_span_ns) + age // 1000)
        return {'reference_status': 'OBSERVED_OK', 'reference': reading | {
                    'observed_at_utc': at.now_utc, 'sample_age_ns': str(age)},
                'utc_error_ns': utc_error}
