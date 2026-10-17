import pytest
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase, QFont
from src.app import main_window as main

_app = None


@pytest.fixture
def window(tmp_path, monkeypatch):
    global _app
    _app=QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    _app.setFont(QFont("Microsoft YaHei",9))
    monkeypatch.setattr(main,'QSettings',lambda *args:QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    w=main.MainWindow()
    yield w
    w.close()
    _app.processEvents()


def test_default_disabled_and_mode_visibility(window):
    w=window
    assert not w.chk_chapter_max_duration.isChecked()
    assert w.spn_chapter_max_duration.value()==60 and not w.spn_chapter_max_duration.isEnabled()
    assert not w.chk_execution_duration.isChecked() and not w.spn_execution_duration.isEnabled()
    assert not w.chk_execution_time.isChecked() and not w.time_execution.isEnabled()
    w.chk_chapter_max_duration.setChecked(True)
    assert w.spn_chapter_max_duration.isEnabled()
    w.cmb_split_mode.setCurrentIndex(1)
    assert w.chk_chapter_max_duration.isHidden() and w.spn_chapter_max_duration.isHidden()
    w.chk_execution_duration.setChecked(True);w.chk_execution_time.setChecked(True)
    assert w.spn_execution_duration.isEnabled() and w.time_execution.isEnabled()
    assert w.chk_execution_duration.parentWidget() is w.chk_execution_time.parentWidget()
    w._set_config_inputs_enabled(False)
    assert not w.spn_execution_duration.isEnabled() and not w.time_execution.isEnabled()


def test_config_round_trip_and_pause_button_interlock(window):
    w=window
    w.chk_chapter_max_duration.setChecked(True);w.spn_chapter_max_duration.setValue(2)
    w.chk_execution_duration.setChecked(True);w.spn_execution_duration.setValue(10)
    cfg=w._get_current_config()
    assert cfg['chapter_max_duration_enabled'] and cfg['chapter_max_duration_minutes']==2
    assert cfg['execution_duration_enabled'] and cfg['execution_duration_minutes']==10
    w.chk_chapter_max_duration.blockSignals(True);w.chk_chapter_max_duration.setChecked(False)
    w._restore_production_controls()
    assert w.chk_chapter_max_duration.isChecked()
    w._is_producing=True;w._apply_button_lock_state('PAUSING')
    assert not any(b.isEnabled() for b in (w.btn_start,w.btn_pause,w.btn_resume))
    w._apply_button_lock_state('PAUSED')
    assert w.btn_resume.isEnabled() and not w.btn_start.isEnabled()


def test_pause_notification_cannot_reenable_start_while_worker_is_running(window):
    from types import SimpleNamespace
    w=window;w._is_producing=True
    w.bridge.worker=SimpleNamespace(isRunning=lambda:True)
    w._apply_button_lock_state('PAUSED')
    assert not w.btn_start.isEnabled() and not w.btn_resume.isEnabled()
    w.bridge.worker=None
    w._apply_button_lock_state('PAUSED')
    assert w.btn_resume.isEnabled() and not w.btn_start.isEnabled()


def test_monitor_order_equal_rows_and_ui_uses_cached_data(window, monkeypatch):
    w=window;bar=w.resource_monitor_bar
    expected=[bar.lbl_cpu,bar.lbl_cpu_power,bar.lbl_mem,bar.lbl_gpu_load,bar.lbl_gpu_clock,bar.lbl_gpu_power,
              bar.lbl_gpu_temp,bar.lbl_gpu_mem,bar.lbl_gpu_memory_clock,bar.lbl_gpu_memory_util]
    assert [bar.layout().itemAt(i).widget() for i in range(10)]==expected
    second=w.monitor_second_row.layout()
    assert [second.itemAt(i).widget() for i in range(3)]==[bar.lbl_disk,bar.lbl_output_disk,w.gpu_control_frame]
    assert not hasattr(bar,'details_button') and not hasattr(bar,'details')
    assert bar.height()==w.monitor_second_row.height()>=48
    assert all(field.width()>=104 for field in (w.spn_gpu_temp_limit,w.spn_gpu_temp_resume,w.spn_gpu_cooling_minutes))
    assert not any('硬件负载' in label.text() for label in bar.findChildren(QLabel))
    monkeypatch.setattr(bar.sampler,'snapshot',lambda:{'cpu':12,'cpu_power':27.6,'disk_read':1.2,'disk_write':3.4,'disk_busy':25,'disk_label':'H: 磁盘0'})
    monkeypatch.setattr(bar.sampler,'gpu',lambda *a,**k:{'gpu_util':42,'temperature':58,'power':5.3,'core_clock':345,'memory_clock':405,'memory_util':16})
    # UI refresh must not launch subprocesses; sampler remains independent.
    import subprocess
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('UI performed I/O')))
    bar._refresh_metrics()
    assert bar.lbl_cpu_power.text()=='CPU功耗: 27.6W'
    assert bar.lbl_gpu_load.text()=='GPU: 42%'
    assert '5.3W' in bar.lbl_gpu_power.text() and '25%' in bar.lbl_disk.text()
    assert '读1.2MB/s 写3.4MB/s' in bar.lbl_disk.text()
    assert bar.lbl_gpu_clock.text()=='GPU频率: 345 MHz'
    assert bar.lbl_gpu_memory_clock.text()=='显存频率: 405 MHz'
    assert bar.lbl_gpu_memory_util.text()=='显存接口: 16%'


