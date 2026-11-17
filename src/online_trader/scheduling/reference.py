"""Explicit read-only NTP calibration for the optional simulation boundary."""
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
from queue import Empty, SimpleQueue
import socket,statistics,struct,time
from threading import Thread
from online_trader.errors import TraderError
from online_trader.scheduling.clock import NS_PER_SECOND, SystemClock

@dataclass(frozen=True,slots=True)
class ReferenceClock:
    wall_anchor:float
    monotonic_anchor:float
    offset_seconds:float
    samples:tuple
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
