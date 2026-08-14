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
        root.title("BCC Dossier Automation v1.0")
        root.geometry("960x820")
        root.minsize(860, 720)

        self.direct_bin = tk.StringVar()
        self.dossier_number = tk.StringVar()
        self.customer_key = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Documents" / "BCC_Dossiers"))
        self.username = tk.StringVar()
        self.password = tk.StringVar()

        # Advanced settings are hidden during normal single-case use.
        self.bin_file = tk.StringVar()
        self.target_url = tk.StringVar(value=DEFAULT_TARGET)
        self.browser_channel = tk.StringVar(value="msedge")
        self.show_advanced = tk.BooleanVar(value=False)
        self.show_details = tk.BooleanVar(value=False)

        # Dashboard values.
        self.cases_completed = tk.StringVar(value="0 / 0")
        self.current_bin = tk.StringVar(value="—")
        self.current_dossier = tk.StringVar(value="—")
        self.files_downloaded = tk.StringVar(value="0")
        self.skipped = tk.StringVar(value="0")
        self.not_found = tk.StringVar(value="0")
        self.errors = tk.StringVar(value="0")
        self.leasing_status = tk.StringVar(value="WAITING")
        self.purchase_status = tk.StringVar(value="WAITING")
        self.run_status = tk.StringVar(value="Ready")
        self.pair_folders = tk.StringVar(value="0")
        self.analyzed_docs = tk.StringVar(value="0")
        self.auto_approved = tk.StringVar(value="0")
        self.quarantined = tk.StringVar(value="0")
        self.unmatched = tk.StringVar(value="0")

        self._total_cases = 0
        self._completed_cases = 0
        self._downloaded_count = 0
        self._skipped_count = 0
        self._not_found_count = 0
        self._error_count = 0

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False

        self._build_ui()
        root.after(100, self.poll_messages)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        title = ttk.Label(outer, text="BCC Electronic Credit Dossier", font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Download, pair and automatically analyze BCC leasing dossiers with the v35 production gate.",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        case = ttk.LabelFrame(outer, text="Case", padding=12)
        case.grid(row=2, column=0, sticky="ew")
        case.columnconfigure(0, weight=1)
        case.columnconfigure(1, weight=1)

        ttk.Label(case, text="BIN / ИИН (12 digits)").grid(row=0, column=0, sticky="w")
        ttk.Entry(case, textvariable=self.direct_bin).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(case, text="Номер досье").grid(row=0, column=1, sticky="w")
        ttk.Entry(case, textvariable=self.dossier_number).grid(row=1, column=1, sticky="ew")

        ttk.Label(case, text="Ключ клиента (leave blank when BIN has only one client)").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(case, textvariable=self.customer_key).grid(row=3, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(case, text="Destination folder").grid(row=2, column=1, sticky="w", pady=(10, 0))
        dest = ttk.Frame(case)
        dest.grid(row=3, column=1, sticky="ew")
        dest.columnconfigure(0, weight=1)
        ttk.Entry(dest, textvariable=self.output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(dest, text="Browse…", command=self.pick_output).grid(row=0, column=1, padx=(6, 0))

        auth = ttk.LabelFrame(outer, text="BCC authentication", padding=12)
        auth.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        auth.columnconfigure(0, weight=1)
        auth.columnconfigure(1, weight=1)
        ttk.Label(auth, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(auth, textvariable=self.username).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(auth, text="Password").grid(row=0, column=1, sticky="w")
        ttk.Entry(auth, textvariable=self.password, show="•").grid(row=1, column=1, sticky="ew")
        ttk.Label(
            auth,
            text="Credentials are used only for this run and are not saved to disk or the Excel log.",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        action = ttk.Frame(outer)
        action.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.start_btn = ttk.Button(action, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        ttk.Label(action, textvariable=self.run_status).pack(side="left", padx=(12, 0))

        dash = ttk.LabelFrame(outer, text="Status", padding=12)
        dash.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        for c in range(4):
            dash.columnconfigure(c, weight=1)

        self._metric(dash, 0, 0, "Cases completed", self.cases_completed)
        self._metric(dash, 0, 1, "Files downloaded", self.files_downloaded)
        self._metric(dash, 0, 2, "Skipped", self.skipped)
        self._metric(dash, 0, 3, "Not found", self.not_found)
        self._metric(dash, 1, 0, "Errors", self.errors)
        self._metric(dash, 1, 1, "Current BIN", self.current_bin)
        self._metric(dash, 1, 2, "Current dossier", self.current_dossier)

        docs = ttk.LabelFrame(outer, text="Important documents", padding=12)
        docs.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        docs.columnconfigure(0, weight=1)
        docs.columnconfigure(1, weight=1)
        ttk.Label(docs, text="Leasing / Заявление о присоединении (all numbered)").grid(row=0, column=0, sticky="w")
        ttk.Label(docs, textvariable=self.leasing_status, font=("Segoe UI", 11, "bold")).grid(row=1, column=0, sticky="w")
        ttk.Label(docs, text="Purchase agreements / Договор купли-продажи (all numbered)").grid(row=0, column=1, sticky="w")
        ttk.Label(docs, textvariable=self.purchase_status, font=("Segoe UI", 11, "bold")).grid(row=1, column=1, sticky="w")

        analysis = ttk.LabelFrame(outer, text="Automatic analysis / production gate", padding=12)
        analysis.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        for c in range(5):
            analysis.columnconfigure(c, weight=1)
        self._metric(analysis, 0, 0, "Lease folders", self.pair_folders)
        self._metric(analysis, 0, 1, "Analyzed PDFs", self.analyzed_docs)
        self._metric(analysis, 0, 2, "AUTO_APPROVED", self.auto_approved)
        self._metric(analysis, 0, 3, "QUARANTINED", self.quarantined)
        self._metric(analysis, 0, 4, "Unmatched", self.unmatched)

        toggles = ttk.Frame(outer)
        toggles.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(toggles, text="Advanced / batch options", variable=self.show_advanced, command=self.toggle_advanced).pack(side="left")
        ttk.Checkbutton(toggles, text="Technical details", variable=self.show_details, command=self.toggle_details).pack(side="left", padx=(14, 0))

        self.advanced = ttk.LabelFrame(outer, text="Advanced / batch", padding=10)
        self.advanced.columnconfigure(0, weight=1)
        ttk.Label(self.advanced, text="Optional BIN list (.xlsx, .csv or .txt) — direct BIN above takes priority").grid(row=0, column=0, sticky="w")
        batch = ttk.Frame(self.advanced)
        batch.grid(row=1, column=0, sticky="ew")
        batch.columnconfigure(0, weight=1)
        ttk.Entry(batch, textvariable=self.bin_file).grid(row=0, column=0, sticky="ew")
        ttk.Button(batch, text="Browse…", command=self.pick_bin).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(self.advanced, text="Browser").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(self.advanced, textvariable=self.browser_channel, state="readonly", values=("msedge", "chrome", "chromium"), width=18).grid(row=3, column=0, sticky="w")
        ttk.Label(self.advanced, text="BCC URL (troubleshooting only)").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self.advanced, textvariable=self.target_url).grid(row=5, column=0, sticky="ew")

        self.details = ttk.LabelFrame(outer, text="Technical log", padding=8)
        self.details.columnconfigure(0, weight=1)
        self.details.rowconfigure(0, weight=1)
        self.log = tk.Text(self.details, height=9, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")

        outer.rowconfigure(10, weight=1)

    def _metric(self, parent, row, col, label, variable):
        box = ttk.Frame(parent, padding=(4, 4))
        box.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
        ttk.Label(box, text=label).pack(anchor="w")
        ttk.Label(box, textvariable=variable, font=("Segoe UI", 11, "bold"), wraplength=180).pack(anchor="w", pady=(2, 0))

    def toggle_advanced(self):
        if self.show_advanced.get():
            self.advanced.grid(row=9, column=0, sticky="ew", pady=(8, 0))
        else:
            self.advanced.grid_remove()

    def toggle_details(self):
        if self.show_details.get():
            self.details.grid(row=10, column=0, sticky="nsew", pady=(8, 0))
        else:
            self.details.grid_remove()

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

    def reset_dashboard(self, total_cases: int):
        self._total_cases = total_cases
        self._completed_cases = 0
        self._downloaded_count = 0
        self._skipped_count = 0
        self._not_found_count = 0
        self._error_count = 0
        self.cases_completed.set(f"0 / {total_cases}")
        self.current_bin.set("—")
        self.current_dossier.set("—")
        self.files_downloaded.set("0")
        self.skipped.set("0")
        self.not_found.set("0")
        self.errors.set("0")
        self.leasing_status.set("WAITING")
        self.purchase_status.set("WAITING")
        self.pair_folders.set("0")
        self.analyzed_docs.set("0")
        self.auto_approved.set("0")
        self.quarantined.set("0")
        self.unmatched.set("0")
        self.run_status.set("Starting…")
        self.log.delete("1.0", "end")

    def start(self):
        if self.running:
            return
        direct = "".join(ch for ch in self.direct_bin.get().strip() if ch.isdigit())
        if direct:
            if len(direct) != 12:
                messagebox.showerror("BIN", "BIN must contain exactly 12 digits.")
                return
            bins = [direct]
        else:
            source_text = self.bin_file.get().strip()
            if not source_text:
                messagebox.showerror("BIN", "Enter a 12-digit BIN. Batch files are available under Advanced options.")
                return
            source = Path(source_text)
            if not source.exists():
                messagebox.showerror("BIN file", "The selected batch file does not exist.")
                return
            bins = load_bins(source)
            if not bins:
                messagebox.showerror("BIN file", "No 12-digit BIN values were found in the selected file.")
                return

        out = Path(self.output_dir.get().strip())
        out.mkdir(parents=True, exist_ok=True)
        username = self.username.get().strip()
        password = self.password.get()
        if bool(username) != bool(password):
            messagebox.showerror("Authentication", "Enter both username and password, or leave both blank for Windows integrated authentication.")
            return

        self.running = True
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.reset_dashboard(len(bins))
        self.messages.put(("log", f"Loaded {len(bins)} case(s). Opening BCC…"))
        threading.Thread(
            target=self.worker,
            args=(bins, out, username, password, self.dossier_number.get().strip(), self.customer_key.get().strip()),
            daemon=True,
        ).start()

    def worker(self, bins: list[str], out: Path, username: str, password: str, dossier_number: str, customer_key: str):
        browser = None
        try:
            with sync_playwright() as p:
                channel = self.browser_channel.get()
                launch_kwargs = {
                    "headless": False,
                    "args": [
                        "--auth-server-allowlist=*.corp.centercredit.kz",
                        "--auth-negotiate-delegate-allowlist=*.corp.centercredit.kz",
                    ],
                }
                if channel != "chromium":
                    launch_kwargs["channel"] = channel
                try:
                    browser = p.chromium.launch(**launch_kwargs)
                except Exception:
                    self.messages.put(("log", f"Could not open {channel}; trying Playwright Chromium…"))
                    browser = p.chromium.launch(headless=False)

                context_kwargs = {"ignore_https_errors": True, "accept_downloads": True}
                if username and password:
                    context_kwargs["http_credentials"] = {"username": username, "password": password}
                    self.messages.put(("log", "Using BCC credentials in memory only."))
                else:
                    self.messages.put(("log", "Trying Windows/Edge integrated corporate authentication."))

                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                try:
                    page.goto(self.target_url.get().strip(), wait_until="domcontentloaded", timeout=90_000)
                except Exception as nav_exc:
                    if "ERR_INVALID_AUTH_CREDENTIALS" in str(nav_exc):
                        raise RuntimeError("BCC rejected the authentication credentials.") from nav_exc
                    raise

                if "/ecd/" not in page.url.lower():
                    raise RuntimeError("BCC Electronic Credit Dossier page did not open.")

                self.messages.put(("log", f"Authenticated BCC page: {page.url}"))
                self.messages.put(("status", "Processing…"))
                downloader = BCCDownloader(
                    context,
                    page,
                    out,
                    progress=lambda text: self.messages.put(("log", text)),
                    event=lambda payload: self.messages.put(("event", payload)),
                )
                downloader.download_bins(bins, dossier_number=dossier_number, customer_key=customer_key)
                self.messages.put(("log", f"Download/grouping finished. Report: {out / 'download_log.xlsx'}"))
                try:
                    browser.close()
                    browser = None
                except Exception:
                    pass

                self.messages.put(("status", "Analyzing…"))
                self.messages.put(("log", "Starting Analyzer v35 production gate…"))
                from integrated_analysis import analyze_download_tree
                analysis_report = analyze_download_tree(
                    out,
                    bins,
                    analysis_mode="auto",
                    progress=lambda text: self.messages.put(("log", text)),
                    event=lambda payload: self.messages.put(("event", payload)),
                )
                self.messages.put(("log",
                    f"Analysis finished: {analysis_report.processed} Excel result(s); "
                    f"AUTO_APPROVED={analysis_report.auto_approved}; "
                    f"QUARANTINED={analysis_report.quarantined}; "
                    f"UNMATCHED={analysis_report.unmatched_files}."
                ))
                self.messages.put(("status", "Finished"))
        except Exception as exc:
            self.messages.put(("log", f"FATAL ERROR: {exc}"))
            self.messages.put(("status", "Error"))
            self.messages.put(("event", {"type": "error"}))
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
        finally:
            self.messages.put(("done", None))

    def handle_event(self, payload: dict):
        kind = payload.get("type")
        if kind == "batch_start":
            self._total_cases = int(payload.get("total_cases", self._total_cases))
            self.cases_completed.set(f"{self._completed_cases} / {self._total_cases}")
        elif kind == "case_start":
            self.current_bin.set(payload.get("bin") or "—")
            self.current_dossier.set(payload.get("dossier") or "Searching…")
            self.leasing_status.set("SEARCHING")
            self.purchase_status.set("SEARCHING")
        elif kind == "dossier":
            self.current_dossier.set(payload.get("dossier") or "—")
        elif kind == "downloaded":
            self._downloaded_count += 1
            self.files_downloaded.set(str(self._downloaded_count))
            if payload.get("kind") in {"leasing_contract", "leasing_application"}:
                self.leasing_status.set("FOUND")
            elif payload.get("kind") == "purchase_contract":
                self.purchase_status.set("FOUND")
        elif kind == "skipped":
            self._skipped_count += 1
            self.skipped.set(str(self._skipped_count))
        elif kind == "not_found":
            self._not_found_count += 1
            self.not_found.set(str(self._not_found_count))
            if payload.get("kind") == "leasing_contract":
                self.leasing_status.set("NOT FOUND")
            elif payload.get("kind") == "purchase_contract":
                self.purchase_status.set("NOT FOUND")
        elif kind == "error":
            self._error_count += 1
            self.errors.set(str(self._error_count))
        elif kind == "case_complete":
            self._completed_cases += 1
            self.cases_completed.set(f"{self._completed_cases} / {self._total_cases}")
            self.current_bin.set(payload.get("bin") or self.current_bin.get())
            self.current_dossier.set(payload.get("dossier") or self.current_dossier.get())
            self.leasing_status.set(payload.get("leasing_contract", self.leasing_status.get()))
            self.purchase_status.set(payload.get("purchase_contract", self.purchase_status.get()))
        elif kind == "analysis_bin_start":
            self.run_status.set("Analyzing…")
            self.pair_folders.set(str(int(self.pair_folders.get() or 0) + int(payload.get("pairs") or 0)))
            self.unmatched.set(str(int(self.unmatched.get() or 0) + int(payload.get("unmatched") or 0)))
        elif kind == "analysis_bin_complete":
            self.analyzed_docs.set(str(int(self.analyzed_docs.get() or 0) + int(payload.get("processed") or 0)))
            self.auto_approved.set(str(int(self.auto_approved.get() or 0) + int(payload.get("approved") or 0)))
            self.quarantined.set(str(int(self.quarantined.get() or 0) + int(payload.get("quarantined") or 0)))
        elif kind == "analysis_complete":
            self.pair_folders.set(str(payload.get("pairs") or 0))
            self.unmatched.set(str(payload.get("unmatched") or 0))
            self.analyzed_docs.set(str(payload.get("processed") or 0))
            self.auto_approved.set(str(payload.get("approved") or 0))
            self.quarantined.set(str(payload.get("quarantined") or 0))

    def poll_messages(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.log.insert("end", str(payload) + "\n")
                    self.log.see("end")
                elif kind == "event":
                    self.handle_event(payload if isinstance(payload, dict) else {})
                elif kind == "status":
                    self.run_status.set(str(payload))
                elif kind == "done":
                    self.running = False
                    self.start_btn.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_messages)

    def on_close(self):
        self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
