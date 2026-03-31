# yt-dlp GUI

A lightweight desktop application that provides a graphical interface for [yt-dlp](https://github.com/yt-dlp/yt-dlp), the popular command-line video downloader. Built with PyQt6, it lets you download videos from thousands of supported sites without touching the terminal.

Current version: **0.1.2**

## Features

- **Batch downloads** -- paste multiple URLs at once (comma or newline separated) and download them all in parallel
- **Real-time progress** -- progress bars, download speed, and status for every item in the queue
- **Best quality by default** -- automatically selects the best video + audio streams and merges them into MP4
- **Livestream support** -- records live streams and salvages partial `.part` files if a download is stopped mid-stream
- **Download management** -- start, stop, retry, or remove individual downloads via right-click context menu
- **Custom output folder** -- choose where files are saved (defaults to `~/Downloads`)
- **URL normalization** -- automatically rewrites supported URLs such as `chaturbot.co` to `chaturbate.com`
- **Bundled FFmpeg** -- ships FFmpeg via `imageio-ffmpeg`, so there's nothing extra to install

## Screenshot

![yt-dlp GUI](screenshots/screenshot%201.png)

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Windows, macOS, or Linux

> [!NOTE]
> Python 3.14+ is currently required by this project configuration.

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/sojiroh/yt-dlp-gui.git
   cd yt-dlp-gui
   ```

2. **Install dependencies with uv:**

   ```bash
   uv sync
   ```

    This creates a `.venv` and installs all dependencies from `pyproject.toml`.

3. **Or install with pip:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

   On Windows PowerShell:

   ```powershell
   .venv\Scripts\Activate.ps1
   pip install -e .
   ```

## Usage

Run the application:

```bash
uv run main.py
```

Or, if you installed with pip:

```bash
python main.py
```

### Quick start

1. Paste one or more video URLs into the input field at the top
2. Click **Add** (or press Enter) to queue them
3. Click **Download All** to start downloading
4. Files are saved to your `Downloads` folder by default -- click **Folder...** to change it
5. If you paste a supported alternate domain such as `chaturbot.co`, the app normalizes it automatically before downloading

### Context menu (right-click a row)

| Action              | Description                                      |
|---------------------|--------------------------------------------------|
| **Stop**            | Cancel an active download                        |
| **Retry**           | Restart a failed or stopped download              |
| **Open file location** | Open the output folder in your file manager   |
| **Remove**          | Remove the entry from the queue                  |

### Bottom toolbar

| Button              | Description                                      |
|---------------------|--------------------------------------------------|
| **Download All**    | Start all pending/errored downloads               |
| **Stop All**        | Cancel every active download                      |
| **Clear Finished**  | Remove all completed entries from the queue        |

## Project Structure

```
yt-dlp-gui/
├── main.py           # Entire application (UI + download logic)
├── pyproject.toml    # Project metadata and dependencies
├── uv.lock           # Locked dependency versions for uv
├── screenshots/      # App screenshots used in documentation
├── .gitignore        # Git ignore rules
└── README.md         # This file
```

## How It Works

The application spawns `yt-dlp` as a subprocess for each download, parses its stdout in real time to extract progress percentages and download speeds, and displays them in a PyQt6 table. Each download runs in its own `QThread` to keep the UI responsive.

Key implementation details:

- **Format selection:** `bestvideo+bestaudio/best` with `--merge-output-format mp4`
- **FFmpeg:** Automatically located via the `imageio-ffmpeg` package (no system install needed)
- **Partial file recovery:** When a livestream download is stopped, `.part` files are renamed to usable video files
- **URL normalization:** Rewrites supported alternate domains before queuing downloads
- **Process cleanup:** Uses `taskkill /F /T` on Windows and `os.killpg(SIGTERM)` on Unix to kill the yt-dlp process tree on cancellation

## Dependencies

| Package          | Purpose                                  |
|------------------|------------------------------------------|
| [PyQt6](https://pypi.org/project/PyQt6/) `>=6.10` | Desktop GUI framework |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) `>=2026.2.4` | Video downloading engine |
| [imageio-ffmpeg](https://pypi.org/project/imageio-ffmpeg/) `>=0.6.0` | Bundled FFmpeg binary for merging streams |

## Troubleshooting

- **`python` version is too old** -- this project currently requires Python 3.14 or newer.
- **FFmpeg issues** -- the app first tries the system `ffmpeg`, then falls back to the binary bundled by `imageio-ffmpeg`.
- **A stopped livestream left a partial file** -- the app attempts to rename `.part` files to a usable video file automatically.

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for the full text.
