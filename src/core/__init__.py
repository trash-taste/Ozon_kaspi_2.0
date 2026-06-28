__all__ = ["AppManager"]


def __getattr__(name):
    if name == "AppManager":
        from .app_manager import AppManager

        return AppManager
    raise AttributeError(name)
