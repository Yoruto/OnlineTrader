"""Explicit read-only NTP calibration for the optional simulation boundary."""
from dataclasses import dataclass
from datetime import datetime,timezone
import socket,statistics,struct,time
from online_trader.errors import TraderError

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
