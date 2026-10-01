from __future__ import annotations

from pathlib import Path
from datetime import datetime
import py_compile
import re
import sys

GUI_NAME = "silence_cutter_gui.py"


def fail(msg: str) -> None:
    print(f"\nERROR: {msg}\n")
    raise SystemExit(1)


def main() -> None:
    here = Path(__file__).resolve().parent
    target = here / GUI_NAME

    if not target.exists():
        fail(f"Could not find {GUI_NAME} beside this installer.\n"
             f"Put this file in the same folder as {GUI_NAME} and run it again.")

    src = target.read_text(encoding="utf-8")

    if "_reset_footage_analysis" in src and "_install_reset_analysis_button" in src:
        print("Reset Analysis is already installed.")
        return

    if "Analyze Moments" not in src:
        fail('Could not find the "Analyze Moments" button in this version of the GUI. No changes were made.')

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.stem}.backup_reset_analysis_{stamp}{target.suffix}")
    backup.write_text(src, encoding="utf-8")

    m = re.search(r"(?m)^(\s*)self\._build_ui\(\)\s*$", src)
    if not m:
        fail("Could not find self._build_ui() in the GUI initializer. "
             f"Backup created at: {backup}")

    indent = m.group(1)
    insertion = (
        m.group(0)
        + "\n"
        + indent
        + "# Install Footage Analysis reset button after widgets exist.\n"
        + indent
        + "self.after(0, self._install_reset_analysis_button)"
    )
    src = src[:m.start()] + insertion + src[m.end():]

    method_block = r'''
    def _install_reset_analysis_button(self):
        """Add Reset Analysis beside Analyze Moments without depending on exact layout code."""
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            return

        if getattr(self, "_reset_analysis_btn", None):
            return

        def walk(widget):
            try:
                children = widget.winfo_children()
            except Exception:
                return None
            for child in children:
                try:
                    if isinstance(child, ttk.Button) and str(child.cget("text")).strip() == "Analyze Moments":
                        return child
                except Exception:
                    pass
                found = walk(child)
                if found is not None:
                    return found
            return None

        analyze = walk(self)
        if analyze is None:
            return

        parent = analyze.master
        btn = ttk.Button(parent, text="Reset Analysis", command=self._reset_footage_analysis)
        self._reset_analysis_btn = btn

        try:
            manager = analyze.winfo_manager()
            if manager == "grid":
                info = analyze.grid_info()
                row = int(info.get("row", 0))
                used_cols = []
                for child in parent.winfo_children():
                    try:
                        gi = child.grid_info()
                        if gi:
                            used_cols.append(int(gi.get("column", 0)))
                    except Exception:
                        pass
                col = (max(used_cols) + 1) if used_cols else int(info.get("column", 0)) + 1
                btn.grid(row=row, column=col, padx=(6, 0), pady=info.get("pady", 0), sticky="e")
            elif manager == "pack":
                btn.pack(side="right", padx=(6, 0))
            else:
                btn.pack(side="right", padx=(6, 0))
        except Exception:
            try:
                btn.pack(side="right", padx=(6, 0))
            except Exception:
                pass

    def _reset_footage_analysis(self):
        """Reset Footage Analysis without touching video, SRT, or Sequence Builder."""
        import tkinter as tk
        from tkinter import ttk

        exact_container_names = {
            "ranked_moments",
            "ranked_results",
            "moment_results",
            "moments",
            "analysis_moments",
            "moment_rankings",
            "moment_reviews",
            "moment_statuses",
            "review_statuses",
            "moment_decisions",
            "reviewed_moments",
        }

        for name, value in list(vars(self).items()):
            lname = name.lower()

            if "sequence" in lname:
                continue

            should_clear_container = (
                name in exact_container_names
                or (
                    isinstance(value, (list, dict, set))
                    and ("moment" in lname or "rank" in lname)
                    and any(k in lname for k in ("result", "review", "status", "decision", "rank", "moment"))
                )
            )

            if should_clear_container:
                try:
                    value.clear()
                except Exception:
                    try:
                        setattr(self, name, type(value)())
                    except Exception:
                        pass

            if "selected" in lname and "moment" in lname and not isinstance(value, tk.Variable):
                try:
                    setattr(self, name, None)
                except Exception:
                    pass

        for name, widget in list(vars(self).items()):
            lname = name.lower()
            if isinstance(widget, ttk.Treeview):
                if "transcript" in lname or "sequence" in lname:
                    continue
                if "moment" in lname or "rank" in lname or "result" in lname:
                    try:
                        widget.delete(*widget.get_children())
                    except Exception:
                        pass

        for name, widget in list(vars(self).items()):
            lname = name.lower()
            if isinstance(widget, tk.Text):
                if "selected" in lname and ("moment" in lname or "detail" in lname):
                    try:
                        old_state = str(widget.cget("state"))
                        if old_state == "disabled":
                            widget.configure(state="normal")
                        widget.delete("1.0", "end")
                        if old_state == "disabled":
                            widget.configure(state="disabled")
                    except Exception:
                        pass

        filter_tokens = (
            "search",
            "must_contain",
            "mustcontain",
            "character_filter",
            "character_var",
            "moment_character",
        )
        preserve_tokens = (
            "video",
            "srt",
            "source",
            "project",
            "version",
            "mode",
            "top",
            "sequence",
        )

        for name, value in list(vars(self).items()):
            lname = name.lower()
            if not isinstance(value, tk.Variable):
                continue
            if any(tok in lname for tok in preserve_tokens):
                continue
            if any(tok in lname for tok in filter_tokens):
                try:
                    value.set("")
                except Exception:
                    pass

        for name in (
            "selected_moment_var",
            "selected_moment_text_var",
            "moment_detail_var",
            "moment_details_var",
            "selected_detail_var",
        ):
            value = getattr(self, name, None)
            if isinstance(value, tk.Variable):
                try:
                    value.set("")
                except Exception:
                    pass

        import inspect
        for meth_name in (
            "_save_moment_reviews",
            "_save_moment_statuses",
            "_save_footage_analysis_state",
            "_save_analysis_state",
            "_save_ranked_moments",
        ):
            meth = getattr(self, meth_name, None)
            if not callable(meth):
                continue
            try:
                sig = inspect.signature(meth)
                required = [
                    p for p in sig.parameters.values()
                    if p.default is inspect._empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                if not required:
                    meth()
            except Exception:
                pass

        for name in (
            "footage_status_var",
            "analysis_status_var",
            "moment_status_var",
            "status_var",
        ):
            value = getattr(self, name, None)
            if isinstance(value, tk.Variable):
                try:
                    value.set("Footage Analysis reset. Video and SRT were kept.")
                    break
                except Exception:
                    pass
'''

    anchor = re.search(r"(?m)^    # ---------- theme ----------\s*$", src)
    if anchor:
        src = src[:anchor.start()] + method_block + "\n" + src[anchor.start():]
    else:
        init_m = re.search(r"(?m)^    def __init__\(", src)
        if not init_m:
            fail(f"Could not locate the main class initializer. Backup created at: {backup}")

        next_method = re.search(r"(?m)^    def [A-Za-z_]\w*\(", src[init_m.end():])
        if not next_method:
            fail(f"Could not find an insertion point for reset methods. Backup created at: {backup}")
        pos = init_m.end() + next_method.start()
        src = src[:pos] + method_block + "\n" + src[pos:]

    target.write_text(src, encoding="utf-8")

    try:
        py_compile.compile(str(target), doraise=True)
    except Exception as exc:
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        fail(
            "The patched GUI did not compile, so the original was restored automatically.\n"
            f"Compiler error: {exc}\nBackup: {backup}"
        )

    print("\nDONE.")
    print(f"Patched: {target}")
    print(f"Backup:  {backup}")
    print("\nFootage Analysis now gets a Reset Analysis button beside Analyze Moments.")
    print("It resets ranked/review state and filters while preserving the loaded video, SRT, and Sequence Builder.")


if __name__ == "__main__":
    main()
