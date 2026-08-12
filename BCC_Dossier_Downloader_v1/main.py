from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from playwright.sync_api import sync_playwright

from bcc_downloader import BCCDownloader, load_bins

DEFAULT_TARGET = (
    "https://bcc-app.bank.corp.centercredit.kz:4030/ecd/"
    "pkg_w_e_dossier.p_main?p_arm=CBS25A9F2F0536520E21BE53CAD7B5"
)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("BCC Electronic Credit Dossier Downloader")
        root.geometry("820x620")

        self.bin_file = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Documents" / "BCC_Dossiers"))
        self.target_url = tk.StringVar(value=DEFAULT_TARGET)
        self.browser_channel = tk.StringVar(value="msedge")
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()
        self.messages: queue.Queue[str] = queue.Queue()
        self.running = False

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="BIN source (.xlsx, .csv or .txt):").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.bin_file).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(frame, text="Browse…", command=self.pick_bin).grid(row=1, column=1, sticky="ew")

        ttk.Label(frame, text="Download folder:").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(frame, textvariable=self.output_dir).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(frame, text="Browse…", command=self.pick_output).grid(row=3, column=1, sticky="ew")

        ttk.Label(frame, text="BCC dossier search URL:").grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(frame, textvariable=self.target_url).grid(row=5, column=0, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Browser:").grid(row=6, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            frame, textvariable=self.browser_channel, state="readonly",
            values=("msedge", "chrome", "chromium"), width=18
        ).grid(row=7, column=0, sticky="w")

        info = (
            "Security: the program does NOT store your BCC username/password. It opens a normal browser. "
            "Log in there. If the BIN search page is not opened automatically, navigate to it normally, "
            "then return here and click ‘I am logged in / continue’."
        )
        ttk.Label(frame, text=info, wraplength=760).grid(row=8, column=0, columnspan=2, sticky="w", pady=(14, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="ew")
        self.start_btn = ttk.Button(buttons, text="1. Open BCC browser", command=self.start)
        self.start_btn.pack(side="left")
        self.continue_btn = ttk.Button(
            buttons, text="2. I am logged in / continue", command=self.continue_after_login, state="disabled"
        )
        self.continue_btn.pack(side="left", padx=8)

        ttk.Label(frame, text="Progress / log:").grid(row=10, column=0, sticky="w", pady=(14, 4))
        self.log = tk.Text(frame, height=20, wrap="word")
        self.log.grid(row=11, column=0, columnspan=2, sticky="nsew")

        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(11, weight=1)
        root.after(150, self.poll_messages)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def pick_bin(self):
        path = filedialog.askopenfilename(
            title="Select BIN list",
            filetypes=[("Supported files", "*.xlsx *.xlsm *.csv *.txt"), ("All files", "*.*")],
        )
        if path:
            self.bin_file.set(path)

    def pick_output(self):
        path = filedialog.askdirectory(title="Select download folder")
        if path:
            self.output_dir.set(path)

    def start(self):
        if self.running:
            return
        source = Path(self.bin_file.get().strip())
        if not source.exists():
            messagebox.showerror("BIN file", "Select an existing .xlsx, .csv or .txt file containing BINs.")
            return
        bins = load_bins(source)
        if not bins:
            messagebox.showerror("BIN file", "No 12-digit BIN values were found in the selected file.")
            return
        out = Path(self.output_dir.get().strip())
        out.mkdir(parents=True, exist_ok=True)
        self.running = True
        self.ready_event.clear()
        self.start_btn.configure(state="disabled")
        self.continue_btn.configure(state="normal")
        self.messages.put(f"Loaded {len(bins)} unique BIN(s). Opening BCC…")
        threading.Thread(target=self.worker, args=(bins, out), daemon=True).start()

    def continue_after_login(self):
        self.messages.put("Continuing with the authenticated BCC session…")
        self.continue_btn.configure(state="disabled")
        self.ready_event.set()

    def worker(self, bins: list[str], out: Path):
        try:
            with sync_playwright() as p:
                channel = self.browser_channel.get()
                launch_kwargs = dict(headless=False)
                if channel != "chromium":
                    launch_kwargs["channel"] = channel
                try:
                    browser = p.chromium.launch(**launch_kwargs)
                except Exception:
                    self.messages.put(f"Could not open {channel}; trying Playwright Chromium…")
                    browser = p.chromium.launch(headless=False)

                context = browser.new_context(ignore_https_errors=True, accept_downloads=True)
                page = context.new_page()
                page.goto(self.target_url.get().strip(), wait_until="domcontentloaded", timeout=90_000)
                self.messages.put("Browser opened. Log in to BCC. Navigate to the BIN search page if needed.")
                self.messages.put("Then return to this window and click ‘I am logged in / continue’.")

                self.ready_event.wait()
                if self.stop_event.is_set():
                    browser.close()
                    return

                # Find the dossier page among all open tabs.
                dossier_page = None
                for candidate in context.pages:
                    if "/ecd/pkg_w_e_dossier.p_main" in candidate.url.lower():
                        dossier_page = candidate
                        break
                if dossier_page is None:
                    dossier_page = page
                if "/ecd/" not in dossier_page.url.lower():
                    raise RuntimeError(
                        "The active browser session is not on the BCC ECD site. Open the Electronic Credit Dossier "
                        "BIN search page, then try again."
                    )

                self.messages.put(f"Authenticated page detected: {dossier_page.url}")
                downloader = BCCDownloader(context, dossier_page, out, progress=self.messages.put)
                downloader.download_bins(bins)
                self.messages.put("FINISHED. You can now close the browser.")
                self.messages.put(f"Download log: {out / 'download_log.xlsx'}")
                # Keep browser open until user closes it, while GUI remains responsive.
                while not self.stop_event.is_set() and context.pages:
                    try:
                        if all(pg.is_closed() for pg in context.pages):
                            break
                    except Exception:
                        break
                    self.stop_event.wait(0.5)
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as exc:
            self.messages.put(f"FATAL ERROR: {exc}")
        finally:
            self.running = False
            self.messages.put("__DONE__")

    def poll_messages(self):
        try:
            while True:
                msg = self.messages.get_nowait()
                if msg == "__DONE__":
                    self.start_btn.configure(state="normal")
                    self.continue_btn.configure(state="disabled")
                    continue
                self.log.insert("end", msg + "\n")
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self.poll_messages)

    def on_close(self):
        self.stop_event.set()
        self.ready_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
