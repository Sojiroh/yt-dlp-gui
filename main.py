import certifi
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field

# PyInstaller bundles don't include system CA certificates, so SSL
# verification fails unless we point Python at certifi's bundle.
if not os.environ.get("SSL_CERT_FILE"):
    os.environ["SSL_CERT_FILE"] = certifi.where()
from pathlib import Path
from typing import Any, cast

import imageio_ffmpeg
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from yt_dlp import YoutubeDL

FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def _fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _fmt_speed(bps: float) -> str:
    return f"@ {_fmt_bytes(bps)}/s"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
_CHATURBOT_RE = re.compile(r"https?://(?:www\.)?chaturbot\.co/")


def _normalize_url(url: str) -> str:
    return _CHATURBOT_RE.sub("https://www.chaturbate.com/", url)


class _CancelledError(Exception):
    pass


# ---------------------------------------------------------------------------
# Subprocess tracker – captures ffmpeg processes spawned by each thread
# so we can terminate them on cancel (yt-dlp delegates livestream
# downloads to ffmpeg, bypassing Python progress hooks entirely).
# ---------------------------------------------------------------------------

_subprocess_registry: dict[int, list[subprocess.Popen]] = {}
_subprocess_lock = threading.Lock()
_original_Popen_init = subprocess.Popen.__init__


def _tracked_Popen_init(self: subprocess.Popen, *args: Any, **kwargs: Any) -> None:
    _original_Popen_init(self, *args, **kwargs)
    tid = threading.get_ident()
    with _subprocess_lock:
        bucket = _subprocess_registry.get(tid)
        if bucket is not None:
            bucket.append(self)


subprocess.Popen.__init__ = _tracked_Popen_init  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class _FileMonitor(threading.Thread):
    """Polls the output directory for growing files and emits size updates."""

    def __init__(self, output_dir: str, callback, interval: float = 0.5):
        super().__init__(daemon=True)
        self.output_dir = output_dir
        self.callback = callback
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        known: dict[str, int] = {}
        # snapshot existing files so we only track new ones
        try:
            for f in os.listdir(self.output_dir):
                fp = os.path.join(self.output_dir, f)
                if os.path.isfile(fp):
                    known[fp] = -1  # mark as pre-existing (ignore)
        except OSError:
            pass

        start = time.monotonic()
        prev_size = 0
        prev_time = start

        while not self._stop.wait(self.interval):
            # find the newest/largest file being written
            current_file = None
            current_size = 0
            try:
                for f in os.listdir(self.output_dir):
                    fp = os.path.join(self.output_dir, f)
                    if not os.path.isfile(fp):
                        continue
                    if known.get(fp) == -1:
                        continue
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    if sz > current_size:
                        current_size = sz
                        current_file = fp
            except OSError:
                continue

            if current_file and current_size > 0:
                now = time.monotonic()
                elapsed = now - start
                dt = now - prev_time
                speed = (current_size - prev_size) / dt if dt > 0 else 0
                prev_size = current_size
                prev_time = now
                self.callback(elapsed, current_size, speed)


