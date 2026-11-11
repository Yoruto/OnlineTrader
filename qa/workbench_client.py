"""External visible desktop exercise using real product IPC, never SDK facts."""
import argparse
import json
from pathlib import Path
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from online_trader.desktop.workbench import WorkbenchWindow


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--pipe',required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    app=QApplication([]);window=WorkbenchWindow(args.pipe);window.timer.setInterval(200);window.show()
    began=time.monotonic();state={'scope':'OFFLINE_REAL_WINDOWS_WORKBENCH','pause_requested':False,'status':'RUNNING'}
    def write():args.output.joinpath('desktop-result.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    def check():
        try:
            if time.monotonic()-began>18:raise AssertionError('Desktop rehearsal deadline: '+window.notice_label.text())
            if window._worker is not None or window.view is None:return
            if not state['pause_requested'] and not window._stale:
                assert window.view['cycles'][0]['remaining_quantity']==3
                state.update(was_visible=window.isVisible(),initial_owned_quantity=3,account_key=window.view['account_key'])
                window.tabs.setCurrentIndex(1);app.processEvents();window.grab().save(str(args.output/'workbench-market.png'))
                window.pause_entries();state['pause_requested']=True;write();return
            if window.view['cycles'] and window.view['cycles'][0]['remaining_quantity']==1 and not window._stale:
                cycle=window.view['cycles'][0]
                assert cycle['phase2_status']=='DONE' and cycle['break_even_status']=='ACTIVE'
                assert window.view['entry_authority']=='DISARMED'
                assert window.view['connection']['broker_connected'] is False
                window.tabs.setCurrentIndex(4);app.processEvents();window.grab().save(str(args.output/'workbench-protection.png'))
                state.update(status='PASS',final_owned_quantity=1,entry_authority='DISARMED',closed_locally=True,
                    protection_continues_after_pause=True,broker_connected=False,view=window.view)
                write();window.close();app.quit()
        except BaseException as error:
            state.update(status='FAIL',error=str(error));write();window.close();app.quit()
    timer=QTimer();timer.setInterval(50);timer.timeout.connect(check);timer.start();window.refresh();app.exec()
    return 0 if state['status']=='PASS' else 2


if __name__=='__main__':raise SystemExit(main())
