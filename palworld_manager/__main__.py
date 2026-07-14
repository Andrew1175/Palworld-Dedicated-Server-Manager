from __future__ import annotations

import tkinter as tk
from pathlib import Path

from .main_window import PalworldServerManagerApp


def main() -> None:
    root = tk.Tk()
    # Folder that contains the `palworld_manager` package (server root when the package lives inside the server).
    initial = Path(__file__).resolve().parent.parent
    PalworldServerManagerApp(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
