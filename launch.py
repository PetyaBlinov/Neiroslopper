"""Windows entry point with a visible startup error instead of a silent exit."""
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)


def main():
    from display_scaling import configure_process_dpi
    configure_process_dpi()
    import tkinter as tk
    from tkinter import messagebox
    try:
        from branding import configure_windows_identity
        configure_windows_identity()
        from app import App
        window = tk.Tk()
        application = App(window, data_dir=ROOT / 'data')

        def report_error(kind, value, trace):
            folder = ROOT / 'data'
            folder.mkdir(exist_ok=True)
            log = folder / 'application-errors.log'
            with log.open('a', encoding='utf-8') as stream:
                traceback.print_exception(kind, value, trace, file=stream)
            messagebox.showerror('Плоттер', f'{value}\n\nПодробности сохранены:\n{log}', parent=window)

        window.report_callback_exception = report_error
        application.root.mainloop()
    except Exception:
        folder = ROOT / 'data'
        folder.mkdir(exist_ok=True)
        log = folder / 'launch-errors.log'
        log.write_text(traceback.format_exc(), encoding='utf-8')
        try:
            error_window = tk.Tk()
            error_window.withdraw()
            messagebox.showerror('Не удалось открыть пульт', f'Подробности:\n{log}\n\n{traceback.format_exc()[-1200:]}', parent=error_window)
            error_window.destroy()
        finally:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