class DownloadWorker(QThread):
    MAX_RETRIES = 5
    RETRY_DELAY = 1.0

    progress = pyqtSignal(float, str)  # percent, status_text
    done = pyqtSignal(bool, str)       # success, message

    def __init__(self, url: str, output_dir: str):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self._cancelled = False
        self._hook_fired = False
        self._subprocesses: list[subprocess.Popen] = []

    def cancel(self):
        self._cancelled = True
        for proc in self._subprocesses:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass

    def _do_download(self, params: dict[str, Any]) -> str:
        with YoutubeDL(cast(Any, params)) as ydl:
            info = ydl.extract_info(self.url, download=True)
            return (info.get("title") if info else None) or self.url

    def run(self):
        tid = threading.get_ident()
        with _subprocess_lock:
            _subprocess_registry[tid] = self._subprocesses

        outtmpl = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        self.progress.emit(0, "Starting…")
        last_live_emit = 0.0

        def progress_hook(d: dict[str, Any]):
            nonlocal last_live_emit
            if self._cancelled:
                raise _CancelledError()

            self._hook_fired = True
            status = d.get("status", "")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                speed_str = (d.get("_speed_str") or "").strip()

                if total > 0:
                    pct = downloaded / total * 100
                    self.progress.emit(pct, f"Downloading  {speed_str}")
                else:
                    now = time.monotonic()
                    if now - last_live_emit < 0.5:
                        return
                    last_live_emit = now
                    size_str = _fmt_bytes(downloaded)
                    speed = d.get("speed")
                    speed_part = _fmt_speed(speed) if speed else ""
                    elapsed = d.get("elapsed")
                    elapsed_part = _fmt_elapsed(elapsed) if elapsed else ""
                    info = " ".join(filter(None, [elapsed_part, size_str, speed_part]))
                    self.progress.emit(0, f"Recording  {info}")
            elif status == "finished":
                self.progress.emit(100, "Processing…")

        def postprocessor_hook(d: dict[str, Any]):
            if self._cancelled:
                raise _CancelledError()
            if d.get("status") == "started":
                self.progress.emit(100, "Merging…")

        def _on_file_monitor(elapsed: float, size: int, speed: float):
            if self._hook_fired:
                return
            elapsed_str = _fmt_elapsed(elapsed)
            size_str = _fmt_bytes(size)
            speed_str = _fmt_speed(speed) if speed > 0 else ""
            info = " ".join(filter(None, [elapsed_str, size_str, speed_str]))
            self.progress.emit(0, f"Recording  {info}")

        monitor = _FileMonitor(self.output_dir, _on_file_monitor)
        monitor.start()

        params: dict[str, Any] = {
            "ffmpeg_location": FFMPEG_PATH,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "quiet": True,
            "no_warnings": True,
        }

        attempt = 0
        last_error = ""
        try:
            while attempt <= self.MAX_RETRIES:
                try:
                    title = self._do_download(params)
                    self.done.emit(True, title)
                    return
                except _CancelledError:
                    self.done.emit(False, "Stopped")
                    return
                except Exception as e:
                    if self._cancelled:
                        self.done.emit(False, "Stopped")
                        return
                    attempt += 1
                    last_error = str(e)
                    if attempt <= self.MAX_RETRIES:
                        self.progress.emit(
                            0,
                            f"Retrying ({attempt}/{self.MAX_RETRIES})…",
                        )
                        time.sleep(self.RETRY_DELAY)
                        self._hook_fired = False

            self.done.emit(False, f"Failed after {self.MAX_RETRIES} retries: {last_error}")
        finally:
            monitor.stop()
            with _subprocess_lock:
                _subprocess_registry.pop(tid, None)