def test_monitor_font_fits_complete_metrics(window, monkeypatch):
    w=window;bar=w.resource_monitor_bar
    monkeypatch.setattr(bar.sampler,'snapshot',lambda:{'cpu':100,'cpu_power':56.7,'ram_used':19.5,'ram_total':39.8,'ram_pct':49})
    monkeypatch.setattr(bar.sampler,'gpu',lambda:{'gpu_util':100,'temperature':80,'power':50.5,'core_clock':1395,'memory_clock':5001,'memory_util':100,'mem_used':4096,'mem_total':4096})
    w.show()
    for width in (1100,1280,1600):
        w.resize(width,1000);_app.processEvents();bar._refresh_metrics();_app.processEvents()
        assert bar._metric_font_size>=(12 if width>=1280 else 10)
        assert bar.height()==w.monitor_second_row.height()
        for label in bar.metric_labels:
            assert label.fontMetrics().horizontalAdvance(label.text())+10<=label.width()


def test_disk_monitor_frame_font_and_large_rates_fit(window, monkeypatch):
    w=window;bar=w.resource_monitor_bar
    snapshot={'cpu':12,'disk_label':'H: 磁盘0','disk_read':0,'disk_write':0,'disk_busy':0}
    monkeypatch.setattr(bar.sampler,'snapshot',lambda:snapshot)
    monkeypatch.setattr(bar.sampler,'gpu',lambda *a,**k:{})
    w.show();w.resize(1280,900);_app.processEvents()
    bar._refresh_metrics();_app.processEvents()
    initial_disk=bar.lbl_disk.width();initial_progress=w.progress_bar.width()
    assert 'border:1px solid #333345' in bar.lbl_disk.styleSheet()
    assert 'color:#A0C8CA' in bar.lbl_disk.styleSheet()
    for read,write in [(12345.6,98765.4),(1234567.8,9876543.2)]:
        snapshot.update(disk_read=read,disk_write=write,disk_busy=100)
        bar._refresh_metrics();_app.processEvents()
        assert bar.lbl_disk.font().pixelSize()==bar.lbl_cpu.font().pixelSize()
        assert bar.lbl_disk.fontMetrics().horizontalAdvance(bar.lbl_disk.text())+10<=bar.lbl_disk.width()
        assert bar.lbl_disk.width()>=initial_disk
        assert w.progress_bar.width()<=initial_progress
        assert bar.height()==w.monitor_second_row.height()


def test_disk_monitor_switches_single_dual_and_narrow_layout(window, monkeypatch):
    w=window;bar=w.resource_monitor_bar
    disks=[{'disk_label':'程序/输出 H: 磁盘0','disk_read':12345.6,'disk_write':98765.4,'disk_busy':100}]
    monkeypatch.setattr(bar.sampler,'snapshot',lambda:{'disks':disks})
    monkeypatch.setattr(bar.sampler,'gpu',lambda *a,**k:{})
    w.show();w.resize(1600,1000);_app.processEvents();bar._refresh_metrics();_app.processEvents()
    assert bar.lbl_output_disk.isHidden()
    single_progress=w.progress_bar.width()
    disks[:]=[dict(disks[0],disk_label='程序 H: 磁盘0'),dict(disks[0],disk_label='输出 D: 磁盘1')]
    for width in (1600,1280,1100):
        w.resize(width,1000);_app.processEvents();bar._refresh_metrics();_app.processEvents()
        assert not bar.lbl_output_disk.isHidden()
        assert w.width()==width
        assert bar.height()==w.monitor_second_row.height()
        assert w.progress_bar.width()>=60 and w.progress_bar.width()<single_progress
        for label in (bar.lbl_disk,bar.lbl_output_disk):
            assert label.font().pixelSize()==bar.lbl_cpu.font().pixelSize()
            assert max(label.fontMetrics().horizontalAdvance(line) for line in label.text().splitlines())+10<=label.width()
            assert len(label.text().splitlines())*label.fontMetrics().height()+8<=label.height()
    disks[:]=[dict(disks[0],disk_label='程序/输出 H: 磁盘0')]
    bar._refresh_metrics();_app.processEvents()
    assert bar.lbl_output_disk.isHidden() and bar.height()==w.monitor_second_row.height()==48


def test_schedule_controls_compact_aligned_and_time_editable(window):
    from PySide6.QtCore import QTime
    w=window;w.resize(1280,900);w.show();_app.processEvents()
    pairs=[(w.chk_execution_duration,w.spn_execution_duration),(w.chk_execution_time,w.time_execution)]
    for check,field in pairs:
        assert 0<=field.x()-(check.x()+check.width())<=8
    assert w.spn_execution_duration.height()==w.time_execution.height()
    assert w.spn_execution_duration.y()==w.time_execution.y()
    assert not w.time_execution.calendarPopup()
    w.chk_execution_time.setChecked(True)
    w.time_execution.setTime(QTime(15,20))
    assert w._get_current_config()['execution_time']=='15:20'
