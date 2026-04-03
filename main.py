import certifi
import os
import re
import subprocess
import sys
import time

# PyInstaller bundles don't include system CA certificates, so SSL
# verification fails unless we point Python at certifi's bundle.
if not os.environ.get("SSL_CERT_FILE"):
    os.environ["SSL_CERT_FILE"] = certifi.where()
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import shutil

import imageio_ffmpeg
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QProgressBar,
    QFileDialog,
    QHeaderView,
    QAbstractItemView,
    QLabel,
    QMenu,
    QMessageBox,
)
from yt_dlp import YoutubeDL

FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
_CHATURBOT_RE = re.compile(r"https?://(?:www\.)?chaturbot\.co/")


def _normalize_url(url: str) -> str:
    return _CHATURBOT_RE.sub("https://www.chaturbate.com/", url)


class _CancelledError(Exception):
    pass


class DownloadWorker(QThread):
    progress = pyqtSignal(float, str)  # percent, status_text
    done = pyqtSignal(bool, str)  # success, message

    def __init__(self, url: str, output_dir: str):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        outtmpl = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        self.progress.emit(0, "Starting…")
        last_live_emit = 0.0

        def progress_hook(d):
            nonlocal last_live_emit
            if self._cancelled:
                raise _CancelledError()
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
                    if now - last_live_emit < 1.0:
                        return
                    last_live_emit = now
                    elapsed = d.get("_elapsed_str", "")
                    size_str = (d.get("_downloaded_bytes_str") or "").strip()
                    self.progress.emit(0, f"Recording {elapsed}  {size_str}")
            elif status == "finished":
                self.progress.emit(100, "Processing…")

        def postprocessor_hook(d):
            if self._cancelled:
                raise _CancelledError()
            if d.get("status") == "started":
                self.progress.emit(100, "Merging…")

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

        try:
            with YoutubeDL(cast(Any, params)) as ydl:
                info = ydl.extract_info(self.url, download=True)
                title = (info.get("title") if info else None) or self.url
            self.done.emit(True, title)
        except _CancelledError:
            self.done.emit(False, "Stopped")
        except Exception as e:
            if self._cancelled:
                self.done.emit(False, "Stopped")
            else:
                self.done.emit(False, str(e))


class MetadataWorker(QThread):
    loaded = pyqtSignal(str, bytes)
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
                with urllib.request.urlopen(thumbnail_url, timeout=15) as response:
                    thumbnail_data = response.read()

            self.loaded.emit(title, thumbnail_data)
        except Exception as e:
            self.failed.emit(str(e))


