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

        self.regex_var = tk.BooleanVar()
        ttk.Checkbutton(filters2, text="Regex", variable=self.regex_var,
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
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # --- Search Tab ---
        search_tab = ttk.Frame(notebook)
        notebook.add(search_tab, text="  Search  ")

        cols = ("name", "type", "size", "modified", "path")
        self.tree = ttk.Treeview(search_tab, columns=cols, show="headings")
        self.tree.heading("name", text="Name ↕", command=lambda: self._sort_column("name"))
        self.tree.heading("type", text="Type ↕", command=lambda: self._sort_column("type"))
        self.tree.heading("size", text="Size ↕", command=lambda: self._sort_column("size"))
        self.tree.heading("modified", text="Modified ↕", command=lambda: self._sort_column("modified"))
        self.tree.heading("path", text="Path ↕", command=lambda: self._sort_column("path"))
        self.tree.column("name", width=220)
        self.tree.column("type", width=60)
        self.tree.column("size", width=70, anchor=tk.E)
        self.tree.column("modified", width=130)
        self.tree.column("path", width=350)

        scrollbar = ttk.Scrollbar(search_tab, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Double-click on column separator to auto-fit width
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # --- Duplicates Tab ---
        dup_tab = ttk.Frame(notebook)
        notebook.add(dup_tab, text="  Duplicates  ")

        dup_controls = ttk.Frame(dup_tab, padding=5)
        dup_controls.pack(fill=tk.X)

        self.scan_dup_btn = ttk.Button(dup_controls, text="🔍 Scan for Duplicates", command=self._scan_duplicates)
        self.scan_dup_btn.pack(side=tk.LEFT, padx=2)

        self.delete_dup_btn = ttk.Button(dup_controls, text="🗑 Delete Selected", command=self._delete_selected_dups, state=tk.DISABLED)
        self.delete_dup_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(dup_controls, text="Select All Except First", command=self._select_all_dups).pack(side=tk.LEFT, padx=2)
        ttk.Button(dup_controls, text="Deselect All", command=self._deselect_all_dups).pack(side=tk.LEFT, padx=2)

        self.dup_status_var = tk.StringVar(value="Click 'Scan for Duplicates' to start")
        ttk.Label(dup_controls, textvariable=self.dup_status_var, foreground="gray").pack(side=tk.RIGHT, padx=5)

        # Duplicates treeview with checkboxes
        dup_cols = ("select", "name", "size", "path")
        self.dup_tree = ttk.Treeview(dup_tab, columns=dup_cols, show="tree headings", selectmode="extended")
        self.dup_tree.heading("#0", text="")
        self.dup_tree.heading("select", text="✓")
        self.dup_tree.heading("name", text="Name")
        self.dup_tree.heading("size", text="Size")
        self.dup_tree.heading("path", text="Path")
        self.dup_tree.column("#0", width=20)
        self.dup_tree.column("select", width=30, anchor=tk.CENTER)
        self.dup_tree.column("name", width=250)
        self.dup_tree.column("size", width=80, anchor=tk.E)
        self.dup_tree.column("path", width=400)

        dup_scroll = ttk.Scrollbar(dup_tab, orient=tk.VERTICAL, command=self.dup_tree.yview)
        self.dup_tree.configure(yscrollcommand=dup_scroll.set)
        self.dup_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        dup_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Click to toggle checkbox, double-click to auto-fit columns
        self.dup_tree.bind("<ButtonRelease-1>", self._toggle_dup_select)
        self.dup_tree.bind("<Double-1>", self._dup_tree_double_click)
        self._dup_selected = set()
        self._dup_groups = []

        # Right-click context menu for duplicates
        self.dup_context_menu = Menu(self.root, tearoff=0)
        self.dup_context_menu.add_command(label="Delete This File", command=self._delete_single_dup)
        self.dup_context_menu.add_command(label="Open File", command=self._open_dup_file)
        self.dup_context_menu.add_command(label="Open Folder", command=self._open_dup_folder)
        self.dup_context_menu.add_separator()
        self.dup_context_menu.add_command(label="Copy Path", command=self._copy_dup_path)
        self.dup_tree.bind("<Button-3>", self._show_dup_context_menu)
        self.dup_tree.bind("<Delete>", lambda e: self._delete_single_dup())

        # Right-click context menu
        self.context_menu = Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Open File", command=self._open_file)
        self.context_menu.add_command(label="Open Folder", command=self._open_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Path", command=self._copy_path)

        self.tree.bind("<Button-3>", self._show_context_menu)

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
    def _on_tree_double_click(self, event):
        """Double-click: auto-fit column if on separator, open file if on row."""
        region = self.tree.identify_region(event.x, event.y)
        if region == "separator":
            # Auto-fit column width
            col = self.tree.identify_column(event.x)
            col_id = self.tree["columns"][int(col.replace("#", "")) - 1]
            max_width = max(
                (len(str(self.tree.set(row, col_id))) for row in self.tree.get_children("")),
                default=5
            ) * 8 + 20  # approximate pixel width
            self.tree.column(col_id, width=max(max_width, 50))
        elif region == "cell":
            self._open_file()

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

        for c in ("name", "type", "size", "modified", "path"):
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
        folder = str(values[4])
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
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
            modified_before=modified_before,
            use_regex=self.regex_var.get(),
        )

        self.tree.delete(*self.tree.get_children())

        if result.get("error"):
            self.status_var.set(f"Error: {result['error']}")
            return

        from datetime import datetime
        for r in result["results"]:
            icon = "📁 " if r.is_dir else ""
            size = "" if r.is_dir else format_size(r.size)
            ftype = "Folder" if r.is_dir else (r.ext[1:].upper() if r.ext else "File")
            mod_date = datetime.fromtimestamp(r.modified).strftime("%Y-%m-%d %H:%M") if r.modified else ""
            self.tree.insert("", tk.END, values=(
                f"{icon}{r.name}", ftype, size, mod_date, os.path.dirname(r.path)
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

    # --- Duplicates ---
    def _scan_duplicates(self):
        """Scan for duplicate files using the index."""
        from duplicates import find_duplicates
        self.scan_dup_btn.config(state=tk.DISABLED)
        self.dup_status_var.set("Scanning...")
        self.dup_tree.delete(*self.dup_tree.get_children())
        self._dup_selected.clear()

        def do_scan():
            def progress(stage, current, total):
                self.root.after(0, lambda: self.dup_status_var.set(f"{stage} ({current:,})"))

            groups = find_duplicates(callback=progress)
            self.root.after(0, lambda: self._show_duplicates(groups))

        threading.Thread(target=do_scan, daemon=True).start()

    def _show_duplicates(self, groups):
        self._dup_groups = groups
        self.dup_tree.delete(*self.dup_tree.get_children())
        self._dup_selected.clear()

        total_wasted = 0
        for i, group in enumerate(groups):
            total_wasted += group.wasted_bytes
            # Parent node for the group
            group_id = self.dup_tree.insert("", tk.END, text="📋",
                values=("", f"{len(group.files)} copies", format_size(group.size),
                        f"Wasted: {format_size(group.wasted_bytes)}"))
            # Child nodes for each file
            for j, filepath in enumerate(group.files):
                item_id = self.dup_tree.insert(group_id, tk.END, text=os.path.basename(filepath),
                    values=("☐", os.path.basename(filepath), format_size(group.size), filepath))

        self.scan_dup_btn.config(state=tk.NORMAL)
        self.delete_dup_btn.config(state=tk.NORMAL if groups else tk.DISABLED)
        self.dup_status_var.set(
            f"Found {len(groups)} duplicate groups | Wasted: {format_size(total_wasted)}")

    def _toggle_dup_select(self, event):
        """Toggle selection on click."""
        item = self.dup_tree.identify_row(event.y)
        if not item:
            return
        parent = self.dup_tree.parent(item)
        if not parent:  # clicked on group header, not a file
            return

        if item in self._dup_selected:
            self._dup_selected.discard(item)
            self.dup_tree.set(item, "select", "☐")
        else:
            self._dup_selected.add(item)
            self.dup_tree.set(item, "select", "☑")

    def _select_all_dups(self):
        """Select all files except the first in each group."""
        self._dup_selected.clear()
        for group_id in self.dup_tree.get_children(""):
            children = self.dup_tree.get_children(group_id)
            for child in children[1:]:  # skip first (keep it)
                self._dup_selected.add(child)
                self.dup_tree.set(child, "select", "☑")
            if children:
                self.dup_tree.set(children[0], "select", "☐")

    def _deselect_all_dups(self):
        """Deselect all."""
        for item in self._dup_selected:
            self.dup_tree.set(item, "select", "☐")
        self._dup_selected.clear()

    def _delete_selected_dups(self):
        """Delete selected duplicate files."""
        if not self._dup_selected:
            return

        from duplicates import delete_files
        from tkinter import messagebox

        count = len(self._dup_selected)
        if not messagebox.askyesno("Delete Duplicates",
                f"Delete {count} selected files?\n\nThis cannot be undone."):
            return

        paths = [self.dup_tree.set(item, "path") for item in self._dup_selected]
        deleted, failed = delete_files(paths)

        # Remove deleted items from tree
        for item in list(self._dup_selected):
            self.dup_tree.delete(item)
        self._dup_selected.clear()

        # Remove empty groups
        for group_id in self.dup_tree.get_children(""):
            if len(self.dup_tree.get_children(group_id)) <= 1:
                self.dup_tree.delete(group_id)

        self.dup_status_var.set(f"Deleted {deleted} files" + (f", {failed} failed" if failed else ""))

    def _show_dup_context_menu(self, event):
        item = self.dup_tree.identify_row(event.y)
        if item and self.dup_tree.parent(item):  # only on file items, not group headers
            self.dup_tree.selection_set(item)
            self.dup_context_menu.post(event.x_root, event.y_root)

    def _get_dup_selected_path(self) -> str:
        item = self.dup_tree.selection()
        if not item:
            return ""
        return str(self.dup_tree.set(item[0], "path"))

    def _dup_tree_double_click(self, event):
        """Auto-fit column on separator double-click."""
        region = self.dup_tree.identify_region(event.x, event.y)
        if region == "separator":
            col = self.dup_tree.identify_column(event.x)
            col_idx = int(col.replace("#", "")) - 1
            cols = list(self.dup_tree["columns"])
            if 0 <= col_idx < len(cols):
                col_id = cols[col_idx]
                max_width = max(
                    (len(str(self.dup_tree.set(row, col_id))) for row in self._get_all_dup_items()),
                    default=5
                ) * 8 + 20
                self.dup_tree.column(col_id, width=max(max_width, 50))

    def _get_all_dup_items(self):
        """Get all child items (files, not group headers)."""
        items = []
        for group_id in self.dup_tree.get_children(""):
            items.extend(self.dup_tree.get_children(group_id))
        return items

    def _delete_single_dup(self):
        """Delete highlighted/selected files (supports multi-select)."""
        items = self.dup_tree.selection()
        if not items:
            return

        # Filter to only file items (not group headers)
        file_items = [i for i in items if self.dup_tree.parent(i)]
        if not file_items:
            return

        from tkinter import messagebox
        paths = [self.dup_tree.set(item, "path") for item in file_items]

        msg = f"Delete {len(paths)} file(s)?\n\n"
        msg += "\n".join(os.path.basename(p) for p in paths[:10])
        if len(paths) > 10:
            msg += f"\n... and {len(paths) - 10} more"
        msg += "\n\nThis cannot be undone."

        if not messagebox.askyesno("Delete", msg):
            return

        from duplicates import delete_files
        deleted, failed = delete_files(paths)

        for item in file_items:
            self._dup_selected.discard(item)
            self.dup_tree.delete(item)

        # Remove empty groups
        for group_id in list(self.dup_tree.get_children("")):
            if len(self.dup_tree.get_children(group_id)) <= 1:
                self.dup_tree.delete(group_id)

        self.dup_status_var.set(f"Deleted {deleted} files" + (f", {failed} failed" if failed else ""))

    def _open_dup_file(self):
        path = self._get_dup_selected_path()
        if path and os.path.exists(path):
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])

    def _open_dup_folder(self):
        path = self._get_dup_selected_path()
        if path:
            folder = os.path.dirname(path)
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", folder])

    def _copy_dup_path(self):
        path = self._get_dup_selected_path()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)

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
    # Wire single-instance show callback
    try:
        import quickfind
        quickfind._show_cb = lambda: app.root.after(0, app._show_window)
    except (ImportError, AttributeError):
        pass
    app.run()


if __name__ == "__main__":
    launch_gui()
