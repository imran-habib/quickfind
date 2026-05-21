"""
QuickFind GUI - instant file search with system tray, hotkey, auto-indexing.
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, Menu

from indexer import DEFAULT_DB, index_paths, get_indexed_paths
from search import search, get_stats, format_size

REINDEX_INTERVAL = 300
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".quickfind_config.json")
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickfind.ico")


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)


def get_all_drives():
    """Detect all available drives/mount points."""
    if os.name == "nt":
        import string
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return drives
    else:
        return ["/home", "/usr", "/opt", "/var"]


class QuickFindGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.config = load_config()
        self._indexing = False
        self._auto_reindex_active = True
        self._tray_icon = None
        self._result_count = 0

        self._restore_geometry()
        self._set_icon()
        self._build_ui()
        self._register_hotkey()
        self._update_title()
        self._auto_index_on_first_launch()
        self._start_auto_reindex()

    def _set_icon(self):
        if os.path.exists(ICON_PATH):
            try:
                self.root.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass

    def _restore_geometry(self):
        geo = self.config.get("geometry", "900x600")
        pos = self.config.get("position", "")
        self.root.geometry(geo + (f"+{pos}" if pos else ""))
        self.root.minsize(600, 400)

    def _save_geometry(self):
        geo = self.root.geometry()
        parts = geo.replace("+", " ").replace("x", " ").split()
        if len(parts) == 4:
            self.config["geometry"] = f"{parts[0]}x{parts[1]}"
            self.config["position"] = f"{parts[2]}+{parts[3]}"
        save_config(self.config)

    def _update_title(self):
        title = "QuickFind"
        if self._result_count > 0:
            title += f" — {self._result_count} results"
        self.root.title(title)

    def _build_ui(self):
        # Menu bar
        menubar = Menu(self.root)
        self.root.config(menu=menubar)

        view_menu = Menu(menubar, tearoff=0)
        view_menu.add_command(label="Minimize to Tray", command=self._minimize_to_tray)
        menubar.add_cascade(label="View", menu=view_menu)

        # Search frame
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="🔍").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)
        self.entry = ttk.Entry(top, textvariable=self.search_var, font=("Segoe UI", 12))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 5))
        self.entry.focus()

        # Filter frame - Row 1
        filters = ttk.Frame(self.root, padding=(10, 0))
        filters.pack(fill=tk.X)

        ttk.Label(filters, text="Extension:").pack(side=tk.LEFT)
        self.ext_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.ext_var, width=8).pack(side=tk.LEFT, padx=(2, 10))
        self.ext_var.trace_add("write", self._on_search)

        ttk.Label(filters, text="Sort:").pack(side=tk.LEFT, padx=(5, 0))
        self.sort_var = tk.StringVar(value="relevance")
        sort_combo = ttk.Combobox(filters, textvariable=self.sort_var, width=14, state="readonly",
                                  values=["relevance", "name_asc", "name_desc",
                                          "size_asc", "size_desc",
                                          "modified_newest", "modified_oldest", "type"])
        sort_combo.pack(side=tk.LEFT, padx=(2, 10))
        sort_combo.bind("<<ComboboxSelected>>", self._on_search)

        ttk.Button(filters, text="Index Folder...", command=self._index_folder).pack(side=tk.RIGHT)
        ttk.Button(filters, text="Re-index", command=self._reindex).pack(side=tk.RIGHT, padx=5)

        # Filter frame - Row 2
        filters2 = ttk.Frame(self.root, padding=(10, 3))
        filters2.pack(fill=tk.X)

        self.files_only_var = tk.BooleanVar()
        ttk.Checkbutton(filters2, text="Files only", variable=self.files_only_var,
                        command=self._on_search_btn).pack(side=tk.LEFT, padx=5)

        self.dirs_only_var = tk.BooleanVar()
        ttk.Checkbutton(filters2, text="Dirs only", variable=self.dirs_only_var,
                        command=self._on_search_btn).pack(side=tk.LEFT, padx=5)

        ttk.Separator(filters2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(filters2, text="Size:").pack(side=tk.LEFT)
        self.size_filter_var = tk.StringVar(value="any")
        size_combo = ttk.Combobox(filters2, textvariable=self.size_filter_var, width=10, state="readonly",
                                  values=["any", "<1 KB", "<1 MB", "<10 MB", "<100 MB",
                                          ">1 KB", ">1 MB", ">10 MB", ">100 MB"])
        size_combo.pack(side=tk.LEFT, padx=(2, 10))
        size_combo.bind("<<ComboboxSelected>>", self._on_search)

        ttk.Separator(filters2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(filters2, text="Modified:").pack(side=tk.LEFT)
        self.date_filter_var = tk.StringVar(value="any")
        date_combo = ttk.Combobox(filters2, textvariable=self.date_filter_var, width=12, state="readonly",
                                  values=["any", "today", "yesterday", "this week",
                                          "this month", "this year", "older than year"])
        date_combo.pack(side=tk.LEFT, padx=(2, 10))
        date_combo.bind("<<ComboboxSelected>>", self._on_search)

        # Progress frame
        progress_frame = ttk.Frame(self.root, padding=(10, 5))
        progress_frame.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, mode="determinate")
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(0, 10))

        self.progress_label = ttk.Label(progress_frame, text="", width=55)
        self.progress_label.pack(side=tk.RIGHT)

        # Status bar
        status_frame = ttk.Frame(self.root, padding=(10, 0))
        status_frame.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT)

        self.auto_label = ttk.Label(status_frame, text="Auto-reindex: ON", foreground="green")
        self.auto_label.pack(side=tk.RIGHT)

        # Results treeview
        tree_frame = ttk.Frame(self.root, padding=(10, 5))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("name", "size", "modified", "path")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("name", text="Name ↕", command=lambda: self._sort_column("name"))
        self.tree.heading("size", text="Size ↕", command=lambda: self._sort_column("size"))
        self.tree.heading("modified", text="Modified ↕", command=lambda: self._sort_column("modified"))
        self.tree.heading("path", text="Path ↕", command=lambda: self._sort_column("path"))
        self.tree.column("name", width=220)
        self.tree.column("size", width=70, anchor=tk.E)
        self.tree.column("modified", width=130)
        self.tree.column("path", width=400)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Right-click context menu
        self.context_menu = Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Open File", command=self._open_file)
        self.context_menu.add_command(label="Open Folder", command=self._open_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Path", command=self._copy_path)

        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Keyboard shortcuts
        self.root.bind("<Escape>", lambda e: self._minimize_to_tray())
        self.root.bind("<Return>", lambda e: self._open_file())
        self.root.bind("<Control-c>", self._keyboard_copy)

        # Track sort state for column click toggling
        self._sort_reverse = {}

    # --- Auto-index on first launch ---
    def _auto_index_on_first_launch(self):
        """If no index exists, automatically index all drives."""
        stats = get_stats()
        if "error" in stats:
            # No index — start indexing all drives immediately
            drives = get_all_drives()
            self.status_var.set(f"First launch — indexing {', '.join(drives)}...")
            self._run_index(drives, incremental=False)
        else:
            self.status_var.set(f"Index: {stats['total_entries']:,} entries ({stats['files']:,} files, {stats['directories']:,} dirs)")

    # --- System Tray ---
    def _minimize_to_tray(self):
        self.root.withdraw()
        self._setup_tray()

    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image as PILImage

            if os.path.exists(ICON_PATH):
                icon_img = PILImage.open(ICON_PATH)
            else:
                icon_img = PILImage.new("RGB", (64, 64), "blue")

            menu = pystray.Menu(
                pystray.MenuItem("Show QuickFind", self._restore_from_tray, default=True),
                pystray.MenuItem("Exit", self._exit_app),
            )
            self._tray_icon = pystray.Icon("QuickFind", icon_img, "QuickFind", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except ImportError:
            self.root.iconify()

    def _restore_from_tray(self, *args):
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.root.after(0, self.root.deiconify)

    def _exit_app(self, *args):
        if self._tray_icon:
            self._tray_icon.stop()
        self._auto_reindex_active = False
        self.root.after(0, self.root.destroy)

    # --- Global Hotkey ---
    def _register_hotkey(self):
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+shift+f", self._hotkey_triggered)
        except ImportError:
            pass

    def _hotkey_triggered(self):
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.entry.focus()

    # --- Column sorting ---
    def _sort_column(self, col):
        """Sort treeview by clicking column header."""
        reverse = self._sort_reverse.get(col, False)
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        if col == "size":
            def size_key(item):
                val = item[0].strip()
                if not val:
                    return 0
                parts = val.split()
                try:
                    num = float(parts[0])
                    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
                    return num * units.get(parts[1], 1) if len(parts) > 1 else num
                except (ValueError, IndexError):
                    return 0
            items.sort(key=size_key, reverse=reverse)
        else:
            items.sort(key=lambda t: t[0].lower(), reverse=reverse)

        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)

        self._sort_reverse[col] = not reverse

        for c in ("name", "size", "modified", "path"):
            arrow = ""
            if c == col:
                arrow = " ▼" if reverse else " ▲"
            self.tree.heading(c, text=c.capitalize() + arrow)

    def _keyboard_copy(self, event):
        if self.tree.selection():
            self._copy_path()
            return "break"

    # --- Context Menu ---
    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _get_selected_path(self) -> str:
        item = self.tree.selection()
        if not item:
            return ""
        values = self.tree.item(item[0])["values"]
        name = str(values[0]).replace("📁 ", "")
        folder = str(values[3])
        return os.path.join(folder, name)

    def _open_file(self):
        path = self._get_selected_path()
        if path and os.path.exists(path):
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])

    def _open_folder(self):
        path = self._get_selected_path()
        if path:
            folder = os.path.dirname(path) if os.path.isfile(path) else path
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", folder])

    def _copy_path(self):
        path = self._get_selected_path()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)

    def _on_double_click(self, event):
        self._open_file()

    # --- Index ---
    def _update_progress(self, message, files_done, eta):
        def update():
            eta_str = ""
            if eta > 0:
                mins, secs = divmod(int(eta), 60)
                eta_str = f" | ETA: {mins}m {secs}s" if mins else f" | ETA: {secs}s"
            self.progress_label.config(text=f"{message}  |  {files_done:,} files{eta_str}")
            if files_done > 0 and self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.config(mode="indeterminate")
                self.progress_bar.start(15)
        self.root.after(0, update)

    def _index_folder(self):
        folder = filedialog.askdirectory(title="Select folder to index")
        if not folder:
            return
        self._run_index([folder], incremental=False)

    def _reindex(self):
        paths = get_indexed_paths()
        if not paths:
            self.status_var.set("Nothing to re-index. Index a folder first.")
            return
        self._run_index(paths, incremental=True)

    def _run_index(self, paths, incremental=True):
        if self._indexing:
            return
        self._indexing = True
        self.progress_bar.config(mode="indeterminate")
        self.progress_bar.start(15)
        self.progress_label.config(text="Starting...")

        def do_index():
            result = index_paths(paths, callback=self._update_progress, incremental=incremental)
            self.root.after(0, lambda: self._index_done(result))

        threading.Thread(target=do_index, daemon=True).start()

    def _index_done(self, result):
        self._indexing = False
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate")
        self.progress_var.set(100)

        if result["was_incremental"]:
            msg = (f"Updated: {result['new_files']} new, {result['updated_files']} modified, "
                   f"{result['removed_files']} removed ({result['elapsed_seconds']}s)")
        else:
            msg = f"Indexed {result['total_files']:,} files in {result['elapsed_seconds']}s"

        self.progress_label.config(text=msg)
        self.status_var.set(f"Index: {result['total_files']:,} files")

    def _start_auto_reindex(self):
        def auto_reindex():
            while self._auto_reindex_active:
                time.sleep(REINDEX_INTERVAL)
                if not self._indexing:
                    paths = get_indexed_paths()
                    if paths:
                        self._indexing = True
                        self.root.after(0, lambda: self.auto_label.config(
                            text="Auto-reindex: running...", foreground="orange"))
                        result = index_paths(paths, callback=self._update_progress, incremental=True)

                        def done(r=result):
                            self._indexing = False
                            self.progress_bar.stop()
                            self.progress_bar.config(mode="determinate")
                            self.progress_var.set(100)
                            self.auto_label.config(text="Auto-reindex: ON", foreground="green")
                            self.progress_label.config(
                                text=f"Auto-update: +{r['new_files']} new, "
                                     f"{r['updated_files']} modified, "
                                     f"-{r['removed_files']} removed")
                            self.status_var.set(f"Index: {r['total_files']:,} files")

                        self.root.after(0, done)

        threading.Thread(target=auto_reindex, daemon=True).start()

    # --- Search ---
    def _on_search(self, *args):
        query = self.search_var.get().strip()
        if not query:
            self.tree.delete(*self.tree.get_children())
            self._result_count = 0
            self._update_title()
            return

        ext = self.ext_var.get().strip()
        if ext and not ext.startswith("."):
            ext = "." + ext

        min_size, max_size = self._parse_size_filter()
        modified_after, modified_before = self._parse_date_filter()

        result = search(
            query=query,
            limit=100,
            ext_filter=ext or None,
            files_only=self.files_only_var.get(),
            dirs_only=self.dirs_only_var.get(),
            sort_by=self.sort_var.get(),
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
            modified_before=modified_before,
        )

        self.tree.delete(*self.tree.get_children())

        if result.get("error"):
            self.status_var.set(f"Error: {result['error']}")
            return

        from datetime import datetime
        for r in result["results"]:
            icon = "📁 " if r.is_dir else ""
            size = "" if r.is_dir else format_size(r.size)
            mod_date = datetime.fromtimestamp(r.modified).strftime("%Y-%m-%d %H:%M") if r.modified else ""
            self.tree.insert("", tk.END, values=(
                f"{icon}{r.name}", size, mod_date, os.path.dirname(r.path)
            ))

        self._result_count = result["count"]
        self._update_title()
        self.status_var.set(f"{result['count']} results in {result['elapsed_ms']}ms")

    def _parse_size_filter(self):
        val = self.size_filter_var.get()
        sizes = {"1 KB": 1024, "1 MB": 1024**2, "10 MB": 10*1024**2, "100 MB": 100*1024**2}
        if val.startswith("<"):
            return None, sizes.get(val[1:], None)
        elif val.startswith(">"):
            return sizes.get(val[1:], None), None
        return None, None

    def _parse_date_filter(self):
        import datetime
        val = self.date_filter_var.get()
        now = time.time()
        day = 86400

        if val == "today":
            start_of_today = datetime.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            return start_of_today, None
        elif val == "yesterday":
            start_of_today = datetime.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            return start_of_today - day, start_of_today
        elif val == "this week":
            return now - 7 * day, None
        elif val == "this month":
            return now - 30 * day, None
        elif val == "this year":
            return now - 365 * day, None
        elif val == "older than year":
            return None, now - 365 * day
        return None, None

    def _on_search_btn(self):
        self._on_search()

    # --- Lifecycle ---
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self._save_geometry()
        self._auto_reindex_active = False
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.destroy()


def launch_gui():
    app = QuickFindGUI()
    app.run()


if __name__ == "__main__":
    launch_gui()
