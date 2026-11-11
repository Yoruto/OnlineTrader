"""Read-only NTP reference anchored to monotonic time; never changes OS time."""
from dataclasses import dataclass
from datetime import datetime,timezone
import socket,statistics,struct,time


@dataclass(frozen=True)
class ClockReference:
    wall_anchor:float
    monotonic_anchor:float
    offset_seconds:float
    samples:tuple

    def now(self):
        elapsed=time.monotonic()-self.monotonic_anchor
        if not 0<=elapsed<300:raise ValueError("Clock reference expired or monotonic clock regressed")
        return datetime.fromtimestamp(self.wall_anchor+elapsed,timezone.utc)

    def record(self):
        return {"mode":"read-only NTP plus monotonic reference; OS clock unchanged","offset_seconds":self.offset_seconds,
                "wall_anchor":self.wall_anchor,"monotonic_anchor":self.monotonic_anchor,"valid_for_seconds":300,"samples":list(self.samples)}


def measure_clock():
    samples=[]
    for host in ("ntp.aliyun.com","ntp1.aliyun.com"):
        for attempt in range(2):
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
                    client.settimeout(1.5);packet=bytearray(48);packet[0]=0x23
                    t1=time.time();stamp=t1+2208988800
                    struct.pack_into("!II",packet,40,int(stamp),int((stamp%1)*2**32))
                    client.sendto(packet,(host,123));raw,_=client.recvfrom(1024);t4=time.time()
                if len(raw)<48 or raw[0]>>6==3 or raw[0]&7!=4 or not 1<=raw[1]<=15 or raw[24:32]!=packet[40:48]:
                    raise ValueError("Invalid NTP response binding")
                fields=struct.unpack("!12I",raw[:48])
                t2=fields[8]+fields[9]/2**32-2208988800;t3=fields[10]+fields[11]/2**32-2208988800
                offset=((t2-t1)+(t3-t4))/2;delay=(t4-t1)-(t3-t2)
                valid=0<=delay<=0.5 and abs(offset)<=10
                samples.append({"host":host,"attempt":attempt,"valid":valid,"offset_seconds":offset,"roundtrip_seconds":delay})
            except Exception as error:samples.append({"host":host,"attempt":attempt,"valid":False,"error_type":type(error).__name__})
    valid=[row for row in samples if row["valid"]]
    if len(valid)<3 or len({row["host"] for row in valid})!=2:raise ValueError("Insufficient valid NTP samples from both configured servers")
    offsets=[row["offset_seconds"] for row in valid]
    if max(offsets)-min(offsets)>0.25:raise ValueError("NTP sample dispersion exceeds bound")
    offset=statistics.median(offsets)
    return ClockReference(time.time()+offset,time.monotonic(),offset,tuple(samples))
