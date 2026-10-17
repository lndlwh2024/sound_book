"""Two-row production monitor: all I/O belongs to the shared sampler."""
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy
from PySide6.QtGui import QFont, QFontMetrics
from ..utils.resource_sampler import ResourceSampler


class ResourceMonitorBar(QFrame):
    disk_height_changed = Signal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sampler = ResourceSampler.shared()
        self.setStyleSheet('QFrame{background:#15151C;border:1px solid #333345;border-radius:4px} QLabel{padding:4px;color:#A0C8CA}')
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 2, 3, 2)
        row.setSpacing(6)
        names = [('lbl_cpu','CPU: --%'),('lbl_cpu_power','CPU功耗: --W'),('lbl_mem','内存: --'),('lbl_gpu_load','GPU: --%'),
                 ('lbl_gpu_clock','GPU频率: -- MHz'),('lbl_gpu_power','GPU功耗: --W'),
                 ('lbl_gpu_temp','GPU温度: --°C'),('lbl_gpu_mem','显存: --'),
                 ('lbl_gpu_memory_clock','显存频率: -- MHz'),('lbl_gpu_memory_util','显存接口: --%')]
        self.metric_labels = []
        self._metric_font_size = None
        for attr, text in names:
            label = QLabel(text)
            setattr(self, attr, label)
            self.metric_labels.append(label)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            row.addWidget(label, 2 if attr in ('lbl_mem', 'lbl_gpu_mem') else 1)
        # Compatibility for old integrations; not part of the visible monitor.
        self.lbl_cpu_power.setToolTip('CPU整包功耗：RAPL Package0，最近采样区间平均功率；不支持或采样无效显示--')
        self.lbl_cuda_status = QLabel(self)
        self.lbl_cuda_status.hide()
        self.lbl_disk = QLabel('磁盘 读--MB/s 写--MB/s 活动--%')
        self.lbl_disk.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Expanding)
        self.lbl_output_disk = QLabel()
        self.lbl_output_disk.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Expanding)
        self.lbl_output_disk.hide()
        self._disk_snapshots = []
        self._disk_height = 48
        self.disk_width_budget = lambda: self.width()
        self.lbl_disk.setToolTip('当前输出目录所属物理磁盘；读写速率MB/s，活动率不是空间占用率')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_metrics)
        self.timer.start(1500)

    def _refresh_metrics(self):
        data, gpu = self.sampler.snapshot(), self.sampler.gpu()
        def fmt(value, spec='.1f'):
            return format(value, spec) if value is not None else '--'
        self.lbl_cpu.setText('CPU: '+fmt(data.get('cpu'))+'%')
        self.lbl_cpu_power.setText('CPU功耗: '+fmt(data.get('cpu_power'))+'W')
        self.lbl_mem.setText(f"内存: {fmt(data.get('ram_used'))}/{fmt(data.get('ram_total'))}GB ({fmt(data.get('ram_pct'),'.0f')}%)")
        self.lbl_gpu_load.setText('GPU: '+fmt(gpu.get('gpu_util'),'.0f')+'%')
        self.lbl_gpu_temp.setText('GPU温度: '+fmt(gpu.get('temperature'),'.0f')+'°C')
        self.lbl_gpu_power.setText('GPU功耗: '+fmt(gpu.get('power'))+'W')
        total, used = gpu.get('mem_total'), gpu.get('mem_used')
        self.lbl_gpu_mem.setText(f"显存: {fmt(used/1024 if used is not None else None,'.2f')}/{fmt(total/1024 if total else None,'.2f')}GB ({fmt(100*used/total if total and used is not None else None,'.0f')}%)")
        self._disk_snapshots = data.get('disks') or [data]
        self.lbl_output_disk.setVisible(len(self._disk_snapshots) > 1)
        for label, disk in zip((self.lbl_disk, self.lbl_output_disk), self._disk_snapshots):
            label.setToolTip(disk.get('disk_label', '程序/输出磁盘')+'；物理硬盘整体读写速率及活动率，非空间占用率')
        self.lbl_gpu_clock.setText('GPU频率: '+fmt(gpu.get('core_clock'),'.0f')+' MHz')
        self.lbl_gpu_memory_clock.setText('显存频率: '+fmt(gpu.get('memory_clock'),'.0f')+' MHz')
        self.lbl_gpu_memory_util.setText('显存接口: '+fmt(gpu.get('memory_util'),'.0f')+'%')

        self._fit_metric_font()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_metric_font()

    def _fit_metric_font(self):
        """Use the largest uniform font that fits every complete metric."""
        if not hasattr(self, 'metric_labels'):
            return
        row = self.layout()
        margins = row.contentsMargins()
        available = self.width() - margins.left() - margins.right() - row.spacing() * (len(self.metric_labels)-1)
        for size in range(14, 9, -1):
            font = QFont(self.font())
            font.setPixelSize(size)
            metrics = QFontMetrics(font)
            widths = [metrics.horizontalAdvance(label.text()) + 14 for label in self.metric_labels]
            disk_texts, disk_widths, disk_lines = self._disk_layout(metrics)
            if sum(widths) <= available and sum(disk_widths) <= self.disk_width_budget():
                break
        for index, (label, width) in enumerate(zip(self.metric_labels, widths)):
            if self._metric_font_size != size:
                label.setStyleSheet(f"font-size:{size}px;")
            label.setToolTip(label.toolTip() if label is self.lbl_cpu_power else label.text())
            row.setStretch(index, width)
        self._metric_font_size = size
        for label, text, width in zip((self.lbl_disk, self.lbl_output_disk), disk_texts, disk_widths):
            label.setStyleSheet(
                f'QLabel {{background:#15151C;border:1px solid #333345;border-radius:4px;'
                f'padding:4px;color:#A0C8CA;font-size:{size}px;}}')
            label.setText(text)
            label.setFixedWidth(width)
        height = max(48, metrics.height()*disk_lines+12)
        if height != self._disk_height:
            self._disk_height = height
            self.disk_height_changed.emit(height)

    def _disk_layout(self, metrics):
        def fmt(value, spec='.1f'):
            return format(value, spec) if value is not None else '--'
        disks = self._disk_snapshots or [{'disk_label': '程序/输出磁盘'}]
        dual = len(disks) > 1
        def build(lines):
            texts, widths = [], []
            for disk in disks[:2]:
                header = disk.get('disk_label','磁盘')
                read = '读'+fmt(disk.get('disk_read'))+'MB/s'
                write = '写'+fmt(disk.get('disk_write'))+'MB/s'
                busy = '活动'+fmt(disk.get('disk_busy'),'.0f')+'%'
                parts = ([header, read+' '+write+' '+busy] if lines == 2 else
                         [header, read, write+' '+busy] if lines == 3 else
                         [header+' '+read+' '+write+' '+busy])
                reserve = ('读99999.9MB/s 写99999.9MB/s 活动100%' if lines == 2 else
                           '写99999.9MB/s 活动100%' if lines == 3 else
                           header+' 读99999.9MB/s 写99999.9MB/s 活动100%')
                texts.append('\n'.join(parts))
                widths.append(max(metrics.horizontalAdvance(t) for t in parts+[reserve])+20)
            return texts, widths, lines
        layout = build(2 if dual else 1)
        if dual and sum(layout[1]) > self.disk_width_budget():
            layout = build(3)
        return layout

    def set_output_path(self, path):
        self.sampler.set_output_path(path)
