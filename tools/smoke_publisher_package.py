"""Exercise the frozen candidate with a clean PATH and disposable user data."""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    executable = args.package.resolve() / 'ShuShengPublisher.exe'
    with tempfile.TemporaryDirectory(prefix='书声冷启动测试_') as folder:
        root = Path(folder)
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
            first.terminate()
            first.wait(timeout=10)
            third = launch()
            time.sleep(4)
            assert third.poll() is None, 'Dead-process lock did not recover'
            report['package_checks'] = {'clean_path':True,'empty_user_data':True,
                'standalone_window':True,'single_instance':True,'dead_lock_recovery':True}
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
