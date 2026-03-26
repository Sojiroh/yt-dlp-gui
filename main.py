import re
import signal
import sys
import os
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import shutil

import imageio_ffmpeg
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QProgressBar, QFileDialog, QHeaderView, QAbstractItemView,
    QMenu, QMessageBox,
)

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
    progress = pyqtSignal(float, str)   # percent, status_text
    done = pyqtSignal(bool, str)        # success, message

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
            sys.executable, "-m", "yt_dlp",
            "--newline",
            "--ffmpeg-location", FFMPEG_PATH,
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", outtmpl,
            self.url,
        ]

        self.progress.emit(0, "Starting…")
        title = ""
        dest_path = ""
        last_error = ""
        last_live_emit = 0.0

        try:
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                popen_kwargs["start_new_session"] = True
            self._proc = subprocess.Popen(cmd, **popen_kwargs)

            for line in self._proc.stdout:
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


@dataclass
class DownloadItem:
    url: str
    title: str = ""
    status: str = "Pending"
    percent: float = 0
    worker: DownloadWorker | None = field(default=None, repr=False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("yt-dlp GUI")
        self.resize(820, 480)

        self.downloads: list[DownloadItem] = []
        self.output_dir = str(Path.home() / "Downloads")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Top bar: URL input + buttons ---
        top = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste URL(s) here — one per line for multiple")
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
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Title / URL", "Status", "Progress", "Total Downloaded"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 180)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
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

    def _add_urls(self):
        text = self.url_input.text().strip()
        if not text:
            return
        urls = [_normalize_url(u.strip()) for u in text.replace(",", "\n").splitlines() if u.strip()]
        for url in urls:
            item = DownloadItem(url=url, title=url)
            self.downloads.append(item)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(url))
            self.table.setItem(row, 1, QTableWidgetItem("Pending"))
            bar = QProgressBar()
            bar.setValue(0)
            self.table.setCellWidget(row, 2, bar)
            self.table.setItem(row, 3, QTableWidgetItem(""))
        self.url_input.clear()

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose download folder", self.output_dir)
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
        self.table.item(row, 1).setText("Starting…")
        bar: QProgressBar = self.table.cellWidget(row, 2)
        bar.setValue(0)
        self.table.item(row, 3).setText("")

        worker = DownloadWorker(item.url, self.output_dir)
        item.worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_finished)
        worker.start()

    def _find_row_for_worker(self, worker: DownloadWorker) -> tuple[int, DownloadItem] | None:
        for r, item in enumerate(self.downloads):
            if item.worker is worker:
                return r, item
        return None

    def _on_progress(self, percent: float, status_text: str):
        try:
            result = self._find_row_for_worker(self.sender())
            if result is None:
                return
            row, item = result
            item.percent = percent
            item.status = status_text

            status_item = self.table.item(row, 1)
            speed_item = self.table.item(row, 3)
            bar: QProgressBar = self.table.cellWidget(row, 2)
            if not status_item or not speed_item or not bar:
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
            result = self._find_row_for_worker(worker)
            if result is None:
                return
            row, item = result
            item.worker = None
            # Keep a reference until the thread fully stops so Qt doesn't
            # destroy the QThread while run() is still on the call stack.
            worker.finished.connect(worker.deleteLater)

            title_item = self.table.item(row, 0)
            status_item = self.table.item(row, 1)
            speed_item = self.table.item(row, 3)
            bar: QProgressBar = self.table.cellWidget(row, 2)
            if not title_item or not status_item or not speed_item:
                return

            if success:
                item.status = "Done"
                item.title = message
                title_item.setText(message)
                status_item.setText("Done")
                if bar:
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

    def _clear_finished(self):
        rows_to_remove = [r for r, item in enumerate(self.downloads) if item.status == "Done"]
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
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def closeEvent(self, event):
        # Stop all running workers and wait for them to finish so QThread
        # objects are not destroyed while still running (causes segfault).
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.cancel()
        for item in self.downloads:
            if item.worker and item.worker.isRunning():
                item.worker.wait(5000)
        event.accept()

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
