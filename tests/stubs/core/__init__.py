"""Minimal stand-in for DCSServerBot's core package."""
import enum
import logging


class Status(enum.Enum):
    RUNNING = 1
    PAUSED = 2
    STOPPED = 3
    UNREGISTERED = 4


class Plugin:
    def __init__(self, bot, eventlistener=None):
        self.bot, self.locals, self.log = bot, {}, logging.getLogger("fh_report.tests")


class TEventListener:
    pass


class Server:
    pass


class Group:
    def __init__(self, **_):
        pass

    def command(self, **_):
        return lambda f: f


class utils:
    class ServerTransformer:
        pass

    @staticmethod
    def print_ruler(ruler_length=34):
        return "─" * ruler_length

    @staticmethod
    def get_ephemeral(interaction):
        return True
