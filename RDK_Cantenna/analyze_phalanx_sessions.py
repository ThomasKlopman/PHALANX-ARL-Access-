"""Offline PHALANX session inventory, replay, and exploratory detection analysis.
Run from RDK_Cantenna with its .venv Python; does not connect to any hardware.
"""
import argparse
import csv
import json
import math
from pathlib import Path
from statistics import median

REFERENCE = [
    ('W1','cadet_toward',30,7.33,1),
    ('W2','cadet_toward',30,11.41,1),
    ('W3','cadet_toward',30,4.74,1),
    ('P1','metal_sheet_toward',30,8.58,1),
    ('P2','metal_sheet_toward',30,7.31,1),
    ('P3','metal_sheet_away',10,6.83,-1),
]

def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--captures', type=Path, default=Path.home()/'PHALANX_data'/'captures')
    ap.add_argument('--out', type=Path, default=Path.home()/'PHALANX_data'/'analysis')
    ap.add_argument('--inventory-only', action='store_true', help='Skip DSP if current code differs')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    folders = sorted(args.captures.glob('*.session'))
    if not folders:
        ap.error(f'No .session folders found in {args.captures}')
    inventory=[]; detections=[]; frames=[]
    for order, folder in enumerate(folders, 1):
        manifest = folder/'session.json'
        if not manifest.exists():
            print(f'SKIP {folder.name}: missing session.json'); continue
        m=json.loads(manifest.read_text(encoding='utf-8'))
        n_files=len(list(folder.glob('frame_*.npz')))
        inventory.append(dict(order=order,session=folder.name,started_utc=m.get('started_utc',''),
            state=m.get('state',''),reason=m.get('reason',''),saved_frames=m.get('saved_frames',''),
            saved_samples=m.get('saved_samples',''),acquired_complete_blocks=m.get('acquired_complete_blocks',''),
            npz_files=n_files,proposed_trial='',label_confidence='',contaminated='',notes='',
            processing_status='not_run'))
    inv_fields=list(inventory[0])
    write_csv(args.out/'session_inventory.csv', inventory, inv_fields)
    refs=[dict(trial_id=ident,scenario=scenario,distance_ft=ft,time_s=t,
               reference_velocity_mps=round(sign*ft*0.3048/t,5),
               reference_speed_mps=round(ft*0.3048/t,5),
               matched_session='',match_confidence='',notes='')
          for ident,scenario,ft,t,sign in REFERENCE]
    write_csv(args.out/'reference_trials.csv',refs,list(refs[0]))
    if args.inventory_only:
        print(f'Inventory and reference trials written to {args.out}'); return
    try:
        from phalanx.hardware.recorded import RecordedRadar
        from phalanx.dsp.pipeline import ProcessorConfig, RangeDopplerProcessor
    except ImportError as e:
        print(f'Cannot import PHALANX DSP: {e}. Inventory CSV was saved.'); return
    for item in inventory:
        folder=args.captures/item['session']
        if item['state'] != 'complete':
            item['processing_status']='skipped_not_complete'; continue
        source=None; count=0
        try:
            source=RecordedRadar(folder, loop=False, realtime=False)
            source.open()
            params=source.params
            proc=RangeDopplerProcessor(params, ProcessorConfig())
            source.start()
            while True:
                try:
                    frame=source.capture()
                except StopIteration:
                    break
                result=proc.process(frame)
                dets=list(result.detections)
                frames.append(dict(session=item['session'],frame_seq=frame.seq,
                                   detection_count=len(dets)))
                for d in dets:
                    detections.append(dict(session=item['session'],frame_seq=frame.seq,
                        range_m=float(d.range_m),velocity_mps=float(d.velocity_mps),
                        snr_db=float(d.snr_db)))
                count+=1
            item['processing_status']=f'processed_{count}_frames'
        except Exception as e:
            item['processing_status']=f'ERROR_{type(e).__name__}: {str(e)[:150]}'
            print(f"{item['session']}: {item['processing_status']}")
        finally:
            if source is not None:
                try: source.stop()
                except Exception: pass
                try: source.close()
                except Exception: pass
    write_csv(args.out/'session_inventory.csv',inventory,inv_fields)
    write_csv(args.out/'detections.csv',detections,['session','frame_seq','range_m','velocity_mps','snr_db'])
    write_csv(args.out/'frame_counts.csv',frames,['session','frame_seq','detection_count'])
    summary=[]
    for item in inventory:
        ds=[d for d in detections if d['session']==item['session']]
        fs=[f for f in frames if f['session']==item['session']]
        summary.append(dict(order=item['order'],session=item['session'],state=item['state'],
            processing_status=item['processing_status'],frames_processed=len(fs),detections=len(ds),
            fraction_frames_with_detection=(sum(f['detection_count']>0 for f in fs)/len(fs) if fs else ''),
            median_abs_velocity_mps=(median(abs(d['velocity_mps']) for d in ds) if ds else ''),
            max_snr_db=(max(d['snr_db'] for d in ds) if ds else '')))
    write_csv(args.out/'session_summary.csv',summary,list(summary[0]))
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,ax=plt.subplots(figsize=(12,4))
        ax.bar([s['order'] for s in summary],[s['detections'] for s in summary])
        ax.set(xlabel='Session order (chronological)',ylabel='CFAR detections',title='PHALANX detections by session (unlabeled trials)')
        fig.tight_layout(); fig.savefig(args.out/'detections_by_session.png',dpi=160); plt.close(fig)
    except ImportError:
        print('matplotlib unavailable; skipping plot')
    print(f'Processed {len(folders)} sessions; {len(detections)} detections. Outputs: {args.out}')
    print('Labels intentionally left blank. Match trials only using independent evidence.')

if __name__=='__main__':
    main()
