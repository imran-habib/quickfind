"""
findfast GUI - basic tkinter interface for double-click usage on Windows.
"""
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from indexer import DEFAULT_DB, index_paths
from search import search, get_stats, format_size


class FindFastGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("findfast - Instant File Search")
        self.root.geometry("900x600")
        self.root.minsize(600, 400)

        self._build_ui()
        self._check_index()

    def _build_ui(self):
        # Search frame
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Search:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)
        self.entry = ttk.Entry(top, textvariable=self.search_var, font=("Consolas", 12))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 5))
        self.entry.focus()

        # Filter frame
        filters = ttk.Frame(self.root, padding=(10, 0))
        filters.pack(fill=tk.X)

        ttk.Label(filters, text="Extension:").pack(side=tk.LEFT)
        self.ext_var = tk.StringVar()
        ext_entry = ttk.Entry(filters, textvariable=self.ext_var, width=8)
        ext_entry.pack(side=tk.LEFT, padx=(2, 10))
        self.ext_var.trace_add("write", self._on_search)

        self.files_only_var = tk.BooleanVar()
        ttk.Checkbutton(filters, text="Files only", variable=self.files_only_var,
                        command=self._on_search_btn).pack(side=tk.LEFT, padx=5)

        self.dirs_only_var = tk.BooleanVar()
        ttk.Checkbutton(filters, text="Dirs only", variable=self.dirs_only_var,
                        command=self._on_search_btn).pack(side=tk.LEFT, padx=5)

        # Index button
        ttk.Button(filters, text="Index Folder...", command=self._index_folder).pack(side=tk.RIGHT)

        # Status
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(filters, textvariable=self.status_var, foreground="gray").pack(side=tk.RIGHT, padx=10)

        # Results
        cols = ("name", "size", "path")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("path", text="Path")
        self.tree.column("name", width=250)
        self.tree.column("size", width=80, anchor=tk.E)
        self.tree.column("path", width=500)

        scrollbar = ttk.Scrollbar(self.root, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Double-click to open file location
        self.tree.bind("<Double-1>", self._open_location)

    def _check_index(self):
        stats = get_stats()
        if "error" in stats:
            self.status_var.set("No index. Click 'Index Folder...' to start.")
        else:
            self.status_var.set(f"Index: {stats['total_entries']:,} entries")

    def _on_search(self, *args):
        query = self.search_var.get().strip()
        if not query:
            self.tree.delete(*self.tree.get_children())
            return

        ext = self.ext_var.get().strip()
        if ext and not ext.startswith("."):
            ext = "." + ext

        result = search(
            query=query,
            limit=100,
            ext_filter=ext or None,
            files_only=self.files_only_var.get(),
            dirs_only=self.dirs_only_var.get(),
        )

        self.tree.delete(*self.tree.get_children())

        if result.get("error"):
            self.status_var.set(f"Error: {result['error']}")
            return

        for r in result["results"]:
            icon = "📁 " if r.is_dir else ""
            size = "" if r.is_dir else format_size(r.size)
            self.tree.insert("", tk.END, values=(
                f"{icon}{r.name}", size, os.path.dirname(r.path)
            ))

        self.status_var.set(f"{result['count']} results in {result['elapsed_ms']}ms")

    def _on_search_btn(self):
        self._on_search()

    def _index_folder(self):
        folder = filedialog.askdirectory(title="Select folder to index")
        if not folder:
            return

        self.status_var.set(f"Indexing {folder}...")
        self.root.update()

        def do_index():
            result = index_paths([folder])
            self.root.after(0, lambda: self._index_done(result))

        threading.Thread(target=do_index, daemon=True).start()

    def _index_done(self, result):
        self.status_var.set(
            f"Indexed {result['total_files']:,} files in {result['elapsed_seconds']}s"
        )

    def _open_location(self, event):
        item = self.tree.selection()
        if not item:
            return
        values = self.tree.item(item[0])["values"]
        path = values[2]  # directory path
        if os.path.isdir(path):
            os.startfile(path) if os.name == "nt" else os.system(f'xdg-open "{path}"')

    def run(self):
        self.root.mainloop()


def launch_gui():
    app = FindFastGUI()
    app.run()


if __name__ == "__main__":
    launch_gui()
