from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any, Callable

from . import config_schema, constants
from .config_schema import ConfigField, SECTION_ORDER
from .ui_theme import HoverToolTip, tk_button


@dataclass
class FieldBinding:
    field: ConfigField
    kind: str
    var: tk.Variable | None = None
    widget: tk.Widget | None = None
    value_label: tk.Label | None = None
    reveal_btn: tk.Widget | None = None
    lock_widgets: list[tk.Widget] = field(default_factory=list)


class ConfigForm:
    def __init__(self, app) -> None:
        self.app = app
        self.c = app.c
        self.bindings: dict[str, FieldBinding] = {}
        self.first_section_header: ttk.Label | None = None
        self.launch_args_entry: ttk.Entry | None = None
        self._rest_api_toggle: Callable[[], None] | None = None
        self._player_max_callback: Callable[[int], None] | None = None
        self._globally_enabled = True

    def build(self, parent: tk.Frame) -> None:
        for section in SECTION_ORDER:
            fields = [f for f in config_schema.CONFIG_FIELDS if f.section == section]
            if not fields:
                continue
            header = ttk.Label(parent, text=section, style="Section.TLabel")
            if self.first_section_header is None:
                self.first_section_header = header
            header.pack(anchor="w", pady=(12 if section != SECTION_ORDER[0] else 0, 4))

            panel = self.app._panel_frame(parent)
            panel.pack(fill=tk.X, pady=(0, 8))
            inner = tk.Frame(panel, bg=self.c["bg_panel"])
            inner.pack(fill=tk.X, padx=12, pady=10)

            for row, cfg in enumerate(fields):
                self._add_field(inner, cfg, row)

            inner.columnconfigure(1, weight=1)

            if section == "Server Settings":
                self._add_launch_args_row(inner, len(fields))

    def set_player_max_callback(self, callback: Callable[[int], None]) -> None:
        self._player_max_callback = callback

    def _add_launch_args_row(self, parent: tk.Frame, row: int) -> None:
        lbl = tk.Label(parent, text="Launch Arguments", bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl.grid(row=row, column=0, sticky="nw", pady=4)
        self.launch_args_entry = ttk.Entry(parent, width=50)
        self.launch_args_entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.launch_args_entry.insert(0, constants.DEFAULT_LAUNCH_ARGS)
        HoverToolTip(
            lbl,
            "Command-line arguments passed when starting PalServer-Win64-Shipping-Cmd.exe. "
            "Saved in manager settings and applied on server start.",
        )

    def _add_field(self, parent: tk.Frame, cfg: ConfigField, row: int) -> None:
        lbl = tk.Label(parent, text=cfg.label, bg=self.c["bg_panel"], fg=self.c["text_dim"])
        lbl.grid(row=row, column=0, sticky="w", pady=4)
        if cfg.tooltip:
            HoverToolTip(lbl, cfg.tooltip)

        binding = FieldBinding(field=cfg, kind=cfg.kind)
        cell = tk.Frame(parent, bg=self.c["bg_panel"])
        cell.grid(row=row, column=1, sticky="ew", pady=4)

        if cfg.kind == "bool":
            var = tk.BooleanVar(value=bool(cfg.default))
            chk = ttk.Checkbutton(cell, variable=var)
            chk.pack(anchor="w")
            binding.var = var
            binding.widget = chk
            binding.lock_widgets = [chk]
            if cfg.key == "RESTAPIEnabled":
                var.trace_add("write", lambda *_: self._toggle_rest_api_port())
                self._rest_api_toggle = self._toggle_rest_api_port

        elif cfg.kind == "enum":
            var = tk.StringVar(value=self._enum_display(cfg.default, cfg.choices))
            combo = ttk.Combobox(cell, textvariable=var, values=list(cfg.choices or ()), state="readonly", width=28)
            combo.pack(anchor="w")
            binding.var = var
            binding.widget = combo
            binding.lock_widgets = [combo]

        elif cfg.kind == "password":
            entry = ttk.Entry(cell, width=40, show="*")
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            reveal = tk_button(cell, "Reveal", small=True)
            reveal.pack(side=tk.LEFT, padx=(6, 0))
            reveal.bind("<ButtonPress-1>", lambda _e, e=entry: self._reveal_press(e))
            reveal.bind("<ButtonRelease-1>", lambda _e, e=entry: self._reveal_release(e))
            reveal.bind("<Leave>", lambda _e, e=entry: self._reveal_release(e))
            binding.widget = entry
            binding.reveal_btn = reveal
            # Keep Reveal usable while config is locked (server running).
            binding.lock_widgets = [entry]

        elif cfg.kind == "tuple_string":
            entry = ttk.Entry(cell, width=50)
            entry.pack(fill=tk.X, expand=True)
            entry.insert(0, str(cfg.default))
            binding.widget = entry
            binding.lock_widgets = [entry]

        elif cfg.kind == "string":
            entry = ttk.Entry(cell, width=50)
            entry.pack(fill=tk.X, expand=True)
            if cfg.default:
                entry.insert(0, str(cfg.default))
            binding.widget = entry
            binding.lock_widgets = [entry]

        elif cfg.kind in ("int", "port"):
            entry = ttk.Entry(cell, width=12)
            entry.pack(anchor="w")
            entry.insert(0, str(cfg.default))
            binding.widget = entry
            binding.lock_widgets = [entry]

        elif cfg.kind == "float_rate":
            row_f = tk.Frame(cell, bg=self.c["bg_panel"])
            row_f.pack(fill=tk.X, expand=True)
            lo = float(cfg.min_val if cfg.min_val is not None else 0.1)
            hi = float(cfg.max_val if cfg.max_val is not None else 10.0)
            scale = tk.Scale(
                row_f,
                from_=lo,
                to=hi,
                resolution=cfg.resolution,
                orient=tk.HORIZONTAL,
                showvalue=0,
                bg=self.c["bg_panel"],
                fg=self.c["accent"],
                highlightthickness=0,
                troughcolor=self.c["border_input"],
            )
            scale.set(float(cfg.default))
            scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
            val_lbl = tk.Label(
                row_f,
                text=self._format_rate(scale.get()),
                fg=self.c["accent"],
                bg=self.c["bg_panel"],
                font=(None, 11, "bold"),
                width=5,
            )
            val_lbl.pack(side=tk.LEFT)
            scale.config(command=lambda v, l=val_lbl: l.config(text=self._format_rate(v)))
            binding.widget = scale
            binding.value_label = val_lbl
            binding.lock_widgets = [scale]

        elif cfg.kind == "player_max":
            row_f = tk.Frame(cell, bg=self.c["bg_panel"])
            row_f.pack(fill=tk.X, expand=True)
            lo = int(cfg.min_val or 1)
            hi = int(cfg.max_val or 32)
            scale = tk.Scale(
                row_f,
                from_=lo,
                to=hi,
                orient=tk.HORIZONTAL,
                showvalue=0,
                bg=self.c["bg_panel"],
                fg=self.c["accent"],
                highlightthickness=0,
                troughcolor=self.c["border_input"],
            )
            scale.set(int(cfg.default))
            scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
            val_lbl = tk.Label(
                row_f,
                text=str(int(scale.get())),
                fg=self.c["accent"],
                bg=self.c["bg_panel"],
                font=(None, 11, "bold"),
                width=3,
            )
            val_lbl.pack(side=tk.LEFT)

            def _on_slide(v, lbl=val_lbl) -> None:
                n = int(round(float(v)))
                lbl.config(text=str(n))
                if self._player_max_callback:
                    self._player_max_callback(n)

            scale.config(command=_on_slide)
            binding.widget = scale
            binding.value_label = val_lbl
            binding.lock_widgets = [scale]

        self.bindings[cfg.key] = binding
        if cfg.key == "RESTAPIPort":
            self._toggle_rest_api_port()

    def _toggle_rest_api_port(self) -> None:
        if not self._globally_enabled:
            return
        rest_enabled = self.bindings.get("RESTAPIEnabled")
        rest_port = self.bindings.get("RESTAPIPort")
        if not rest_enabled or not rest_port or not rest_port.widget:
            return
        enabled = bool(rest_enabled.var.get()) if rest_enabled.var else True
        rest_port.widget.config(state=tk.NORMAL if enabled else tk.DISABLED)

    @staticmethod
    def _reveal_press(entry: ttk.Entry) -> None:
        # Works even when the field is locked (server running).
        entry.config(show="")

    @staticmethod
    def _reveal_release(entry: ttk.Entry) -> None:
        entry.config(show="*")

    @staticmethod
    def _format_rate(value: Any) -> str:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        if num == int(num):
            return str(int(num))
        return f"{num:.1f}".rstrip("0").rstrip(".")

    @staticmethod
    def _enum_display(value: Any, choices: tuple[str, ...] | None) -> str:
        if value is None:
            return "None"
        text = str(value)
        if choices and text in choices:
            return text
        if choices and "None" in choices and text in ("", "none", "None"):
            return "None"
        return text

    def populate(self, opts: dict[str, Any], launch_args: str) -> None:
        for key, binding in self.bindings.items():
            value = opts.get(key, binding.field.default)
            self._set_binding_value(binding, value)
        if self.launch_args_entry is not None:
            self.launch_args_entry.delete(0, tk.END)
            self.launch_args_entry.insert(0, launch_args or constants.DEFAULT_LAUNCH_ARGS)
        self._toggle_rest_api_port()

    def _set_binding_value(self, binding: FieldBinding, value: Any) -> None:
        cfg = binding.field
        if cfg.kind == "bool":
            if binding.var is not None:
                binding.var.set(bool(value))
            return
        if cfg.kind == "enum":
            if binding.var is not None:
                binding.var.set(self._enum_display(value, cfg.choices))
            return
        if cfg.kind == "player_max":
            if binding.widget is not None:
                try:
                    n = max(int(cfg.min_val or 1), min(int(cfg.max_val or 32), int(value)))
                except (TypeError, ValueError):
                    n = int(cfg.default)
                binding.widget.set(n)
                if binding.value_label is not None:
                    binding.value_label.config(text=str(n))
                if self._player_max_callback:
                    self._player_max_callback(n)
            return
        if cfg.kind == "float_rate":
            if binding.widget is not None:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    num = float(cfg.default)
                binding.widget.set(num)
                if binding.value_label is not None:
                    binding.value_label.config(text=self._format_rate(num))
            return
        if binding.widget is None:
            return
        widget = binding.widget
        if cfg.kind == "password":
            prev = str(widget.cget("state"))
            widget.config(state=tk.NORMAL)
            widget.delete(0, tk.END)
            text = "" if value is None else str(value)
            if text:
                widget.insert(0, text)
            if prev == "disabled":
                widget.config(state=prev)
            return
        if isinstance(widget, ttk.Entry):
            widget.delete(0, tk.END)
            if value is not None and str(value) != "":
                widget.insert(0, str(value))
            return
        if isinstance(widget, tk.Scale):
            try:
                widget.set(float(value))
            except (TypeError, ValueError):
                widget.set(float(cfg.default))

    def collect(self) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for key, binding in self.bindings.items():
            updates[key] = self._get_binding_value(binding)
        return updates

    def _get_binding_value(self, binding: FieldBinding) -> Any:
        cfg = binding.field
        if cfg.kind == "bool":
            return bool(binding.var.get()) if binding.var else bool(cfg.default)
        if cfg.kind == "enum":
            raw = binding.var.get() if binding.var else str(cfg.default)
            return None if raw == "None" else raw
        if cfg.kind == "player_max":
            if binding.widget is not None:
                return int(round(float(binding.widget.get())))
            return int(cfg.default)
        if cfg.kind == "float_rate":
            if binding.widget is not None:
                return float(binding.widget.get())
            return float(cfg.default)
        if binding.widget is None:
            return cfg.default
        widget = binding.widget
        if cfg.kind in ("string", "password", "tuple_string", "int", "port"):
            text = widget.get().strip()
            if cfg.kind == "port":
                try:
                    return max(1, min(65535, int(text or cfg.default)))
                except ValueError:
                    return int(cfg.default)
            if cfg.kind == "int":
                try:
                    return int(text if text != "" else cfg.default)
                except ValueError:
                    return int(cfg.default)
            if cfg.kind == "tuple_string":
                return text
            if cfg.kind == "password":
                return text
            if cfg.key == "ServerName":
                return text or "Default Palworld Server"
            return text
        if isinstance(widget, tk.Scale):
            return float(widget.get())
        return cfg.default

    def get_launch_arguments(self) -> str:
        if self.launch_args_entry is None:
            return constants.DEFAULT_LAUNCH_ARGS
        return self.launch_args_entry.get().strip() or constants.DEFAULT_LAUNCH_ARGS

    def set_enabled(self, enabled: bool) -> None:
        self._globally_enabled = enabled
        state = tk.NORMAL if enabled else tk.DISABLED
        chk_state = ["!disabled"] if enabled else ["disabled"]
        for binding in self.bindings.values():
            if binding.kind == "bool":
                if binding.widget is not None:
                    binding.widget.state(chk_state)
                continue
            for widget in binding.lock_widgets:
                if isinstance(widget, ttk.Combobox):
                    widget.config(state="readonly" if enabled else "disabled")
                elif hasattr(widget, "config"):
                    widget.config(state=state)
            # Reveal stays clickable so admins can view passwords while locked.
            if binding.reveal_btn is not None:
                binding.reveal_btn.config(state=tk.NORMAL)
        if self.launch_args_entry is not None:
            self.launch_args_entry.config(state=state)
        if enabled:
            self._toggle_rest_api_port()

    def get_entry(self, key: str) -> ttk.Entry | None:
        binding = self.bindings.get(key)
        if binding and isinstance(binding.widget, ttk.Entry):
            return binding.widget
        return None

    def get_scale(self, key: str) -> tk.Scale | None:
        binding = self.bindings.get(key)
        if binding and isinstance(binding.widget, tk.Scale):
            return binding.widget
        return None
