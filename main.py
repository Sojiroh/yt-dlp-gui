import re
import signal
import sys
import os
import subprocess
import time
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
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?\s*[KMG]?iB/s)")
_DEST_RE = re.compile(r"\[(?:download|Merger)\].*?Destination:\s*(.+)")
_FRAG_RE = re.compile(r"\[download\]\s+(.+\.part)")
_MERGE_RE = re.compile(r"\[Merger\]")
_ALREADY_RE = re.compile(r"has already been downloaded")
_LIVE_RE = re.compile(r"size=\s*(\d+\S+)\s+.*?time=(\d{2}:\d{2}:\d{2})")
_CHATURBOT_RE = re.compile(r"https?://(?:www\.)?chaturbot\.co/")


def _normalize_url(url: str) -> str:
    return _CHATURBOT_RE.sub("https://www.chaturbate.com/", url)


class DownloadWorker(QThread):
    progress = pyqtSignal(float, str)  # percent, status_text
    done = pyqtSignal(bool, str)  # success, message

    def __init__(self, url: str, output_dir: str):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def _finalize_partial(dest_path: str):
        """Rename .part file to its final name so the recording is usable."""
        if not dest_path:
            return
        part = Path(dest_path)
        if not part.exists():
            # yt-dlp may append .part to the destination
            part = Path(dest_path + ".part")
        if part.exists() and part.suffix == ".part":
            final = part.with_suffix("")
            # if the stem also has no video extension, default to .mp4
            if final.suffix not in (".mp4", ".mkv", ".webm", ".ts", ".flv"):
                final = final.with_suffix(".mp4")
            part.rename(final)

    def cancel(self):
        if self._proc and self._proc.poll() is None:
            # Kill the entire process tree (yt-dlp + ffmpeg child)
            try:
                if sys.platform == "win32":
                    subprocess.call(
                        ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run(self):
        outtmpl = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--newline",
            "--ffmpeg-location",
            FFMPEG_PATH,
            "-f",
            "bestvideo+bestaudio/best",
            "--merge-output-format",
            "mp4",
            "-o",
            outtmpl,
            self.url,
        ]

        self.progress.emit(0, "Starting…")
        title = ""
        dest_path = ""
        last_error = ""
        last_live_emit = 0.0

        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )

            stdout = self._proc.stdout
            if stdout is None:
                raise RuntimeError("Could not capture yt-dlp output")

            for line in stdout:
                line = line.strip()
                if not line:
                    continue

                # capture destination filename as title
                m = _DEST_RE.search(line)
                if m:
                    dest_path = m.group(1)
                    title = Path(dest_path).stem

                # capture .part path for livestreams
                fm = _FRAG_RE.search(line)
                if fm:
                    dest_path = fm.group(1)

                if _ALREADY_RE.search(line):
                    title = title or self.url

                if _MERGE_RE.search(line):
                    self.progress.emit(100, "Merging…")
                    continue

                # parse progress percentage + speed
                pct_m = _PCT_RE.search(line)
                if pct_m:
                    pct = float(pct_m.group(1))
                    speed_m = _SPEED_RE.search(line)
                    speed = speed_m.group(1) if speed_m else ""
                    self.progress.emit(pct, f"Downloading  {speed}")
                else:
                    # livestream: ffmpeg outputs size + time instead of percentage
                    live_m = _LIVE_RE.search(line)
                    if live_m:
                        now = time.monotonic()
                        if now - last_live_emit < 1.0:
                            continue
                        last_live_emit = now
                        size, time_ = live_m.group(1), live_m.group(2)
                        self.progress.emit(0, f"Recording {time_}  {size}")
                    elif line.startswith("ERROR"):
                        last_error = line

            self._proc.wait()
            rc = self._proc.returncode
            self._proc = None

            if rc == 0:
                self.done.emit(True, title or self.url)
            elif rc < 0 or last_error == "":
                # killed — rename .part to usable file
                self._finalize_partial(dest_path)
                self.done.emit(False, "Stopped")
            else:
                self.done.emit(False, last_error)
        except Exception as e:
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
        worker.loaded.connect(self._on_metadata_loaded)
        worker.failed.connect(self._on_metadata_failed)
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
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_finished)
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

    def _on_progress(self, percent: float, status_text: str):
        try:
            worker = self.sender()
            if not isinstance(worker, DownloadWorker):
                return
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

    def _on_finished(self, success: bool, message: str):
        try:
            worker = self.sender()
            if not isinstance(worker, DownloadWorker):
                return
            result = self._find_row_for_worker(worker)
            if result is None:
                return
            row, item = result
            item.worker = None
            # Keep a reference until the thread fully stops so Qt doesn't
            # destroy the QThread while run() is still on the call stack.
            worker.finished.connect(worker.deleteLater)

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

    def _on_metadata_loaded(self, title: str, image_data: bytes):
        try:
            worker = self.sender()
            if not isinstance(worker, MetadataWorker):
                return
            result = self._find_row_for_metadata_worker(worker)
            if result is None:
                return
            row, item = result
            item.metadata_worker = None
            worker.finished.connect(worker.deleteLater)

            item.title = title
            title_item = self.table.item(row, self.TITLE_COL)
            if title_item:
                title_item.setText(title)
                title_item.setToolTip(item.url)
            self._set_thumbnail(row, image_data, title)
        except Exception:
            traceback.print_exc()

    def _on_metadata_failed(self, message: str):
        try:
            worker = self.sender()
            if not isinstance(worker, MetadataWorker):
                return
            result = self._find_row_for_metadata_worker(worker)
            if result is None:
                return
            row, item = result
            item.metadata_worker = None
            worker.finished.connect(worker.deleteLater)
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
