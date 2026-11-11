"""Prepare the explicit 2026-11-10 DSV comparison reference; never connects.

This dated input packet is not a product calendar generator or trading scope.
No credentials, NTP, SDK import, account catalogue or ledger is accessed.
"""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.runtime.comparison import read_dsv_profile
from online_trader.scheduling.calendar import TradingCalendar


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dsv-config',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();profile=read_dsv_profile(args.dsv_config)
    codes=[row['code'] for row in profile.to_record()['settings']['symbols']]
    ends={'DCE.p2701':'2026-11-10T15:00:00Z','DCE.l2701':'2026-11-10T15:00:00Z',
          'SHFE.bu2701':'2026-11-10T15:00:00Z','CZCE.OI701':'2026-11-10T15:00:00Z',
          'SHFE.ag2702':'2026-11-10T18:30:00Z'}
    if set(codes)!=set(ends):raise ValueError('The dated packet only covers the five currently reviewed contracts')
    calendar={'schema_version':'TRADING_CALENDAR/1','calendar_id':'dsv-simnow-night-20261110','calendar_version':'1',
        'complete':True,'coverage_from_utc':'2026-11-10T12:00:00Z','coverage_to_utc':'2026-11-11T08:00:00Z',
        'contracts':[]}
    # Explicit following daytime windows keep a completed 23:00 contract bound
    # to cabinet day 11 while silver continues until 02:30, without inventing
    # another live trading session in the intervening recess.
    for code in codes:
        sessions=[{'session_id':'night-20261111','trading_day':'2026-11-11','open_utc':'2026-11-10T13:00:00Z',
                   'close_utc':ends[code],'is_opening_session':True,'force_close_at_end':True}]
        for sid,start,end,opening,force in [('morning-1','01:00','02:15',True,False),
            ('morning-2','02:30','03:30',False,False),('afternoon','05:30','07:00',False,True)]:
            sessions.append({'session_id':sid+'-20261111','trading_day':'2026-11-11',
                'open_utc':'2026-11-11T'+start+':00Z','close_utc':'2026-11-11T'+end+':00Z',
                'is_opening_session':opening,'force_close_at_end':force})
        calendar['contracts'].append({'contract_id':code,'sessions':sessions})
    calendar=TradingCalendar.from_record(calendar)
    provenance={'kind':'DATED_DSV_COMPARISON_REFERENCE','generated_at_utc':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,'contracts':codes,
        'business_source':str(args.dsv_config),'private_configuration_exported':False,
        'calendar_basis':'Explicit Nov 10 night / Nov 11 cabinet-day reference, checked against DSV session rules and exchange hours.',
        'calendar_sources':['https://www.shfe.com.cn/services/calenderandholidays/tradinghours/',
            'https://www.shfe.com.cn/services/calenderandholidays/holiday/',
            'https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207144928864.pdf',
            'https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf'],
        'cabinet_day_source':'EXPLICIT_CALENDAR_REFERENCE_NOT_LIVE_CABINET_REPLY',
        'broker_connected':False,'ntp_requested':False,'credentials_read':False,'entry_authority':'DISARMED',
        'scope_created':False,'remaining_runtime_inputs':['SimNow credential reference','explicit finite account scope and limits'],
        'suggested_comparison_window_utc':{'from':'2026-11-10T12:55:00Z','to':'2026-11-10T18:31:00Z'}}
    for name,value in [('dsv-profile.json',profile.to_record()),('calendar.json',calendar.to_record()),('preparation.json',provenance)]:
        publish_new_bytes(canonical_bytes(value),args.output/name)
    print(args.output)


if __name__=='__main__':main()
