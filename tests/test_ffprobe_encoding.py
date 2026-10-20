"""Real FFprobe output must be readable under a normal Windows GUI launch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from src.audio import ffmpeg_utils


ROOT = Path(__file__).resolve().parents[1]


def test_chinese_mp4_probe_in_non_utf8_python_process(tmp_path):
    video = tmp_path/'巴菲特致股东的信_第38集_1994年_57分钟.mp4'
    shutil.copy2(ROOT/'resources/media_probe_h264_aac.mp4', video)
    script = '''import json,locale,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from src.audio.ffmpeg_utils import get_audio_info
print(json.dumps({'encoding':locale.getencoding(),'info':get_audio_info(Path(sys.argv[2]))}))
'''
    env = dict(os.environ, PYTHONUTF8='0')
    result = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', script, str(ROOT), str(video)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=35, check=True)
    data = json.loads(result.stdout)
    assert data['info']['duration'] == pytest.approx(1, abs=.1)
    assert data['info']['sample_rate'] > 0 and data['info']['channels'] > 0
    assert b'UnicodeDecodeError' not in result.stderr


def test_native_wav_keeps_fast_path_without_ffprobe(tmp_path, monkeypatch):
    file = tmp_path/'原声.wav'
    with wave.open(str(file), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\x01\x01'*8000)
    def unexpected(*args, **kwargs):
        raise AssertionError('Standard WAV should not start FFprobe')
    monkeypatch.setattr(ffmpeg_utils.subprocess, 'run', unexpected)
    assert ffmpeg_utils.get_audio_info(file)['duration'] == 1


def test_chinese_wav_quality_log_is_readable_in_non_utf8_python_process(tmp_path):
    file = tmp_path/'巴菲特致股东的信_原声.wav'
    with wave.open(str(file), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\x01\x01'*8000)
    script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from src.audio.audio_qc import AudioQC
print(json.dumps(AudioQC().check_wav(Path(sys.argv[2]))))
'''
    result = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', script, str(ROOT), str(file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONUTF8='0'), timeout=35, check=True)
    assert json.loads(result.stdout)['valid']
    assert b'UnicodeDecodeError' not in result.stderr


@pytest.mark.parametrize('output', [None, b'', b'not-json', b'{"streams":[]}'])
def test_missing_or_invalid_probe_output_is_not_a_zero_length_success(tmp_path, monkeypatch, caplog, output):
    monkeypatch.setattr(ffmpeg_utils.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=b''))
    assert ffmpeg_utils.get_audio_info(tmp_path/'video.mp4') == {}
    assert caplog.records


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('ffprobe', 30),
    subprocess.CalledProcessError(1, 'ffprobe', stderr=b'Invalid data found when processing input')])
def test_probe_failure_remains_a_read_error(tmp_path, monkeypatch, caplog, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(ffmpeg_utils.subprocess, 'run', fail)
    assert ffmpeg_utils.get_audio_info(tmp_path/'video.mp4') == {}
    assert '获取音频信息失败' in caplog.text
