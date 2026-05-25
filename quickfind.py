#!/usr/bin/env python3
"""
QuickFind - Instant file search for Windows & Linux.
Like 'Everything' but cross-platform and in Python.
"""
import argparse
import os
import sys
import time
from datetime import datetime

from indexer import DEFAULT_DB, index_paths
from search import search, get_stats, format_size

# ANSI colors
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def supports_color():
    if os.name == "nt":
        return os.environ.get("ANSICON") or "WT_SESSION" in os.environ
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


USE_COLOR = supports_color()


def c(text, color):
    return f"{color}{text}{RESET}" if USE_COLOR else text


def print_results(results, show_size=True, show_date=False):
    """Print search results in a formatted table."""
    for r in results:
        icon = "📁" if r.is_dir else "📄"
        size_str = f" {format_size(r.size):>10}" if show_size and not r.is_dir else ""
        date_str = ""
        if show_date and r.modified:
            date_str = f" {datetime.fromtimestamp(r.modified).strftime('%Y-%m-%d %H:%M')}"
        name = c(r.name, CYAN) if r.is_dir else r.name
        path_display = c(os.path.dirname(r.path), DIM)
        print(f"  {icon}{size_str}{date_str}  {name}  {path_display}")


def cmd_index(args):
    """Index filesystem paths."""
    paths = args.paths or (["/"] if os.name != "nt" else ["C:\\"])

    def progress(msg, count, eta):
        eta_str = ""
        if eta > 0:
            mins, secs = divmod(int(eta), 60)
            eta_str = f" | ETA: {mins}m {secs}s" if mins else f" | ETA: {secs}s"
        print(f"\r  {msg} ({count:,} files){eta_str}    ", end="", flush=True)

    print(f"Indexing: {', '.join(paths)}")
    result = index_paths(paths, db_path=args.db, num_workers=args.workers, callback=progress)
    print(f"\n\n  ✓ Indexed {result['total_files']:,} files in {result['elapsed_seconds']}s")
    print(f"    Speed: {result['files_per_second']:,} files/sec")
    print(f"    Database: {result['db_path']}")


def cmd_search(args):
    """One-shot search."""
    result = search(
        query=args.query,
        db_path=args.db,
        limit=args.max,
        ext_filter=args.ext,
        dirs_only=args.dirs,
        files_only=args.files,
        path_filter=args.path_contains,
        use_regex=args.regex,
    )

    if result["error"]:
        print(f"Error: {result['error']}")
        sys.exit(1)

    if not result["results"]:
        print("  No results found.")
        return

    print_results(result["results"], show_size=True, show_date=args.date)
    print(f"\n  {c(str(result['count']), GREEN)} results in {result['elapsed_ms']}ms")


def cmd_interactive(args):
    """Interactive real-time search mode."""
    print(f"{c('QuickFind', BOLD)} interactive mode (type to search, Ctrl+C to exit)\n")

    # Check if index exists
    stats = get_stats(args.db)
    if "error" in stats:
        print(f"  {stats['error']} Run: quickfind index <path>")
        return
    print(f"  Index: {stats['total_entries']:,} entries ({stats['files']:,} files, {stats['directories']:,} dirs)")
    print(f"  {c('Type to search...', DIM)}\n")

    try:
        while True:
            try:
                query = input(f"{c('>', GREEN)} ")
            except EOFError:
                break

            if not query.strip():
                continue
            if query.strip() in ("exit", "quit", "q"):
                break

            result = search(
                query=query,
                db_path=args.db,
                limit=args.max or 20,
                ext_filter=args.ext,
                dirs_only=args.dirs,
                files_only=args.files,
            )

            if result["error"]:
                print(f"  Error: {result['error']}")
                continue

            if not result["results"]:
                print(f"  No results.")
                continue

            print_results(result["results"])
            print(f"  {c(str(result['count']), DIM)} results ({result['elapsed_ms']}ms)\n")

    except KeyboardInterrupt:
        print("\n\nBye!")


def cmd_stats(args):
    """Show index statistics."""
    stats = get_stats(args.db)
    if "error" in stats:
        print(f"  {stats['error']}")
        return
    print(f"  Total entries: {stats['total_entries']:,}")
    print(f"  Files:         {stats['files']:,}")
    print(f"  Directories:   {stats['directories']:,}")
    print(f"  Database size: {stats['db_size']}")
    print(f"  Database path: {stats['db_path']}")


def main():
    p = argparse.ArgumentParser(
        prog="quickfind",
        description="QuickFind - Instant file search for Windows & Linux",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="Database path")
    sub = p.add_subparsers(dest="command")

    # index
    idx = sub.add_parser("index", help="Index filesystem paths")
    idx.add_argument("paths", nargs="*", help="Paths to index (default: all drives)")
    idx.add_argument("-w", "--workers", type=int, default=4, help="Concurrent crawlers")

    # search
    s = sub.add_parser("search", help="Search indexed files")
    s.add_argument("query", help="Search query")
    s.add_argument("-n", "--max", type=int, default=50, help="Max results")
    s.add_argument("-e", "--ext", help="Filter by extension (e.g., .py)")
    s.add_argument("-d", "--dirs", action="store_true", help="Directories only")
    s.add_argument("-f", "--files", action="store_true", help="Files only")
    s.add_argument("-p", "--path-contains", help="Path must contain this string")
    s.add_argument("-r", "--regex", action="store_true", help="Use regex instead of FTS")
    s.add_argument("--date", action="store_true", help="Show modified date")

    # interactive
    i = sub.add_parser("interactive", aliases=["i"], help="Interactive search mode")
    i.add_argument("-n", "--max", type=int, default=20, help="Max results per query")
    i.add_argument("-e", "--ext", help="Filter by extension")
    i.add_argument("-d", "--dirs", action="store_true", help="Directories only")
    i.add_argument("-f", "--files", action="store_true", help="Files only")

    # stats
    sub.add_parser("stats", help="Show index statistics")

    args = p.parse_args()

    if args.command == "index":
        cmd_index(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command in ("interactive", "i"):
        cmd_interactive(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        # No command given — launch GUI (for double-click on Windows)
        try:
            import atexit
            from single_instance import ensure_single_instance, cleanup_lock

            _show_cb = None
            def _on_show():
                if _show_cb:
                    _show_cb()

            ensure_single_instance("quickfind", on_show_callback=_on_show)
            atexit.register(cleanup_lock, "quickfind")

            from gui import launch_gui, QuickFindGUI
            launch_gui()
        except ImportError:
            p.print_help()


if __name__ == "__main__":
    main()