class MetadataWorker(QThread):
    loaded = pyqtSignal(str, bytes)  # title, thumbnail_data
    failed = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            params: dict[str, Any] = {
                "quiet": True,
                "skip_download": True,
                "noplaylist": True,
            }
            with YoutubeDL(cast(Any, params)) as ydl:
                info = cast(dict[str, Any], ydl.extract_info(self.url, download=False))

            title = info.get("title") or self.url
            thumbnail_url = info.get("thumbnail") or ""
            thumbnail_data = b""
            if thumbnail_url:
                with urllib.request.urlopen(thumbnail_url, timeout=15) as resp:
                    thumbnail_data = resp.read()

            self.loaded.emit(title, thumbnail_data)
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class DownloadItem:
    url: str
    title: str = ""
    status: str = "Pending"
    percent: float = 0
    worker: DownloadWorker | None = field(default=None, repr=False)
    metadata_worker: MetadataWorker | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    THUMBNAIL_COL = 0
    TITLE_COL = 1
    STATUS_COL = 2
    PROGRESS_COL = 3
    TOTAL_COL = 4

    def __init__(self):
        super().__init__()
        self.setWindowTitle("yt-dlp GUI")
        self.resize(940, 480)

        self.downloads: list[DownloadItem] = []
        self._dying_workers: set[QThread] = set()
        self.output_dir = str(Path.home() / "Downloads")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Top bar ---
        top = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "Paste URL(s) here \u2014 one per line for multiple"
        )
        self.url_input.returnPressed.connect(self._add_urls)
        top.addWidget(self.url_input, stretch=1)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_urls)
        top.addWidget(add_btn)

        folder_btn = QPushButton("Folder\u2026")
        folder_btn.clicked.connect(self._pick_folder)
        top.addWidget(folder_btn)

        layout.addLayout(top)

        # --- Table ---
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Thumbnail", "Title / URL", "Status", "Progress", "Total Downloaded"]
        )
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(self.THUMBNAIL_COL, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(self.TITLE_COL, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(self.STATUS_COL, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(self.PROGRESS_COL, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(self.TOTAL_COL, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.THUMBNAIL_COL, 120)
        self.table.setColumnWidth(self.PROGRESS_COL, 180)
        self.table.setColumnWidth(self.TOTAL_COL, 220)
        vheader = self.table.verticalHeader()
        if vheader is not None:
            vheader.setDefaultSectionSize(72)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table)

        # --- Bottom bar ---
        bottom = QHBoxLayout()

        dl_btn = QPushButton("Download All")
        dl_btn.clicked.connect(self._download_all)
        bottom.addWidget(dl_btn)

        stop_btn = QPushButton("Stop All")
        stop_btn.clicked.connect(self._stop_all)
        bottom.addWidget(stop_btn)

        bottom.addStretch()

        clear_btn = QPushButton("Clear Finished")
        clear_btn.clicked.connect(self._clear_finished)
        bottom.addWidget(clear_btn)

        layout.addLayout(bottom)

    # ── Helpers ─────────────────────────────────────────

    def _build_thumbnail_label(self) -> QLabel:
        label = QLabel("No preview")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("padding: 4px;")
        label.setToolTip("Loading preview\u2026")
        return label

    def _set_thumbnail(self, row: int, image_data: bytes | None, tooltip: str = ""):
        label = self.table.cellWidget(row, self.THUMBNAIL_COL)
        if not isinstance(label, QLabel):
            return
        if image_data:
            pixmap = QPixmap()
            if pixmap.loadFromData(image_data):
                label.setPixmap(
                    pixmap.scaled(
                        104, 58,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                label.setText("")
                label.setToolTip(tooltip or "Video thumbnail")
                return
        label.setPixmap(QPixmap())
        label.setText("No preview")
        label.setToolTip(tooltip)

    def _retire_worker(self, worker: QThread):
        self._dying_workers.add(worker)
        worker.finished.connect(lambda: self._dying_workers.discard(worker))
        worker.finished.connect(worker.deleteLater)

    def _find_row_for_worker(self, worker: DownloadWorker) -> tuple[int, DownloadItem] | None:
        for r, item in enumerate(self.downloads):
            if item.worker is worker:
                return r, item
        return None

    def _find_row_for_metadata_worker(self, worker: MetadataWorker) -> tuple[int, DownloadItem] | None:
        for r, item in enumerate(self.downloads):
            if item.metadata_worker is worker:
                return r, item
        return None

    # ── Actions ─────────────────────────────────────────

    def _add_urls(self):
        text = self.url_input.text().strip()
        if not text:
            return
        urls = [
            _normalize_url(u.strip())
            for u in text.replace(",", "\n").splitlines()
            if u.strip()
        ]
        for url in urls:
            item = DownloadItem(url=url, title=url)
            self.downloads.append(item)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setCellWidget(row, self.THUMBNAIL_COL, self._build_thumbnail_label())
            self.table.setItem(row, self.TITLE_COL, QTableWidgetItem(url))
            self.table.setItem(row, self.STATUS_COL, QTableWidgetItem("Pending"))
            bar = QProgressBar()
            bar.setValue(0)
            self.table.setCellWidget(row, self.PROGRESS_COL, bar)
            self.table.setItem(row, self.TOTAL_COL, QTableWidgetItem(""))
            self._start_metadata_fetch(item)
        self.url_input.clear()

    def _start_metadata_fetch(self, item: DownloadItem):
        worker = MetadataWorker(item.url)
        item.metadata_worker = worker
        worker.loaded.connect(lambda title, data, w=worker: self._on_metadata_loaded(w, title, data))
        worker.failed.connect(lambda msg, w=worker: self._on_metadata_failed(w, msg))
        worker.start()

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose download folder", self.output_dir)
        if folder:
            self.output_dir = folder

    def _download_all(self):
        for row, item in enumerate(self.downloads):
            if item.status in ("Pending", "Error"):
                self._start_download(row)

    def _start_download(self, row: int):
        item = self.downloads[row]
        item.status = "Starting\u2026"
        item.percent = 0

        status_item = self.table.item(row, self.STATUS_COL)
        if status_item:
            status_item.setText("Starting\u2026")
        bar = self.table.cellWidget(row, self.PROGRESS_COL)
        if isinstance(bar, QProgressBar):
            bar.setValue(0)
        total_item = self.table.item(row, self.TOTAL_COL)
        if total_item:
            total_item.setText("")

        worker = DownloadWorker(item.url, self.output_dir)
        item.worker = worker
        worker.progress.connect(lambda pct, txt, w=worker: self._on_progress(w, pct, txt))
        worker.done.connect(lambda ok, msg, w=worker: self._on_finished(w, ok, msg))
        worker.start()

    def _stop_all(self):
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.cancel()

    def _stop_download(self, row: int):
        item = self.downloads[row]
        if item.worker and item.worker.isRunning():
            item.worker.cancel()

    def _clear_finished(self):
        rows_to_remove = [r for r, item in enumerate(self.downloads) if item.status == "Done"]
        for row in reversed(rows_to_remove):
            self.table.removeRow(row)
            self.downloads.pop(row)

    def _remove_row(self, row: int):
        item = self.downloads[row]
        if item.worker and item.worker.isRunning():
            QMessageBox.warning(self, "Busy", "Cannot remove an active download.")
            return
        if item.metadata_worker and item.metadata_worker.isRunning():
            QMessageBox.warning(self, "Busy", "Cannot remove while preview is loading.")
            return
        self.table.removeRow(row)
        self.downloads.pop(row)

    def _open_folder(self):
        if sys.platform == "win32":
            os.startfile(self.output_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.output_dir])
        else:
            subprocess.Popen(["xdg-open", self.output_dir])

    # ── Callbacks ───────────────────────────────────────

    def _on_progress(self, worker: DownloadWorker, percent: float, status_text: str):
        try:
            result = self._find_row_for_worker(worker)
            if result is None:
                return
            row, item = result
            item.percent = percent
            item.status = status_text

            status_item = self.table.item(row, self.STATUS_COL)
            speed_item = self.table.item(row, self.TOTAL_COL)
            bar = self.table.cellWidget(row, self.PROGRESS_COL)
            if not status_item or not speed_item or not isinstance(bar, QProgressBar):
                return

            parts = status_text.split("  ")
            status_item.setText(parts[0])
            bar.setValue(int(percent))
            speed_item.setText(parts[1] if len(parts) > 1 else "")
        except Exception:
            traceback.print_exc()

    def _on_finished(self, worker: DownloadWorker, success: bool, message: str):
        try:
            self._retire_worker(worker)
            result = self._find_row_for_worker(worker)
            if result is None:
                return
            row, item = result
            item.worker = None

            title_item = self.table.item(row, self.TITLE_COL)
            status_item = self.table.item(row, self.STATUS_COL)
            speed_item = self.table.item(row, self.TOTAL_COL)
            bar = self.table.cellWidget(row, self.PROGRESS_COL)

            if not title_item or not status_item or not speed_item:
                return

            if success:
                item.status = "Done"
                item.title = message
                title_item.setText(message)
                status_item.setText("Done")
                if isinstance(bar, QProgressBar):
                    bar.setValue(100)
            elif message == "Stopped":
                item.status = "Stopped"
                status_item.setText("Stopped")
            else:
                item.status = "Error"
                status_item.setText("Error")
                status_item.setToolTip(message)
            speed_item.setText("")
        except Exception:
            traceback.print_exc()

    def _on_metadata_loaded(self, worker: MetadataWorker, title: str, image_data: bytes):
        try:
            self._retire_worker(worker)
            result = self._find_row_for_metadata_worker(worker)
            if result is None:
                return
            row, item = result
            item.metadata_worker = None
            item.title = title

            title_item = self.table.item(row, self.TITLE_COL)
            if title_item:
                title_item.setText(title)
                title_item.setToolTip(item.url)
            self._set_thumbnail(row, image_data, title)
        except Exception:
            traceback.print_exc()

    def _on_metadata_failed(self, worker: MetadataWorker, message: str):
        try:
            self._retire_worker(worker)
            result = self._find_row_for_metadata_worker(worker)
            if result is None:
                return
            row, item = result
            item.metadata_worker = None
            self._set_thumbnail(row, None, f"Preview unavailable: {message}")
        except Exception:
            traceback.print_exc()

    # ── Context menu ────────────────────────────────────

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.downloads[row]
        menu = QMenu(self)

        if item.worker and item.worker.isRunning():
            menu.addAction("Stop", lambda: self._stop_download(row))
        if item.status in ("Error", "Pending", "Stopped"):
            menu.addAction("Retry", lambda: self._start_download(row))
        if item.status == "Done":
            menu.addAction("Retry", lambda: self._start_download(row))
            menu.addAction("Open file location", lambda: self._open_folder())
        menu.addAction("Remove", lambda: self._remove_row(row))

        viewport = self.table.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    # ── Shutdown ────────────────────────────────────────

    def closeEvent(self, a0: QCloseEvent | None):
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.cancel()
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.wait(5000)
            if item.metadata_worker and item.metadata_worker.isRunning():
                item.metadata_worker.wait(5000)
        for w in list(self._dying_workers):
            if w.isRunning():
                w.wait(5000)
        if a0 is not None:
            a0.accept()


def _excepthook(exc_type, exc_value, exc_tb):
    traceback.print_exception(exc_type, exc_value, exc_tb)


if __name__ == "__main__":
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
