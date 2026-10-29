"""Exercise the frozen candidate with a clean PATH and disposable user data."""
import argparse
import json
import os
import subprocess
import shutil
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='书声冷启动测试_',dir=args.output.parent.resolve()) as folder:
        root = Path(folder)
        copied=root/'old-package'
        shutil.copytree(args.package.resolve(),copied)
        executable=copied/'ShuShengPublisher.exe'
        environment = dict(os.environ)
        for name in ('PYTHONHOME', 'PYTHONPATH', 'PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_NODEJS_PATH'):
            environment.pop(name, None)
        system = Path(os.environ['SystemRoot'])
        environment.update(PATH=str(system/'System32') + os.pathsep + str(system),
            LOCALAPPDATA=str(root/'user'), QT_QPA_PLATFORM='offscreen')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        report_path = root/'self-check.json'
        result = subprocess.run([str(executable),'--self-check','--output',str(report_path)],
            env=environment,cwd=root,creationflags=flags,timeout=60)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        assert result.returncode == 0 and report['passed'], 'Frozen runtime self-check failed'
        processes = []
        try:
            def launch():
                process = subprocess.Popen([str(executable)],env=environment,cwd=root,
                    creationflags=flags,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                processes.append(process)
                return process
            first = launch()
            deadline = time.monotonic() + 20
            lock = root/'user/ShuShengPublisher/publisher.lock'
            while not lock.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(.1)
            time.sleep(3)
            assert first.poll() is None and lock.exists(), 'Standalone GUI did not stay running'
            second = launch()
            assert second.wait(timeout=15) == 0 and first.poll() is None, 'Second instance was not rejected cleanly'
            def request_exit():
                response=subprocess.run([str(executable),'--request-exit'],env=environment,cwd=root,
                    creationflags=flags,timeout=15)
                assert response.returncode==0,'Owned exit request was not accepted'
            def wait_for_sweep(expected):
                # GUI exit precedes the external watchdog's final resource sweep.
                control=root/'user/ShuShengPublisher/shutdown'
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    requests=list(control.glob('*/request.json'))
                    if len(requests)==expected and all((p.parent/'result.json').exists() for p in requests):
                        time.sleep(.3)
                        return
                    time.sleep(.1)
                raise AssertionError('Exit helper did not finish its resource sweep')
            request_exit()
            assert first.wait(timeout=25)==0,'Real GUI exit did not complete normally'
            wait_for_sweep(1)
            renamed=root/'renamed-package'
            deadline=time.monotonic()+5
            while True:
                try:copied.rename(renamed);break
                except PermissionError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.1)
            executable=renamed/'ShuShengPublisher.exe'
            third = launch()
            time.sleep(4)
            assert third.poll() is None, 'Dead-process lock did not recover'
            request_exit()
            assert third.wait(timeout=25)==0,'Replacement instance did not exit normally'
            wait_for_sweep(2)
            assert renamed.resolve().relative_to(root.resolve()).parts
            shutil.rmtree(renamed)
            # The helper lives outside the old package, but its control-folder
            # cwd must also be released before TemporaryDirectory cleans up.
            control=root/'user/ShuShengPublisher/shutdown'
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                requests=list(control.glob('*/request.json'))
                if len(requests)==2 and all((path.parent/'result.json').exists() for path in requests):break
                time.sleep(.1)
            assert len(requests)==2 and all((path.parent/'result.json').exists() for path in requests),'Exit helper did not finish'
            time.sleep(.3)
            report['package_checks'] = {'clean_path':True,'empty_user_data':True,
                'standalone_window':True,'single_instance':True,'dead_lock_recovery':True,
                'real_exit':True,'old_package_rename_delete':True,'replacement_instance':True}
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'report':str(args.output)}))


if __name__ == '__main__':
    main()
