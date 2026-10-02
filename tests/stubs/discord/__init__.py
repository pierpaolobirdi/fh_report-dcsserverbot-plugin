"""Minimal stand-in for discord.py — just enough for FH_Report's tests."""


class _Field:
    def __init__(self, name, value, inline):
        self.name, self.value, self.inline = name, value, inline


class _Footer:
    def __init__(self, text):
        self.text = text


class Embed:
    def __init__(self, title=None, description=None, color=None, timestamp=None):
        self.title, self.description, self.color, self.timestamp = title, description, color, timestamp
        self._fields: list[_Field] = []
        self.footer = None

    @property
    def fields(self):
        return list(self._fields)   # discord.py also hands out a copy

    def add_field(self, name=None, value=None, inline=False):
        self._fields.append(_Field(name, value, inline))

    def set_field_at(self, index, name=None, value=None, inline=False):
        self._fields[index] = _Field(name, value, inline)

    def remove_field(self, index):
        del self._fields[index]

    def set_footer(self, text=None):
        self.footer = _Footer(text)


class Interaction:
    pass


class HTTPException(Exception):
    pass


class NotFound(HTTPException):
    pass


class _Passthrough:
    def __class_getitem__(cls, item):
        return object


class app_commands:
    class Choice:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def __class_getitem__(cls, item):
            return cls

    Transform = _Passthrough
    Range = _Passthrough

    @staticmethod
    def describe(**_):
        return lambda f: f

    @staticmethod
    def rename(**_):
        return lambda f: f

    @staticmethod
    def autocomplete(**_):
        return lambda f: f