@dataclass
class DownloadItem:
    url: str
    title: str = ""
    status: str = "Pending"
    percent: float = 0
    worker: DownloadWorker | None = field(default=None, repr=False)
    metadata_worker: MetadataWorker | None = field(default=None, repr=False)


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

        # --- Top bar: URL input + buttons ---
        top = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "Paste URL(s) here — one per line for multiple"
        )
        self.url_input.returnPressed.connect(self._add_urls)
        top.addWidget(self.url_input, stretch=1)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_urls)
        top.addWidget(add_btn)

        folder_btn = QPushButton("Folder…")
        folder_btn.clicked.connect(self._pick_folder)
        top.addWidget(folder_btn)

        layout.addLayout(top)

        # --- Download table ---
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Thumbnail", "Title / URL", "Status", "Progress", "Total Downloaded"]
        )
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(
                self.THUMBNAIL_COL, QHeaderView.ResizeMode.Fixed
            )
            header.setSectionResizeMode(self.TITLE_COL, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(
                self.STATUS_COL, QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(self.PROGRESS_COL, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(
                self.TOTAL_COL, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.setColumnWidth(self.THUMBNAIL_COL, 120)
        self.table.setColumnWidth(self.PROGRESS_COL, 180)
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

    # ── Actions ──────────────────────────────────────────

    def _build_thumbnail_label(self) -> QLabel:
        label = QLabel("No preview")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("padding: 4px;")
        label.setToolTip("Loading preview…")
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
                        104,
                        58,
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

    def _start_metadata_fetch(self, item: DownloadItem):
        worker = MetadataWorker(item.url)
        item.metadata_worker = worker
        worker.loaded.connect(lambda title, data, w=worker: self._on_metadata_loaded(w, title, data))
        worker.failed.connect(lambda msg, w=worker: self._on_metadata_failed(w, msg))
        worker.start()

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
            self.table.setCellWidget(
                row, self.THUMBNAIL_COL, self._build_thumbnail_label()
            )
            self.table.setItem(row, self.TITLE_COL, QTableWidgetItem(url))
            self.table.setItem(row, self.STATUS_COL, QTableWidgetItem("Pending"))
            bar = QProgressBar()
            bar.setValue(0)
            self.table.setCellWidget(row, self.PROGRESS_COL, bar)
            self.table.setItem(row, self.TOTAL_COL, QTableWidgetItem(""))
            self._start_metadata_fetch(item)
        self.url_input.clear()

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.output_dir
        )
        if folder:
            self.output_dir = folder

    def _download_all(self):
        for row, item in enumerate(self.downloads):
            if item.status == "Pending" or item.status == "Error":
                self._start_download(row)

    def _start_download(self, row: int):
        item = self.downloads[row]
        item.status = "Starting…"
        item.percent = 0
        status_item = self.table.item(row, self.STATUS_COL)
        if status_item:
            status_item.setText("Starting…")
        bar = self.table.cellWidget(row, self.PROGRESS_COL)
        if not isinstance(bar, QProgressBar):
            return
        bar.setValue(0)
        total_item = self.table.item(row, self.TOTAL_COL)
        if total_item:
            total_item.setText("")

        worker = DownloadWorker(item.url, self.output_dir)
        item.worker = worker
        worker.progress.connect(lambda pct, txt, w=worker: self._on_progress(w, pct, txt))
        worker.done.connect(lambda ok, msg, w=worker: self._on_finished(w, ok, msg))
        worker.start()

    def _find_row_for_worker(
        self, worker: DownloadWorker
    ) -> tuple[int, DownloadItem] | None:
        for r, item in enumerate(self.downloads):
            if item.worker is worker:
                return r, item
        return None

    def _find_row_for_metadata_worker(
        self, worker: MetadataWorker
    ) -> tuple[int, DownloadItem] | None:
        for r, item in enumerate(self.downloads):
            if item.metadata_worker is worker:
                return r, item
        return None

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

            status_item.setText(status_text.split("  ")[0])
            bar.setValue(int(percent))
            # speed is after the double-space
            parts = status_text.split("  ")
            speed = parts[1] if len(parts) > 1 else ""
            speed_item.setText(speed)
        except Exception:
            traceback.print_exc()

    def _retire_worker(self, worker: QThread):
        """Keep a Python reference to the worker until its thread fully stops."""
        self._dying_workers.add(worker)
        worker.finished.connect(lambda: self._dying_workers.discard(worker))
        worker.finished.connect(worker.deleteLater)

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

    def _clear_finished(self):
        rows_to_remove = [
            r for r, item in enumerate(self.downloads) if item.status == "Done"
        ]
        for row in reversed(rows_to_remove):
            self.table.removeRow(row)
            self.downloads.pop(row)

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

    def closeEvent(self, a0: QCloseEvent | None):
        # Stop all running workers and wait for them to finish so QThread
        # objects are not destroyed while still running (causes segfault).
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

    def _open_folder(self):
        if sys.platform == "win32":
            os.startfile(self.output_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.output_dir])
        else:
            subprocess.Popen(["xdg-open", self.output_dir])

    def _stop_download(self, row: int):
        item = self.downloads[row]
        if item.worker and item.worker.isRunning():
            item.worker.cancel()

    def _stop_all(self):
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.cancel()

    def _remove_row(self, row: int):
        item = self.downloads[row]
        if item.worker and item.worker.isRunning():
            QMessageBox.warning(self, "Busy", "Cannot remove an active download.")
            return
        if item.metadata_worker and item.metadata_worker.isRunning():
            QMessageBox.warning(
                self, "Busy", "Cannot remove an item while its preview is loading."
            )
            return
        self.table.removeRow(row)
        self.downloads.pop(row)


def _excepthook(exc_type, exc_value, exc_tb):
    traceback.print_exception(exc_type, exc_value, exc_tb)


if __name__ == "__main__":
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
