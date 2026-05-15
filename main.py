"""Application entry point for the Golden Laravel Scanner GUI."""

from core.assets import ensure_assets
from gui.main_window import GoldenApp, SplashScreen


def main() -> None:
    """Start the Golden themed GUI application."""
    ensure_assets()
    app = GoldenApp()
    app.withdraw()
    splash = SplashScreen(app)
    def _show_app() -> None:
        splash.destroy()
        app.deiconify()
        app.state("zoomed")
    app.after(2200, _show_app)
    app.mainloop()


if __name__ == "__main__":
    main()
