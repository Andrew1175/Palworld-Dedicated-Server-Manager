from __future__ import annotations

import tkinter as tk
from pathlib import Path

from .main_window import WindroseServerManagerApp


def main() -> None:
    root = tk.Tk()
    # Folder that contains the `windrose_manager` package (server root when the package lives inside the server).
    initial = Path(__file__).resolve().parent.parent
    WindroseServerManagerApp(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
